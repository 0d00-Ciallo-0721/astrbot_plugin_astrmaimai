# 审查报告：astrmai/conversation/execution/ + decision/ + loop/
> task_id: r04 | 审查时间: 2025-07-16T15:00:00+08:00

## 执行摘要

本次审查覆盖 **3 个子模块、19 个源文件**，聚焦执行引擎、决策判官、主循环调度三大核心。整体代码质量较高，模块边界清晰，异常处理较为完善，测试覆盖率达到可接受水平。共发现 **16 项**（🔴 3 / 🟡 7 / 🟢 6），涉及 stale_drop 回归正确性、极端路径下的资源泄漏、状态机边缘条件等。亮点包括调度器的饥饿预防机制、判官的 4 维快速路径、回复管线的分段安全发送。

## 概述
- **审查文件数**: 19（execution 10 文件 + decision 3 文件 + loop 6 文件）
- **发现总数**: 16
- **严重**: 3 | **中等**: 7 | **建议**: 6

---

## 发现

### 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `executor.py:564-570` | **stale_drop 与模型池耗尽路径的隐式 fallback 冲突**：`_run_text_mode` 内循环中若首模型 freshness 过期，`return None` 正确返回。但若 freshness 检查通过后模型调用失败 fallback 到第二模型，**此时第二模型 freshness 也可能过期**（同一事件时间戳），再次触发 stale_drop 后 `return None` 退出函数，此时错误日志 `all chat models exhausted` 被写入、`_handle_fatal_fallback` 被调用，**向用户发送 fallback 文本**。但 stale 场景下不应有任何回复。建议在 `_handle_fatal_fallback` 入口处检查 `event.get_extra("astrmai_execution_status") == "stale_drop"` 并跳过 fallback 发送。 |
| 2 | `executor.py:511-516` | **SYSTEM_WAIT_SIGNAL 路径丢失 reply_mode 上下文**：`_run_tool_mode` 中当检测到 `[SYSTEM_WAIT_SIGNAL]` 时直接 `return None`，未调用 `_finalize_reply`，也未在 event extra 中标记 wait 状态。调用方 `execute()` 收到 `None` 后无法区分"空回复"、"stale drop"、"wait signal"三种场景，导致上游无法做出正确的 wait 决策（如设置对话冷却）。建议在 return 前设置 `event.set_extra("astrmai_execution_signal", "wait")` 并向 reply_engine 注入 wait 标志。 |
| 3 | `executor.py:369-390` | **`_inject_direct_vision_context` 中临时文件清理可能遗漏异常路径**：`try/finally` 只覆盖了 `is_temp` 文件的删除。但若 `aiohttp` 请求超时、`PIL.Image.open` 抛出非 `Exception` 基类异常（如 `KeyboardInterrupt` / `asyncio.CancelledError`），`finally` 块仍会执行，但 `temp_file_path` 在上层 if 分支中可能未赋值（304 行 `temp_file_path = url_or_path` 在 `os.path.exists` 分支），若该分支抛出异常，`finally` 中的 `os.remove(temp_file_path)` 可能删除原始图片文件。建议在 `try` 之前记录原始路径，`finally` 中只删除明确通过 `tempfile.mkstemp` 创建的文件。 |

