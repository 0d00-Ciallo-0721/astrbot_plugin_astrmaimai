# Round 03：决策、规划与实际发送状态

数量：9。依赖：Round 01-02。

完成标准：Planner 使用真实执行 trace 和发送 outcome；所有 proactive 终止路径 exactly-once 结算；phantom reply 不进入状态。

## R03-01 / P1：Agency TTL/cooldown 混用 epoch 与 monotonic
- 原始 ID：`FFA-03-001`；验证级别：B。
- 主文件：`astrmai/conversation/planning/agency_runtime.py`。
- 修复边界：同一生命周期数据统一时钟域；持久化值与进程单调时钟不能直接相减。
- 回归目标：10 分钟 cooldown 和 30 分钟 reflection TTL 按边界过期。

## R03-02 / P1：可用工具被记录为已执行动作
- 原始 ID：`FFA-03-002`；验证级别：B。
- 主文件：`astrmai/conversation/planning/planner.py`, `astrmai/conversation/execution/executor.py`。
- 修复边界：Agency cooldown/feedback 只能消费实际 tool execution trace，不得消费候选 ToolSet。
- 回归目标：普通文本回复不产生 meme/like/poke cooldown；真实调用才产生。

## R03-03 / P1：Planner-owned runtime components 不接收热配置
- 原始 ID：`FFA-03-003`；验证级别：B。
- 主文件：`astrmai/app/plugin_facade.py`, `astrmai/conversation/planning/planner.py`, `context_engine.py`, `prompt_refiner.py`, `astrmai/conversation/execution/executor.py`。
- 修复边界：建立 Planner 统一 refresh，原子更新 ContextEngine、PromptRefiner、Executor、CognitiveLoop、ActionModifier 等派生字段。
- 回归目标：成功、幂等、回滚后每个 child 的配置版本一致，运行态队列/会话不重置。

## R03-04 / P1：Executor 返回 `None` 时 proactive completion 丢失
- 原始 ID：`FFA-03-004`, `AM-LP-10-13`；验证级别：A。
- 主文件：`astrmai/conversation/execution/executor.py`, `astrmai/conversation/planning/planner.py`, `astrmai/proactive/dispatcher.py`。
- 修复边界：引入或复用 typed execution outcome，区分 fallback 已发送、wait、stale、fatal/no-send；每条路径 exactly-once 调 completion。
- 回归目标：已发送 fallback 用 `reply_sent=True`；wait/stale/failure 用 false；callback 被移除且 energy/cooldown 语义正确。

## R03-05 / P2：Judge 宣告已移除 action，随后把合法输出改成 IGNORE
- 原始 ID：`FFA-03-005`；验证级别：B。
- 主文件：`astrmai/conversation/decision/judge.py`。
- 修复边界：prompt enum、parser 与 runtime valid actions 使用单一来源；不支持 action 不得出现在提示中。
- 回归目标：每个声明 action 都有可达处理；未知 action 才进入明确 fallback。

## R03-06 / P2：WaitTool 结果只写 event extra，Planner 当 stale drop
- 原始 ID：`FFA-03-006`；验证级别：B。
- 主文件：`astrmai/conversation/planning/tools/pfc_tools.py`, `astrmai/conversation/execution/executor.py`, `astrmai/conversation/planning/planner.py`。
- 修复边界：wait 必须作为 typed outcome 返回并执行 no-send settlement、trace 和 proactive completion。
- 回归目标：工具 wait 不发文本、记录 `skipped_wait`，不会遗留 callback。

## R03-07 / P2：合法空 goal list 无法清除旧 goals
- 原始 ID：`FFA-03-007`；验证级别：B。
- 主文件：`astrmai/conversation/planning/goal_service.py`。
- 修复边界：区分 parse failure 与成功解析 `[]`；空列表执行清理/衰减规则。
- 回归目标：`[]` 清除旧 goal，非法 JSON 保留旧状态并记录失败。

## R03-08 / P1：发送失败后的 claim 永久阻止 fallback model 重试
- 原始 ID：`FF-02`；验证级别：B。
- 主文件：`astrmai/infrastructure/runtime/chat_runtime_coordinator.py`, `astrmai/conversation/execution/reply_artifact_builder.py`。
- 修复边界：failed claim 可由同 turn 安全重试；committed/in-flight claim 才拒绝 duplicate。
- 回归目标：首模型发送异常、次模型成功时用户收到一次；已成功发送仍拒绝重复。

## R03-09 / P1：stale 回复未发送却写入 dialogue/learning/proactive sent 状态
- 原始 ID：`FF-03`；验证级别：B。
- 主文件：`astrmai/conversation/execution/reply_service.py`, `executor.py`, `astrmai/conversation/planning/planner.py`。
- 修复边界：ReplyService 返回 sent/blocked/partial artifact；上层只对真实可见内容提交历史和 sent 状态。
- 回归目标：freshness 过期时平台、dialogue、learning、memory 均无 phantom assistant turn。
