import asyncio
import importlib
import sys
import tempfile
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

    async def claim_send(self, _chat_id, key):
        if key in self.claims:
            return False
        self.claims.add(key)
        return True

    async def commit_send(self, chat_id, key, message_ids):
        self.commits.append((chat_id, key, list(message_ids)))

    async def mark_send_failed(self, *_args):
        pass

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
        self.assertEqual(decision.text, "早")
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


if __name__ == "__main__":
    unittest.main()
