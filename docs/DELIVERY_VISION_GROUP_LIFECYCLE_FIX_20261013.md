# 发送错误、视觉配置故障及退群生命周期：开发审计与交接

日期：2026-10-13。状态：`ready_for_independent_revalidation`。

本报告针对用户提供的两轮服务器日志签名及当前工作树。没有连接服务器、修改生产目录/配置/真实数据库、切换学习开关、发送 QQ 消息、构建候选、提交或推送。测试数据库为 pytest 临时文件。当前工作树还包含此前后台重启恢复专题；见 `BACKGROUND_RESTART_RECOVERY_FIX_20261012.md`，不能把整个 diff 都归为本报告。

## 1. 已实施合同及证据边界

### B1：发送边界与模型级联

- `conversation/execution/delivery_failure.py` 对真实 host send 边界的异常分类为 `rejected` / `uncertain`。明确“已被移出该群”等文本才是成员关系拒发；泛 `retcode=1200` 保持 uncertain，不自动退休群。
- `reply_artifact_builder.py` 的文字/引用/TTS 发送经过该边界。原始 Exception 在 ReplyService 出口保持原类型并带内部分类；CancelledError 原样传播。
- `executor.py` 的普通/工具正文路径识别发送失败，结束本轮，不换模型、不触发 fatal fallback。模型调用失败仍按原级联处理。
- False 是明确未发送，True 不作为 message ID；None 与其他回执保持项目既有兼容合同。True 仅代表宿主 send 调用成功，不能证明手机端投递。
- 普通正文与引用正文的不确定发送保留不可重试 claim。部分发送只提交已确认正文和 message ID，不再次生成合并正文。
- TTS-only 的 False 写入普通 send_failure_reason，避免 executor 把语音投递失败重新归为模型失败；语音投递异常发生在尚无正文送达时进入统一投递处理，已有文字送达时仍允许可选 TTS 降级。
- 不从 executor 日志推断 Gateway 已扣模型健康分。该轮最终 host send 位于 Gateway 调用之外；现场原始异常栈/健康记录仍需复验。

### B2：后台预算诊断

- `background_task_budget.py` 输出 active owner 的 task、scope、owner_task、age_ms、deferred，以及 queue timeout/full 的有界 owner 摘要。
- 未调整并发 2、queue limit、120 秒超时或前后台 Gateway 配额。
- 真实预算回放验证 holder/等待者/队列深度。不宣称服务器视觉任务占满两槽已经证实；需要现场 owner 快照及持有时长。
- 先前 jitter 占槽、空转和恢复措施属于另一个后台恢复专题，已单列验证。

### B3：视觉永久配置错误

- `runtime_contracts.py` 增加 CONFIGURATION；`gateway_policy.py` 识别 model_not_found、Model not exist、Provider ... not found 等签名。
- 缺失模型只设置具体 pool/model cooldown，不写 provider 全局冷却；其他限流、权限及临时故障保持现有合同。
- `executor.py` 记录 vision_configuration_error 和 vision_failure_class。独立文字任务仍可继续，必须依赖图片时仍不猜图。
- 未全局取消 suppress_passive_group / notify_failure。服务器六条 suppressed 输入尚未取得，不能声明它们都是代码误判。
- C1/C3 provider 配置核验与关 kill switch 仍为独立操作，本轮无配置修改。

### 退出群：记录清理、持久栅栏和恢复

- `infrastructure/runtime/group_lifecycle.py` 持久化 chat_id、group_id、active/retired、generation 等字段；schema 176 建表、177 建索引，migration audit 同步到 177。
- 仅机器人自身 group_decrease 才退休，自身 group_increase 才激活。普通成员变动不触发。群入站在生命周期读取失败时 fail-closed。
- retired 先关闭内存发送栅栏再写库；激活只在数据库提交成功后发布。数据库路径不同的 registry 不继承状态；同库热重载继承缓存，旧 runtime 退群通知同步关闭新 runtime 栅栏。
- 捕获的 group generation 在最后发送守卫再次比较，包括旧值 0。退群后重新入群只放行新事件，旧回合不能复活发送。
- 清理运行态 attention/state/coordinator/dialogue/reread/social/chat_loop/heartflow/wait，并清除 ReflectTracker 的该群待审核请求。
- 精确按 chat_id/scope_id 清理七张表：attention_deferred_outbox、attention_deferred_outbox_terminal_history、qq_action_ledger、proactive_scenario_delivery、reflection_outbox、background_task_ledger、memory_turn_ledger。
- `state_profile_persistence.py` / `chat_state_service.py` 清除该群持久运行状态，避免主动任务重启后从旧状态恢复候选。
- 缺少旧版可选表可跳过；其他清理写入错误回滚本次删除并记录 degraded，退休栅栏继续阻止发送。
- 保留历史消息、学习数据、画像等证据。本需求删除的是可能继续发送的运行状态、队列和租约；不批量抹除历史数据。
- 最后发送前的退休/代际守卫不能撤回已经提交平台的网络请求。真实 notice 是否由目标适配器送至现有 ALL handler 尚未获得宿主证据。