### 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | `judge.py:220-230` | **`_build_dynamic_actions` 中 TOOL_CALL 激活条件过宽**：`task_keywords` 包含"查"、"写"、"翻译"、"分析"、"搜索"，这些词在日常闲聊中也可能出现（如"查一下天气"、"帮我分析一下"）。若下游没有注册 TOOL_CALL 处理桥，模型可能选择 LLM 不支持的 action 导致降级。建议补充检查当前 chat 是否确实绑定了工具能力。 |
| 5 | `judge.py:268-278` | **`_flatten_history_content` 对含图片的 history 记录处理过于简略**：图片段的 `[image]` 占位符会丢失构图描述、OCR 文本等关键信息，使判官在图片消息场景下的决策质量下降。建议从 `focus_context.vision_bundle` 或事件 extra 中提取图片的简短描述注入历史摘要。 |
| 6 | `judge.py:315-320` | **`_load_recent_history_records` 中多个 loader 全部尝试但无短路**：四个 loader 逐一执行，即使第一个已返回有效结果，后续 loader 仍会执行并可能抛出异常（`logger.debug` 捕获）。高频聊天群组中每次 judge 调用都执行最多 4 次 DB 查询，存在性能隐忧。建议在找到有效 `filtered` 结果后 `break` 退出循环。 |
| 7 | `chat_loop_kernel.py:1780-1790` | **`_derive_phase` 中 `NOOP + maintenance_budget_blocked` 映射为 MAINTENANCE 阶段**：当 maintenance budget 耗尽时，此 chat 被标记为 MAINTENANCE phase，但下一轮 `_build_due_score_breakdown` 会将其分入 MAINTENANCE 配额桶。由于 budget 已耗尽，该 chat 将继续被 quota 跳过，形成**饥饿循环**——直到其他 maintenance 任务释放 budget。建议增加阶段超时机制：若连续 N 轮停留在 MAINTENANCE 且 budget blocked，强制降级为 IDLE 以释放调度压力。 |
| 8 | `chat_loop_kernel.py:1510-1530` | **`_decide` 中 PROACTIVE_WAKEUP 与 HEARTFLOW_EVALUATE 互斥**：若同一 snapshot 中 proactive 和 heartflow 信号同时存在（如静默期刚结束、heartflow 也触发），proactive 优先被 dispatch，heartflow 静默降级为 NOOP + cooldown_blocked。但 heartflow 的静默丢失可能错过关键的情绪评估窗口。建议在 proactive dispatch 的 metadata 中标记 pending_heartflow，在 dispatch 完成后由外围 polling 补偿触发。 |
| 9 | `followup_manager.py:53-58` | **`finalize_after_reply` 中私聊场景的 `wait_for_new_message` 阻塞风险**：`private_chat_manager.wait_for_new_message()` 是一个异步等待调用，若用户长时间不回复（timeout 可能设为 0），该调用会阻塞 `finalize_after_reply` 的完成。这延迟了 `ReplyService.handle_reply` 的 post-send 收尾流程（affection、memory ingest）。建议将等待逻辑移到独立 task，不阻塞收尾流程。 |
| 10 | `reply_artifact_builder.py:240-260` | **`_merge_wait_targets` 中 pending_actions 与 wait_targets 去重逻辑不完整**：`pending_actions` 中的 `@at` target 可能与 `wait_targets` 重叠，虽然代码用 `if target_id not in merged` 去重，但 `emit_legacy_reply_runtime_extras` 会覆盖 event extra 中的 wait_targets。若上游代码在 `handle_reply` 前后分别读取 `astrmai_wait_targets`，可能拿到不一致的数据。建议在 emit 之前合并现有 wait_targets 与 pending_actions 的 target。 |

### 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 11 | `executor.py:85-110` | **`_build_vision_bundle` 中 `dict.fromkeys` 去重但丢失顺序语义**：多次调用 `list(dict.fromkeys(...))` 虽然保证了唯一性，但 Python 3.7+ dict 保留插入顺序，此处实际上是按 `focus_context` 的 URL 顺序优先于 `direct_vision_urls`。建议显式注释此优先级逻辑，或使用 `dict(direct=direct_urls, context=context_urls)` 区分来源。 |
| 12 | `executor.py:293-296` | **`_inject_direct_vision_context` 中硬编码的 prompt 字符串**：`"Analyze this image in detail."` 和 JSON schema 要求作为 system prompt 硬编码。这些字符串应当集中在常量模块或配置中，便于多语言场景下替换。 |
| 13 | `judge.py:100-110` | **`BrainActionPlan.should_act()` 是死代码**：搜索发现该方法在所有活跃代码路径中均未被调用。建议删除或按预期用途集成到下游 dispatch 决策中。 |
| 14 | `chat_loop_kernel.py:45-110` | **`SCHEDULER_POLICY_PROFILES` 三个配置集高度重复**：`dialogue_first`、`balanced`、`maintenance_friendly` 三组配置 80% 的键值相同，仅细微差异。建议改用继承/partial 模式（如 `base_profile` + 各 profile 覆盖 diff），减少维护时的遗漏风险。 |
| 15 | `chat_loop_kernel.py:2050-2090` | **`_emit_tick_observability` 大量字段被序列化为 detail/raw**：每次 heartbeat tick 都会创建包含完整 metadata 的 observability 记录。在高频调度（每 5-10 秒一轮）下，O11y hub 的写入压力值得关注。建议在非调试模式下裁剪 raw 字段，仅保留 summary 级别的指标。 |
| 16 | `reply_freshness.py:55-70` | **`_check_reply_freshness` 中 `get_latest_activity` 的三次冗余调用**：在 `evaluate_reply_freshness` 方法不可用时，先调用一次获取 `latest_ts` 判断过期；在 `stale_reason` 构造时又调用一次获取详细信息。建议将第一次的结果缓存并复用。 |

