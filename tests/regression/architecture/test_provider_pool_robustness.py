"""OPT-09 回归测试：Provider 与模型池健壮性（TG-02 / RT-07 / RT-08 / TL-04）。

守护不变式：
1. TG-02: not-found 类错误（生产实证"没有找到 ID 为 openai/deepseek-v4-pro 的提供商"）
   判定为 fatal——单次尝试即切下一模型，不得 backoff 空转重试。
2. RT-08: 能力解析对完整模型 ID 按 '/' 前缀降级匹配（对象查找 → 全量扫描 → 字符串
   前缀家族），不再 1005/1005 全 unknown。
3. RT-07: compaction 配置的 provider 做一次性存在校验，无效剔除且只告警一次。
4. TL-04: 副作用足迹只统计真实对外动作（pending_actions + cross_session_sends），
   纯查询不计入。
"""

import asyncio
import unittest
from types import SimpleNamespace

from astrmai.conversation.attention.compaction_providers import CompactionProviderMixin
from astrmai.conversation.execution.executor import ConcurrentExecutor
from astrmai.infrastructure.gateway.gateway_policy import GatewayPolicyMixin
from astrmai.infrastructure.gateway.provider_capabilities import resolve_provider_capabilities


class _Event:
    def __init__(self):
        self._extras = {}

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


class FatalFailureMatrixTests(unittest.TestCase):
    """TG-02：失败矩阵——not-found 必须 fatal。"""

    def _policy(self):
        return GatewayPolicyMixin.__new__(GatewayPolicyMixin)

    def test_not_found_variants_are_fatal(self):
        policy = self._policy()
        self.assertTrue(policy._is_fatal_failure("没有找到 ID 为 openai/deepseek-v4-pro 的提供商"))
        self.assertTrue(policy._is_fatal_failure("ProviderNotFoundError: openai/deepseek-v4-pro"))
        self.assertTrue(policy._is_fatal_failure("provider not found: xx"))

    def test_retryable_errors_stay_non_fatal(self):
        policy = self._policy()
        self.assertFalse(policy._is_fatal_failure("502 Bad Gateway"))
        self.assertFalse(policy._is_fatal_failure("connection reset by peer"))
        self.assertFalse(policy._is_fatal_failure("", error=asyncio.TimeoutError()))

    def test_existing_fatal_classes_unchanged(self):
        policy = self._policy()
        self.assertTrue(policy._is_fatal_failure("429 Too Many Requests"))
        self.assertTrue(policy._is_fatal_failure("quota exceeded for billing cycle"))


class ProviderCapabilityResolutionTests(unittest.TestCase):
    """RT-08：完整模型 ID 的能力解析。"""

    def test_string_fallback_uses_slash_prefix_family(self):
        caps = resolve_provider_capabilities(None, "gemini/gemini-3-flash-preview")
        self.assertEqual(caps.provider_family, "gemini")

    def test_all_providers_scan_matches_registered_prefix(self):
        provider = SimpleNamespace(
            meta=lambda: SimpleNamespace(id="code2", type="deepseek"),
        )
        context = SimpleNamespace(
            get_provider_by_id=lambda pid: None,
            get_all_providers=lambda: [provider],
        )

        caps = resolve_provider_capabilities(context, "code2/deepseek-v4-flash")

        self.assertEqual(caps.provider_family, "native_chat")

    def test_direct_provider_lookup_wins(self):
        provider = SimpleNamespace(meta=lambda: SimpleNamespace(type="anthropic"))
        context = SimpleNamespace(
            get_provider_by_id=lambda pid: provider if pid == "claude/opus" else None,
        )

        caps = resolve_provider_capabilities(context, "claude/opus")

        self.assertEqual(caps.provider_family, "anthropic")
        self.assertTrue(caps.supports_cache_control)


class CompactionProviderValidationTests(unittest.TestCase):
    """RT-07：无效 compaction provider 剔除且只查一次。"""

    def _mixin(self, context):
        mixin = CompactionProviderMixin.__new__(CompactionProviderMixin)
        mixin.provider_id = "openai/deepseek-v4-pro"
        mixin.gateway = SimpleNamespace(context=context)
        return mixin

    def test_missing_provider_removed_and_validated_once(self):
        lookups = []

        async def _current(chat_id):
            return "current/provider"

        context = SimpleNamespace(
            get_provider_by_id=lambda pid: lookups.append(pid) or None,
            get_current_chat_provider_id=_current,
        )
        mixin = self._mixin(context)

        first = asyncio.run(mixin._resolve_provider_candidates("chat-1"))
        second = asyncio.run(mixin._resolve_provider_candidates("chat-1"))

        self.assertNotIn("openai/deepseek-v4-pro", first)
        self.assertIn("current/provider", first)
        self.assertEqual(first, second)
        # 缓存生效：存在性校验只查一次
        self.assertEqual(lookups.count("openai/deepseek-v4-pro"), 1)

    def test_valid_provider_stays_first(self):
        async def _current(chat_id):
            return "current/provider"

        context = SimpleNamespace(
            get_provider_by_id=lambda pid: SimpleNamespace(id=pid),
            get_current_chat_provider_id=_current,
        )
        mixin = self._mixin(context)

        candidates = asyncio.run(mixin._resolve_provider_candidates("chat-1"))

        self.assertEqual(candidates[0], "openai/deepseek-v4-pro")


class SideEffectFootprintTests(unittest.TestCase):
    """TL-04：副作用足迹口径。"""

    def test_counts_pending_actions_and_cross_session_sends(self):
        event = _Event()
        self.assertEqual(ConcurrentExecutor._side_effect_footprint(event), 0)

        event.set_extra("astrmai_pending_actions", [{"action": "poke"}])
        event.set_extra("astrmai_cross_session_sends", ["dedup-key-1", "dedup-key-2"])

        self.assertEqual(ConcurrentExecutor._side_effect_footprint(event), 3)

    def test_query_only_traces_do_not_count(self):
        event = _Event()
        event.set_extra("astrmai_tool_execution_trace", [{"tool": "qq_friend_lookup"}])

        self.assertEqual(ConcurrentExecutor._side_effect_footprint(event), 0)


if __name__ == "__main__":
    unittest.main()
