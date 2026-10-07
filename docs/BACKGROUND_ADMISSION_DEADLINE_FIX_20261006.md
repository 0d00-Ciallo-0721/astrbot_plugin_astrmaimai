# BUFFERED 准入 deadline 修复及交接

```yaml
decision: ready_for_independent_revalidation
production_directory_modified: false
production_deployment_authorized: false
candidate_verified: false
production_config_modified: false
database_modified: false
existing_candidate_modified: false
deployed: false
committed: false
pushed: false
real_qq_messages_sent: false
```

## 范围及启动证据

只修改开发工作区。完整读取技能与事件、配置 reference、QQ 只读报告。
基线 commit `16cc463`；启动时 `git status --short` 与 `git diff --stat` 已记录。
此前 QQ 修复为 9 个 tracked 文件改动、2 个 Python 新文件及审计文档，完整保留，未覆盖。
本轮未访问生产目录、服务器、真实数据库、配置或候选包，不发真实消息。
测试使用隔离临时数据，不把它们等同于生产数据库。

启动 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| `gate.py` | `a0d888b26bcf46a8a6982b35f32c9b78a9b4d66d240efda567d038896c5bd2f3` |
| `background_task_budget.py` | `dc38ac31bd157c7f6aa9cc3a39bc12747610fa4ad40fe17b22b71ce53096a010` |
| `turn_call_ledger.py` | `0cde1221cc3a590deb4c9249ea298164438ec783543bd622cb1282a77be7171e` |
| `message_entry.py` | `a5aee8c6e1b779878f63e5bb06b854a1b7c81f0d793064a1b622c27648c464a5` |
| `config.py` | `3acf28322f1ea6481a57afa5fff93b7f4933af1f5bd9e73033eccf78e2964203` |
| `_conf_schema.json` | `32321847675a7505d51e65c15dbcfb504dfe41f33eb99c61dcd61298fbb52132` |

本机 Python 3.11 / AstrBot 4.14.6，目标 4.26.4 尚未实测。
用户提示词中的 `trace=a892b16ae531 / 14.97s` 为本轮输入证据；
QQ 报告并未提供这条新取消 trace 的完整导出。本轮不连接服务器补取日志，
因此以下结论确认代码缺陷及本机复现，不独立证明这条服务器样本的实际取消发出者。

## 根因及两个时钟

旧 `gate._after_slot()` 用 `wait_for(budget.run(...), remaining_admission)` 包住
budget 排队、lease 获取、planning/System2 全部执行。即使 lease 已取得，
准入时间到也会把执行中的 planner 取消；runner 再把它记录成外部取消。

新合同：

1. local semaphore 与 global budget 共用一个准入 deadline。
2. 同一任务的 `asyncio.timeout` 仅覆盖取得资格前的等待；
   budget `on_acquired` 立即解除准入 timer。旧/custom budget 的 factory 调用边界也解除 timer，
   未调用 factory 的 custom budget 仍会被有界取消。过期 deadline 不能被迟到回调重新放行。
3. lease 取得后的执行由现有 `BackgroundTaskBudget.execution_timeout_sec` 保护。
   System2 总预算、side-input reply reserve、锁等待保护均保留。
4. deferred replay 仍有独立 replay 总 deadline；其准入也受配置上限约束，
   不将它的执行 deadline 再当作完整 budget.run 的准入计时。
5. 不捕获 CancelledError 后继续业务执行，不新增平行预算状态机。

默认值核查发现：启动代码、schema、既有默认值测试已经都是 **30.0**，
无需假装本轮再次将数值从 15 改为 30；本轮更新描述、确认显式 15.0 的优先级，
并去掉预算排队处另一个硬编码 30 上限，改用实际配置。
已有用户显式 15.0 仍是 15.0，不写回或覆盖服务器配置。
非法字符串被现有 Pydantic 校验拒绝，不混淆成显式 15 或缺省 30。
30 秒相比 15 秒允许更久拥塞等待，但不增长执行预算或总预算。

## 修改文件

| 文件 | 局部修改 |
| --- | --- |
| `astrmai/conversation/attention/gate.py` | acquired 解除 admission scope；共享队列 deadline、遵循 reserve；补 budget/execution stage；准入终态先于 callback flush。 |
| `astrmai/infrastructure/runtime/background_task_budget.py` | 在已有 lease 保留 Timeout scope，并提供只读的执行超时来源查询。 |
| `astrmai/conversation/execution/system2_runner.py` | 在 CancelledError 到达时识别真实 budget 执行超时；stage 与 terminal 不冒充外部取消。 |
| `config.py` / `_conf_schema.json` | 数值已为 30，更新准入与执行边界说明，不修改用户配置。 |
| `tests/unit/attention/test_atwake_budget_and_replay.py` | 增加实际 gate/budget/runner 的最小 Red 回放。 |
| `tests/unit/attention/test_background_admission_deadline.py` | 实际时长、队列、custom budget、超时、抢占、停机、reserve/flush、无回复与发送后超时。 |
| 本文 | 审计及独立复验交接。 |

