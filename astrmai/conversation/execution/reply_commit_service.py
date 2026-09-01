from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping

from astrbot.api import logger

from ..contracts.committed_reply import CommittedBotTurn
from ..runtime.architecture_rollout import (
    ArchitectureTimer,
    record_architecture_observation,
)


CommitConsumer = Callable[[CommittedBotTurn], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class ReplyCommitResult:
    commit_id: str
    consumer_status: dict[str, str] = field(default_factory=dict)
    repair_scheduled: bool = False


class ReplyCommitService:
    """Coordinates post-send consumers without ever resending the reply."""

    def __init__(self, outbox_store=None, *, owner_registry=None) -> None:
        self._lock = asyncio.Lock()
        self._consumer_status: dict[str, dict[str, str]] = {}
        self._outbox_store = outbox_store
        self._owner_registry = owner_registry
        self._detached_tasks: set[asyncio.Task[Any]] = set()

    def _register_owner_task(self, task: asyncio.Task[Any], *, run_id: str) -> None:
        registry = getattr(self, "_owner_registry", None)
        register = getattr(registry, "register", None)
        if not callable(register):
            return
        try:
            register(
                task,
                task_family="reply_commit.consumer",
                scope_id="GLOBAL",
                run_id=run_id,
                owner="ReplyCommitService",
                generation=getattr(registry, "generation", 0),
                cancel_status="cancelled",
            )
        except Exception as exc:
            logger.debug("[ReplyCommit] owner registry registration degraded: %s", exc)

    async def enqueue(
        self,
        event: Any,
        committed_turn: CommittedBotTurn,
        *,
        consumers: Mapping[str, CommitConsumer],
        repair_context: Mapping[str, Any] | None = None,
        inline_consumer_names: Iterable[str] = (),
    ) -> ReplyCommitResult:
        """Persist a post-send commit and execute consumers outside the reply path.

        The visible reply is already sent when this method is called.  Only the
        small durable outbox write is awaited; consumer work is detached and
        recovered by ``repair_pending`` after reloads.
        """
        context = dict(repair_context or {})
        statuses = {str(name): "pending" for name in consumers}
        inline_names = {
            str(name) for name in inline_consumer_names if str(name) in consumers
        }
        statuses = self._consumer_status.setdefault(
            committed_turn.commit_id,
            statuses,
        )

        async def _run_inline_consumers() -> None:
            for name in inline_names:
                consumer = consumers[name]
                try:
                    outcome = str(
                        await consumer(committed_turn) or "committed"
                    ).strip().lower()
                    statuses[name] = (
                        outcome if outcome.startswith("skipped") else "committed"
                    )
                except Exception as exc:
                    statuses[name] = "failed"
                    logger.warning(
                        "[ReplyCommit] inline consumer failed "
                        f"commit={committed_turn.commit_id} consumer={name}: {exc}"
                    )
        # Hosts without a durable store still must not make post-send work part
        # of the visible reply path.  The caller can stop this in-process task
        # through ``stop``; the event is marked degraded because a crash cannot
        # recover the detached work without an outbox.
        if self._outbox_store is None:
            # Group dialogue and native history are small, non-LLM persistence
            # operations.  Keep the concrete ReplyService implementations
            # synchronous for compatibility with hosts that do not provide an
            # outbox, while treating arbitrary/custom callables as detached so
            # an inline hint can never make a blocking consumer hold the reply.
            lightweight_names = {"group_dialogue", "native_history"}
            lightweight_inline_names = {
                name
                for name in inline_names
                if name in lightweight_names
                and bool(
                    getattr(consumers[name], "_reply_commit_lightweight", False)
                )
            }

            skip_semantic = bool(context.get("skip_semantic_persistence", False))
            skipped_names = {
                name
                for name in consumers
                if skip_semantic and name in {"memory", "learning", "social_feedback"}
            }
            for name in skipped_names:
                statuses[name] = "skipped_nonsemantic_media"

            detached_consumers = {
                name: consumer
                for name, consumer in consumers.items()
                if name not in lightweight_inline_names and name not in skipped_names
            }
            inline_names = lightweight_inline_names

            if inline_names:
                await _run_inline_consumers()

            async def _run_without_outbox() -> None:
                try:
                    if not detached_consumers:
                        return
                    await self.commit(
                        None,
                        committed_turn,
                        consumers=detached_consumers,
                        repair_context=context,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "[ReplyCommit] detached commit without outbox failed "
                        f"commit={committed_turn.commit_id}: {exc}"
                    )

            if detached_consumers:
                task = asyncio.create_task(_run_without_outbox())
                self._detached_tasks.add(task)
                self._register_owner_task(
                    task,
                    run_id=f"reply-commit-{committed_turn.commit_id}",
                )
                task.add_done_callback(self._detached_tasks.discard)
            result_status = dict(self._consumer_status.get(committed_turn.commit_id, {})) or dict(statuses)
            repair_scheduled = bool(detached_consumers)
            if hasattr(event, "set_extra"):
                event.set_extra("astrmai_reply_commit_id", committed_turn.commit_id)
                event.set_extra("astrmai_reply_commit_status", committed_turn.send_status.value)
                event.set_extra("astrmai_reply_commit_consumer_status", result_status)
                event.set_extra("astrmai_reply_commit_repair_scheduled", repair_scheduled)
                event.set_extra("astrmai_post_send_status", "post_send_degraded")
                event.set_extra("astrmai_committed_bot_turn", committed_turn)
            return ReplyCommitResult(
                commit_id=committed_turn.commit_id,
                consumer_status=result_status,
                repair_scheduled=repair_scheduled,
            )
        save_claimed = getattr(self._outbox_store, "save_claimed", None)
        lease_token = ""
        if callable(save_claimed):
            lease_token = await save_claimed(
                committed_turn,
                repair_context=context,
                consumer_status=statuses,
                lease_seconds=300.0,
            )
        else:
            await self._outbox_store.save(
                committed_turn,
                repair_context=context,
                consumer_status=statuses,
                attempts=0,
                next_retry_at=0.0,
                last_error="",
            )

        if inline_names:
            await _run_inline_consumers()
            owns_outbox = await self._save_outbox(
                committed_turn,
                repair_context=context,
                statuses=statuses,
                attempts=0,
                next_retry_at=0.0,
                last_error="",
                lease_token=lease_token,
            )
            if lease_token and not owns_outbox:
                raise RuntimeError(
                    f"reply commit lease lost: {committed_turn.commit_id}"
                )

        inline_consumers = {
            name: consumer
            for name, consumer in consumers.items()
            if name in inline_names
        }
        detached_consumers = {
            name: consumer
            for name, consumer in consumers.items()
            if name not in inline_names
        }
        async def _run() -> None:
            try:
                if not detached_consumers:
                    return
                await self.commit(
                    None,
                    committed_turn,
                    consumers=detached_consumers,
                    repair_context=context,
                    lease_token=lease_token,
                )
            except asyncio.CancelledError:
                release_lease = getattr(self._outbox_store, "release_lease", None)
                if lease_token and callable(release_lease):
                    try:
                        await asyncio.shield(
                            release_lease(
                                committed_turn.commit_id,
                                lease_token=lease_token,
                                next_retry_at=0.0,
                            )
                        )
                    except Exception:
                        logger.warning(
                            "[ReplyCommit] cancelled worker lease release failed "
                            f"commit={committed_turn.commit_id}"
                        )
                raise
            except Exception as exc:
                logger.warning(
                    f"[ReplyCommit] detached commit failed commit={committed_turn.commit_id}: {exc}"
                )

        if detached_consumers and (lease_token or not callable(save_claimed)):
            task = asyncio.create_task(_run())
            self._detached_tasks.add(task)
            self._register_owner_task(
                task,
                run_id=f"reply-commit-{committed_turn.commit_id}",
            )
            task.add_done_callback(self._detached_tasks.discard)
        result_status = dict(self._consumer_status.get(committed_turn.commit_id, {}))
        if not result_status:
            result_status = dict(statuses)
        repair_scheduled = bool(detached_consumers) or any(
            status == "failed" for status in result_status.values()
        )
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_reply_commit_id", committed_turn.commit_id)
            event.set_extra("astrmai_reply_commit_status", committed_turn.send_status.value)
            event.set_extra(
                "astrmai_reply_commit_consumer_status",
                result_status,
            )
            event.set_extra("astrmai_reply_commit_repair_scheduled", repair_scheduled)
            event.set_extra("astrmai_committed_bot_turn", committed_turn)
        return ReplyCommitResult(
            commit_id=committed_turn.commit_id,
            consumer_status=result_status,
            repair_scheduled=repair_scheduled,
        )

    async def stop(self) -> None:
        """Cancel in-process detached consumers; durable rows remain for replay."""
        tasks = [task for task in self._detached_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._detached_tasks.clear()

    async def _save_outbox(
        self,
        committed_turn: CommittedBotTurn,
        *,
        repair_context: Mapping[str, Any],
        statuses: Mapping[str, str],
        attempts: int,
        next_retry_at: float,
        last_error: str,
        lease_token: str = "",
    ) -> bool:
        if self._outbox_store is None:
            return False
        try:
            update_claimed = getattr(self._outbox_store, "update_claimed", None)
            if lease_token and callable(update_claimed):
                return bool(
                    await update_claimed(
                        committed_turn,
                        repair_context=repair_context,
                        consumer_status=statuses,
                        attempts=attempts,
                        last_error=last_error,
                        lease_token=lease_token,
                    )
                )
            await self._outbox_store.save(
                committed_turn,
                repair_context=repair_context,
                consumer_status=statuses,
                attempts=attempts,
                next_retry_at=next_retry_at,
                last_error=last_error,
            )
            return True
        except Exception as exc:
            logger.warning(
                "[ReplyCommit] durable outbox update failed "
                f"commit={committed_turn.commit_id}: {exc}"
            )
            return False

    async def commit(
        self,
        event: Any,
        committed_turn: CommittedBotTurn,
        *,
        consumers: Mapping[str, CommitConsumer],
        repair_context: Mapping[str, Any] | None = None,
        lease_token: str = "",
    ) -> ReplyCommitResult:
        timer = ArchitectureTimer()
        context = dict(repair_context or {})
        async with self._lock:
            persisted = None
            if self._outbox_store is not None:
                try:
                    persisted = await self._outbox_store.get(
                        committed_turn.commit_id
                    )
                except Exception as exc:
                    logger.warning(
                        "[ReplyCommit] durable outbox read failed "
                        f"commit={committed_turn.commit_id}: {exc}"
                    )
            statuses = self._consumer_status.setdefault(committed_turn.commit_id, {})
            if persisted is not None:
                statuses.update(persisted.consumer_status)
                if not context:
                    context = dict(persisted.repair_context)
            for name in consumers:
                statuses.setdefault(name, "pending")
            attempts = int(getattr(persisted, "attempts", 0) or 0)
            owns_outbox = await self._save_outbox(
                committed_turn,
                repair_context=context,
                statuses=statuses,
                attempts=attempts,
                next_retry_at=0.0,
                last_error="",
                lease_token=lease_token,
            )
            if lease_token and not owns_outbox:
                raise RuntimeError(
                    f"reply commit lease lost: {committed_turn.commit_id}"
                )
            last_error = ""
            for name, consumer in consumers.items():
                current_status = str(statuses.get(name, "") or "")
                if current_status == "committed" or current_status.startswith("skipped"):
                    continue
                try:
                    outcome = str(
                        await consumer(committed_turn) or "committed"
                    ).strip().lower()
                    statuses[name] = outcome if outcome.startswith("skipped") else "committed"
                except Exception as exc:
                    statuses[name] = "failed"
                    last_error = f"{name}:{exc}"
                    logger.warning(
                        "[ReplyCommit] consumer failed "
                        f"commit={committed_turn.commit_id} consumer={name}: {exc}"
                    )
                owns_outbox = await self._save_outbox(
                    committed_turn,
                    repair_context=context,
                    statuses=statuses,
                    attempts=attempts,
                    next_retry_at=0.0,
                    last_error=last_error,
                    lease_token=lease_token,
                )
                if lease_token and not owns_outbox:
                    raise RuntimeError(
                        f"reply commit lease lost: {committed_turn.commit_id}"
                    )
            result_status = dict(statuses)
            # Keep the durable row while any consumer (for example a detached
            # semantic consumer) is still pending, even when inline consumers
            # have already completed successfully.
            repair_scheduled = any(
                status in {"failed", "pending"}
                for status in result_status.values()
            )
            if self._outbox_store is not None:
                try:
                    if repair_scheduled:
                        attempts += 1
                        next_retry_at = (
                            time.time()
                            + self._outbox_store.retry_delay(attempts)
                        )
                        release_claim = getattr(
                            self._outbox_store, "release_claim", None
                        )
                        if lease_token and callable(release_claim):
                            await release_claim(
                                committed_turn,
                                repair_context=context,
                                consumer_status=result_status,
                                attempts=attempts,
                                next_retry_at=next_retry_at,
                                last_error=last_error,
                                lease_token=lease_token,
                            )
                        else:
                            await self._outbox_store.save(
                                committed_turn,
                                repair_context=context,
                                consumer_status=result_status,
                                attempts=attempts,
                                next_retry_at=next_retry_at,
                                last_error=last_error,
                            )
                    else:
                        await self._outbox_store.delete(
                            committed_turn.commit_id,
                            lease_token=lease_token,
                        )
                except Exception as exc:
                    logger.warning(
                        "[ReplyCommit] durable outbox finalize failed "
                        f"commit={committed_turn.commit_id}: {exc}"
                    )
        if hasattr(event, "set_extra"):
            event.set_extra("astrmai_reply_commit_id", committed_turn.commit_id)
            event.set_extra(
                "astrmai_reply_commit_status",
                committed_turn.send_status.value,
            )
            event.set_extra(
                "astrmai_reply_commit_consumer_status",
                result_status,
            )
            event.set_extra(
                "astrmai_reply_commit_repair_scheduled",
                repair_scheduled,
            )
            event.set_extra(
                "astrmai_committed_bot_turn",
                committed_turn,
            )
        record_architecture_observation(
            event,
            "reply_commit",
            {
                "commit_id": committed_turn.commit_id,
                "send_status": committed_turn.send_status.value,
                "consumer_count": len(result_status),
                "committed_consumer_count": sum(
                    status == "committed" for status in result_status.values()
                ),
                "failed_consumer_count": sum(
                    status == "failed" for status in result_status.values()
                ),
                "repair_scheduled": repair_scheduled,
                "elapsed_ms": timer.elapsed_ms,
            },
        )
        return ReplyCommitResult(
            commit_id=committed_turn.commit_id,
            consumer_status=result_status,
            repair_scheduled=repair_scheduled,
        )

    async def repair_pending(
        self,
        consumer_factory: Callable[
            [CommittedBotTurn, Mapping[str, Any]],
            Mapping[str, CommitConsumer],
        ],
        *,
        limit: int = 50,
    ) -> int:
        if self._outbox_store is None:
            return 0
        claim_due = getattr(self._outbox_store, "claim_due", None)
        claimed = (
            await claim_due(limit=limit, lease_seconds=300.0)
            if callable(claim_due)
            else [(entry, "") for entry in await self._outbox_store.list_due(limit=limit)]
        )
        repaired = 0
        for entry, lease_token in claimed:
            try:
                consumers = consumer_factory(
                    entry.committed_turn,
                    entry.repair_context,
                )
                result = await self.commit(
                    None,
                    entry.committed_turn,
                    consumers=consumers,
                    repair_context=entry.repair_context,
                    lease_token=lease_token,
                )
                if not result.repair_scheduled:
                    repaired += 1
            except asyncio.CancelledError:
                release_lease = getattr(self._outbox_store, "release_lease", None)
                if lease_token and callable(release_lease):
                    try:
                        await asyncio.shield(
                            release_lease(
                                entry.commit_id,
                                lease_token=lease_token,
                                next_retry_at=0.0,
                            )
                        )
                    except Exception:
                        logger.warning(
                            "[ReplyCommit] cancelled repair lease release failed "
                            f"commit={entry.commit_id}"
                        )
                raise
            except Exception as exc:
                logger.warning(
                    "[ReplyCommit] pending repair failed "
                    f"commit={entry.commit_id}: {exc}"
                )
        return repaired


__all__ = ["ReplyCommitResult", "ReplyCommitService"]