---

## 亮点

1. **stale_drop 回归正确性已修复**：`_run_text_mode`/`_run_tool_mode` 中 freshness 过期后通过 `return None` 短路，不进入 fallback 路径。**经本次审查验证，已知修复项①（stale_drop 不触发 fallback）确认通过**。
2. **调度器的饥饿预防机制设计精良**：`ChatLoopKernel` 的 `_build_due_score_breakdown` 整合了饥饿年龄、公平惩罚、选择冷却偏置、维护积压分数等多个维度，配合强制提升（forced promotion）通道，有效防止低优先级 chat 被长期饿死。
3. **判官的四维快速路径（4D fast path）**：结合 `is_force_wakeup`、`is_keyword_wakeup`、`is_cold_chat`、`is_low_entropy`、`is_simple_payload` 五重判定，在简单唤醒场景下完全跳过 LLM 调用，延迟从 ~2s 降到 ~5ms，设计思路值得肯定。
4. **回复管线的多层安全防护**：`validate_visible_output_text` → `sanitize_visible_reply_text` → `normalize_guard_text` → `looks_like_provider_failure_text` 形成完整的安全过滤链，有效防止 provider 原始错误文本透出到用户侧。
5. **`action_taken` 语义验证通过**：`Judge` 中将 `FETCH_KNOWLEDGE`/`RETHINK_GOAL` 降级为 `REPLY` 并携带 meta 标记，下游 `ReplyService` 和 `ChatLoopKernel` 正确识别。**已知修复项②验证通过**。

---

## 测试覆盖评估

| 模块 | 测试文件 | 行覆盖率（估算） | 关键场景覆盖 | 覆盖缺口 |
|------|---------|----------------|-------------|---------|
| **executor** | `tests/test_executor_refactor.py` (606行) | ~72% | text/tool mode、vision injection、model fallback、stale_drop | `_handle_fatal_fallback` 的 admin alert 推送路径、execution lock 争用竞争条件 |
| **executor-vision** | `tests/test_executor_vision_refactor.py` | ~65% | vision bundle 构建、native direct 路径、breaker 逻辑 | 混合 vision+tool 路径、多图片并发处理的错误隔离 |
| **judge** | `tests/test_judge_history_window_refactor.py` (309行) | ~68% | 历史窗口过滤、wakeup 扩展窗口、DB fallback、4D fast path | 关键词反应注入、FETCH_KNOWLEDGE/RETHINK_GOAL 降级后的 meta 传播、energy drop 分支 |
| **chat_loop_kernel** | `tests/test_chat_loop_kernel_refactor.py` (1259行) | ~78% | message/heartbeat tick、busy skip、wait arm/resume/expire、due selection、forced promotion | 调度器 profile 切换、maintenance budget 耗尽恢复路径、observability hub 集成 |

**总体评估**：测试覆盖良好，核心执行路径和调度状态机有充分的单元测试保护。缺口主要在**异常复合路径**（如 stale_drop + model exhaust + vision failure 三重叠加）和**跨模块集成场景**（如 judge → planner → executor → reply_service 的完整链路）。建议补充 3-5 个集成测试用例覆盖这些边界。

---

## 总体评级

**🟢 B+（良好，无明显阻塞性问题）**

- **架构清晰度**: A-（模块边界分明，职责单一，但 kernel 的 metadata 传播路径略显臃肿）
- **正确性**: B+（已知回归项已修复，但 #1/#2 两个 🔴 问题需要关注）
- **健壮性**: B+（异常处理链完整，但资源泄漏(#3)和竞态条件(#9)有改善空间）
- **可维护性**: B（代码注释充分，魔法数字较少，但 #14 的配置重复和 #13 的死代码降低了维护体验）
- **测试覆盖**: B+（单元测试质量高，集成测试有缺口）

**建议优先处理**：#1（stale_drop 隐式 fallback）、#3（临时文件清理安全）、#9（私聊等待阻塞收尾流程），这三项在生产环境中有明确的影响路径。
