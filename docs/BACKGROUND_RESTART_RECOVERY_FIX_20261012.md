# 后台重启恢复与空闲准入：开发审计及复验交接

日期：2026-10-12（本地执行日期；用户提供的启动日志标记为 2026-10-13）。

状态：`ready_for_independent_revalidation`。仅表示本地开发交接，不表示独立验收、真实宿主验证或生产发布通过。

## 1. 范围和证据边界

本轮只修改开发工作区中的后台调度、记忆投影恢复、维护周期及相邻回归测试。未提交、推送、构建或覆盖发布候选、同步生产、连接服务器、修改真实配置、切换学习 kill switch 或发送真实 QQ 消息。

所有本轮新增 SQLite 复现使用 pytest 临时目录或隔离探针临时目录。没有真实开发数据库的完整前后哈希证据，因此不宣称某个开发数据库 SHA-256 已获独立核验。没有删除用户既有缓存或修改历史导出文件。

依据两轮用户证据及当前源码：

- 前轮 learning 返回形状与结算可见性已有独立专题修复，本轮不重新实现其解析器或挖矿业务。
- 新启动日志共 121 条事件，6 次后台排队 120 秒超时；此前日志没有足够 owner 信息，不能逐次确认占槽者。
- `runtime running` 约为 03:30:24，向量引擎 ready 为 03:34:04；不把整个对话启动阻塞在向量 ready 上。
- 654 条 pending 原因均为 `projection_rebuild_in_progress`；单条维护日志长 38,386 字符。这证明当时存在依赖等待，不证明这些记录已永久失败。
- diary 的 `duration_ms=198271.8` 是总耗时，不能等同模型执行时长，也不能据此认定它是全部拥堵的唯一原因。
- Gateway 已有 global=3/background=2 的并发保护，本轮保留原值。
- shutdown 的 vector drain 告警不等于已确认泄漏；保留物理任务退出前的资源保护。

## 2. 计划 00–04 的实施与验收映射

| 阶段 | 实际实现 | 本地证据 |
|---|---|---|
| 00 诊断 | task/scope/run_id、延迟/准入/执行/结算耗时；排队 owner 的有界摘要；业务 outcome 与结算 committed 分开记录；告警采样不丢计数 | queue owner、三次超时采样、654 条日志有界、真实 ledger outcome 测试 |
| 01 时序和空转 | 周期统一 monotonic；diary jitter 在 budget 之前；维护服务入队前检查 due/work；blocked learning 不进入 discovery budget | 5/59/60 秒边界、diary 不占槽、idle 服务不入队、blocked gate 与 retention 测试 |
| 02 依赖恢复 | dependency deferral 不增加持久 attempts；真实失败仍退避和耗尽；ready 与周期重试互斥；原因筛选先于 limit；ready replay 沿用原 budget | 真 SQLite restart/attempts、真实失败耗尽、ready 合并、队首公平、取消释放锁测试 |
| 03 持久周期 | scheduled/daily decay 两个独立 fence；衰减与 checkpoint 同事务；canonical 和整体维护标记分开；失败不先写成功；结算取消恢复原意图 | 并发原子衰减、checkpoint 故障回滚、重启不重复衰减、索引失败只重试 index、成功结算取消测试 |
| 04 综合验证 | 慢 vector + diary jitter + 前台配额回放；physical owner 未结束不能假释放；跨模块同进程回归和嵌套包实调用 | 综合回归、反向顺序、drain、离线 host-shape 调用；真实宿主验证仍待办 |

不改 timeout 数值、并发数、idle 轮询周期、阈值、学习开关或视觉 provider 策略。不是通过延长等待、关闭守卫或批量吞异常让测试通过。

## 3. 修改文件及合同

### 运行源码（10 个）

