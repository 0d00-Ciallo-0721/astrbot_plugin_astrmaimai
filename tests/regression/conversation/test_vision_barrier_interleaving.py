"""G2 / TG-07 回归测试：私聊图片屏障的并发交织分支。

4da2910 新增、OPT-07 又改过（burst deadline 跨迭代持久化）的三条分支此前零测试——
现有 gate fixture 的 prepare_batch stub 都是瞬时返回，永远命中不到回填分支。
本文件用 asyncio.Event 精确控制交织时刻（不使用 sleep 竞态写法）：

1. 屏障执行期间新消息入池 → 第二轮批次含旧+新且不重复派发下游
2. abort 分支：整批只发一次失败通知；池非空时新消息继续处理，池空才收工
3. resolve 超时 → outcome=resolve_timeout，downstream_action 按策略
   （require_analysis→abort_required_vision / timeout_fallback→continue_with_placeholder）

附带锚定 OPT-07：burst deadline 跨迭代不重置（否则屏障期间不断有新消息时视觉预算无上界）。
"""

import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _Event:
    def __init__(self, text: str, sender_id: str = "user-1"):
        self._extras: dict = {"astrmai_timestamp": time.time(), "is_private_chat": True}
        self._sender_id = sender_id
        self.message_str = text
        self.unified_msg_origin = "default:FriendMessage:user-1"
        self.message_obj = SimpleNamespace(message=[], self_id="bot-1", raw_message=None)
        self.sent_texts: list[str] = []

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "Alice"

    def get_group_id(self):
        return ""

    def plain_result(self, text):
        return text

    async def send(self, payload):
        self.sent_texts.append(str(payload))


class _ScriptedCoordinator:
    """按脚本返回屏障结果，并用 Event 精确控制第 N 次调用的阻塞时刻。"""

    def __init__(self, outcomes, *, gate_call_index=None):
        self._outcomes = list(outcomes)
        self._gate_call_index = gate_call_index
        self.calls: list[dict] = []
        self.budget_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def wait_for_input_stability(self, session):
        return None

    def merge_pending_batch(self, chat_id, batch_events):
        return batch_events

    def bind_batch_context(self, batch_events, focus_event):
        return None

    def vision_total_budget_sec(self) -> float:
        # 每次返回不同值：既能被调用次数断言捕获，也让 deadline 数值差异不再
        # 依赖 time.monotonic 的时钟分辨率（Windows 约 15.6ms，两轮迭代常落同一刻度）
        self.budget_calls += 1
        return 180.0 * self.budget_calls

    async def prepare_batch(self, events, chat_id, *, deadline=None):
        index = len(self.calls)
        self.calls.append(
            {
                "texts": [item.message_str for item in events],
                "deadline": deadline,
            }
        )
        if self._gate_call_index is not None and index == self._gate_call_index:
            self.started.set()
            await self.release.wait()
        return self._outcomes[min(index, len(self._outcomes) - 1)]


def _outcome(downstream_action: str) -> SimpleNamespace:
    return SimpleNamespace(downstream_action=downstream_action, should_abort=downstream_action == "abort_required_vision")


class VisionBarrierInterleavingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.conversation.attention.gate", None)
        self.gate_mod = importlib.reload(
            importlib.import_module("astrmai.conversation.attention.gate")
        )
        self.config = SimpleNamespace(
            attention=SimpleNamespace(
                focus_thread_enabled=True,
                focus_thread_core_max_messages=4,
                focus_thread_related_max_messages=3,
                ambient_background_max_messages=2,
                thread_same_speaker_followup_sec=8,
                thread_reply_priority_enabled=True,
            ),
            system1=SimpleNamespace(wakeup_words=[], nicknames=["AstrMai"]),
            global_settings=SimpleNamespace(debug_mode=False),
            private_chat=SimpleNamespace(input_settle_sec=0.0),
        )
        self.gate = self.gate_mod.AttentionGate(
            state_engine=SimpleNamespace(config=self.config),
            judge=SimpleNamespace(),
            sensors=SimpleNamespace(),
            system2_callback=None,
        )
        self.gate.config = self.config
        self.gate._compute_debounce_delay = lambda *a, **k: 0.0
        self.dispatched: list[list[str]] = []

        async def _fake_sys2(event, events):
            self.dispatched.append([item.message_str for item in events])

        self.gate.sys2_process = _fake_sys2
        self.finalized: list[str] = []

        async def _fake_finalize(event, chat_id, *, status, reply_text=None):
            self.finalized.append(status)

        self.gate._finalize_pre_planner_turn = _fake_finalize

        async def _fake_append_segment(event):
            return None

        self.gate._append_dialogue_segment = _fake_append_segment

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _session(self, events):
        session = self.gate_mod.SessionContext()
        session.accumulation_pool = list(events)
        session.is_evaluating = True
        return session

    def test_new_message_during_barrier_is_remerged_without_duplicate_dispatch(self):
        first = _Event("看看这张图")
        late = _Event("补充一句")
        coordinator = _ScriptedCoordinator(
            [_outcome("continue"), _outcome("abort_required_vision")],
            gate_call_index=0,
        )
        self.gate.private_turn_coordinator = coordinator

        async def _run():
            session = self._session([first])
            worker = asyncio.create_task(
                self.gate._debounce_and_judge(
                    "default:FriendMessage:user-1", session, "bot-1", is_private=True
                )
            )
            # 精确等到屏障进入执行中，再注入晚到消息（无 sleep 竞态）
            await coordinator.started.wait()
            async with session.lock:
                session.accumulation_pool.append(late)
            coordinator.release.set()
            await asyncio.wait_for(worker, timeout=5.0)

        asyncio.run(_run())

        self.assertEqual(len(coordinator.calls), 2, "屏障期间入池的消息应触发第二轮 prepare_batch")
        self.assertEqual(coordinator.calls[0]["texts"], ["看看这张图"])
        self.assertEqual(
            coordinator.calls[1]["texts"],
            ["看看这张图", "补充一句"],
            "第二轮必须携带旧批次+晚到消息（re-merge），不能丢也不能只剩新消息",
        )
        # 第一轮未派发下游（被 re-merge 中断），第二轮 abort，故全程零重复派发
        self.assertEqual(self.dispatched, [])

    def test_burst_deadline_is_not_reset_across_remerge_iterations(self):
        # OPT-07/RT-05 锚定：屏障 deadline 必须跨迭代持久化，否则每轮重新起算
        # 总额，屏障期间持续来消息时视觉预算无上界
        first = _Event("图一")
        late = _Event("图二")
        coordinator = _ScriptedCoordinator(
            [_outcome("continue"), _outcome("abort_required_vision")],
            gate_call_index=0,
        )
        self.gate.private_turn_coordinator = coordinator

        async def _run():
            session = self._session([first])
            worker = asyncio.create_task(
                self.gate._debounce_and_judge(
                    "default:FriendMessage:user-1", session, "bot-1", is_private=True
                )
            )
            await coordinator.started.wait()
            async with session.lock:
                session.accumulation_pool.append(late)
            coordinator.release.set()
            await asyncio.wait_for(worker, timeout=5.0)
            return session

        session = asyncio.run(_run())

        deadlines = [call["deadline"] for call in coordinator.calls]
        self.assertEqual(len(deadlines), 2)
        self.assertIsNotNone(deadlines[0])
        # 主断言（时钟无关）：整个 burst 只允许计算一次预算
        self.assertEqual(
            coordinator.budget_calls,
            1,
            "burst deadline 必须跨迭代复用；重新起算会让屏障期间持续来消息时视觉预算无上界",
        )
        self.assertEqual(deadlines[0], deadlines[1], "第二轮不得重新起算 burst deadline")
        # 批次真正派发后清零，下一 burst 才重新计时
        self.assertEqual(float(getattr(session, "vision_burst_deadline", 0.0) or 0.0), 0.0)

    def test_abort_sends_single_notice_and_keeps_processing_late_arrivals(self):
        batch = [_Event("图一"), _Event("图二"), _Event("图三")]
        late = _Event("屏障失败后又说了一句")
        coordinator = _ScriptedCoordinator([_outcome("abort_required_vision")])
        self.gate.private_turn_coordinator = coordinator
        notices: list[str] = []
        injected = {"done": False}

        async def _send_failure(event):
            notices.append(event.message_str)
            # 在失败通知发出的瞬间注入新消息：命中 "abort 后池非空" 分支
            if not injected["done"]:
                injected["done"] = True
                session = injected["session"]
                async with session.lock:
                    session.accumulation_pool.append(late)
            return "这张图片暂时没有识别成功，请稍后再发一次。"

        self.gate._send_required_vision_failure = _send_failure

        async def _run():
            session = self._session(batch)
            injected["session"] = session
            await asyncio.wait_for(
                self.gate._debounce_and_judge(
                    "default:FriendMessage:user-1", session, "bot-1", is_private=True
                ),
                timeout=5.0,
            )
            return session

        session = asyncio.run(_run())

        self.assertEqual(len(coordinator.calls), 2, "abort 后池非空必须继续处理新消息")
        self.assertEqual(coordinator.calls[0]["texts"], ["图一", "图二", "图三"])
        self.assertEqual(coordinator.calls[1]["texts"], ["屏障失败后又说了一句"])
        # 每轮 abort 只发一次通知（3 条消息的批次不得发 3 次）
        self.assertEqual(len(notices), 2)
        self.assertEqual(notices[0], "图三", "通知应基于批次焦点事件（最后一条）发出")
        self.assertEqual(self.finalized, ["skipped_vision_required", "skipped_vision_required"])
        self.assertFalse(session.is_evaluating, "池排空后 worker 必须收工")
        self.assertEqual(self.dispatched, [])


