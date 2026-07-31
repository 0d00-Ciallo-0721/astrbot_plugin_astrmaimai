from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

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

    def __init__(self, outbox_store=None) -> None:
        self._lock = asyncio.Lock()
        self._consumer_status: dict[str, dict[str, str]] = {}
        self._outbox_store = outbox_store

    async def _save_outbox(
        self,
        committed_turn: CommittedBotTurn,
        *,
        repair_context: Mapping[str, Any],
        statuses: Mapping[str, str],
        attempts: int,
        next_retry_at: float,
        last_error: str,
    ) -> bool:
        if self._outbox_store is None:
            return False
        try:
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
            await self._save_outbox(
                committed_turn,
                repair_context=context,
                statuses=statuses,
                attempts=attempts,
                next_retry_at=0.0,
                last_error="",
            )
            last_error = ""
            for name, consumer in consumers.items():
                if statuses.get(name) in {"committed", "skipped"}:
                    continue
                try:
                    outcome = str(
                        await consumer(committed_turn) or "committed"
                    ).strip().lower()
                    statuses[name] = (
                        "skipped" if outcome.startswith("skipped") else "committed"
                    )
                except Exception as exc:
                    statuses[name] = "failed"
                    last_error = f"{name}:{exc}"
                    logger.warning(
                        "[ReplyCommit] consumer failed "
                        f"commit={committed_turn.commit_id} consumer={name}: {exc}"
                    )
                await self._save_outbox(
                    committed_turn,
                    repair_context=context,
                    statuses=statuses,
                    attempts=attempts,
                    next_retry_at=0.0,
                    last_error=last_error,
                )
            result_status = dict(statuses)
            repair_scheduled = any(
                status == "failed" for status in result_status.values()
            )
            if self._outbox_store is not None:
                try:
                    if repair_scheduled:
                        attempts += 1
                        await self._outbox_store.save(
                            committed_turn,
                            repair_context=context,
                            consumer_status=result_status,
                            attempts=attempts,
                            next_retry_at=(
                                time.time()
                                + self._outbox_store.retry_delay(attempts)
                            ),
                            last_error=last_error,
                        )
                    else:
                        await self._outbox_store.delete(committed_turn.commit_id)
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
        entries = await self._outbox_store.list_due(limit=limit)
        repaired = 0
        for entry in entries:
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
                )
                if not result.repair_scheduled:
                    repaired += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[ReplyCommit] pending repair failed "
                    f"commit={entry.commit_id}: {exc}"
                )
        return repaired


__all__ = ["ReplyCommitResult", "ReplyCommitService"]
