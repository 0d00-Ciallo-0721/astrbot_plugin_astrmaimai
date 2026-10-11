"""
P1 历史连续性修复 - 单元测试
测试分层 TTL 和历史保留逻辑
"""
import pytest
import time
from astrmai.conversation.planning.conversation_continuity import (
    ConversationContinuityStore,
    ConversationContinuityState,
    ConversationTurnRecord,
)


class TestConversationContinuityTTL:
    """测试分层 TTL 清理策略"""

    def create_manager(self, **kwargs):
        """创建测试用的 ConversationContinuityManager"""
        manager = ConversationContinuityStore()
        # 设置分层 TTL（使用较小的值便于测试）
        manager._group_topic_active_ttl_seconds = kwargs.get('topic_active_ttl', 60)  # 1分钟
        manager._group_topic_confirm_after_seconds = kwargs.get('topic_confirm_ttl', 120)  # 2分钟
        manager._group_history_readable_ttl_seconds = kwargs.get('history_readable_ttl', 300)  # 5分钟
        manager._group_history_persist_ttl_seconds = kwargs.get('history_persist_ttl', 600)  # 10分钟
        return manager

    def create_state_with_history(self, chat_id: str, turns_count: int, age_seconds: float):
        """创建带有历史的状态"""
        chat_id = f"offline:GroupMessage:{chat_id}"
        state = ConversationContinuityState(chat_id=chat_id)
        state.current_topic = "测试话题"
        state.current_goal = "测试目标"
        state.last_goal_update_ts = time.time() - age_seconds

        # 创建多条历史记录
        for i in range(turns_count):
            state.turns.append(
                ConversationTurnRecord(
                    timestamp=state.last_goal_update_ts - (turns_count - i - 1) * 10,
                    chat_id=chat_id,
                    focus_preview=f"用户消息{i}",
                    goal_summary="",
                    social_intent="",
                    action_tier="",
                    action_taken="",
                    reply_preview=f"机器人回复{i}",
                    reply_need="",
                )
            )
        return state

    def test_topic_active_no_cleanup(self):
        """测试话题活跃期内不清理"""
        manager = self.create_manager(topic_active_ttl=60)
        state = self.create_state_with_history("test", turns_count=5, age_seconds=30)  # 30秒前

        now = time.time()
        expired = manager._expire_state_if_stale(state, now)

        # 验证
        assert not expired
        assert state.current_topic == "测试话题"
        assert state.current_goal == "测试目标"
        assert len(state.turns) == 5  # 历史完整保留

    def test_topic_expired_but_history_remains(self):
        """测试话题过期但历史保留"""
        manager = self.create_manager(topic_active_ttl=60, topic_confirm_ttl=120)
        state = self.create_state_with_history("test", turns_count=5, age_seconds=90)  # 90秒前

        now = time.time()
        expired = manager._expire_state_if_stale(state, now)

        # 验证
        assert not expired  # 未彻底清空
        assert state.current_topic == "测试话题"  # 弱窗口仍需可评估的话题证据
        assert state.current_goal == "测试目标"
        assert len(state.turns) == 5  # ⚠️ 历史保留

    def test_history_fades_after_readable_ttl(self):
        """测试历史可读期过后淡化"""
        manager = self.create_manager(
            topic_confirm_ttl=120,
            history_readable_ttl=300,
        )
        state = self.create_state_with_history("test", turns_count=10, age_seconds=330)  # 5.5分钟前

        now = time.time()
        expired = manager._expire_state_if_stale(state, now)

        # 验证
        assert not expired
        assert state.current_topic == ""
        assert state.current_goal == ""
        assert len(state.turns) == 2  # 可读期后仅保留有界背景

    def test_history_deep_fade_after_persist_ttl(self):
        """测试历史持久期过后深度淡化"""
        manager = self.create_manager(
            history_readable_ttl=300,
            history_persist_ttl=600,
        )
        state = self.create_state_with_history("test", turns_count=10, age_seconds=550)  # 9分钟前

        now = time.time()
        expired = manager._expire_state_if_stale(state, now)

        # 验证
        assert not expired
        assert len(state.turns) == 2  # 只保留最后2条

    def test_history_cleared_after_persist_ttl(self):
        """测试超过持久期后彻底清空"""
        manager = self.create_manager(history_persist_ttl=600)
        state = self.create_state_with_history("test", turns_count=10, age_seconds=650)  # 超过10分钟

        now = time.time()
        expired = manager._expire_state_if_stale(state, now)

        # 验证
        assert expired  # 彻底清空
        assert state.current_topic == ""
        assert state.current_goal == ""
        assert len(state.turns) == 0  # 历史清空

    def test_no_cleanup_during_confirmation(self):
        """测试话题确认请求中不清理"""
        manager = self.create_manager()
        state = self.create_state_with_history("test", turns_count=5, age_seconds=1000)
        state.topic_confirmation_requested_at = time.time()  # 正在请求确认

        now = time.time()
        expired = manager._expire_state_if_stale(state, now)

        # 验证：即使时间很长，也不清理
        assert not expired
        assert state.current_topic == "测试话题"
        assert len(state.turns) == 5

    def test_real_world_scenario_31_minutes(self):
        """真实场景：静默31分钟后历史保留"""
        manager = self.create_manager(
            topic_active_ttl=1200,  # 20分钟
            topic_confirm_ttl=1800,  # 30分钟
            history_readable_ttl=7200,  # 2小时
            history_persist_ttl=86400,
        )
        state = self.create_state_with_history(
            "test",
            turns_count=4,  # 2轮问答
            age_seconds=1860,  # 31分钟
        )

        now = time.time()
        expired = manager._expire_state_if_stale(state, now)

        # 验证：符合 P1 目标
        assert not expired
        assert state.current_topic == ""  # 话题不承接
        assert len(state.turns) == 4  # 历史仍保留


class TestHistoryRestore:
    @pytest.mark.asyncio
    async def test_restore_by_count_not_time(self, tmp_path):
        from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore

        source = GroupDialogueStore(snapshot_dir=tmp_path, restore_recent_message_limit=5)
        await source.append_segment("group", event_id="old", speaker_id="alice", content="old conversation", timestamp=1)
        assert await source.persist_snapshot()
        restored = GroupDialogueStore(snapshot_dir=tmp_path, restore_recent_message_limit=5)
        assert await restored.restore_snapshot() == 1
        assert restored._threads["group"].segments[0].timestamp == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
