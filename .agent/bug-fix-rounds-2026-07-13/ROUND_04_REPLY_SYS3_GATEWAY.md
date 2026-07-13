# Round 04：回复、Sys3 与 Gateway 边界

数量：9。依赖：Round 03 的 execution outcome。

完成标准：deliberate follow-up 与 partial send 有独立状态；Sys3 工具遵守 AstrBot FunctionTool 契约；tool-loop 基础超时、并发和重试统一。

## R04-01 / P2：Planner follow-up 与首条回复复用 `final` send key
- 原始 ID：`FF-04`；验证级别：B。
- 主文件：`astrmai/conversation/planning/planner.py`, `astrmai/conversation/execution/reply_artifact_builder.py`。
- 修复边界：follow-up 使用独立 response kind/attempt identity，仍保持自身 exactly-once。
- 回归目标：首条和 follow-up 各发送一次；重复执行同 follow-up 被拒绝。

## R04-02 / P2：分段中途 stale 后把未发送尾段持久化为完整回复
- 原始 ID：`FF-05`；验证级别：B。
- 主文件：`astrmai/conversation/execution/reply_artifact_builder.py`, `reply_service.py`。
- 修复边界：artifact 记录实际 delivered segments 和 partial outcome；只持久化可见文本。
- 回归目标：第二段前 stale 时，历史只含第一段，send claim/trace 标为 partial。

## R04-03 / P1：普通聊天 Sys3 light tool 用 AstrMessageEvent 调用 ContextWrapper contract
- 原始 ID：Assignment 11 Finding 1；验证级别：B。
- 主文件：`astrmai/workmode/router.py`, `astrmai/workmode/subagents/base_agent.py`, `astrmai/conversation/planning/planner_side_inputs.py`。
- 修复边界：不要把 `raw_agent.call` 填成 decorator handler；保留 FunctionTool call contract 和原参数 schema。
- 回归目标：普通 TOOL_CALL 能把 query 交给 static/dynamic subagent；直接 `/work` 行为不回归。

## R04-04 / P2：静态 SubAgent 忽略配置 agent pool，改用普通会话 provider
- 原始 ID：Assignment 11 Finding 2；验证级别：B。
- 主文件：`astrmai/workmode/subagents/base_agent.py`, `astrmai/app/plugin_facade.py`, `astrmai/infrastructure/gateway/model_gateway.py`。
- 修复边界：从 AstrMai runtime/gateway 获得已验证的 agent model，避免从 host Context 重新取普通 provider。
- 回归目标：外层和内层 agent 均使用配置池；普通 provider 缺失时 `/work` 仍可执行。

## R04-05 / P1：正常技术回答含通用错误词就被 output guard 拒绝
- 原始 ID：`AM-GW-07-01`；验证级别：B。
- 主文件：`astrmai/infrastructure/gateway/output_guard.py`, `gateway_call.py`。
- 修复边界：provider failure 需要结构/信封证据，不能仅靠正文子串；visible sanitizer 与 gateway 使用同一严格判定。
- 回归目标：讨论 quota/status code/response 的正常回答通过，真实错误信封仍拒绝。

## R04-06 / P1：15 秒 API timeout 覆盖 120 秒 tool timeout
- 原始 ID：`AM-GW-07-02`；验证级别：B。
- 主文件：`astrmai/infrastructure/gateway/gateway_lane.py`, `astrmai/app/plugin_facade.py`, `config.py`。
- 修复边界：Agent 总预算不得短于单工具预算，并明确 max_steps 与总超时关系。
- 回归目标：20 秒工具在 120 秒预算内成功；超过总预算后只取消一次且不重复副作用。

## R04-07 / P1：模型成功后的 lane/trace 失败被反向判为模型失败
- 原始 ID：`AM-GW-07-03`；验证级别：B。
- 主文件：`astrmai/infrastructure/gateway/gateway_lane.py`, `astrmai/infrastructure/runtime/lane_manager.py`。
- 修复边界：usage/history/trace/benchmark 属于可降级 success artifacts；失败不得撤销 LLM/工具成功或切换模型重做。
- 回归目标：finalizer 抛错时仍返回原成功结果，只记录 degradation；工具副作用不重复。

## R04-08 / P2：tool-loop 绕过全局 LLM semaphore
- 原始 ID：`AM-GW-07-04`；验证级别：B。
- 主文件：`astrmai/infrastructure/gateway/model_gateway.py`, `gateway_lane.py`。
- 修复边界：普通生成与 tool-loop 共享全局并发预算，避免同协程嵌套重复获取造成死锁。
- 回归目标：并发 tool-loop 峰值不超过配置，普通聊天仍可获得公平执行。

## R04-09 / P2：tool-loop 不使用模型重试/退避，TimeoutError 被记为 unknown
- 原始 ID：`AM-GW-07-05`；验证级别：B。
- 主文件：`astrmai/infrastructure/gateway/gateway_lane.py`, `gateway_call.py`, `model_gateway.py`。
- 修复边界：复用统一 failure classifier 和每模型 retry policy；有副作用工具只在可安全重试时重试。
- 回归目标：空消息 TimeoutError 分类为 timeout；单模型瞬时网络错误按配置退避重试。