`turn_call_ledger.py`、`planning_input_loader.py`、`message_entry.py` 未修改，
沿用现有总预算/侧输入/入口绑定，不改 generation、wait、发送所有权与重放策略。

## Red / Green

最小 Red 命令：

```powershell
python -B -m pytest -p no:cacheprovider tests/unit/attention/test_atwake_budget_and_replay.py -k admitted -q
```

初稿 admission=0.05 被现有最小 0.1 clamp，未复现业务取消，仅失败在缺少新诊断字段；
更正为 admission=0.1 / planning=0.2 后，exit=1，1 failed / 23 deselected，
真实堆栈为 `gate.wait_for -> budget.run -> System2Runner -> slow_execute CancelledError`，
result=None、没有完成回复。Green 同命令 exit=0，1 passed / 23 deselected。

专项总预算初稿遗漏强唤醒 extra，runner 按普通回合执行，先被执行预算取消；
测试加回真实入口会写入的 `astrmai_at_bot_wakeup=True`，正确区分两个预算。
队列清理追加断言一度误用不存在的 `_waiting`；读取真实结构后改为 `_waiters`，
不放宽清理断言。修后定向 4 passed。

## 终态样例及资源

| 样例 | terminal / 原始 reason | 发送、资源 |
| --- | --- | --- |
| BUFFERED 15.0 准入、planning 15.1 秒 | reply_sent | 一次发送，lease/semaphore 释放。 |
| ENGAGED 对照 15.1 秒 | reply_sent | 直接路径一次发送，不经过后台准入。 |
| semaphore / budget / custom budget 排队 30 秒 | queue_timeout / background_queue_timeout | System2 未进入，零发送，队列清理。 |
| lease 后执行 0.1 秒超时 | timeout / execution_timeout；source=background_execution_timeout | 非 queue、非 external；零发送，lease 释放。 |
| 强唤醒总预算耗尽 | timeout / budget_exhausted | planner stage 原因一致；不绕过总预算。 |
| generation 取消 | superseded / generation_advanced | CancelledError 传播，零发送、资源释放。 |
| shutdown 取消（排队或执行） | cancelled / shutdown source | CancelledError 传播，无新重试、无发送。 |
| admitted 但无可见回复 | no_visible_reply | 零发送、一个 terminal。 |
| 成功发送后执行超时 | reply_sent，execution_status=execution_timeout | 已发一次，不重复发或 deferred replay。 |
| queue full | error / background_queue_rejected | 不冒充 queue_timeout；既有 defer 语义保留。 |

保留原 terminal 词表：`background_queue_timeout`、`execution_timeout`、
`budget_exhausted` 是原始 execution_status/reason，不造一套不兼容的 terminal 枚举。
shutdown 以 cancelled + shutdown 来源表达，不伪造另一种产品结果。
阶段 ledger 保留等待/执行 elapsed、configured timeout、remaining budget、acquired_at。
event extra 增加 acquired_at/execution_started/execution_elapsed；已有 trace/terminal 带 generation、attempt。
executor/send 是否进入仍按既有 executor/send stage 与实际回执判定，不因取得 lease 就伪报 sent。

## 验证命令及结果

所有 pytest 使用 PowerShell 7、开发根目录，退出码及既有失败分别记录。

```powershell
python -B -m pytest -p no:cacheprovider tests/unit/attention/test_background_admission_deadline.py tests/unit/attention/test_atwake_budget_and_replay.py tests/unit/attention/test_atwake_supersession.py tests/unit/attention/test_atwake_wait_signal_policy.py tests/test_atwake_stage00_observability.py -q
```

首次完整核心 Green：exit=0，128 passed / 11 warnings，130.60s（当时新专项16项；后续追加5项）。
追加测试后快速合跑上述同文件命令加 `-k 'not 30.0 and not 15.0'`：
exit=0，128 passed / 5 deselected / 10 warnings，10.75s。
实际时长用例保留在源码，不靠时间缩放声称超过真实 15/30 秒。

```powershell
python -B -m pytest -p no:cacheprovider tests/unit/attention/test_background_admission_deadline.py -q
python -B -m pytest -p no:cacheprovider tests/unit/attention/test_background_admission_deadline.py -k '30.0 or 15.0' -q
```