class VisionResolveTimeoutTests(unittest.TestCase):
    """第三条分支：resolve 超时的 outcome 与 downstream 策略。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        self.mod = importlib.import_module(
            "astrmai.conversation.attention.private_turn_coordinator"
        )

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _coordinator(self, policy_text: str):
        config = SimpleNamespace(
            timing=SimpleNamespace(
                image_resolve_timeout_sec=0.05,
                image_analysis_timeout_sec=0.05,
                vision_barrier_total_timeout_sec=1.0,
            ),
            private_chat=SimpleNamespace(input_settle_sec=0.0),
            vision=SimpleNamespace(vision_reply_policy=policy_text, image_analysis_retries=1),
        )

        class _StuckResolver:
            def __init__(self):
                self.never = asyncio.Event()

            async def resolve_event_images(self, event):
                # 永不 set：wait_for 必定超时，结论确定（非 sleep 竞态）
                await self.never.wait()

        return self.mod.PrivateTurnCoordinator(
            config=config,
            image_resolver=_StuckResolver(),
            visual_cortex=None,
        )

    def _image_event(self):
        event = _Event("看图")
        event.set_extra("direct_vision_urls", ["http://example.com/a.png"])
        return event

    # 注意：每个 asyncio.run 必须用**全新** coordinator——_StuckResolver 内的
    # asyncio.Event 会绑定首次 await 它的事件循环，跨 run 复用会抛异常被
    # prepare_batch 吞成 unexpected_failure（timeout_count=0），测试就会在
    # 验证一条完全不同的代码路径。

    def test_resolve_timeout_aborts_under_require_analysis(self):
        policy = "必须识别成功后再回复"
        # 明细层：outcome 精确到 resolve_timeout（聚合层按设计归并为 failed，
        # 所以精确 outcome 只能在 _prepare_event 这一层断言）
        detail = asyncio.run(
            self._coordinator(policy)._prepare_event(
                self._image_event(), "default:FriendMessage:user-1"
            )
        )
        self.assertEqual(detail.outcome, "resolve_timeout")
        self.assertEqual(detail.timeout_count, 1)
        self.assertEqual(detail.downstream_action, "abort_required_vision")

        # 批次层：should_abort 与 timeout 计数必须透传，事件被打 required_failed
        batch_event = self._image_event()
        outcome = asyncio.run(
            self._coordinator(policy).prepare_batch(
                [batch_event], "default:FriendMessage:user-1"
            )
        )
        self.assertEqual(outcome.downstream_action, "abort_required_vision")
        self.assertTrue(outcome.should_abort)
        self.assertEqual(outcome.timeout_count, 1)
        self.assertEqual(outcome.outcome, "failed", "聚合层按设计归并明细 outcome")
        self.assertTrue(batch_event.get_extra("astrmai_vision_required_failed", False))

    def test_resolve_timeout_notifies_private_user_by_default(self):
        policy = "超时后忽略图片并继续回复"
        detail = asyncio.run(
            self._coordinator(policy)._prepare_event(
                self._image_event(), "default:FriendMessage:user-1"
            )
        )
        self.assertEqual(detail.outcome, "resolve_timeout")
        self.assertEqual(detail.timeout_count, 1)
        self.assertEqual(detail.downstream_action, "notify_failure")

        batch_event = self._image_event()
        outcome = asyncio.run(
            self._coordinator(policy).prepare_batch(
                [batch_event], "default:FriendMessage:user-1"
            )
        )
        self.assertEqual(outcome.downstream_action, "notify_failure")
        self.assertEqual(outcome.timeout_count, 1)
        self.assertTrue(outcome.should_abort)
        self.assertTrue(batch_event.get_extra("astrmai_vision_unavailable", False))
        self.assertEqual(
            batch_event.get_extra("astrmai_vision_failure_disposition"),
            "notify_failure",
        )
        self.assertNotIn("[图片]", batch_event.get_extra("astrmai_rich_text", ""))
        self.assertTrue(batch_event.get_extra("astrmai_vision_barrier_complete", False))


if __name__ == "__main__":
    unittest.main()
