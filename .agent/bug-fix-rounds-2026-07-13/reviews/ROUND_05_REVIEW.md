# ROUND 05 REVIEW — Gateway、Compaction 与 Lane 并发

**审查日期**: 2026-07-14
**审查方式**: 静态源码分析 (仅读，零修改)
**审查范围**: R05-01 ~ R05-09，共 9 项

---

## R05-01 / P2：视觉池绕过健康排序，业务无效结果先记成功

**审查文件**: strmai/infrastructure/gateway/gateway_tasks.py L38-192

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| 按 router health 排序 vision 模型 | ✅ | L56-61:
outer.get_ranked_models("vision", vision_models, ...) |
| 视觉字段校验通过后才 report success | ✅ | L92-94: is_valid 后 return parsed，上层 chat_in_lane_result → _elastic_call_result 才调
outer.report_success |
| 无效结果反馈 failure | ✅ | L98: _open_model_cooldown("vision", model_id, failure_reason) |
| 首模型空 description 时次模型接管 | ✅ | L73-111: for 循环中 validation 失败 continue，异常体 catch 后 continue |
| 首模型健康分下降且无无效 lane artifact | ✅ | L98 冷却 + _elastic_call_result 中失败路径不写 lane artifact |

**注释**: _normalize_vision_failure_reason (L11-36) 正确处理了 mpty_description、provider_failure_text、invalid_emotion_tags 等场景。

---

## R05-02 / P2：compaction provider 调用无超时

**审查文件**: strmai/conversation/attention/compaction_providers.py L144-312, strmai/conversation/attention/context_compaction.py L285-327

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| 有限超时 wrapping | ✅ | L197-204 & L281-288: syncio.wait_for(... timeout=self._compaction_provider_timeout_seconds()) |
| 下一 provider fallback | ✅ | L184: for loop 遍历 provider_candidates；L206-218/290-301: except 后 continue 到下一 provider |
| 本地 fallback | ✅ | context_compaction.py L1384-1386: provider 返回空 → fallback _build_summary_v2 |
| pending task cleanup 在 finally 中 | ✅ | context_compaction.py L309-326: _pending_tasks 的 finally 块清理 |

**注释**: _compaction_provider_timeout_seconds() (L43-55) 上限 60s，从 gateway 的 _api_timeout 或 settings.api_timeout 获取。

---

## R05-03 / P2：compaction 把 provider 错误正文持久化为 cold summary

**审查文件**: strmai/conversation/attention/compaction_providers.py L220-222, L305-307; strmai/infrastructure/gateway/output_guard.py L157-208

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| 复用严格 provider result contract | ✅ | L220-222 & L305-307: looks_like_provider_failure_text(rendered) 检查 |
| 错误正文不进入 summary/economy success | ✅ | 检测失败 → continue，不调
ecord_trace |
| 限流/权限信封走下一 provider 或本地 | ✅ | continue → 下一 provider；全部失败后段 _build_summary_v2 |

**注释**: looks_like_provider_failure_text (output_guard.py L157-208) 覆盖了 403/429、空响应、safety_ratings、usage_metadata、JSON 错误信封等场景，与 gateway 主路径共用同一检测逻辑。

---

## R05-04 / P2：Provider capability 由可自定义 ID 子串推断

**审查文件**: strmai/infrastructure/gateway/provider_capabilities.py L72-121

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| 优先实际 provider 类型/显式能力 | ✅ | L78-101: infer_provider_capabilities 先查 provider.meta() → provider_type/provider_family/	ype |
| 仅在无元数据时保守 fallback | ✅ | L73-74: 若 provider_or_type 是 None 或 str，才直接用 _capabilities_for_provider_type |
| 显式 bool 能力覆盖推断值 | ✅ | L96-100: meta 中的 supports_cache_control 等 bool 字段覆盖 inferred 值 |

**注释**: 即使 provider 显示 ID 含 "claude"，只要其 meta.provider_family 不是 "anthropic"，就不会被误判为 Anthropic。_capabilities_for_provider_type 仅在无 meta 时作为兜底。

---

## R05-05 / P3：主模型池内轮询模型被统计为 fallback

**审查文件**: strmai/infrastructure/gateway/gateway_policy.py L91-111, gateway_call.py L180-211, L304-335; strmai/infrastructure/gateway/model_router.py L73-157; strmai/infrastructure/context_economy/center.py L364

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| trace 保留 primary pool membership | ✅ | gateway_policy.py L100-106: primary_models = router.get_ranked_models(pool_name, ...) |
| fallback 仅指进入独立 fallback pool | ✅ | L107-110: ttempt_queue = primary + fallback pool models |
| 主池第二模型命中仍计 primary | ✅ | gateway_call.py L211:
eport_pool = pool_name if model_id in primary_models else "fallback" |
| 真正 fallback 才增加 fallback_count | ✅ | center.py L364: metrics.fallback_count += int(bool(trace.fallback_used)), 其中 allback_used=bool(model_id not in primary_models) |

**注释**: get_agent_models (gateway_tasks.py L435-443) 也正确分离了 agent primary 和 fallback 模型列表。

---

## R05-06 / P3：cache usage 支持判断与 token 字段读取集合不一致