- `astrmai/infrastructure/runtime/background_task_budget.py`：排队满/超时携带有界 owner 诊断；每类最多 8 个名称、名称长度上限 120，保留总数及最长持有时间。
- `astrmai/proactive/proactive_task.py`：维护时钟修正、预算前 jitter、due 准入、业务报告分类、分阶段耗时、日志聚合和结算取消回收。`finished` 表示 coroutine 结束，不能将未提供报告或 partial 当成业务完整成功。
- `astrmai/proactive/decay_service.py`：due 检查、daily 持久 fence、返回 processed/failed/status；失败不更新内存成功时间。
- `astrmai/proactive/group_signin_service.py`：按开关、时间窗口和当天签到状态检查 due；返回业务报告，保留管理页原 `_last_run` 状态约定。
- `astrmai/proactive/heartflow/topic_digest_service.py`：沿用 50 session、topic heat、participation、cooldown 的现有条件预检；区分 no_work/failed/partial/succeeded。
- `astrmai/learning/evolution_manager.py`：DISCOVERY gate 在预算之前检查；blocked 时只执行到期且受支持的 retention。预期 kill switch/flag_disabled 记 INFO，其他 gate 异常仍 ERROR。保留 direct backlog、入站、retention、结算及持久学习合同。
- `astrmai/memory/services/v2_store.py`：统一 dependency deferral 原因，暂缓不消耗 attempts；旧 exhausted/dead_letter 不复活；ready 查询 SQL 先筛选再限量；新增原子 `apply_decay_if_due`，原手动 `apply_decay` 仍返回 int。
- `astrmai/memory/services/memory_index_projector.py`：批次互斥、依赖/空队列前置检查、ready replay 受原预算控制、报告 deferred。先 budget 再批次锁，避免反序等待；draining 优先退出，不被空队列检查绕过。
- `astrmai/memory/services/memory_maintenance_service.py`：scheduled decay 持久 fence、canonical 完成标记与整体成功标记分离；index failed/deferred 不标整体成功；daily 使用独立 checkpoint。
- `astrmai/memory/services/memory_engine.py`：仅传递 daily decay 的可选间隔，不改变其他记忆业务。

### 测试（3 个）

- 新增 `tests/regression/lifecycle/test_background_restart_recovery.py`：41 个参数展开用例；使用真实预算、真实 ledger/SQLite 和受控 provider/事件等待，不访问生产。收尾新增三例钉住取消/失败的分段诊断，以及真实准入超时耗时。
- `tests/regression/learning/test_background_schedule_contracts.py`：四维护任务调度测试补齐真实可 due 的配置/记忆 fixture；原路由及结果断言保留。
- `tests/regression/memory/test_memory_data_quality_hotfix.py`：scheduled 维护 policy 原空字典断言更新为包含 `maintenance_interval_sec=86400`；purge 参数及其他保护语义保持。

### 核心持久状态

- `scheduled_decay_completed_at` 和 `daily_decay_completed_at` 分别约束两条原有策略；不合并 decay rate。
- `scheduled_canonical_maintenance_completed_at` 只在 canonical 阶段成功时设置；index 失败后重试不再重复 canonical 阶段。
- `scheduled_maintenance_completed_at` 仅无错误且无 remaining pending 时设置。
- 未改变既有表结构；新增 fence 位于现有 `memory_v2_meta`。
- dependency waiting 更新 outbox revision，但不消耗真实失败次数；真正失败的原耗尽规则保留。
- no_work/blocked/disabled 对应现有 ledger `rejected`，partial/deferred/failed/errors 对应 `retry_wait`；真正成功才 `succeeded`。
- 业务已完成而结算 await 被取消时，以同 lease/token 恢复原合法意图，不再次执行业务、不改写为可重跑失败。没有确认 committed 时不宣称主表已提交或 durable pending 已保存。

## 4. Red / Green 和中间失败

所有失败均如实列出，不把 fixture 或命令错误算作业务 Red：

| 验证 | Red / 故障 | Green / 处理 |
|---|---|---|
| 初始 16 个用例 | 15 failed，其中 scheduler fixture 漏 `_last_diary_date`、学习 fixture 未显式启用 kill switch；不是有效总业务 Red | 补齐 fixture 后重新定位；未靠修改产品断言放行 |
| HEAD 时钟与 gate 隔离回放 | 修正 fixture 后 3 failed / 1 passed，退出码 1：5/59 秒提前维护、blocked learning 仍申请 budget；60 秒控制通过 | 当前源码边界和 gate 用例通过；脚本只在隔离进程内挂载 HEAD 函数，不恢复工作树 |
| 维护重启 | 实际出现 decay 0.92 -> 0.8464、失败前写成功时间、partial 不报告失败 | 原子 decay fence 与成功标记分离，专项通过 |
| ready 查询 | 1 failed / 22 passed：无关队首挡住 eligible row | SQL 先筛选原因再 limit |
| idle 准入 | 1 failed / 1 passed：无工作服务仍入队；其后空统计字典测试读取 KeyError | 增加 due 检查；空统计使用 get 默认值，不改业务结果 |
| outcome 结算 | 3 failed / 5 passed：partial/no_work 被记成 succeeded | 真 ledger 行按业务报告分类 |
| index failure | 1 failed：索引失败仍设置整体成功 | canonical/overall 两层 fence，index 可重试而不重复衰减 |
| 空投影队列 | 1 failed：空队列仍占槽；修补后 draining+empty 既有 shutdown 回归挂起 | 前置 drain 检查；原单测重新 1 passed，最终合跑正常退出 |
| settlement 取消 | 1 failed：业务完成但 running lease 残留 | 保存原 settlement intent，恢复成功；业务仅执行一次 |
| 取消 done callback | 曾重复结算，引起 SQLite thread warning / 进程不退出 | 仅未返回结算进入恢复，断言调用次数 1；最终组合正常退出 |
| 离线 nested probe | 首次业务断言通过，但仍位于临时 cwd，清理 WinError32，退出码 1 | 清理前恢复 cwd 后退出码 0；不是宿主故障 |
| HEAD Red 脚本首版 | globals 副本使 monkeypatch 未作用，运行挂起后终止 | 改用真实 module namespace 后才得到有效 3 failed / 1 passed |
| 最终合跑命令首版 | architecture 文件名漏 `_refactor`，命令退出码 1，no tests ran | `rg --files` 核实真实路径后重新执行，不算业务失败 |
| 收尾诊断边界 | 3 failed / 38 deselected，退出码 1：取消/失败未保留分段信息，超时无实际准入耗时 | 仅移动共用诊断写入点、补未取得槽时的耗时，41 passed；未改调度与取消策略 |