完整专项 exit=0，21 passed / 5 warnings，128.34s。
最后一次来源/迟到准入边界修补后，长时长筛选命令 exit=0：
5 passed / 16 deselected / 4 warnings，126.40s。
快速核心128项与长时长5项合计覆盖最终核心133项；没有将筛选后的结果冒称完整单次合跑。

```powershell
python -B -m pytest -p no:cacheprovider tests/test_config_standalone_refactor.py tests/test_background_task_budget.py tests/regression/conversation/test_attention_deferred_queue.py tests/test_lifecycle_shutdown_regression.py -q
```

exit=0，146 passed / 3 warnings，15.52s。
最终小范围追加核验（上述命令去掉 lifecycle）：exit=0，82 passed / 3 warnings，11.45s。

```powershell
python -B -m pytest -p no:cacheprovider tests/test_attention_gate_refactor.py tests/test_executor_refactor.py tests/test_chat_runtime_coordinator_refactor.py tests/test_lifecycle_shutdown_regression.py -q
python -B -m pytest -p no:cacheprovider tests/regression/conversation tests/unit/infrastructure -q
python -B -m pytest -p no:cacheprovider tests/test_reply_service_refactor.py tests/test_turn_outcome_refactor.py -q
```

依次 exit=0：230 passed / 25 warnings，219 passed / 5 warnings，128 passed / 104 warnings。
最后一组覆盖 Stage 04 False/True 回执与已有 QQ 修复取消/发送合同，不构建候选。

```powershell
python -m compileall -q astrmai
python -B -c "import sys; sys.path.insert(0,sys.argv[1]); import main; print('main_import_ok')" DEV_ROOT
git diff --check
```

均 exit=0。compileall 的 PYTHONPYCACHEPREFIX 为工作区外专用短路径；
main import 的 cwd 为新建隔离临时目录，输出 main_import_ok。未在真实 data/ 执行。
临时目录仅保留本轮验证产物，未删除任何既有用户缓存。

技能根目录 `python -B scripts/check_plugin.py DEV_ROOT`：exit=1，
15 error / 1023 warning / 2049 Python 文件。
错误仍是既有 U+FEFF 与 manual/network isolation 测试中的 requests；本轮不修改这些文件。
同检查器 `check_python(path,root)` 对本轮6个 Python 文件：exit=0，0 error / 7 W120 warnings。
这是修改范围检查，不是全仓或候选包通过。

### 既有失败及合跑限制

基线命令（尚未修改本轮代码）：

```powershell
python -B -m pytest -p no:cacheprovider tests/test_attention_gate_refactor.py tests/test_background_task_budget.py tests/unit/attention/test_atwake_budget_and_replay.py tests/test_config_standalone_refactor.py -q
```

exit=1，3 failed / 169 passed / 25 warnings：pressure heartbeat 0.203>0.1；
两项 AtWake group ingress 的 `At() takes no arguments`（前序测试安装未恢复的组件替身）。
原 pressure 测试在后续230项命令通过，没有改阈值或删除断言。

把 reply_service/turn_outcome 接到上述230项命令末尾：exit=1，4 failed / 354 passed / 129 warnings。
失败均 Reply 组件缺失/变成 Plain；旧 stub installer 未提供 Reply，而 reply_service 测试只 reload
service、不 reload artifact builder，缓存了前序组件模块。单独 reply_service/turn_outcome 为128 passed。
本轮不顺手重写这两套测试的全局替身生命周期；不能将拆开进程后的通过冒称任意顺序同进程通过。
独立验收应单列复核该隔离缺陷，评估是否需要独立测试修复。

## 独立复验与剩余证据

1. 重跑完整新专项及核心合跑，检查每个 attempt 只有一个 terminal。
2. 核对30秒真实等待、15.1秒真实执行、自定义 budget factory/lease 边界、执行超时来源。
3. 验证 reserve、总预算、deferred replay deadline、generation、停机和已发送后的不重发。
4. 复验完整入口真实 executor/ReplyService：本轮 Harness 的 planner/send边界为离线替身，
   gate、session worker、budget、runner、coordinator、claims及terminal均为真实实现。
5. 收集目标 AstrBot 4.26.4 的完整15秒样本导出、生效配置、取消来源和实际QQ投递证据；
   不把本机producer复现认定为该服务器trace已独立确认。
6. 单列既有测试替身顺序污染与全仓技能错误，不能据局部Green授权生产。

保持 ready_for_independent_revalidation。不得打包、构建候选、同步生产、部署、
修改生产配置、重启或发送真实QQ消息，亦未提交或推送。
