"""
P2 唤醒策略细化 - 单元测试
测试昵称分级唤醒逻辑
"""
import pytest
from unittest.mock import Mock, MagicMock
from types import SimpleNamespace


class TestNicknameWakeupScore:
    """测试昵称唤醒分数计算"""

    def create_mock_config(self, **kwargs):
        """创建模拟配置"""
        config = Mock()
        config.system1 = Mock()
        config.system1.nicknames = kwargs.get('nicknames', ["小舞", "麦麦"])
        config.system1.nickname_wakeup_threshold = kwargs.get('threshold', 0.7)
        config.system1.nickname_at_others_with_question_score = kwargs.get('at_others_question', 0.7)
        config.system1.nickname_prefix_score = kwargs.get('prefix', 0.8)
        config.system1.nickname_with_question_score = kwargs.get('question', 0.6)
        config.system1.nickname_mention_only_score = kwargs.get('mention', 0.3)
        config.global_settings = Mock()
        config.global_settings.command_prefixes = ["/", "!", "！"]
        return config

    def create_mock_event(self, message_str: str, at_components=None):
        """创建模拟事件"""
        event = Mock()
        event.message_str = message_str
        event.get_extra = Mock(return_value=None)
        event.set_extra = Mock()

        # 构造 message_obj
        message_obj = Mock()
        message = []

        if at_components:
            for at_data in at_components:
                comp = SimpleNamespace(type="At", qq=at_data.get("qq", ""), target=at_data.get("target", ""))
                message.append(comp)

        message_obj.message = message
        event.message_obj = message_obj

        return event

    def create_sensors(self, config=None):
        """创建 PrefilterSensors 实例"""
        from astrmai.conversation.ingress.sensors import PreFilters

        if config is None:
            config = self.create_mock_config()

        sensors = PreFilters(config)
        sensors.foreign_commands = set()
        return sensors

    def test_at_bot_with_nickname_returns_1_0(self):
        """测试 @bot + 昵称 → 分数 1.0"""
        config = self.create_mock_config()
        sensors = self.create_sensors(config)

        event = self.create_mock_event(
            "@机器人 小舞你在吗",
            at_components=[{"qq": "bot_id"}],
        )

        score = sensors._calculate_nickname_wakeup_score(
            event,
            raw_msg="@机器人 小舞你在吗",
            nicknames=["小舞", "麦麦"],
            bot_self_id="bot_id",
        )

        assert score == 1.0

    def test_at_others_with_nickname_no_question_returns_0(self):
        """测试 @别人 + 昵称（无疑问） → 分数 0.0"""
        config = self.create_mock_config()
        sensors = self.create_sensors(config)

        event = self.create_mock_event(
            "你和@小明一起去麦麦家玩",
            at_components=[{"qq": "111"}],
        )

        score = sensors._calculate_nickname_wakeup_score(
            event,
            raw_msg="你和@小明一起去麦麦家玩",
            nicknames=["小舞", "麦麦"],
            bot_self_id="bot_id",
        )

        assert score == 0.0

    def test_at_others_with_nickname_and_question_returns_0_7(self):
        """测试 @别人 + 昵称 + 疑问 → 分数 0.7"""
        config = self.create_mock_config(at_others_question=0.7)
        sensors = self.create_sensors(config)

        event = self.create_mock_event(
            "麦麦，你看@小明说得对吗",
            at_components=[{"qq": "111"}],
        )

        score = sensors._calculate_nickname_wakeup_score(
            event,
            raw_msg="麦麦，你看@小明说得对吗",
            nicknames=["小舞", "麦麦"],
            bot_self_id="bot_id",
        )

        assert score == 0.7

    def test_nickname_prefix_returns_0_8(self):
        """测试昵称开头 → 分数 0.8"""
        config = self.create_mock_config(prefix=0.8)
        sensors = self.create_sensors(config)

        event = self.create_mock_event("小舞，你在吗")

        score = sensors._calculate_nickname_wakeup_score(
            event,
            raw_msg="小舞，你在吗",
            nicknames=["小舞"],
            bot_self_id="bot_id",
        )

        assert score == 0.8

    def test_nickname_with_question_returns_0_6(self):
        """测试昵称 + 疑问 → 分数 0.6"""
        config = self.create_mock_config(question=0.6)
        sensors = self.create_sensors(config)

        event = self.create_mock_event("今天小舞在不在呢")

        score = sensors._calculate_nickname_wakeup_score(
            event,
            raw_msg="今天小舞在不在呢",
            nicknames=["小舞"],
            bot_self_id="bot_id",
        )

        assert score == 0.6

    def test_nickname_mention_only_returns_0_3(self):
        """测试纯提及 → 分数 0.3"""
        config = self.create_mock_config(mention=0.3)
        sensors = self.create_sensors(config)

        event = self.create_mock_event("我今天遇到小舞了")

        score = sensors._calculate_nickname_wakeup_score(
            event,
            raw_msg="我今天遇到小舞了",
            nicknames=["小舞"],
            bot_self_id="bot_id",
        )

        assert score == 0.3

    def test_no_nickname_returns_0(self):
        """测试无昵称 → 分数 0.0"""
        config = self.create_mock_config()
        sensors = self.create_sensors(config)

        event = self.create_mock_event("今天天气真好")

        score = sensors._calculate_nickname_wakeup_score(
            event,
            raw_msg="今天天气真好",
            nicknames=["小舞"],
            bot_self_id="bot_id",
        )

        assert score == 0.0

    def test_nickname_with_request_keyword(self):
        """测试昵称 + 请求关键词 → 分数 0.6"""
        config = self.create_mock_config(question=0.6)
        sensors = self.create_sensors(config)

        event = self.create_mock_event("请小舞帮我查一下")

        score = sensors._calculate_nickname_wakeup_score(
            event,
            raw_msg="请小舞帮我查一下",
            nicknames=["小舞"],
            bot_self_id="bot_id",
        )

        assert score == 0.6


class TestWakeupThreshold:
    """测试唤醒阈值逻辑"""

    def test_score_above_threshold_triggers_wakeup(self):
        """测试分数超过阈值触发唤醒"""
        from astrmai.conversation.attention.gate import AttentionGate

        # 模拟配置
        config = Mock()
        config.system1 = Mock()
        config.system1.nickname_wakeup_threshold = 0.7

        gate = AttentionGate(state_engine=Mock(), judge=None, config=config, sensors=None, system2_callback=None)

        # 模拟事件
        event = Mock()
        event.get_extra = Mock(side_effect=lambda key, default=0.0:
            0.8 if key == "astrmai_bonus_score" else default)

        result = gate._is_direct_wakeup_event(event, "bot_id")

        # 验证：0.8 >= 0.7，应该触发
        assert result is True

    def test_score_below_threshold_no_wakeup(self):
        """测试分数低于阈值不触发唤醒"""
        from astrmai.conversation.attention.gate import AttentionGate

        config = Mock()
        config.system1 = Mock()
        config.system1.nickname_wakeup_threshold = 0.7

        gate = AttentionGate(state_engine=Mock(), judge=None, config=config, sensors=None, system2_callback=None)

        event = Mock()
        event.get_extra = Mock(side_effect=lambda key, default=0.0:
            0.5 if key == "astrmai_bonus_score" else default)

        result = gate._is_direct_wakeup_event(event, "bot_id")

        # 验证：0.5 < 0.7，不应该触发
        assert result is False


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
