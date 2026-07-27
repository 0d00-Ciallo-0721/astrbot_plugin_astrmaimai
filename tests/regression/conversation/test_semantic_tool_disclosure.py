"""G5 / TL-01 后半回归测试：语义意图直接并包只读查询工具。

OPT-12 已加 guidance 提示（告诉模型可调 bot_capability_lookup 自检），但二段披露
16h 零触发说明不能只靠模型自检。本目标让"我叫什么名字"这类身份问句在关键词未命中时
也能拿到 identity 查询工具——与 OPT-06 的记忆检索门复用同一个 QueryIntentClassifier，
两处对身份类问句的判定保持一致。
"""

import unittest

from astrmai.conversation.planning.tool_disclosure import ToolDisclosurePlanner


class SemanticIntentDisclosureTests(unittest.TestCase):
    def _plan(self, message: str):
        return ToolDisclosurePlanner().plan(
            message=message,
            requested_tier="chat",
            explicit_tool_intent=False,
            explicit_tool_families=(),
        )

    def test_identity_question_without_keyword_gets_identity_package(self):
        # "我叫什么名字" 不含 IDENTITY_KEYWORDS 里的任何词（我是谁/身份/昵称…）
        plan = self._plan("我叫什么名字来着")

        self.assertIn("identity", plan.packages)
        self.assertIn("identity:identity_semantic_intent", plan.package_reasons)
        self.assertIn("qq_user_identity_lookup", plan.tool_names)

    def test_location_question_maps_to_identity_package(self):
        plan = self._plan("我在哪个城市来着")

        self.assertIn("identity", plan.packages)
        self.assertIn("identity:identity_semantic_intent", plan.package_reasons)

    def test_memory_recall_question_does_not_pull_contact_tools(self):
        # 决策记录：recent_reference（记忆回想）刻意不映射到 relationship——
        # 那是记忆注入链路（OPT-06）的职责，并包 qq_friend_lookup /
        # contact_route_suggest_tool 等联系人路由工具属语义错配
        plan = self._plan("你还记得我之前说的吗")

        self.assertNotIn("relationship", plan.packages)
        self.assertNotIn("qq_friend_lookup", plan.tool_names)
        self.assertNotIn("contact_route_suggest_tool", plan.tool_names)

    def test_keyword_hit_keeps_original_reason_and_no_duplicate(self):
        # 关键词命中时保持原 reason，不得因语义兜底重复并包
        plan = self._plan("他是谁啊")

        self.assertIn("identity", plan.packages)
        self.assertIn("identity:identity_signal", plan.package_reasons)
        self.assertNotIn("identity:identity_semantic_intent", plan.package_reasons)
        self.assertEqual(tuple(plan.packages).count("identity"), 1)

    def test_smalltalk_stays_core_only(self):
        plan = self._plan("哈哈哈哈")

        self.assertEqual(tuple(plan.packages), ("core",))
        self.assertNotIn("identity", plan.packages)
        self.assertNotIn("relationship", plan.packages)

    def test_empty_message_is_safe(self):
        plan = self._plan("")

        self.assertEqual(tuple(plan.packages), ("core",))


if __name__ == "__main__":
    unittest.main()