## 2. 修改文件定位

本专题运行文件：

| 文件 | 作用 |
|---|---|
| `conversation/execution/delivery_failure.py` | 投递分类、host send 边界 |
| `conversation/execution/reply_artifact_builder.py` | 文字/引用/TTS 回执、uncertain claim 保护 |
| `conversation/execution/executor.py` | 投递失败短路模型级联、视觉诊断 |
| `infrastructure/gateway/gateway_policy.py`、`runtime/runtime_contracts.py` | 配置故障类型和模型级冷却 |
| `infrastructure/runtime/group_lifecycle.py`、`outbound_send_guard.py` | 持久退休和旧 generation 发送栅栏 |
| `app/bootstrap.py`、`runtime_context.py`、`plugin_facade.py` | registry 构建、notice、清理和绑定 |
| `presentation/events/message_entry.py` | 群入口绑定及 fail-closed |
| `proactive/dispatcher.py`、`review_dispatcher.py`、`proactive_task.py` | 主动/审核出口绑定 |
| `learning/review/reflect_tracker.py` | 精确删除退出群待审核请求 |
| `state/chat_state_service.py`、`persistence/state_profile_persistence.py` | 持久运行状态删除 |
| `persistence/persistence_schema.py`、`architecture_migration_audit.py` | 迁移 176/177 和表/索引审计 |
| `infrastructure/runtime/background_task_budget.py` | owner/scope 诊断 |

路径前缀均为 `astrmai/`。`main.py` 复用既有 membership notice handler，本专题未改它。

新增专项 `tests/regression/reply/test_delivery_failure_boundary.py`、`tests/regression/lifecycle/test_group_lifecycle_fence.py`；修改 `test_gateway_vision_refactor.py` 的配置故障合同，以及 `test_reply_service_refactor.py` 两条明确拒发的结算断言。

## 3. Red → Green

本节列出收尾阶段重新实际执行的 Red，不补造前轮起始 Red 数字：

| 复现 | Red（exit 1） | 最小修补与 Green |
|---|---|---|
| 退出再入群，旧事件 generation=0 | 1 failed / 16 passed；outbound_send_allowed(old) 错误为 True | 最终守卫比较 group generation；新回合可发，旧回合不可发 |
| 普通 send timeout 丢回执 | 真实 coordinator claim 为 failed，可重试 | uncertain 保留 claimed，重复 claim 返回 False |
| 退群后的 ReflectTracker 请求 | 与上述合跑 2 failed / 32 passed；gone 仍存在 | clear_chat 精确删除目标群，other 保留 |
| TTS-only False | 1 failed / 19 passed；缺 send_failure_reason | 标 failed/host_send_returned_false；不记 reply_sent |
| 删除队列遇真实写异常 | 1 failed / 20 deselected；吞异常后返回 True | 非缺表写错误传播至清理错误出口，事务回滚，退休状态保持 |

中间验证问题如实说明：

- 两条旧 ReplyService 断言把所有 transport error 当成明确未发送，与防不确定投递重发合同冲突；改为明确“已被移出该群”拒发，同时保留 failed/failed_unconfirmed 断言；超时由真实 claim 新用例单列。
- 更正输入后曾漏改一条完整错误串断言，1 failed / 473 passed；修正为完整拒发串，未改产品行为。
- TTS 参数用例首次在文件单跑发生 4 条 SQLModel duplicate table 错误，属于惰性导入与逐例模块恢复顺序问题。把实际发送模块在 collection 阶段加载后，得到真正的业务 Red；无 skip/xfail、固定顺序或拆进程绕过。

## 4. 验证命令

本机 Python 3.11.4 / AstrBot 4.14.6；目标 Python 3.12 / AstrBot 4.26.4 未连接。

### 最终专项合跑

