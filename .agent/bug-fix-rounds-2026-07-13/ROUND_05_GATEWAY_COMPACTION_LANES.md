# Round 05：Gateway、Compaction 与 Lane 并发

数量：9。依赖：Round 04 Gateway success boundary。

完成标准：视觉与压缩调用正确降级；Context Economy 指标真实；同 lane 并发不丢历史；generation 不发生 ABA。

## R05-01 / P2：视觉池绕过健康排序，业务无效结果先记成功
- 原始 ID：`AM-GW-07-06`；验证级别：B。
- 主文件：`astrmai/infrastructure/gateway/gateway_tasks.py`, `gateway_call.py`, `gateway_lane.py`。
- 修复边界：先按 router health 排序，视觉字段校验通过后再 report success 和写 lane；无效结果反馈 failure。
- 回归目标：首模型空 description 时次模型接管，首模型健康分下降且无无效 lane artifact。

## R05-02 / P2：compaction provider 调用无超时，永久占用 pending slot
- 原始 ID：`AM-GW-07-07`；验证级别：B。
- 主文件：`astrmai/conversation/attention/compaction_providers.py`, `context_compaction.py`。
- 修复边界：有限超时、下一 provider、本地 fallback 和 pending task cleanup 必须在 finally 中闭环。
- 回归目标：provider 挂起后按时 fallback，后续同 chat 可再次调度 compaction。

## R05-03 / P2：compaction 把 provider 错误正文持久化为 cold summary
- 原始 ID：`AM-GW-07-08`；验证级别：B。
- 主文件：`astrmai/conversation/attention/compaction_providers.py`, `astrmai/infrastructure/gateway/output_guard.py`。
- 修复边界：复用严格 provider result contract；错误正文不进入 summary/economy success。
- 回归目标：限流/权限信封走下一 provider 或本地摘要，cold summary 不含错误文本。

## R05-04 / P2：Provider capability 由可自定义 ID 子串推断
- 原始 ID：`AM-GW-07-09`；验证级别：B（条件型）。
- 主文件：`astrmai/infrastructure/gateway/provider_capabilities.py`, `gateway_lane.py`。
- 修复边界：优先实际 provider 类型/显式能力；仅在无元数据时保守 fallback，不能因显示 ID 注入不支持参数。
- 回归目标：误含 `claude` 的非 Anthropic provider 不收专用参数；真实 provider 可显式声明能力。

## R05-05 / P3：主模型池内轮询模型被统计为 fallback
- 原始 ID：`AM-GW-07-10`；验证级别：B。
- 主文件：`astrmai/infrastructure/context_economy/center.py`, `astrmai/infrastructure/gateway/gateway_lane.py`。
- 修复边界：trace 保留 primary pool membership，fallback 仅表示进入独立 fallback pool。
- 回归目标：主池第二模型命中仍计 primary；真正 fallback 才增加 fallback_count。

## R05-06 / P3：cache usage 支持判断与 token 字段读取集合不一致
- 原始 ID：`AM-GW-07-11`；验证级别：B。
- 主文件：`astrmai/infrastructure/gateway/gateway_result.py`。
- 修复边界：统一 evidence 与 extraction，支持 `cache_read_input_tokens` 和嵌套 `prompt_tokens_details`。
- 回归目标：支持字段存在时 cached token/hit 正确；未知 shape 不声称 supported。

## R05-07 / P3：tool-loop 成功不写 Context Economy benchmark sample
- 原始 ID：`AM-GW-07-12`；验证级别：B。
- 主文件：`astrmai/infrastructure/gateway/gateway_lane.py`, `gateway_call.py`, `astrmai/infrastructure/runtime/context_economy_benchmark_store.py`。
- 修复边界：tool-loop 复用统一 success accounting，避免重复写 usage/trace。
- 回归目标：CHAT_TOOLS/Sys3 成功各写一条 benchmark sample，普通 chat 不重复。

## R05-08 / P1：同 lane 并发 append 以整段历史覆盖造成 lost update
- 原始 ID：Assignment 08 lane history Finding；验证级别：B。
- 主文件：`astrmai/infrastructure/runtime/lane_storage.py`, `lane_manager.py`。
- 修复边界：read-modify-write 全段按 lane 串行化，或提交时读取最新历史合并；锁不能只保护 meta。
- 回归目标：A/B 同时基于 H 完成后最终含 H+A+B，重启后仍保留。

## R05-09 / P2：有界 thread-generation 驱逐导致 generation ABA
- 原始 ID：Assignment 08 generation Finding；验证级别：B。
- 主文件：`astrmai/infrastructure/runtime/chat_runtime_coordinator.py`, `astrmai/conversation/contracts/turn_identity.py`。
- 修复边界：generation/token 全局或 per-chat 单调不可复用；驱逐不得让 in-flight thread 回到旧值。
- 回归目标：超过 128 thread 后旧 A 与新 A identity 不同，旧回复 freshness 失败且不争同 send key。
