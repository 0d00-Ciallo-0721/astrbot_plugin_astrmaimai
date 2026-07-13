# Round 02：入口生命周期、外部结果与错误终止

数量：9。依赖：Round 01。

完成标准：入口去重不丢合法消息；外部插件结果保留来源与 scope；错误/ghost 只影响一次事件；后台入口任务可被清理。

## R02-01 / P2：ingress dedupe 忽略 AstrBot message identity
- 原始 ID：`FFA-02-009`；验证级别：B。
- 主文件：`astrmai/conversation/attention/gate.py` 及 ingress dedupe helper。
- 修复边界：优先使用 message ID/adapter identity；内容哈希只能作为无 ID 的短窗降级。
- 回归目标：同文不同 message ID 均处理；同一平台重投的同 message ID 只处理一次。

## R02-02 / P2：外部插件结果先被 self-message 过滤
- 原始 ID：`FFA-02-010`；验证级别：B。
- 主文件：`astrmai/conversation/ingress/external_result_bridge.py`, `astrmai/conversation/attention/gate.py`。
- 修复边界：可信 external result 必须在 generic self filter 前进入专用分支，并保留 loop guard。
- 回归目标：白名单插件结果进入上下文；AstrMai 自己的结果不形成循环。

## R02-03 / P2：debounce worker 不随 clear chat 或插件 shutdown 取消
- 原始 ID：`FFA-02-011`；验证级别：B。
- 主文件：`astrmai/conversation/attention/gate.py`, `astrmai/app/lifecycle.py`。
- 修复边界：session worker 纳入 owner lifecycle；clear/shutdown 要 cancel、await 并清除引用。
- 回归目标：清理后旧 worker 不再调用 Judge/System2，新 runtime 无旧任务副作用。

## R02-04 / P1：热开启 Sys3 后 feature flag 为真但 router/cron guard 不存在
- 原始 ID：`FFA-ENTRY-002`；验证级别：B。
- 主文件：`astrmai/app/plugin_facade.py`, `astrmai/app/bootstrap.py`, `astrmai/app/runtime_context.py`。
- 修复边界：需要重启的开关不能提前改变 live feature flag；或原子构建完整 Sys3 stack。命令入口必须检查真实 runtime availability。
- 回归目标：false->true 热应用后 `/work` 返回 restart-required 或可用结果，绝不 AttributeError。

## R02-05 / P1：基础热配置成功但 reply/compaction/persona 运行对象仍使用旧值
- 原始 ID：`FFA-ENTRY-003`（范围收紧后保留）；验证级别：B。
- 主文件：`astrmai/app/plugin_facade.py`, `astrmai/state/energy/frequency_controller.py`, `astrmai/conversation/attention/context_compaction.py`, `astrmai/memory/persona/persona_summarizer.py`。
- 修复边界：只处理未被后续专项覆盖的 live object；为 derived field 提供 refresh 或标为 reload-required。
- 回归目标：成功热更、重复热更幂等、后续组件失败时回滚三条路径都保持同一配置版本。

## R02-06 / P1：错误 fallback 已发送但事件未 stop，框架默认 LLM 可再次回复
- 原始 ID：`FFA-ENTRY-004`, `FF-09`；验证级别：A。
- 主文件：`astrmai/presentation/events/message_entry.py`。
- 修复边界：terminal fallback 必须 suppress default LLM 并 stop event；不能影响非 terminal 的降级流程。
- 回归目标：attention 抛错时群聊/私聊均只产生一条 fallback，框架后续处理器不再运行。

## R02-07 / P2：external synthetic event 丢失 group/private scope
- 原始 ID：`FF-06`；验证级别：B。
- 主文件：`astrmai/presentation/events/result_sniffer.py`, `astrmai/conversation/ingress/external_result_bridge.py`。
- 修复边界：桥接事件必须携带原 UMO、group ID、sender/self ID 或走显式 external-event 类型，不能被误判为私聊空用户。
- 回归目标：群插件结果进入原群 attention；私聊结果绑定原用户；不创建空 sender session。

## R02-08 / P2：result sniffing 早于 ghost/error interception，隐藏文本已污染状态
- 原始 ID：`FF-07`；验证级别：B。
- 主文件：`main.py`, `astrmai/presentation/events/result_sniffer.py`, `astrmai/conversation/execution/outbound_error_policy.py`。
- 修复边界：先分类 terminal ghost/error，再允许外部结果注入和 bot history commit。
- 回归目标：被拦截文本不进入 attention、dialogue、learning 或 memory。

## R02-09 / P2：`error_interception_mode` 三个值行为不符合配置契约
- 原始 ID：`FF-08`；验证级别：B。
- 主文件：`astrmai/conversation/execution/outbound_error_policy.py`, `config.py`, `_conf_schema.json`。
- 修复边界：`log_only` 不清结果，`block_only` 只清结果，`block_and_stop` 清结果并 stop。
- 回归目标：三种模式分别断言 result 和 event propagation，不共享错误分支。
