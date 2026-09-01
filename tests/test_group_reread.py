import asyncio
import collections
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _Event:
    def __init__(self, text, *, sender_id, message_id, self_id="bot-1", group_id="group-1", chain=None):
        self.message_str = text
        self.unified_msg_origin = f"default:GroupMessage:{group_id}"
        self._sender_id = sender_id
        self._self_id = self_id
        self._group_id = group_id
        self.message_obj = SimpleNamespace(message_id=message_id, message=list(chain or []))
        self._extra = {}

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_group_id(self):
        return self._group_id

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class _Coordinator:
    def __init__(self):
        self.claims = set()
        self.commits = []
        self.failures = []

    async def claim_send(self, _chat_id, key):
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    async def commit_send(self, chat_id, key, message_ids):
        self.commits.append((chat_id, key, list(message_ids)))

    async def mark_send_failed(self, *_args):
        self.failures.append(_args)
        if len(_args) > 1:
            self.claims.discard(_args[1])

    async def is_current_turn(self, _turn):
        return True


class _Context:
    def __init__(self):
        self.sent = []

    async def send_message(self, origin, chain):
        self.sent.append((origin, chain))
        return "outbound-1"


class _Store:
    def __init__(self):
        self.calls = []

    async def append_segment(self, chat_id, **kwargs):
        self.calls.append((chat_id, kwargs))


async def _raise_record_failure(*_args, **_kwargs):
    raise RuntimeError("record unavailable")


async def _noop_async(*_args, **_kwargs):
    return None


class GroupRereadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for name in (
            "astrmai.conversation.contracts.reread",
            "astrmai.conversation.attention.group_reread_observer",
            "astrmai.conversation.execution.reread_action_dispatcher",
            "astrmai.conversation.execution.reply_service",
        ):
            sys.modules.pop(name, None)
        self.observer_mod = importlib.import_module("astrmai.conversation.attention.group_reread_observer")
        self.dispatcher_mod = importlib.import_module("astrmai.conversation.execution.reread_action_dispatcher")
        self.reply_service_mod = importlib.import_module("astrmai.conversation.execution.reply_service")
        self.dispatcher_mod.MessageChain = type("MessageChain", (), {"__init__": lambda self: setattr(self, "chain", [])})
        self.dispatcher_mod.Comp.Plain = lambda text: SimpleNamespace(text=text)
        self.config = SimpleNamespace(
            conversation=SimpleNamespace(
                group_reread_enabled=True,
                group_reread_threshold=5,
                group_reread_window_sec=60,
                group_reread_cooldown_sec=0,
                group_reread_max_groups=64,
                group_reread_state_ttl_sec=600,
            )
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_five_distinct_members_trigger_passive_reread(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)

        async def _run():
            decision = None
            for index in range(5):
                decision = await observer.observe(_Event("  早\u200b ", sender_id=f"user-{index}", message_id=f"m-{index}"))
            return decision

        decision = asyncio.run(_run())
        self.assertIsNotNone(decision)
        self.assertEqual(decision.text, "  早\u200b ")
        self.assertEqual(decision.trigger_kind, "group_reread_passive")
        self.assertEqual(len(decision.participant_ids), 5)

    def test_bot_seed_and_four_members_trigger_but_reread_is_not_seeded(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)

        async def _run():
            await observer.record_outbound_text_seed("default:GroupMessage:group-1", "早", bot_id="bot-1", event_id="bot-original")
            decision = None
            for index in range(4):
                decision = await observer.observe(_Event("早", sender_id=f"user-{index}", message_id=f"m-{index}"))
            return decision

        decision = asyncio.run(_run())
        self.assertIsNotNone(decision)
        self.assertIn("Bot 先前", decision.explanation)
        self.assertEqual(decision.participant_ids[0], "bot-1")

    def test_reply_service_records_normal_bot_text_as_seed(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)
        event = _Event("收到", sender_id="user-0", message_id="inbound-1")
        service = SimpleNamespace(group_reread_observer=observer)
        receipt = SimpleNamespace(sent_segments=("收到",), outbound_message_ids=("bot-original",))

        async def _run():
            await self.reply_service_mod.ReplyService._record_group_reread_seeds(
                service,
                event,
                event.unified_msg_origin,
                receipt,
            )
            decision = None
            for index in range(4):
                decision = await observer.observe(_Event("收到", sender_id=f"user-{index + 1}", message_id=f"m-{index}"))
            return decision

        decision = asyncio.run(_run())
        self.assertIsNotNone(decision)
        self.assertEqual(decision.participant_ids[0], "bot-1")

    def test_repeated_sender_or_non_plain_message_does_not_trigger(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)
        image = SimpleNamespace(type="Image")

        async def _run():
            for index in range(5):
                result = await observer.observe(_Event("早", sender_id="same-user", message_id=f"m-{index}"))
                self.assertIsNone(result)
            return await observer.observe(_Event("早", sender_id="user-image", message_id="image", chain=[image]))

        self.assertIsNone(asyncio.run(_run()))

    def test_commands_anonymous_and_self_messages_do_not_enter_reread_window(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)

        async def _run():
            for index in range(5):
                event = _Event("早", sender_id=f"user-{index}", message_id=f"cmd-{index}")
                event.set_extra("heartflow_is_command", True)
                self.assertIsNone(await observer.observe(event))
            for index in range(5):
                event = _Event("早", sender_id=f"80000000{index}", message_id=f"anon-{index}")
                self.assertIsNone(await observer.observe(event))
            for index in range(5):
                event = _Event("早", sender_id="bot-1", message_id=f"self-{index}")
                self.assertIsNone(await observer.observe(event))

        asyncio.run(_run())

    def test_dispatcher_claims_once_and_records_internal_note(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)
        request = asyncio.run(observer.observe(_Event("早", sender_id="u0", message_id="m0")))
        self.assertIsNone(request)
        request = asyncio.run(observer.observe(_Event("早", sender_id="u1", message_id="m1")))
        request = asyncio.run(observer.observe(_Event("早", sender_id="u2", message_id="m2")))
        request = asyncio.run(observer.observe(_Event("早", sender_id="u3", message_id="m3")))
        request = asyncio.run(observer.observe(_Event("早", sender_id="u4", message_id="m4")))
        context = _Context()
        store = _Store()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=context,
            config=self.config,
            runtime_coordinator=_Coordinator(),
            dialogue_store=store,
        )
        event = _Event("早", sender_id="u4", message_id="m4")
        first = asyncio.run(dispatcher.dispatch(event, request))
        second = asyncio.run(dispatcher.dispatch(event, request))

        self.assertTrue(first.sent)
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(len(context.sent), 1)
        self.assertEqual(context.sent[0][1].chain[0].text, "早")
        self.assertEqual(store.calls[0][1]["provenance"], "group_reread_passive")
        self.assertIn("不表示事实认可", store.calls[0][1]["internal_note"])

    def test_active_reread_is_not_blocked_by_passive_cooldown(self):
        self.config.conversation.group_reread_cooldown_sec = 60
        observer = self.observer_mod.GroupRereadObserver(config=self.config)
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        passive = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="passive",
            trigger_kind="group_reread_passive",
            source_event_ids=("m1",),
        )
        active = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="晚安",
            fingerprint="active",
            trigger_kind="group_reread_active",
            source_event_ids=("m2",),
        )
        context = _Context()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=context,
            config=self.config,
            runtime_coordinator=_Coordinator(),
            dialogue_store=_Store(),
            reread_observer=observer,
        )
        event = _Event("早", sender_id="u1", message_id="m1")

        async def _run():
            return await dispatcher.dispatch(event, passive), await dispatcher.dispatch(event, active)

        first, second = asyncio.run(_run())
        self.assertTrue(first.sent)
        self.assertTrue(second.sent)
        self.assertEqual(len(context.sent), 2)

    def test_cooldown_rejection_releases_send_claim_for_retry(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="retry",
            trigger_kind="group_reread_active",
            source_event_ids=("m-retry",),
        )

        class _RejectOnceObserver:
            def __init__(self):
                self.calls = 0

            async def claim_dispatch(self, _chat_id):
                self.calls += 1
                return self.calls > 1

            async def release_dispatch(self, _chat_id):
                return True

        coordinator = _Coordinator()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=_Context(),
            config=self.config,
            runtime_coordinator=coordinator,
            reread_observer=_RejectOnceObserver(),
        )
        event = _Event("早", sender_id="u1", message_id="m-retry")

        async def _run():
            first = await dispatcher.dispatch(event, request)
            second = await dispatcher.dispatch(event, request)
            return first, second

        first, second = asyncio.run(_run())
        self.assertEqual(first.status, "cooldown")
        self.assertTrue(second.sent)
        self.assertEqual(len(coordinator.failures), 1)

    def test_observer_claim_failure_rolls_back_send_claim(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="observer-failure",
            trigger_kind="group_reread_active",
            source_event_ids=("m-observer-failure",),
        )

        class _FailingObserver:
            async def claim_dispatch(self, _chat_id, **_kwargs):
                raise RuntimeError("observer unavailable")

        coordinator = _Coordinator()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=_Context(),
            runtime_coordinator=coordinator,
            reread_observer=_FailingObserver(),
        )

        result = asyncio.run(dispatcher.dispatch(_Event("早", sender_id="u1", message_id="m-observer-failure"), request))

        self.assertEqual(result.status, "failed")
        self.assertEqual(coordinator.claims, set())
        self.assertEqual(len(coordinator.failures), 1)

    def test_failed_mark_falls_back_to_release_send_claim(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="mark-failure",
            trigger_kind="group_reread_active",
            source_event_ids=("m-mark-failure",),
        )

        class _FailingMarkCoordinator(_Coordinator):
            def __init__(self):
                super().__init__()
                self.releases = []

            async def mark_send_failed(self, *_args):
                raise RuntimeError("coordinator unavailable")

            async def release_send_claim(self, chat_id, send_key):
                self.releases.append((chat_id, send_key))
                self.claims.discard(send_key)

        class _RejectingObserver:
            async def claim_dispatch(self, _chat_id, **_kwargs):
                return None

        coordinator = _FailingMarkCoordinator()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=_Context(),
            runtime_coordinator=coordinator,
            reread_observer=_RejectingObserver(),
        )

        result = asyncio.run(dispatcher.dispatch(_Event("早", sender_id="u1", message_id="m-mark-failure"), request))

        self.assertEqual(result.status, "cooldown")
        self.assertEqual(coordinator.claims, set())
        self.assertEqual(len(coordinator.releases), 1)

    def test_missing_claim_rollback_interface_does_not_escape_dispatch(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="missing-rollback",
            trigger_kind="group_reread_active",
            source_event_ids=("m-missing-rollback",),
        )

        class _ClaimOnlyCoordinator:
            async def claim_send(self, _chat_id, _send_key):
                return True

        class _FailingContext:
            async def send_message(self, _origin, _chain):
                raise RuntimeError("send failed")

        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=_FailingContext(),
            runtime_coordinator=_ClaimOnlyCoordinator(),
        )

        result = asyncio.run(dispatcher.dispatch(_Event("早", sender_id="u1", message_id="m-missing-rollback"), request))

        self.assertEqual(result.status, "failed")

    def test_shutdown_coordinator_claim_is_released_after_stale_dispatch(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="shutdown-claim",
            trigger_kind="group_reread_active",
            source_event_ids=("m-shutdown-claim",),
        )

        class _ShutdownCoordinator:
            def __init__(self):
                self.claims = set()
                self.shutdown = False
                self.releases = []

            async def claim_send(self, _chat_id, send_key):
                self.claims.add(send_key)
                self.shutdown = True
                return True

            async def is_current_turn(self, _turn):
                return False

            async def mark_send_failed(self, _chat_id, _send_key, _reason):
                return False

            async def get_send_claim(self, _chat_id, send_key):
                if send_key in self.claims:
                    return {"status": "claimed"}
                return None

            async def release_send_claim(self, *, chat_id, send_key):
                self.releases.append((chat_id, send_key))
                self.claims.discard(send_key)

        coordinator = _ShutdownCoordinator()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=_Context(),
            runtime_coordinator=coordinator,
        )
        event = _Event("早", sender_id="u1", message_id="m-shutdown-claim")
        event.set_extra("astrmai_turn_identity", object())

        result = asyncio.run(dispatcher.dispatch(event, request))

        self.assertEqual(result.status, "stale")
        self.assertEqual(coordinator.claims, set())
        self.assertEqual(len(coordinator.releases), 1)

    def test_release_still_claimed_is_reported_as_degraded(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="silent-release",
            trigger_kind="group_reread_active",
            source_event_ids=("m-silent-release",),
        )

        class _SilentReleaseCoordinator:
            def __init__(self):
                self.claims = set()

            async def claim_send(self, _chat_id, send_key):
                self.claims.add(send_key)
                return True

            async def is_current_turn(self, _turn):
                return False

            async def mark_send_failed(self, _chat_id, _send_key, _reason):
                return False

            async def get_send_claim(self, _chat_id, send_key):
                if send_key in self.claims:
                    return {"status": "claimed"}
                return None

            async def release_send_claim(self, _chat_id, _send_key):
                return False

        coordinator = _SilentReleaseCoordinator()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=_Context(),
            runtime_coordinator=coordinator,
        )
        event = _Event("早", sender_id="u1", message_id="m-silent-release")
        event.set_extra("astrmai_turn_identity", object())

        result = asyncio.run(dispatcher.dispatch(event, request))

        self.assertEqual(result.status, "stale")
        self.assertEqual(dispatcher.describe_status()["claim_rollback_degraded"], 1)

    def test_real_coordinator_shutdown_stale_dispatch_can_reclaim_after_reopen(self):
        from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator

        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="real-shutdown",
            trigger_kind="group_reread_active",
            source_event_ids=("m-real-shutdown",),
        )

        class _ShutdownAfterClaimCoordinator(ChatRuntimeCoordinator):
            async def claim_send(self, chat_id, send_key):
                claimed = await super().claim_send(chat_id, send_key)
                if claimed:
                    await self.shutdown()
                return claimed

        coordinator = _ShutdownAfterClaimCoordinator()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=_Context(),
            runtime_coordinator=coordinator,
        )
        event = _Event("早", sender_id="u1", message_id="m-real-shutdown")
        event.set_extra("astrmai_turn_identity", object())

        async def _run():
            result = await dispatcher.dispatch(event, request)
            send_key = dispatcher._send_key(request)
            await coordinator.reopen()
            reclaimable = await coordinator.claim_send(request.chat_id, send_key)
            return result, reclaimable

        result, reclaimable = asyncio.run(_run())

        self.assertEqual(result.status, "stale")
        self.assertTrue(reclaimable)

    def test_dispatch_lease_token_cannot_release_newer_claim(self):
        self.config.conversation.group_reread_cooldown_sec = 60
        observer = self.observer_mod.GroupRereadObserver(config=self.config)

        async def _run():
            first = await observer.claim_dispatch("default:GroupMessage:group-1", trigger_kind="group_reread_active")
            self.assertTrue(first)
            await observer.release_dispatch("default:GroupMessage:group-1", first)
            second = await observer.claim_dispatch("default:GroupMessage:group-1", trigger_kind="group_reread_active")
            self.assertTrue(second)
            self.assertFalse(await observer.release_dispatch("default:GroupMessage:group-1", first))
            self.assertTrue(await observer.commit_dispatch("default:GroupMessage:group-1", second, trigger_kind="group_reread_active"))
            return second

        second = asyncio.run(_run())
        self.assertTrue(second)
        self.assertEqual(observer.describe_status()["cooldown_groups"], 0)

    def test_cancelled_dispatch_releases_inflight_lease(self):
        self.config.conversation.group_reread_cooldown_sec = 60
        observer = self.observer_mod.GroupRereadObserver(config=self.config)
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="cancel",
            trigger_kind="group_reread_active",
        )
        started = asyncio.Event()

        class _BlockingContext(_Context):
            async def send_message(self, origin, chain):
                started.set()
                await asyncio.Event().wait()

        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=_BlockingContext(),
            runtime_coordinator=_Coordinator(),
            reread_observer=observer,
        )
        event = _Event("早", sender_id="u1", message_id="cancel")

        async def _run():
            task = asyncio.create_task(dispatcher.dispatch(event, request))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return await observer.claim_dispatch(request.chat_id, trigger_kind=request.trigger_kind)

        token = asyncio.run(_run())
        self.assertTrue(token)

    def test_zero_cooldown_is_respected_and_passive_switch_does_not_disable_active(self):
        self.config.conversation.group_reread_cooldown_sec = 0
        self.config.conversation.group_reread_enabled = False
        observer = self.observer_mod.GroupRereadObserver(config=self.config)

        async def _run():
            active = await observer.claim_dispatch("default:GroupMessage:group-1", trigger_kind="group_reread_active")
            self.assertTrue(active)
            self.assertTrue(await observer.commit_dispatch("default:GroupMessage:group-1", active, trigger_kind="group_reread_active"))
            return await observer.claim_dispatch("default:GroupMessage:group-1", trigger_kind="group_reread_active")

        self.assertTrue(asyncio.run(_run()))

    def test_protected_capacity_blocks_new_claims_without_growing_state(self):
        self.config.conversation.group_reread_max_groups = 16
        self.config.conversation.group_reread_cooldown_sec = 600
        observer = self.observer_mod.GroupRereadObserver(config=self.config)

        async def _run():
            tokens = []
            for index in range(100):
                token = await observer.claim_dispatch(
                    f"default:GroupMessage:group-{index}",
                    trigger_kind="group_reread_active",
                )
                if token:
                    tokens.append((index, token))
            return tokens

        tokens = asyncio.run(_run())
        self.assertEqual(len(tokens), 16)
        self.assertEqual(observer.describe_status()["active_groups"], 16)

    def test_cooldown_hit_discards_old_observation_chain(self):
        self.config.conversation.group_reread_cooldown_sec = 60
        observer = self.observer_mod.GroupRereadObserver(config=self.config)

        async def _run():
            token = await observer.claim_dispatch("default:GroupMessage:group-1", trigger_kind="group_reread_passive")
            await observer.commit_dispatch("default:GroupMessage:group-1", token, trigger_kind="group_reread_passive")
            for index in range(4):
                result = await observer.observe(_Event("早", sender_id=f"u-{index}", message_id=f"m-{index}"))
                self.assertIsNone(result)
            return observer._states["default:GroupMessage:group-1"].records

        self.assertEqual(asyncio.run(_run()), [])

    def test_send_completed_cancellation_does_not_retry_visible_message(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="post-send-cancel",
            trigger_kind="group_reread_active",
        )
        sent = []
        gate = asyncio.Event()

        class _Context:
            async def send_message(self, origin, chain):
                sent.append(origin)
                return "outbound-1"

        class _BlockingCoordinator:
            def __init__(self):
                self.calls = 0

            async def commit_send(self, chat_id, key, message_ids):
                self.calls += 1
                if self.calls == 1:
                    await gate.wait()

        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=_Context(),
            runtime_coordinator=_BlockingCoordinator(),
            reread_observer=observer,
        )
        event = _Event("早", sender_id="u1", message_id="post-send-cancel")

        async def _run():
            task = asyncio.create_task(dispatcher.dispatch(event, request))
            while not sent:
                await asyncio.sleep(0)
            task.cancel()
            result = await task
            return result

        result = asyncio.run(_run())
        self.assertTrue(result.sent)
        self.assertEqual(len(sent), 1)

    def test_failed_cooldown_commit_is_reported_without_retrying_send(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="commit-fail",
            trigger_kind="group_reread_active",
        )

        class _Observer:
            async def claim_dispatch(self, chat_id, *, trigger_kind):
                return "token"

            async def commit_dispatch(self, chat_id, token, *, trigger_kind):
                return False

            async def release_dispatch(self, chat_id, token):
                return True

        context = _Context()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=context,
            runtime_coordinator=_Coordinator(),
            reread_observer=_Observer(),
        )
        result = asyncio.run(dispatcher.dispatch(_Event("早", sender_id="u1", message_id="commit-fail"), request))

        self.assertTrue(result.sent)
        self.assertEqual(result.detail, "settlement_degraded")
        self.assertEqual(len(context.sent), 1)

    def test_post_send_coordinator_failure_is_retryable_without_resending(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="commit-send-fail",
            trigger_kind="group_reread_active",
        )

        class _FlakyCoordinator(_Coordinator):
            def __init__(self):
                super().__init__()
                self.commit_attempts = 0

            async def commit_send(self, chat_id, key, message_ids):
                self.commit_attempts += 1
                if self.commit_attempts == 1:
                    raise RuntimeError("commit unavailable")
                await super().commit_send(chat_id, key, message_ids)

        context = _Context()
        coordinator = _FlakyCoordinator()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=context,
            runtime_coordinator=coordinator,
            reread_observer=self.observer_mod.GroupRereadObserver(config=self.config),
        )

        async def _run():
            result = await dispatcher.dispatch(
                _Event("早", sender_id="u1", message_id="commit-send-fail"), request
            )
            await asyncio.sleep(0.2)
            return result

        result = asyncio.run(_run())
        self.assertTrue(result.sent)
        self.assertEqual(result.detail, "settlement_degraded")
        self.assertEqual(len(context.sent), 1)
        self.assertEqual(coordinator.commit_attempts, 2)

    def test_post_send_settlement_failure_releases_cancelled_lease(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="cancel-settle-fail",
            trigger_kind="group_reread_active",
        )
        gate = asyncio.Event()

        class _FailingCoordinator(_Coordinator):
            async def commit_send(self, _chat_id, _key, _message_ids):
                await gate.wait()
                raise RuntimeError("commit unavailable")

        context = _Context()
        observer = self.observer_mod.GroupRereadObserver(config=self.config)
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=context,
            runtime_coordinator=_FailingCoordinator(),
            reread_observer=observer,
        )

        async def _run():
            task = asyncio.create_task(
                dispatcher.dispatch(_Event("早", sender_id="u1", message_id="cancel-settle-fail"), request)
            )
            while not context.sent:
                await asyncio.sleep(0)
            task.cancel()
            gate.set()
            result = await task
            await dispatcher.drain_settlement_retries()
            state = observer._states[request.chat_id]
            return result, state.inflight_token

        result, token = asyncio.run(_run())
        self.assertTrue(result.sent)
        self.assertEqual(result.detail, "settlement_degraded")
        self.assertEqual(token, "")

    def test_settlement_retry_skips_already_committed_coordinator(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="idempotent-settle",
            trigger_kind="group_reread_active",
        )

        class _CommitAfterWriteCoordinator(_Coordinator):
            def __init__(self):
                super().__init__()
                self.commit_attempts = 0
                self.committed = False

            async def get_send_claim(self, _chat_id, _key):
                return {"status": "committed"} if self.committed else {"status": "claimed"}

            async def commit_send(self, chat_id, key, message_ids):
                self.commit_attempts += 1
                self.committed = True
                if self.commit_attempts == 1:
                    raise RuntimeError("response lost after commit")
                await super().commit_send(chat_id, key, message_ids)

        context = _Context()
        coordinator = _CommitAfterWriteCoordinator()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=context,
            runtime_coordinator=coordinator,
            reread_observer=self.observer_mod.GroupRereadObserver(config=self.config),
        )

        async def _run():
            result = await dispatcher.dispatch(
                _Event("早", sender_id="u1", message_id="idempotent-settle"), request
            )
            await asyncio.sleep(0.2)
            return result, dispatcher.describe_status()

        result, status = asyncio.run(_run())
        self.assertTrue(result.sent)
        self.assertEqual(result.detail, "settlement_degraded")
        self.assertEqual(coordinator.commit_attempts, 1)
        self.assertEqual(status["retry_pending"], 0)

    def test_settlement_retry_shutdown_and_capacity_are_bounded(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="bounded-settle",
            trigger_kind="group_reread_active",
        )
        released = []

        class _Observer:
            async def release_dispatch(self, chat_id, token):
                released.append((chat_id, token))

            async def commit_dispatch(self, _chat_id, _token, *, trigger_kind):
                return False

        class _FailingCoordinator:
            async def commit_send(self, _chat_id, _key, _message_ids):
                raise RuntimeError("unavailable")

        async def _run():
            dispatcher = self.dispatcher_mod.RereadActionDispatcher(
                runtime_coordinator=_FailingCoordinator(), reread_observer=_Observer()
            )
            dispatcher.MAX_SETTLEMENT_RETRY_TASKS = 1
            await dispatcher._schedule_settlement_retry(request, "key-1", (), "token-1", dispatcher.runtime_coordinator)
            await asyncio.sleep(0)
            await dispatcher._schedule_settlement_retry(request, "key-2", (), "token-2", dispatcher.runtime_coordinator)
            status_before = dispatcher.describe_status()
            await dispatcher.shutdown()
            return status_before, dispatcher.describe_status(), released

        status_before, status_after, released = asyncio.run(_run())
        self.assertEqual(status_before["retry_pending"], 1)
        self.assertEqual(status_before["retry_rejected"], 1)
        self.assertEqual(status_after["retry_pending"], 0)
        self.assertEqual(status_after["shutting_down"], True)
        self.assertIn((request.chat_id, "token-1"), released)
        self.assertIn((request.chat_id, "token-2"), released)

    def test_shutdown_blocks_dispatch_until_resume(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="shutdown-barrier",
            trigger_kind="group_reread_active",
        )
        context = _Context()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(context=context)
        event = _Event("早", sender_id="u1", message_id="shutdown-barrier")

        async def _run():
            await dispatcher.shutdown()
            blocked = await dispatcher.dispatch(event, request)
            dispatcher.resume()
            resumed = await dispatcher.dispatch(event, request)
            return blocked, resumed

        blocked, resumed = asyncio.run(_run())
        self.assertEqual(blocked.status, "shutdown")
        self.assertEqual(blocked.detail, "dispatcher_shutdown")
        self.assertTrue(resumed.sent)
        self.assertEqual(len(context.sent), 1)

    def test_shutdown_waits_for_inflight_dispatch(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="inflight-shutdown",
            trigger_kind="group_reread_active",
        )
        started = asyncio.Event()
        release = asyncio.Event()

        class _BlockingContext:
            async def send_message(self, origin, chain):
                started.set()
                await release.wait()
                return "outbound-1"

        dispatcher = self.dispatcher_mod.RereadActionDispatcher(context=_BlockingContext())
        event = _Event("早", sender_id="u1", message_id="inflight-shutdown")

        async def _run():
            dispatch_task = asyncio.create_task(dispatcher.dispatch(event, request))
            await started.wait()
            shutdown_task = asyncio.create_task(dispatcher.shutdown())
            await asyncio.sleep(0)
            pending_before_release = not shutdown_task.done()
            release.set()
            result = await dispatch_task
            await shutdown_task
            return pending_before_release, result

        pending_before_release, result = asyncio.run(_run())
        self.assertTrue(pending_before_release)
        self.assertTrue(result.sent)

    def test_retry_cancelled_before_first_run_releases_lease(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="cancel-before-run",
            trigger_kind="group_reread_active",
        )
        released = []

        class _Observer:
            async def release_dispatch(self, chat_id, token):
                released.append((chat_id, token))

        dispatcher = self.dispatcher_mod.RereadActionDispatcher(reread_observer=_Observer())

        async def _run():
            await dispatcher._schedule_settlement_retry(
                request, "cancel-before-run-key", (), "token-before-run", None
            )
            task = next(iter(dispatcher._settlement_retry_tasks.values()))
            task.cancel()
            for _ in range(3):
                await asyncio.sleep(0)
                cleanup = list(dispatcher._retry_cleanup_tasks)
                if cleanup:
                    await asyncio.gather(*cleanup, return_exceptions=True)
                if not dispatcher._retry_leases:
                    break
            return dispatcher.describe_status()

        status = asyncio.run(_run())
        self.assertEqual(released, [(request.chat_id, "token-before-run")])
        self.assertEqual(status["retry_pending"], 0)

    def test_release_compat_supports_unnamed_positional_token(self):
        released = []

        class _LegacyObserver:
            async def release_dispatch(self, chat_id, lease):
                released.append((chat_id, lease))

        dispatcher = self.dispatcher_mod.RereadActionDispatcher(reread_observer=_LegacyObserver())

        asyncio.run(dispatcher._release_observer_claim("chat-1", "lease-1"))
        self.assertEqual(released, [("chat-1", "lease-1")])

    def test_force_shutdown_timeout_is_bounded_and_blocks_resume(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class _Context:
            async def send_message(self, origin, chain):
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                return "late"

        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1", text="早", fingerprint="forced-timeout",
            trigger_kind="group_reread_active",
        )
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(context=_Context())
        event = _Event("早", sender_id="u1", message_id="forced-timeout")

        async def _run():
            task = asyncio.create_task(dispatcher.dispatch(event, request))
            await started.wait()
            await dispatcher.force_shutdown(timeout_sec=0.01)
            status = dispatcher.describe_status()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            return status

        status = asyncio.run(_run())
        self.assertTrue(status["pending_dispatch_shutdown"])
        self.assertFalse(dispatcher.resume())

    def test_release_exhaustion_keeps_resume_blocked(self):
        class _Observer:
            async def release_dispatch(self, _chat_id, _token):
                raise RuntimeError("observer unavailable")

        dispatcher = self.dispatcher_mod.RereadActionDispatcher(reread_observer=_Observer())
        dispatcher._retry_leases["lease-key"] = ("chat-1", "token-1")

        async def _run():
            for _ in range(3):
                await dispatcher._finalize_retry("lease-key", settled=False, cancelled=True)
            return dispatcher.describe_status()

        status = asyncio.run(_run())
        self.assertIn("lease-key", dispatcher._retry_leases)
        self.assertFalse(dispatcher.resume())
        self.assertGreaterEqual(status["release_exhausted"], 1)

    def test_successful_release_clears_exhausted_tombstone(self):
        class _Observer:
            async def release_dispatch(self, _chat_id, _token):
                return True

        dispatcher = self.dispatcher_mod.RereadActionDispatcher(reread_observer=_Observer())
        dispatcher._retry_leases["lease-key"] = ("chat-1", "token-1")
        dispatcher._retry_release_exhausted.add("lease-key")
        dispatcher._settlement_retry_stats["release_exhausted"] = 1

        async def _run():
            await dispatcher._finalize_retry("lease-key", settled=False, cancelled=True)

        asyncio.run(_run())
        self.assertNotIn("lease-key", dispatcher._retry_release_exhausted)
        self.assertEqual(dispatcher.describe_status()["release_exhausted"], 0)
        self.assertTrue(dispatcher.resume())

    def test_pending_request_survives_threshold_until_commit(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)

        async def _run():
            request = None
            for index in range(5):
                request = await observer.observe(
                    _Event("早", sender_id=f"pending-{index}", message_id=f"pending-{index}")
                )
            state = observer._states[request.chat_id]
            pending_before = state.pending_request
            token = await observer.claim_dispatch(request.chat_id)
            restored = await observer.restore_pending(request.chat_id, token)
            return request, pending_before, restored

        request, pending_before, restored = asyncio.run(_run())
        self.assertIsNotNone(request)
        self.assertIsNotNone(pending_before)
        self.assertTrue(restored)
        self.assertEqual(observer.describe_status()["pending_groups"], 0)
        self.assertEqual(len(observer._states[request.chat_id].records), 5)

    def test_original_display_text_is_preserved_while_fingerprint_is_normalized(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)

        async def _run():
            result = None
            for index in range(5):
                result = await observer.observe(
                    _Event("  早\u200b  晚 ", sender_id=f"raw-{index}", message_id=f"raw-{index}")
                )
            return result

        request = asyncio.run(_run())
        self.assertEqual(request.text, "  早\u200b  晚 ")
        self.assertEqual(request.fingerprint, observer.fingerprint("早 晚"))

    def test_other_bot_messages_do_not_count_as_reread_members(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)
        bot_event = _Event("早", sender_id="other-bot", message_id="bot-event")
        bot_event.is_bot = True

        async def _run():
            self.assertIsNone(await observer.observe(bot_event))
            result = None
            for index in range(4):
                result = await observer.observe(
                    _Event("早", sender_id=f"human-{index}", message_id=f"human-{index}")
                )
            return result

        self.assertIsNone(asyncio.run(_run()))

    def test_nested_bot_sender_is_not_overridden_by_top_level_false(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)
        bot_event = _Event("早", sender_id="nested-bot", message_id="nested-bot")
        bot_event.is_bot = False
        bot_event.message_obj.sender = SimpleNamespace(is_bot=True)

        self.assertIsNone(asyncio.run(observer.observe(bot_event)))

    def test_missing_event_id_gets_stable_per_event_identity(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)
        first = _Event("早", sender_id="identity-1", message_id="")
        second = _Event("早", sender_id="identity-1", message_id="")
        first.message_obj.message_id = ""
        second.message_obj.message_id = ""

        async def _run():
            first_id = observer._stable_event_identity(
                first, first.unified_msg_origin, first.get_sender_id(), "早"
            )
            first_retry_id = observer._stable_event_identity(
                first, first.unified_msg_origin, first.get_sender_id(), "早"
            )
            second_id = observer._stable_event_identity(
                second, second.unified_msg_origin, second.get_sender_id(), "早"
            )
            return first_id, first_retry_id, second_id

        first_id, first_retry_id, second_id = asyncio.run(_run())
        self.assertEqual(first_id, first_retry_id)
        self.assertNotEqual(first_id, second_id)

    def test_coordinator_commit_false_does_not_commit_observer(self):
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1", text="早", fingerprint="commit-false",
            trigger_kind="group_reread_active", source_event_ids=("commit-false",),
        )

        class _FalseCoordinator(_Coordinator):
            async def commit_send(self, *_args):
                return False

        class _Observer:
            def __init__(self):
                self.committed = False

            async def claim_dispatch(self, _chat_id, *, trigger_kind):
                return "token"

            async def commit_dispatch(self, *_args, **_kwargs):
                self.committed = True
                return True

            async def release_dispatch(self, *_args):
                return True

        observer = _Observer()
        dispatcher = self.dispatcher_mod.RereadActionDispatcher(
            context=_Context(), runtime_coordinator=_FalseCoordinator(), reread_observer=observer
        )
        result = asyncio.run(dispatcher.dispatch(_Event("早", sender_id="u1", message_id="commit-false"), request))
        self.assertTrue(result.sent)
        self.assertEqual(result.detail, "settlement_degraded")
        self.assertFalse(observer.committed)

    def test_facade_record_incoming_without_reply_is_idempotent(self):
        facade_mod = importlib.import_module("astrmai.app.plugin_facade")
        calls = []

        class _Gate:
            async def record_incoming_without_reply(self, event):
                calls.append("gate")
                return True

        class _Evolution:
            async def record_user_message(self, event):
                calls.append("evolution")

        facade = object.__new__(facade_mod.PluginFacade)
        facade.runtime = SimpleNamespace(attention_gate=_Gate(), evolution=_Evolution())
        event = _Event("早", sender_id="u1", message_id="facade-record")

        async def _run():
            first = await facade.record_incoming_without_reply(event)
            second = await facade.record_incoming_without_reply(event)
            return first, second

        first, second = asyncio.run(_run())
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(calls, ["gate", "evolution"])

    def test_observer_release_failure_is_retried_in_background(self):
        released = []

        class _FlakyObserver:
            def __init__(self):
                self.attempts = 0

            async def release_dispatch(self, _chat_id, _token):
                self.attempts += 1
                if self.attempts < 2:
                    raise RuntimeError("temporary release failure")
                released.append(self.attempts)
                return True

        async def _run():
            observer = _FlakyObserver()
            dispatcher = self.dispatcher_mod.RereadActionDispatcher(reread_observer=observer)
            self.assertFalse(await dispatcher._release_observer_claim("chat", "lease"))
            await asyncio.sleep(0.2)
            return dispatcher.describe_status()

        status = asyncio.run(_run())
        self.assertEqual(released, [2])
        self.assertEqual(status["release_retry_pending"], 0)

    def test_release_without_owned_token_cannot_clear_concurrent_lease(self):
        released = []

        class _Observer:
            async def release_dispatch(self, chat_id, token):
                released.append((chat_id, token))
                return True

        dispatcher = self.dispatcher_mod.RereadActionDispatcher(reread_observer=_Observer())
        self.assertFalse(asyncio.run(dispatcher._release_observer_claim("chat-1")))
        self.assertEqual(released, [])

    def test_lease_expiry_restores_passive_pending_before_new_claim(self):
        observer = self.observer_mod.GroupRereadObserver(config=self.config)

        async def _run():
            request = None
            for index in range(5):
                request = await observer.observe(
                    _Event("早", sender_id=f"lease-{index}", message_id=f"lease-{index}")
                )
            first_token = await observer.claim_dispatch(request.chat_id)
            state = observer._states[request.chat_id]
            state.inflight_started_at = time.monotonic() - 120.0
            second_token = await observer.claim_dispatch(request.chat_id)
            return request, first_token, second_token, state

        request, first_token, second_token, state = asyncio.run(_run())
        self.assertTrue(first_token)
        self.assertTrue(second_token)
        self.assertNotEqual(first_token, second_token)
        self.assertIsNone(state.pending_request)
        self.assertEqual(len(state.records), 5)
        self.assertGreaterEqual(
            observer.describe_status()["stats"].get("pending_restored_after_lease_expired", 0),
            1,
        )

    def test_facade_group_reread_records_after_observe_before_dispatch(self):
        facade_mod = importlib.import_module("astrmai.app.plugin_facade")
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        order = []
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="facade-order",
            trigger_kind="group_reread_passive",
        )

        class _Observer:
            async def observe(self, _event):
                order.append("observe")
                return request

            async def restore_pending(self, _chat_id):
                order.append("restore")
                return True

        class _Dispatcher:
            async def dispatch(self, _event, _request):
                order.append("dispatch")
                return contract_mod.RereadDispatchResult("sent")

        class _Gate:
            async def record_incoming_without_reply(self, _event):
                order.append("record")
                return True

        facade = object.__new__(facade_mod.PluginFacade)
        facade.runtime = SimpleNamespace(
            group_reread_observer=_Observer(),
            reread_action_dispatcher=_Dispatcher(),
            attention_gate=_Gate(),
            evolution=None,
        )
        event = _Event("早", sender_id="facade-user", message_id="facade-order")
        self.assertTrue(asyncio.run(facade.try_dispatch_group_reread(event)))
        self.assertEqual(order, ["observe", "record", "dispatch"])

    def test_facade_evolution_record_failure_does_not_block_reread_dispatch(self):
        facade_mod = importlib.import_module("astrmai.app.plugin_facade")
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="evolution-failure",
            trigger_kind="group_reread_passive",
        )
        calls = []
        learning_failed = asyncio.Event()

        class _Observer:
            async def observe(self, _event):
                return request

            async def abandon_pending(self, chat_id):
                calls.append(("abandon", chat_id))
                return True

        class _Gate:
            async def record_incoming_without_reply(self, event):
                event.set_extra("astrmai_incoming_recorded", True)
                return True

        class _Evolution:
            async def record_user_message(self, _event):
                try:
                    raise RuntimeError("learning store unavailable")
                finally:
                    learning_failed.set()

        class _Dispatcher:
            async def dispatch(self, *_args):
                calls.append(("dispatch",))
                return contract_mod.RereadDispatchResult("sent")

        facade = object.__new__(facade_mod.PluginFacade)
        facade.runtime = SimpleNamespace(
            group_reread_observer=_Observer(),
            reread_action_dispatcher=_Dispatcher(),
            attention_gate=_Gate(),
            evolution=_Evolution(),
        )
        event = _Event("早", sender_id="evolution-failure", message_id="evolution-failure")

        async def _run():
            result = await facade.try_dispatch_group_reread(event)
            await asyncio.wait_for(learning_failed.wait(), timeout=1.0)
            await asyncio.sleep(0)
            return result

        result = asyncio.run(_run())
        self.assertTrue(result)
        self.assertEqual(calls, [("dispatch",)])
        self.assertFalse(event.get_extra("astrmai_evolution_recorded", False))
        self.assertEqual(event.get_extra("astrmai_evolution_record_failed"), "RuntimeError")

    def test_facade_inbound_record_exception_restores_pending(self):
        facade_mod = importlib.import_module("astrmai.app.plugin_facade")
        contract_mod = importlib.import_module("astrmai.conversation.contracts.reread")
        request = contract_mod.RereadActionRequest(
            chat_id="default:GroupMessage:group-1",
            text="早",
            fingerprint="record-raises",
            trigger_kind="group_reread_passive",
        )
        calls = []

        class _Observer:
            async def observe(self, _event):
                return request

            async def restore_pending(self, chat_id):
                calls.append(("restore", chat_id))
                return True

        class _Gate:
            async def record_incoming_without_reply(self, _event):
                raise RuntimeError("database unavailable")

        facade = object.__new__(facade_mod.PluginFacade)
        facade.runtime = SimpleNamespace(
            group_reread_observer=_Observer(),
            reread_action_dispatcher=SimpleNamespace(),
            attention_gate=_Gate(),
            evolution=None,
        )
        event = _Event("早", sender_id="record-raises", message_id="record-raises")

        result = asyncio.run(facade.try_dispatch_group_reread(event))
        self.assertFalse(result)
        self.assertEqual(calls, [("restore", request.chat_id)])
        self.assertEqual(event.get_extra("astrmai_group_reread_status"), "record_failed")

    def test_inbound_record_failure_releases_message_claim(self):
        gate_mod = importlib.import_module("astrmai.conversation.attention.gate")
        gate = object.__new__(gate_mod.AttentionGate)
        gate._global_message_cache = collections.OrderedDict()
        gate._record_event_activity = _raise_record_failure
        gate._append_dialogue_segment = _noop_async
        event = _Event("早", sender_id="record-failure", message_id="record-failure")

        result = asyncio.run(gate.record_incoming_without_reply(event))
        self.assertFalse(result)
        self.assertFalse(event.get_extra("astrmai_incoming_recorded", False))
        self.assertTrue(event.get_extra("astrmai_incoming_record_failed", False))
        self.assertEqual(len(gate._global_message_cache), 0)


if __name__ == "__main__":
    unittest.main()
