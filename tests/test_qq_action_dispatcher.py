import asyncio
import importlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs
from astrmai.infrastructure.runtime.outbound_send_guard import OUTBOUND_SEND_GATE


class _Api:
    def __init__(self, result=None):
        self.calls = []
        self.result = {"status": "ok"} if result is None else result

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.result


class _TimeoutSendApi(_Api):
    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "send_msg":
            raise TimeoutError("request timed out after remote acceptance")
        return {"status": "ok"}


class _CancelDuringSendApi(_Api):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "send_poke":
            self.started.set()
            await asyncio.Event().wait()
        return {"status": "ok"}


class _BrokenClaimStore:
    async def claim(self, *_args, **_kwargs):
        raise RuntimeError("ledger unavailable")


class _Event:
    def __init__(self):
        self._extra = {}
        self.bot = SimpleNamespace(api=_Api())

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class _Coordinator:
    def __init__(self, *, current=True, previous=None):
        self.current = current
        self.previous = list(previous or [])

    async def is_current_turn(self, _turn):
        return self.current

    async def get_latest_committed_outbound(self, _chat_id, *, exclude_send_key=""):
        return self.previous


class QQActionDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.conversation.execution.qq_action_dispatcher", None)
        self.mod = importlib.import_module("astrmai.conversation.execution.qq_action_dispatcher")
        self.config = SimpleNamespace(
            conversation=SimpleNamespace(
                qq_native_tools_enabled=True,
                qq_deferred_action_commit_enabled=True,
            )
        )

    def tearDown(self):
        OUTBOUND_SEND_GATE.close()
        self.temp_dir.cleanup()

    def _persistent_store(self):
        from astrmai.infrastructure.persistence.persistence_schema import _run_migrations

        db_path = Path(self.temp_dir.name) / "qq_action_ledger.db"
        with sqlite3.connect(db_path) as db:
            db.execute("PRAGMA user_version = 121")
            _run_migrations(db)
        store_mod = importlib.import_module(
            "astrmai.infrastructure.persistence.qq_action_ledger"
        )
        return store_mod.QQActionLedgerStore(db_path)

    def test_shutdown_gate_rejects_deferred_actions_before_napcat(self):
        event = _Event()
        event.set_extra("astrmai_pending_actions", [{"action": "poke", "target_id": "123"}])
        OUTBOUND_SEND_GATE.open()
        OUTBOUND_SEND_GATE.close(enforce_provider=True)
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-shutdown"))

        self.assertEqual(event.bot.api.calls, [])
        self.assertEqual(result[-1]["status"], "skipped")
        self.assertEqual(result[-1]["detail"], "shutdown_rejected")
        self.assertTrue(result[-1]["action_id"])
        self.assertTrue(result[-1]["transport_idempotency_key"])
        self.assertEqual(result[-1]["failure_kind"], "shutdown")

    def test_commits_native_actions_once_after_reply(self):
        event = _Event()
        event.set_extra("astrmai_turn_identity", SimpleNamespace())
        event.set_extra(
            "astrmai_pending_actions",
            [
                {"action": "poke", "target_id": "123", "group_id": "456"},
                {"action": "message_emoji_like", "message_id": "789", "payload": {"emoji_id": "66"}},
            ],
        )
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        asyncio.run(dispatcher.commit(event, "ff:GroupMessage:456", send_key="turn-1"))
        asyncio.run(dispatcher.commit(event, "ff:GroupMessage:456", send_key="turn-1"))

        self.assertEqual(
            event.bot.api.calls,
            [
                ("send_poke", {"user_id": 123, "group_id": 456}),
                ("set_msg_emoji_like", {"message_id": 789, "emoji_id": "66", "set": True}),
            ],
        )
        statuses = [item["status"] for item in event.get_extra("astrmai_qq_action_results")]
        self.assertEqual(statuses, ["sending", "sent", "sending", "sent", "skipped", "skipped"])

    def test_turn_outcome_blocks_action_after_dispatcher_replacement(self):
        event = _Event()
        event.set_extra(
            "astrmai_pending_actions",
            [{"action": "poke", "target_id": "123", "group_id": "456"}],
        )
        first = self.mod.QQActionDispatcher(self.config, _Coordinator())
        second = self.mod.QQActionDispatcher(self.config, _Coordinator())

        asyncio.run(first.commit(event, "ff:GroupMessage:456", send_key="turn-1"))
        asyncio.run(second.commit(event, "ff:GroupMessage:456", send_key="turn-1"))

        self.assertEqual(len(event.bot.api.calls), 1)
        outcome = event.get_extra("astrmai_turn_outcome", {})
        self.assertTrue(outcome["tool_actions_sent"])
        self.assertEqual(outcome["tool_action_count"], 1)

    def test_cancel_after_send_started_marks_action_uncertain(self):
        event = _Event()
        api = _CancelDuringSendApi()
        event.bot.api = api
        event.set_extra("astrmai_pending_actions", [{"action": "poke", "target_id": "123"}])
        store = self._persistent_store()
        dispatcher = self.mod.QQActionDispatcher(
            self.config,
            _Coordinator(),
            action_store=store,
        )

        async def _run():
            task = asyncio.create_task(
                dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-cancel-send")
            )
            await api.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(_run())
        outcome = event.get_extra("astrmai_turn_outcome", {})
        self.assertEqual(len(outcome.get("uncertain_tool_action_keys", [])), 1)
        key = outcome["uncertain_tool_action_keys"][0]
        row = asyncio.run(store.get(key))
        self.assertEqual(row["status"], "uncertain")

    def test_stale_turn_never_calls_napcat(self):
        event = _Event()
        event.set_extra("astrmai_turn_identity", SimpleNamespace())
        event.set_extra("astrmai_pending_actions", [{"action": "poke", "target_id": "123"}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator(current=False))

        asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-2"))

        self.assertEqual(event.bot.api.calls, [])
        self.assertEqual(event.get_extra("astrmai_qq_action_results")[0]["detail"], "stale_turn")
        self.assertTrue(event.get_extra("astrmai_qq_action_results")[0]["action_id"])
        self.assertEqual(event.get_extra("astrmai_qq_action_results")[0]["failure_kind"], "stale_turn")

    def test_api_unavailable_result_keeps_action_identity(self):
        event = _Event()
        event.bot.api = None
        event.set_extra("astrmai_pending_actions", [{"action": "like", "target_id": "123"}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-no-api"))

        self.assertEqual(result[-1]["status"], "failed")
        self.assertTrue(result[-1]["action_id"])
        self.assertTrue(result[-1]["transport_idempotency_key"])
        self.assertEqual(result[-1]["failure_kind"], "api_unavailable")

    def test_withdraw_targets_previous_committed_reply(self):
        event = _Event()
        event.set_extra("astrmai_pending_actions", [{"action": "withdraw"}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator(previous=["991", "992"]))

        asyncio.run(dispatcher.commit(event, "ff:GroupMessage:456", send_key="current-turn"))

        self.assertEqual(event.bot.api.calls, [("delete_msg", {"message_id": 992})])

    def test_withdraw_prefers_message_id_resolved_before_reply_send(self):
        event = _Event()
        event.set_extra("astrmai_pending_actions", [{"action": "withdraw", "message_id": "812"}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator(previous=["991"]))

        asyncio.run(dispatcher.commit(event, "ff:GroupMessage:456", send_key="current-turn"))

        self.assertEqual(event.bot.api.calls, [("delete_msg", {"message_id": 812})])

    def test_disabled_dispatcher_does_not_construct_side_effects(self):
        event = _Event()
        event.set_extra("astrmai_pending_actions", [{"action": "poke", "target_id": "123"}])
        self.config.conversation.qq_deferred_action_commit_enabled = False
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:GroupMessage:456", send_key="turn-3"))

        self.assertEqual(result, [])
        self.assertEqual(event.bot.api.calls, [])

    def test_sent_result_contains_canonical_trace_and_transport_key(self):
        event = _Event()
        event.set_extra("astrmai_pending_actions", [{"action": "like", "target_id": "123", "payload": {"times": 1}}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-meta"))

        sent = next(item for item in result if item["status"] == "sent")
        self.assertEqual(sent["canonical_tool_name"], "proactive_like_action")
        self.assertTrue(sent["action_id"].startswith("qqact_"))
        self.assertTrue(sent["transport_idempotency_key"])
        self.assertGreater(sent["sent_at"], 0)

    def test_identical_action_instances_are_both_sent_but_retry_is_deduplicated(self):
        from astrmai.conversation.contracts.qq_action import PendingQQAction

        event = _Event()
        first = PendingQQAction(action_type="like", target_id="123", payload={"times": 1})
        second = PendingQQAction(action_type="like", target_id="123", payload={"times": 1})
        self.assertNotEqual(first.action_instance_id, second.action_instance_id)
        event.set_extra("astrmai_pending_actions", [first.to_dict(), second.to_dict()])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-duplicate"))
        self.assertEqual(len(event.bot.api.calls), 2)

        asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-duplicate"))
        self.assertEqual(len(event.bot.api.calls), 2)

    def test_nonzero_retcode_is_failed_even_when_status_ok(self):
        event = _Event()
        event.bot.api = _Api(result={"status": "ok", "retcode": 100})
        event.set_extra("astrmai_pending_actions", [{"action": "like", "target_id": "123"}])
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-retcode"))

        self.assertEqual(len(event.bot.api.calls), 1)
        self.assertEqual(result[-1]["status"], "failed")
        self.assertIn("retcode=100", result[-1]["detail"])

    def test_quote_send_timeout_does_not_fallback_and_duplicate(self):
        event = _Event()
        event.bot.api = _TimeoutSendApi()
        event.set_extra(
            "astrmai_pending_actions",
            [{"action": "quote_reply", "target_id": "123", "message_id": "88", "payload": {"text": "hi"}}],
        )
        dispatcher = self.mod.QQActionDispatcher(self.config, _Coordinator())

        result = asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-timeout"))
        asyncio.run(dispatcher.commit(event, "ff:FriendMessage:123", send_key="turn-timeout"))

        self.assertEqual([name for name, _ in event.bot.api.calls], ["send_msg"])
        self.assertEqual(result[-1]["status"], "uncertain")

    def test_persistent_sent_action_is_deduplicated_after_dispatcher_reload(self):
        payload = {
            "action": "like",
            "action_instance_id": "qqai-persisted-sent",
            "target_id": "123",
            "payload": {"times": 1},
        }
        first_event = _Event()
        first_event.set_extra("astrmai_pending_actions", [payload])
        first = self.mod.QQActionDispatcher(
            self.config,
            _Coordinator(),
            action_store=self._persistent_store(),
        )

        first_result = asyncio.run(
            first.commit(first_event, "ff:FriendMessage:123", send_key="turn-persisted")
        )

        second_event = _Event()
        second_event.set_extra("astrmai_pending_actions", [payload])
        second = self.mod.QQActionDispatcher(
            self.config,
            _Coordinator(),
            action_store=self._persistent_store(),
        )
        second_result = asyncio.run(
            second.commit(second_event, "ff:FriendMessage:123", send_key="turn-persisted")
        )

        self.assertEqual(len(first_event.bot.api.calls), 1)
        self.assertEqual(second_event.bot.api.calls, [])
        self.assertEqual(first_result[-1]["status"], "sent")
        self.assertEqual(second_result[-1]["status"], "skipped")
        self.assertEqual(second_result[-1]["detail"], "duplicate")

    def test_concurrent_dispatchers_share_one_persistent_claim(self):
        payload = {
            "action": "like",
            "action_instance_id": "qqai-concurrent-claim",
            "target_id": "123",
        }
        first_event = _Event()
        second_event = _Event()
        first_event.set_extra("astrmai_pending_actions", [payload])
        second_event.set_extra("astrmai_pending_actions", [payload])
        first = self.mod.QQActionDispatcher(
            self.config,
            _Coordinator(),
            action_store=self._persistent_store(),
        )
        second = self.mod.QQActionDispatcher(
            self.config,
            _Coordinator(),
            action_store=self._persistent_store(),
        )

        async def run():
            return await asyncio.gather(
                first.commit(
                    first_event,
                    "ff:FriendMessage:123",
                    send_key="turn-concurrent",
                ),
                second.commit(
                    second_event,
                    "ff:FriendMessage:123",
                    send_key="turn-concurrent",
                ),
            )

        first_result, second_result = asyncio.run(run())

        self.assertEqual(
            len(first_event.bot.api.calls) + len(second_event.bot.api.calls),
            1,
        )
        self.assertEqual(
            sorted((first_result[-1]["status"], second_result[-1]["status"])),
            ["sent", "skipped"],
        )

    def test_ledger_claim_failure_is_fail_closed_before_napcat(self):
        event = _Event()
        event.set_extra(
            "astrmai_pending_actions",
            [
                {
                    "action": "like",
                    "action_instance_id": "qqai-ledger-down",
                    "target_id": "123",
                }
            ],
        )
        dispatcher = self.mod.QQActionDispatcher(
            self.config,
            _Coordinator(),
            action_store=_BrokenClaimStore(),
        )

        result = asyncio.run(
            dispatcher.commit(
                event,
                "ff:FriendMessage:123",
                send_key="turn-ledger-down",
            )
        )

        self.assertEqual(event.bot.api.calls, [])
        self.assertEqual(result[-1]["status"], "retryable")
        self.assertEqual(result[-1]["failure_kind"], "ledger")

    def test_transport_timeout_is_persisted_uncertain_and_never_auto_replayed(self):
        payload = {
            "action": "quote_reply",
            "action_instance_id": "qqai-timeout-uncertain",
            "target_id": "123",
            "message_id": "88",
            "payload": {"text": "hi"},
        }
        store = self._persistent_store()
        first_event = _Event()
        first_event.bot.api = _TimeoutSendApi()
        first_event.set_extra("astrmai_pending_actions", [payload])
        first = self.mod.QQActionDispatcher(
            self.config,
            _Coordinator(),
            action_store=store,
        )

        first_result = asyncio.run(
            first.commit(first_event, "ff:FriendMessage:123", send_key="turn-uncertain")
        )
        persisted = asyncio.run(store.get("qqai-timeout-uncertain"))

        second_event = _Event()
        second_event.set_extra("astrmai_pending_actions", [payload])
        second = self.mod.QQActionDispatcher(
            self.config,
            _Coordinator(),
            action_store=self._persistent_store(),
        )
        second_result = asyncio.run(
            second.commit(second_event, "ff:FriendMessage:123", send_key="turn-uncertain")
        )

        self.assertEqual(first_result[-1]["status"], "uncertain")
        self.assertGreater(first_result[-1]["completed_at"], 0)
        self.assertEqual(persisted["status"], "uncertain")
        self.assertEqual(second_event.bot.api.calls, [])
        self.assertEqual(second_result[-1]["failure_kind"], "uncertain")

    def test_expired_sending_claim_becomes_uncertain_and_old_lease_cannot_finish(self):
        store = self._persistent_store()

        first_claim = asyncio.run(
            store.claim(
                "qqai-expired-sending",
                action_instance_id="qqai-expired-sending",
                action_id="qqact-expired",
                action_type="like",
                chat_id="ff:FriendMessage:123",
                turn_id="turn-expired",
                trace_id="trace-expired",
                lease_seconds=30.0,
            )
        )
        self.assertTrue(first_claim.acquired)
        self.assertFalse(
            asyncio.run(
                store.mark_sent(
                    "qqai-expired-sending",
                    lease_token="stale-token",
                )
            )
        )
        with sqlite3.connect(store.db_path) as db:
            db.execute(
                "UPDATE qq_action_ledger SET lease_until = 0 WHERE transport_idempotency_key = ?",
                ("qqai-expired-sending",),
            )
            db.commit()

        second_claim = asyncio.run(
            store.claim(
                "qqai-expired-sending",
                action_instance_id="qqai-expired-sending",
                action_id="qqact-expired",
                action_type="like",
                chat_id="ff:FriendMessage:123",
                turn_id="turn-expired",
                trace_id="trace-expired",
            )
        )
        persisted = asyncio.run(store.get("qqai-expired-sending"))

        self.assertFalse(second_claim.acquired)
        self.assertEqual(second_claim.status, "uncertain")
        self.assertEqual(persisted["status"], "uncertain")
        self.assertFalse(
            asyncio.run(
                store.mark_sent(
                    "qqai-expired-sending",
                    lease_token=first_claim.lease_token,
                )
            )
        )

    def test_explicit_api_rejection_is_persisted_failed_and_can_retry(self):
        payload = {
            "action": "like",
            "action_instance_id": "qqai-explicit-rejection",
            "target_id": "123",
        }
        store = self._persistent_store()
        rejected_event = _Event()
        rejected_event.bot.api = _Api(result={"status": "ok", "retcode": 100})
        rejected_event.set_extra("astrmai_pending_actions", [payload])
        rejected = self.mod.QQActionDispatcher(
            self.config,
            _Coordinator(),
            action_store=store,
        )

        rejected_result = asyncio.run(
            rejected.commit(
                rejected_event,
                "ff:FriendMessage:123",
                send_key="turn-explicit-rejection",
            )
        )
        failed = asyncio.run(store.get("qqai-explicit-rejection"))

        retry_event = _Event()
        retry_event.set_extra("astrmai_pending_actions", [payload])
        retry = self.mod.QQActionDispatcher(
            self.config,
            _Coordinator(),
            action_store=self._persistent_store(),
        )
        retry_result = asyncio.run(
            retry.commit(
                retry_event,
                "ff:FriendMessage:123",
                send_key="turn-explicit-rejection",
            )
        )

        self.assertEqual(rejected_result[-1]["status"], "failed")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(retry_event.bot.api.calls), 1)
        self.assertEqual(retry_result[-1]["status"], "sent")