没有 skip/xfail、拆进程或固定测试顺序来规避本轮业务与隔离断言。反向顺序是附加污染检查，不替代同进程综合回归。

## 5. 验证命令与结果

环境：本地 Python 3.11.4 / AstrBot 4.14.6；目标 Python 3.12 / AstrBot 4.26.4 未访问。pytest 使用 `-B -p no:cacheprovider`；临时 SQLite 由 tmp_path 创建。以下计数来自各自命令，存在覆盖重叠，不能累加成独立测试总数。

### 最终综合命令

```text
python -B -m pytest -p no:cacheprovider -o faulthandler_timeout=60
  tests/regression/lifecycle/test_background_restart_recovery.py
  tests/test_background_task_budget.py
  tests/regression/learning tests/unit/learning
  tests/test_learning_refactor.py tests/test_learning_event_collaboration_refactor.py
  tests/unit/memory tests/regression/memory tests/integration/memory
  tests/test_proactive_scheduler_refactor.py
  tests/unit/proactive/test_proactive_gap_coverage.py tests/test_heartflow_refactor.py
  tests/test_lifecycle_shutdown_regression.py
  tests/regression/lifecycle/test_background_lifecycle_contracts.py
  tests/regression/architecture/test_gateway_concurrency_priority.py
  tests/test_attention_gate_refactor.py tests/unit/infrastructure
  tests/regression/architecture/test_import_boundaries_refactor.py
  tests/regression/architecture/test_directory_contracts_refactor.py
  tests/regression/architecture/test_memory_runtime_boundaries_refactor.py
  tests/regression/architecture/test_lifecycle_resilience.py
  tests/regression/architecture/test_shared_test_support_refactor.py -q
```

以上换行仅供阅读，实际执行为同一条命令。最终结果为 **1564 passed / 23 warning，156.88s，退出码 0**，进程正常退出；收尾诊断修补前为 1561 passed。覆盖最新 41 例专项及学习、记忆、生命周期、前台 gate、并发和架构回归。最后仅更正 stats 类型注解以包含字符串和 outcome 计数映射，另跑专项与编译；此命令不是全仓所有测试或真实宿主验收。

### 已结束的验证

| 命令 / 范围 | 结果 | 退出码 |
|---|---|---|
| `python -B -m pytest -p no:cacheprovider tests/regression/lifecycle/test_background_restart_recovery.py -q` | 最终类型注解修正后 41 passed / 1 warning，9.77s | 0 |
| 上述专项 + budget + background_schedule + memory_v2_services + projection_outbox + memory_data_quality_hotfix + memory_process_restart + mining_settlement + proactive_scheduler + proactive_gap + heartflow | 461 passed / 1 warning，34.23s | 0 |
| `python -B -m pytest -p no:cacheprovider tests/unit/memory tests/regression/memory tests/integration/memory -q` | 414 passed / 1 warning，40.97s | 0 |
| `python -B -m pytest -p no:cacheprovider tests/test_heartflow_refactor.py tests/test_proactive_scheduler_refactor.py tests/regression/lifecycle/test_background_restart_recovery.py -q` | 最新 101 passed / 1 warning，11.72s；修补前 98 passed | 0 |
| lifecycle_shutdown + background_lifecycle + gateway_priority + attention_gate + unit/infrastructure + heartflow | 286 passed / 23 warning | 0 |
| import_boundaries + directory_contracts + memory_runtime_boundaries + lifecycle_resilience + shared_test_support | 42 passed / 2 warning | 0 |
| learning unit/regression + learning_refactor + event_collaboration + restart 专项（当时 35 例） | 699 passed / 1 warning；不是最新 41 例全量声明 | 0 |
| `python -B <隔离证据根>/baseline_clock_gate_red.py <开发根>` | 本次复跑 3 failed / 1 passed / 1 warning，HEAD 预期 Red | 1 |
| `python -m compileall -q -x '[/\\]venv[/\\]' astrmai` | 最终源码编译通过；PYTHONPYCACHEPREFIX 指向隔离目录 | 0 |
| 隔离 cwd：`python -I -B -c "... import main; print('main_import_ok') ..."` | main_import_ok；ASTRBOT_ROOT 指向临时目录 | 0 |
| `python -I -B <隔离证据根>/background_host_probe.py <开发根>` | nested 实调用：atomic_decay=true、dependency_attempts=0、canonical_restart_fence=true、replay_deferred=true、top_level_astrmai_available=false | 0 |
| 技能 check_python 原实现逐个检查本轮 13 个修改 Python 文件 | 0 error / 9 warning | 0 |
| 技能根：`python -B scripts/check_plugin.py <开发根>` | 5 error / 1057 warning，2060 Python 文件；不是全仓通过 | 1 |
| `git diff --check` | 无 whitespace error，仅现有 CRLF 转换提示 | 0 |

