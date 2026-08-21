"""OPT-10 回归测试：配置真源与容错（PL-03 / PL-04 / PL-05 / PL-06 / PL-11）。

守护不变式：
1. PL-06: 坏配置降级加载而非整插件拒载——违例字段剔除+默认兜底，剔除后仍失败
   整体回退默认。
2. PL-03: UI 的 timing.turn_merge_enabled 真实生效（pydantic 保值 + 消费方优先读）。
3. PL-04/PL-05: 9 个死配置键从 schema 与 pydantic 双侧移除，UI 不再展示无效承诺。
4. PL-11: agent.max_steps 声明下限与执行层硬底线（5）对齐。
5. 结构性守卫: schema 每个叶子键都能被 pydantic 接受并映射到真实字段/合法别名
   （防止再出现"挂错分节被静默丢弃"的死开关）。
"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

import config as config_mod
from astrmai.conversation.attention.private_turn_coordinator import PrivateTurnCoordinator

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "_conf_schema.json"


class DegradedConfigLoadTests(unittest.TestCase):
    """PL-06：越界配置不再拒载。"""

    def test_invalid_values_pruned_with_defaults(self):
        cfg = config_mod.load_astrmai_config(
            {"infra": {"api_timeout": -5}, "reply": {"meme_probability": "abc"}}
        )
        self.assertEqual(cfg.infra.api_timeout, 45.0)
        self.assertEqual(cfg.reply.meme_probability, 60)

    def test_valid_values_untouched(self):
        cfg = config_mod.load_astrmai_config({"reply": {"meme_probability": 10}})
        self.assertEqual(cfg.reply.meme_probability, 10)

    def test_totally_broken_config_falls_back_to_defaults(self):
        cfg = config_mod.load_astrmai_config({"reply": "not-a-dict"})
        self.assertIsInstance(cfg, config_mod.AstrMaiConfig)

    def test_strict_constructor_still_raises(self):
        # 直接构造仍保持严格语义，供测试与校验路径使用
        with self.assertRaises(ValidationError):
            config_mod.AstrMaiConfig(**{"infra": {"api_timeout": -5}})


class TurnMergeSwitchTests(unittest.TestCase):
    """PL-03：私聊合并开关真实生效。"""

    def test_timing_switch_survives_model_parse(self):
        cfg = config_mod.load_astrmai_config({"timing": {"turn_merge_enabled": False}})
        self.assertIs(cfg.timing.turn_merge_enabled, False)

    def test_coordinator_prefers_timing_switch(self):
        coordinator = PrivateTurnCoordinator.__new__(PrivateTurnCoordinator)
        coordinator.config = SimpleNamespace(
            timing=SimpleNamespace(turn_merge_enabled=False),
            private_chat=SimpleNamespace(turn_merge_enabled=True),
        )
        self.assertFalse(coordinator.turn_merge_enabled())

    def test_coordinator_falls_back_to_private_chat_when_unset(self):
        coordinator = PrivateTurnCoordinator.__new__(PrivateTurnCoordinator)
        coordinator.config = SimpleNamespace(
            timing=SimpleNamespace(turn_merge_enabled=None),
            private_chat=SimpleNamespace(turn_merge_enabled=True),
        )
        self.assertTrue(coordinator.turn_merge_enabled())


class DeadKeyRemovalTests(unittest.TestCase):
    """PL-04/PL-05：死键双侧移除。"""

    _DEAD_KEYS = [
        ("reply", "enable_content_safety_filter"),
        ("attention", "debounce_window"),
        ("attention", "throttle_probability"),
        ("attention", "throttle_min_entropy"),
        ("attention", "repeater_threshold"),
        ("attention", "max_message_length"),
        ("evolution", "enable_relationship_engine"),
        ("mood", "unknown_decay"),
    ]

    def test_dead_keys_absent_from_schema(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        for section, key in self._DEAD_KEYS:
            items = schema.get(section, {}).get("items", {})
            self.assertNotIn(key, items, f"{section}.{key} 应已从 schema 移除")

    def test_dead_keys_absent_from_models(self):
        cfg = config_mod.AstrMaiConfig()
        for section, key in self._DEAD_KEYS:
            model = getattr(cfg, section, None)
            if model is None:
                continue
            self.assertNotIn(key, type(model).model_fields, f"{section}.{key} 应已从 pydantic 移除")


class MaxStepsFloorTests(unittest.TestCase):
    """PL-11：声明下限与执行层底线一致。"""

    def test_below_floor_rejected_then_degraded_to_default(self):
        with self.assertRaises(ValidationError):
            config_mod.AstrMaiConfig(**{"agent": {"max_steps": 2}})
        cfg = config_mod.load_astrmai_config({"agent": {"max_steps": 2}})
        self.assertEqual(cfg.agent.max_steps, 5)


class SchemaPydanticContractTests(unittest.TestCase):
    """结构性守卫：schema 叶子键必达 pydantic。"""

    # timing 分节通过 LEGACY 别名把值下发到目标分节；这些键在 TimingConfig 上
    # 可能不存在同名字段，属合法路由
    _TIMING_ALIASES = {alias for alias, _, _ in config_mod.LEGACY_TIMING_NAMESPACE_FIELDS}

    def test_every_schema_leaf_maps_to_model_field(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        root_fields = config_mod.AstrMaiConfig.model_fields
        missing: list[str] = []
        for section, section_schema in schema.items():
            if not isinstance(section_schema, dict) or section_schema.get("type") != "object":
                continue
            if section not in root_fields:
                missing.append(section)
                continue
            model_cls = root_fields[section].annotation
            model_field_names = getattr(model_cls, "model_fields", {})
            for key in (section_schema.get("items") or {}):
                if key in model_field_names:
                    continue
                if section == "timing" and key in self._TIMING_ALIASES:
                    continue
                missing.append(f"{section}.{key}")
        self.assertEqual(missing, [], f"schema 键未映射到 pydantic 字段（会被静默丢弃）: {missing}")


if __name__ == "__main__":
    unittest.main()
