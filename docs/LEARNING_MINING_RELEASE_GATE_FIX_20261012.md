# 学习挖矿 release gate 返回值与结算修复

状态：`ready_for_independent_revalidation`。本轮仅完成开发与本地回归，未构建候选、修改生产包、访问服务器、提交、推送或部署。

## 核实后的根因

`astrmai/learning/evolution_manager.py::process_logs_and_mine` 正常返回的是按 `expression` / `jargon` 分组的结果映射。包含 `run_id`、`group_id`、`pipelines` 的聚合报告只写入 `_last_mining_outcomes`，并不作为成功返回值。服务器分析把这两层混为一谈；因此不能采用 `result.get("pipelines") or result.get("outcomes") or {}`，该写法会把真实成功结果丢掉。

真实故障是 DISCOVERY 门禁拒绝时返回 `{"status": "blocked", "reason": "...", "outcomes": {}}`。实时触发和 backlog 消费者把其中的字符串当作 pipeline 结果调用 `.get()`。backlog 将正常拒绝误记为输入异常、增加失败计数与 1800 秒冷却；实时入口同样错误进入异常重试。单纯过滤字符串或改为空映射仍然不够：原调度器把空结果结算为 `succeeded`，保留新的挖矿时间，引入默认 21600 秒成功间隔。

本地源码与原本地生产包的该文件在改动前哈希相同：`71d60373f365d23be3f5755200776844a19d37adc27add32de139d0f64ac2208`。服务器历史日志和回滚包证据来自操作员，本轮没有独立访问服务器验证。

## 局部实现

只修改以下业务与测试文件，保留原工作树全部其它改动：

- `astrmai/learning/evolution_manager.py`：两个入口在 snapshot / claim 前检查既有 DISCOVERY gate；保留挖矿方法内部检查，覆盖 await 期间开关变化。新增私有结果解析，分别处理正常 pipeline 映射、严格的整轮 blocked 包装和空结果；非法形状使用已有 `_LearningInputContractError`，backlog 保留其 stage / pipeline / value_type 诊断。
- `tests/unit/learning/test_mining_release_gate_settlement.py`：48 个用例，使用真实 release gate、真实 EvolutionManager 与临时 SQLite BackgroundTaskLedger。消息读取/元数据存储是隔离内存适配器，provider 为离线替身；不调用真实模型、不接触开发或生产数据库。
- `tests/regression/learning/test_background_schedule_contracts.py`：仅修正原“provider 调用前持久化冷却”测试的替身，使其返回真正 completed 的 pipeline 结果；增加 provider 进入时的持久化断言。原断言未放宽。

| 情况 | 挖矿时间与冷却 | lease / 报告 |
|---|---|---|
| 入口已被 release gate 拒绝 | 不修改挖矿时间，不建立成功或失败冷却 | 不读取 snapshot、不 claim；backlog 写 skipped，保留 gate reason |
| claim 后拒绝或空结果 | 恢复本轮原时间；不增加失败计数 | 当前 token 结算 `rejected`，retry_count 不增加、lease 清空；backlog 写 skipped |
| 真实 pipeline 全失败 / quarantined | 恢复原时间 | 保留既有 `retry_wait` / `all_pipelines_failed` 与短冷却 |
| 正常或部分成功 | 保留新时间与既有成功间隔 | 保留既有成功、报告与 cursor 语义 |
| 结果尚未确定时取消 | 恢复原时间 | 保留既有取消重试，CancelledError 继续传播 |
| 结果已确定、在结算中取消 | 按已知结果结算 | rejected 或 succeeded 的意图保持；不把已完成结果变为业务重试，取消继续传播 |
| 结算写入失败 | 按已知结果保持时间 | 尝试保存 pending settlement；只有入队成功才具有持久重放保证，不重新运行 provider |

所有结算复用既有 `settle_task_lease` / token CAS。旧 token 无法清理新持有者的 lease。报告或日志在结果确定后抛异常，不回滚已经完成的挖矿结果。结算返回 False 不保证已进入持久重试：数据库不可用或队列满时也可能返回 False。后续结算可见性及结算返回后取消误报修复见 `LEARNING_SETTLEMENT_OBSERVABILITY_FIX_20261012.md`；此处保留上一轮开发验证与状态，不追认新的独立或宿主验收。

不改 kill switch 配置实值、阈值、pipeline timeout、成功间隔或失败冷却数值；不重写发布门禁及公共 ledger。pipeline 内部 `blocked` 是已有的依赖阻断结果，不能冒充整轮 release 拒绝；其批次调度策略保持原状。`waiting` / `retry_wait` 等非全失败批次的现有成功间隔策略也未在本轮改写，应另行评估。

## Red / Green 与本地验证

环境：Python 3.11.4 / AstrBot 4.14.6。命令在开发目录运行，技能命令在技能根目录运行；TEMP / TMP / ASTRBOT_ROOT 指向本轮隔离目录。测试统一使用 `-B -p no:cacheprovider --basetemp <隔离子目录>`，每次使用不同子目录；compileall 的 `PYTHONPYCACHEPREFIX` 指向隔离目录并排除已有 webui venv。不清理用户已有缓存。

完整日志在 `{系统临时目录}/astrmai_learning_mining_fix_20261012_3d594f0e69e84730929f8244c815a8c9/`。