隔离证据根为本机临时目录中的 `astrmai_background_verify_1461b3b8`，脚本不属于发布运行文件。HEAD Red 脚本按 AST 抽取原 `_loop` / `_run_backlog_mining_budgeted`，只在测试进程内绑定原函数；不会写回工作区。nested 探针沿用 `scripts/probe_host_import_shape.py`，实际调用本轮 store/maintenance/projector 方法，不仅检查 import。

warning 主要为 NumPy/Faiss 弃用及既有 focus API 弃用，不视为本轮新增业务失败。

### 全仓技能检查的既有错误

四个 E100 是 `.agent/cloud_exports` 历史文件首行 BOM：

- parser 的 `core/cookie.py`；
- astrmai 的 `conversation/planning/think_level_policy.py`；
- astrmai 的 `memory/services/v2_store.py`；
- astrmai 的 `webui/plugin_pages.py`。

另一个 E104 为 `tests/test_local_acceptance_network_isolation.py:159` 的故意 requests 调用。上述文件不在本轮 diff 中。既有审计记录为 5 error / 1053 warning，本轮最终为 5 error / 1057 warning；错误集合保持，warning 不能写成完全不变或全绿。修改文件 0 error 不等于全仓技能门禁通过。

## 6. 独立验收清单与剩余限制

独立验收须以当前工作树为对象，不只 checkout HEAD 或复述开发自测数字：

1. 重跑综合命令及反向顺序；检查临时模块替身、线程和任务均能退出。
2. 用真 SQLite 回读 attempts、revision、status、checkpoint 和 lease；至少覆盖 deferred、真实失败耗尽、partial 重试与结算取消。
3. 模拟 vector 慢启动期间前台事件；验证前台模型配额不被后台占满，diary jitter 不占物理预算。
4. 同时触发 ready replay 与周期重试；同一记录只处理一次，取消后 pending 和锁状态正确。
5. 连续重启及 checkpoint 故障；scheduled/daily 两个原有策略分别最多执行一次，不把两条合法策略合并成一次。
6. blocked learning 不能 discovery，但到期 retention 仍执行；不修改 kill switch。
7. 检查 namespace 为 `data.plugins.astrmai.astrmai.*` 且顶层 `astrmai` 不可见；实际调用变更路径。
8. 审查最终 diff 不含配置、数据库、字节码、候选包或不相关业务变更。

不能从本地验证推导以下结论：

- 不能反推旧版本本日已经完成衰减；首次上线没有新 fence 的历史数据，需要先只读核对历史进度。新 fence 不提供旧执行的回溯 exactly-once。
- 不自动恢复历史 exhausted/dead_letter；真实历史分布及恢复授权另行处理。
- 未验证真实 provider、QQ 投递、关闭 kill switch 后完整挖矿或长期 pending 收敛。
- 未确认服务器 vector physical owner 最终退出、RSS/CPU 或 IO 是否异常；现有 shutdown 告警不足以定性泄漏。
- 未确认早前 15 秒外部取消来源，仍为“外部取消候选，来源未证实”。本轮不改 attention deadline。
- 不修现存视觉模型 HTTP 400；需要单独 provider 诊断，不能与本轮后台修复一起改生产配置。
- 没有新发布候选，因此不声称候选技能检查、manifest、生产一致性或真实宿主门禁通过。

后续顺序：独立本地复验 -> 已授权隔离 Python 3.12/AstrBot 4.26.4 宿主回放 -> 另行授权构建/部署。上线和关闭学习 kill switch 分开变更。

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
server_accessed: false
kill_switch_changed: false
committed: false
pushed: false
deployed: false
release_accepted_for_production: false
```
