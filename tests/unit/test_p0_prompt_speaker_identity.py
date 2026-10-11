"""
P0 身份事实统一 - 单元测试
测试 Prompt 构建中的身份正确性
"""
import pytest
from unittest.mock import Mock, MagicMock
from astrmai.conversation.contracts.turn_target import TurnTarget, TargetKind, ActorSet


class TestPromptSpeakerIdentity:
    """测试 Prompt 中实际发言人的正确性"""

    def create_mock_event(self, sender_id: str, sender_name: str):
        """创建模拟事件"""
        event = Mock()
        event.get_extra = Mock(return_value=False)
        event.get_sender_id = Mock(return_value=sender_id)
        event.get_sender_name = Mock(return_value=sender_name)
        event.unified_msg_origin = f"group:123"
        event.message_str = "测试消息"
        return event

    def create_mock_context(
        self,
        actual_sender_id: str,
        reply_target_id: str = "",
        at_ids: tuple = (),
    ):
        """创建模拟上下文"""
        context = Mock()

        # ActorSet：包含实际发言者
        actor_set = ActorSet(
            current_actor_id=actual_sender_id,
            at_actor_ids=at_ids,
            quoted_actor_ids=(reply_target_id,) if reply_target_id else (),
        )
        context.actor_set = actor_set

        # TurnTarget：可能指向引用目标
        if reply_target_id:
            turn_target = TurnTarget(
                target_kind=TargetKind.MESSAGE,
                target_actor_id=reply_target_id,
                target_actor_name="用户B",
                target_event_id="msg-b",
                evidence="reply",
                confidence=1.0,
            )
        else:
            turn_target = None
        context.turn_target = turn_target

        context.focus_sender_id = ""
        context.focus_sender_name = ""
        context.vision_bundle = None

        return context

    def test_actual_sender_from_actor_set(self):
        """测试实际发言人从 actor_set 获取"""
        from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin

        event = self.create_mock_event("111", "用户A")
        context = self.create_mock_context(actual_sender_id="111")

        result = PlannerPromptContextMixin._build_current_speaker_block(
            event, context, is_lightweight_event=False
        )

        # 验证
        assert "实际发言人：用户A(QQ:111)" in result
        assert "你的回复对象是实际发言人" in result

    def test_reply_shows_both_sender_and_target(self):
        """测试 A 引用 B 时，Prompt 显示实际发言人 A 和引用目标 B"""
        from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin

        event = self.create_mock_event("111", "用户A")
        context = self.create_mock_context(
            actual_sender_id="111",
            reply_target_id="222",
        )

        result = PlannerPromptContextMixin._build_current_speaker_block(
            event, context, is_lightweight_event=False
        )

        # 验证
        assert "实际发言人：用户A(QQ:111)" in result
        assert "该消息引用了：用户B(QQ:222)" in result
        assert "实际发言人：用户B" not in result  # 确保不混淆

    def test_reply_and_at_shows_all_identities(self):
        """测试 A 引用 B 同时 @ C 时，所有身份都正确显示"""
        from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin

        event = self.create_mock_event("111", "用户A")
        context = self.create_mock_context(
            actual_sender_id="111",
            reply_target_id="222",
            at_ids=("333",),
        )

        result = PlannerPromptContextMixin._build_current_speaker_block(
            event, context, is_lightweight_event=False
        )

        # 验证三个身份都出现
        assert "实际发言人：用户A(QQ:111)" in result
        assert "该消息引用了：用户B(QQ:222)" in result
        assert "同时@了：QQ:333" in result

    def test_no_confusion_when_reply_target_is_sender(self):
        """测试引用目标与实际发言者相同时不重复显示"""
        from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin

        event = self.create_mock_event("111", "用户A")
        context = self.create_mock_context(
            actual_sender_id="111",
            reply_target_id="111",  # 引用自己的消息
        )

        result = PlannerPromptContextMixin._build_current_speaker_block(
            event, context, is_lightweight_event=False
        )

        # 验证：不显示引用（因为引用的是自己）
        assert "实际发言人：用户A(QQ:111)" in result
        assert "该消息引用了" not in result

    def test_proactive_event_returns_empty(self):
        """测试主动候选返回空字符串"""
        from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin

        event = self.create_mock_event("proactive", "主动候选")
        event.get_extra = Mock(side_effect=lambda key, default=None:
            True if key == "astrmai_is_proactive_event" else default)

        context = self.create_mock_context(actual_sender_id="proactive")

        result = PlannerPromptContextMixin._build_current_speaker_block(
            event, context, is_lightweight_event=False
        )

        # 验证：主动候选不显示发言人
        assert result == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