**审查文件**: strmai/infrastructure/gateway/gateway_result.py L30-91

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| input_cached 字段包括 cache_read_input_tokens | ✅ | L59-63: "input_cached", "cached_tokens", "cache_read_input_tokens" |
| 嵌套 prompt_tokens_details 检查 | ✅ | L64-77: 先读 prompt_tokens_details 再用相同字段名提取 |
| cached_usage_supported 与 extraction 字段一致 | ✅ | L79-84: 同样检查 "input_cached", "cached_tokens", "cache_read_input_tokens" + nested |
| 未知 shape 不声称 supported | ✅ | L90: cached_usage_supported = bool(cached_usage_supported or input_cached > 0)，有实际数据才算 supported |

**注释**: _has_usage_field 和 _read_usage_field 的字段集合完全对齐，消除了检测/提取的不一致窗口。

---

## R05-07 / P3：tool-loop 成功不写 Context Economy benchmark sample

**审查文件**: strmai/infrastructure/gateway/gateway_lane.py L592-785; gateway_call.py L35-159; strmai/infrastructure/runtime/context_economy_benchmark_store.py

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| CHAT_TOOLS/Sys3 成功各写一条 benchmark | ✅ | L660-668 (wait_signal/terminal_yield) & L752-760 (normal tool reply) 均调用 _safe_record_tool_benchmark |
| 复用统一 success accounting | ✅ | _safe_record_tool_benchmark → _record_benchmark_sample |
| 普通 chat 不重复写 | ✅ | chat_in_lane_result 走 _record_success_artifacts 单入口；tool-loop 有自己的成功路径，不穿过 _elastic_call_result |

**注释**: 两条工具成功路径 (WAIT_SIGNAL/TERMINAL_YIELD vs 普通 tool reply) 都记录 benchmark，且在 _build_success_result 后将
esult.economy 传给 benchmark。

---

## R05-08 / P1：同 lane 并发 append 以整段历史覆盖造成 lost update

**审查文件**: strmai/infrastructure/runtime/lane_storage.py L185-236; lane_manager.py L92-104

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| read-modify-write 全段按 lane 串行化 | ✅ | L209-210: lane_lock = await self._get_lane_lock(lane_umo) + sync with lane_lock |
| 提交时读取最新历史合并 | ✅ | L211-217: 在锁内重新 get_curr_conversation_id → get_conversation → _load_history，获取最新状态 |
| 锁不仅保护 meta | ✅ | L238-276: ppend_visible_reply_artifact 也走 ppend_exchange，受同一锁保护 |
| A/B 同时基于 H 完成后最终含 H+A+B | ✅ | 锁内重读 + append + save 保证了线性化 |
| 重启后仍保留 | ✅ | 所有写操作走 conversation_manager.update_conversation → 持久化 |

**注释**: _get_lane_lock (lane_manager.py L92-104) 在 _lock 下创建 per-lane Lock，容量上限 100 并惰性清理非活跃锁。

---

## R05-09 / P2：有界 thread-generation 驱逐导致 generation ABA

**审查文件**: strmai/infrastructure/runtime/chat_runtime_coordinator.py L44-131; strmai/conversation/contracts/turn_identity.py

**验证结论**: ✅ 已实现

| 检查项 | 状态 | 代码锚点 |
|--------|------|----------|
| generation 全局单调不可复用 | ✅ | L52: _generation_sequence = 0 全局计数器；L122: 每次 +1；L123: 新值来自全局序列 |
| 驱逐不让 in-flight thread 回到旧值 | ✅ | L117-121: 超 128 时 pop 旧 entry，但 L123 的
ext_generation 来自全局序列，非复用旧值 |
| 超过 128 thread 后旧 A 与新 A identity 不同 | ✅ | L124: state.turn_generations[normalized_thread_id] = next_generation 覆盖为新全局值 |
| 旧回复 freshness 失败且不争同 send key | ✅ | L146-148:
egister_turn_task 检查 generation 匹配 → 拒绝 stale turn；L125: 旧 task 被 cancel |

**注释**: uild_turn_send_key (turn_identity.py L26-28) 包含 	urn.generation，ABA 下旧 turn 的 send_key 与新 turn 不同，不会冲突。

---

## 总结

| 修复 ID | 优先级 | 状态 | 置信度 |
|---------|--------|------|--------|
| R05-01 | P2 | ✅ 已实现 | HIGH |
| R05-02 | P2 | ✅ 已实现 | HIGH |
| R05-03 | P2 | ✅ 已实现 | HIGH |
| R05-04 | P2 | ✅ 已实现 | HIGH |
| R05-05 | P3 | ✅ 已实现 | HIGH |
| R05-06 | P3 | ✅ 已实现 | HIGH |
| R05-07 | P3 | ✅ 已实现 | HIGH |
| R05-08 | P1 | ✅ 已实现 | HIGH |
| R05-09 | P2 | ✅ 已实现 | HIGH |

**通过率**: 9/9 (100%)

**未检查**: 无动态测试验证，仅静态源码审查。

**建议**: 若有集成测试条件，优先验证 R05-08 (并发 lane history) 和 R05-09 (generation ABA)，这两项修复正确性依赖运行时锁行为，静态分析可见锁粒度和单调性，但竞态窗口仅能通过集成测试确认。