| 命令（pytest 均额外使用上述隔离参数） | 结果 | 退出码 / 日志 |
|---|---|---|
| `python -B -m pytest tests/unit/learning/test_learning_gap_coverage.py tests/unit/learning/test_learning_pipeline_checkpoints.py tests/unit/learning/test_gray_release_contracts.py tests/test_learning_refactor.py -q` | 改动前 66 passed / 1 warning | 0 / baseline.log |
| `python -B -m pytest tests/unit/learning/test_mining_release_gate_settlement.py -q` | Red：6 failed；确证字符串 .get 崩溃及空结果错误成功 | 1 / red.log |
| 同上 | 初始 Green：6 passed / 1 warning | 0 / green-initial.log |
| 同上（扩展回归首次） | 42 passed / 4 failed；新测试将真实 ledger 的 running 误写为 claimed，已更正测试，不修改业务断言 | 1 / green-expanded.log |
| `python -B -m pytest tests/unit/learning tests/regression/learning/test_background_schedule_contracts.py tests/test_learning_refactor.py tests/test_learning_event_collaboration_refactor.py -q` | 600 passed / 1 warning | 0 / learning-regression.log |
| `python -B -m pytest tests/unit/learning tests/regression/learning tests/test_learning_refactor.py tests/test_learning_event_collaboration_refactor.py -q` | 最终 616 passed / 1 warning；包含 48 个新增回归 | 0 / final-learning.log |
| `python -B -m pytest tests/regression/architecture/test_import_boundaries_refactor.py tests/regression/architecture/test_memory_runtime_boundaries_refactor.py tests/regression/architecture/test_shared_test_support_refactor.py tests/regression/lifecycle/test_background_lifecycle_contracts.py tests/test_background_task_budget.py -q` | 54 passed / 1 warning | 0 / architecture-lifecycle.log |
| `python -B -m pytest tests/unit/infrastructure -q` | 75 passed / 1 warning | 0 / infrastructure.log |
| `python -m compileall -q -x '[/\\]venv[/\\]' astrmai` | 源码编译通过，字节码在隔离目录 | 0 / compile.log |
| `python -I -B -c "import sys; sys.path.insert(0, '<开发根>'); import main; print('main_import_ok')"`（隔离 cwd） | main_import_ok | 0 / main-import.log |
| `python -I -B scripts/probe_host_import_shape.py <开发根>`（脚本绝对路径、隔离 cwd） | data.plugins.astrmai.astrmai 命名空间及真实 speaker block 调用通过；顶层 astrmai 不可见 | 0 / host-shape-probe.log |
| `ruff check --no-cache --select E9,F63,F7,F82 <本轮三个 Python 文件>` | All checks passed | 0 |
| 技能 `check_python` 检查本轮三个 Python 文件 | 0 error / 5 warning；测试导入检查告警，业务文件无新增告警 | 0 / skill-scoped.log |
| `python -B scripts/check_plugin.py <开发根>` | 5 error / 1053 warning；不能称全仓通过 | 1 / skill-full.log |
| `git diff --check` | 通过 | 0 |

全仓技能错误集合与上一轮 `skill-source.log` 完全相同：四个 `.agent/cloud_exports` 历史 BOM 语法错误，及 `tests/test_local_acceptance_network_isolation.py` 故意验证 requests 的 E104。相较基线 1050 warning，仅新增测试文件的 astrmai / config / pytest 三个 W120，不是运行依赖新增，不应因此向插件 requirements 添加自身包或测试框架。

新增回放覆盖两个入口的入口拒绝与解禁、claim 后开关翻转与同批次重试、空结果、非法顶层/内层/混合包装、成功/全失败/部分成功/依赖 blocked、真实成功返回合同、临时结算故障及恢复、stale token、provider/时间戳/未尝试结算/成功结算期间取消、完成后结算故障、报告失败防止重复挖矿。assert 的是 SQLite lease、retry_count、时间戳、processed IDs、报告与 provider 次数，不仅是方法调用。

## 独立验收与范围

独立验收须复跑两个入口，重点检查 gate reason、无 snapshot/provider/claim 的拒绝路径、claim 后拒绝的 rejected 结算、时间恢复、同批次即时恢复、取消传播、持久 settlement 重放、旧 token 所有权及已完成批次不会重挖。需审查成功结果仍按 pipeline 直接返回，不能恢复服务器建议的错误操作数。

真实 Python 3.12.13 / AstrBot 4.26.4 上的学习行为、真实 provider、数据库 cursor 与 pattern/jargon 持久化尚未由本轮验证。临时 SQLite ledger 回放与离线宿主命名探针不等于服务器验收。关闭生产学习 kill switch 不在本轮授权内。

开发数据库 SHA-256 在本轮前后均为 `392dd9210bb67669c174a883801f9004790efcfc947553768bcc2b96432d541d`。已有大量未提交用户改动保留；本轮新增差异仅本说明、一个测试文件、一个既有测试的三行局部修改及 evolution_manager 的局部修复。

```yaml
handoff_status: ready_for_independent_revalidation
production_directory_modified: false
production_config_modified: false
production_database_modified: false
development_database_modified: false
candidate_modified: false
committed: false
pushed: false
deployed: false
real_host_learning_verified: false
```