```text
python -B -m pytest -p no:cacheprovider tests/regression/reply/test_delivery_failure_boundary.py tests/regression/lifecycle/test_group_lifecycle_fence.py tests/test_executor_refactor.py tests/test_executor_vision_refactor.py tests/test_gateway_vision_refactor.py tests/test_reply_service_refactor.py tests/test_qq_action_dispatcher.py tests/test_chat_loop_kernel_refactor.py tests/test_proactive_scheduler_refactor.py tests/test_outbound_send_guard.py tests/regression/learning/test_background_schedule_contracts.py tests/regression/architecture/test_architecture_migration_audit.py tests/unit/presentation/test_message_entry_gap_coverage.py tests/test_background_task_budget.py tests/regression/learning/test_round9_learning_review.py tests/regression/review/test_review_service_migrated.py -q
```

最终结果：**497 passed / 111 warning，28.97s，exit 0**，进程正常退出。前一版（增加清理写失败回放前）496 passed / 111 warning、23.92s、exit 0。各组覆盖重叠，不能相加为独立测试总数。

### 其他验证

| 命令 | 实测 |
|---|---|
| `python -B -m pytest -p no:cacheprovider tests/regression/lifecycle/test_group_lifecycle_fence.py -q` | 最新 21 passed / 1 warning，exit 0 |
| `python -B -m pytest -p no:cacheprovider tests/regression/lifecycle/test_background_restart_recovery.py tests/regression/learning/test_background_schedule_contracts.py tests/regression/memory/test_memory_data_quality_hotfix.py tests/test_lifecycle_shutdown_regression.py -q` | 210 passed / 1 warning，20.88s，exit 0 |
| `python -m compileall -q -x '[/\\]venv[/\\]' astrmai`，PYTHONPYCACHEPREFIX 指向全新临时目录 | exit 0 |
| 隔离 cwd / ASTRBOT_ROOT：`python -I -B -c "... import main; print('main_import_ok')"` | main_import_ok，exit 0 |
| `python -I -B scripts/probe_host_import_shape.py <开发根>` | nested main import 与 speaker 函数实调用 passed；top_level_astrmai_available=false，exit 0 |
| 技能根 `python -B scripts/check_plugin.py <开发根>` | 5 error / 1062 warning，2064 Python files，exit 1 |
| 技能 check_python 逐个检查当前 35 个已修改/新增 Python 文件 | 0 error / 18 warning，exit 0 |
| `git diff --check` | exit 0，仅 LF/CRLF 提示 |

全仓五个 error 与已有错误集合相同：`.agent/cloud_exports` 四个历史 BOM 文件（cookie.py、think_level_policy.py、v2_store.py、plugin_pages.py）及 `tests/test_local_acceptance_network_isolation.py` 中 requests。不能把全仓技能检查写成通过。warning 为既有依赖/测试引用及 focus/legacy API 弃用，不声明整个仓库无 warning。

compile/import 的输出写临时目录，没有删除既有缓存。当前 diff 不含数据库、配置、候选包或字节码。没有真实数据库完整前后哈希证据，因此不宣称已获得真实 DB 哈希独立核验。

## 5. 独立验收与服务器待办

1. 以工作树实际文件独立复验普通/工具/引用/TTS、False、明确拒发、回执不确定、部分发送、取消及 shutdown。核对模型调用次数、无 fatal fallback、claim、history 和终态 reason。
2. 用真实 schema 175 → 177 迁移回放，验证退群持久化、冷重启读取、热重载、不同数据库隔离及重入群的代际边界。
3. 七表清理回读其他群保留，注入写故障确认回滚；审核请求清理不误删其他群。
4. 目标 AstrBot 4.26.4/NapCat 原始 group_decrease/increase notice 必须确认能进入 handler，UMO 与普通消息一致。不把本地 SimpleNamespace notice 回放写为目标宿主通过。
5. B2 采集 owner/task/scope、持有时长、队列深度后再决定容量策略。视觉任务占槽仍是待证推断。
6. B3 回放服务器 suppressed_vision_failure 的脱敏输入；配置修复与代码部署、关闭学习开关分开。
7. 回执漏斗按 trace/attempt/generation/send route/segment 配对。聚合总数不用于判重复发送或丢消息；遗留 claimed/done 差异仍待独立服务器取证。

```yaml
development_implementation_completed: true
local_regression_completed: true
handoff_status: ready_for_independent_revalidation
independent_revalidation_completed: false
live_host_verified: false
full_repository_skill_check_passed: false
candidate_built: false
production_directory_modified: false
production_config_modified: false
production_database_modified: false
server_accessed: false
kill_switch_changed: false
committed: false
pushed: false
deployed: false
release_accepted_for_production: false
```
