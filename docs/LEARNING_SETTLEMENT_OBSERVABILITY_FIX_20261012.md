# 学习挖矿结算可见性修复：开发审计与独立复验交接

日期：2026-10-12。状态：`ready_for_independent_revalidation`，不是独立验收通过或生产放行。

对应计划：`plan/learning/MINING_SETTLEMENT_PREDEPLOY_PLAN_20261012.md`。执行阶段 A 的局部修复和本地回归，阶段 B 完成只读发布差异清单；没有提交、推送、构建正式新候选、覆盖生产包、连接服务器或解除学习开关。发布合同测试自身的临时候选不作为交付包。

## 根因与改动

上一轮返回值形状修复保留。成功路径仍直接返回 expression/jargon 的结果映射，不返回 `_last_mining_outcomes` 的 aggregate。此次只修复结算结果被丢弃及其观测边界：

1. `_settle_mining_result` 原先丢弃 `settle_task_lease` 返回值。现在返回 `True`（主表 CAS 确认提交）、`False`（主表未确认），没有 ledger/lease 时返回 `None`（不适用）。不能把 `False` 等同于已持久入队、存储故障或 stale。
2. 诊断复用 `_last_mining_outcomes`，同 run 保留 pipeline 报告，不同 run 不携带旧 pipeline。结算诊断含 group_id/run_id/task_id/status/reason/attempt_returned/committed/recorded_at，可由 `describe_learning_runtime()['mining']['last_outcomes']` 读取。只保存最近 20 组；更新当前组后再淘汰旧组，避免较大的历史缓存删掉当前诊断。
3. backlog processed/skipped 记录增加 `settlement_committed`，不改变原业务分类。入口 gate 提前拒绝时不调用结算，保持原 skipped 合同。
4. `settlement_finished` 改名为 `settlement_attempt_returned`，表示调用返回，而非主表已提交。False 不触发重复挖矿、成功时间回滚或真实业务失败冷却。
5. 新增 unconfirmed warning 明确主表与持久入队均未确认，不承诺 durable pending。warning 自身的普通异常只写附加 logging_error_type，不替换业务结果；不吞 CancelledError 或其他 BaseException。
6. Red 发现结算返回后取消会再次使用旧 token 结算：主表已 succeeded，但第二次返回 False、保存 superseded pending 并覆盖原诊断。两个取消入口现在只对尚未返回的结算做原有 shield 恢复；已返回时直接继续传播取消，保留原提交结果/重放意图。
7. 独立复验发现 `_last_mining_outcomes` 同时使用裸 `group_id` 和 `pipeline:group_id` 顶层键，且分别按 20/40 条裁剪。现在四个写入点统一复用群级更新入口：顶层只保留最近 20 个群，pipeline 状态进入该群的 `pipelines` 子映射；同 run 合并，different run 不继承旧 pipeline 或 settlement。

实际改动文件：

- `astrmai/learning/evolution_manager.py`：仅私有结算方法、两个消费者的标记/报告与取消分支。文件开头已有 TYPE_CHECKING 差异保留，不是本轮新增。
- `tests/unit/learning/test_mining_release_gate_settlement.py`：在原结算回归上增加群级键空间、20 群容量、pipeline/settlement 共存及跨 run 隔离测试；专项总计 96。
- `docs/LEARNING_MINING_RELEASE_GATE_FIX_20261012.md`：局部更正持久化保证的过强表述，指向本交接。
- 本交接及计划中的本轮执行记录。

公共 ledger、proactive scheduler、配置/schema、冷却数值、provider、cursor 和 pattern/jargon 写入逻辑没有修改。已有未提交文件全部保留，不全文件重写或格式化。

## Red / Green 与实际状态

所有 lease 测试回读临时 SQLite 主表/pending 表，并检查时间、retry_count 与 provider 次数。消息/元数据为隔离内存适配器，provider 为替身，不宣称真实完整挖矿落库。

| 场景 | 实际断言 |
|---|---|
| succeeded / rejected / retry_wait 正常提交 | committed=True；对应主表状态、清空 token；成功不回滚时间，其他恢复时间；retry_wait 冷却落库 |
| 主表故障、pending 成功 | committed=False；主表仍 running；保存原合法意图；恢复后结算成功且不重跑 provider |
| pending 满或入队也失败 | committed=False；pending 为空；警告不声称已持久；不把已完成业务转换成失败重挖 |
| stale token | committed=False，pending 为 superseded；新持有者 token/lease/status 不变 |
| 无 ledger / lease | committed=None，不虚构主表提交 |
| 旧测试/嵌入适配器无 task_id | 不要求新接口，task_id 空字符串；已有 finish 返回值仍可观测 |
| 结果未知时取消 | 原 cancelled 重试语义，CancelledError 传播 |
| 结果确定、结算 await 中取消 | shield 保持原合法意图，取消传播 |
| 结算返回 True/False 后取消 | 不再次结算，不用第二次 False 覆盖第一次 True；原 pending 不被改成 superseded |
| 日志/报告失败 | 诊断失败不替换业务结果，不重复结算、不重挖已处理 IDs |
| scheduler recovery | 真实 ProactiveTask 的 start、周期 loop、stop 重放 succeeded/rejected/retry_wait；周期测试只替换模块本地 sleep，断言 30 秒合同，不污染全局 asyncio |

三轮确定性 Red：返回合同/两个入口 8 failed；历史缓存边界 1 failed；结算返回后取消 1 failed。均保留原失败日志，随后 Green，不使用 skip/xfail 或改测试顺序绕过。

独立复验 P2-A 的 Red 为 3 failed：同群产生 3 个顶层键、20 群形成 22 个混合条目、pipeline 新 run 后群级入口仍暴露旧 run。局部统一写入后聚焦 5 passed，专项 96 passed，学习单元与回归 654 passed。

## 命令与本地门禁

环境：Python 3.11.4 / AstrBot 4.14.6。目标 Python 3.12 / AstrBot 4.26.4 未在本轮访问。证据目录位于系统临时目录下 `astrmai_settlement_observation_20261012_a81591cec9d84fa782c223d73604b7bf`；每条命令有 JSON（完整参数、cwd、退出码、耗时、隔离根）及完整日志。

pytest 使用 `-B -p no:cacheprovider --basetemp <本轮独立子目录>`；TEMP/TMP/ASTRBOT_ROOT 指向隔离根。compileall 的缓存写隔离目录，排除既有 webui venv。不删除用户缓存。

| 命令（pytest 的隔离参数如上） | 结果 / 退出码 | 证据 |
|---|---|---|
| `python -B -m pytest tests/unit/learning/test_mining_release_gate_settlement.py tests/regression/learning/test_background_schedule_contracts.py -q` | 开工 120 passed / 4 warning；0 | baseline |
| 专项 `-k 'settlement_returns_primary_commit_result or entry_exposes_unconfirmed_commit'` | 8 failed / 48 deselected；1 | red |
| 专项全文件，初始 Green | 56 passed；0 | green-initial |
| 专项扩展，含 pending 故障 | 80 passed；0 | green-expanded |
| 专项增加 recovery 与所有权 | 89 passed；0 | green-complete |
| 专项 `-k keeps_latest_diagnostic` | 1 failed / 89 deselected；1 | red-bounded-report |
| 专项 `-k cancellation_after_commit`（重命名前） | 1 failed / 91 deselected；1 | red-postcommit-cancel |
| `python -B -m pytest tests/unit/learning tests/regression/learning tests/test_learning_refactor.py tests/test_learning_event_collaboration_refactor.py -q` | 最终 661 passed / 4 warning；0 | learning-complete |
| 架构三文件 + lifecycle + budget | 54 passed / 4 warning；0 | architecture-lifecycle |
| `python -B -m pytest tests/unit/infrastructure -q` | 75 passed / 4 warning；0 | infrastructure |
| `python -B -m pytest tests/test_release_candidate_contract.py -q` | 4 passed / 1 warning；0 | release-contract-final |
| 上述架构/lifecycle/budget/infrastructure/release 同进程最终合跑 | 133 passed / 4 warning；0 | surrounding-complete |
| `python -m compileall -q -x '[/\\]venv[/\\]' astrmai` | 0；缓存隔离 | compile-complete |
| 隔离 cwd：`python -I -B -c "import sys; sys.path.insert(0, '<开发根>'); import main; print('main_import_ok')"` | main_import_ok；0 | main-import |
| 隔离 cwd：`python -I -B <证据根>/host_learning_probe.py <开发根>` | nested namespace + speaker block + parser + 真 SQLite settlement；0 | host-shape-complete |
| 隔离 Ruff 0.17.0：`ruff check --no-cache --select E9,F63,F7,F82` 两个修改 Python 文件 | All checks passed；0 | lint-complete |
| 技能原 check_python 检查两个修改文件 | 0 error / 3 warning；0 | skill-scoped-complete |
| 技能根：`python -B scripts/check_plugin.py <源码根>` | 5 error / 1053 warning；1，错误集合与开工相同 | skill-baseline / skill-final |
| `git diff --check` | 0 | diff-check |

最终相关非重复测试计数为 661 + 133 = 794；专项 93 已包含在学习回归内，不重复加总。未跑全仓所有业务测试，不称全仓通过。

### 必须披露的中间失败

- 完整学习回归首次为 1 failed / 657 passed：既有适配器 lease 没有 task_id，诊断直接访问抛 AttributeError。改为局部可选身份读取，新增 legacy 回归，不放宽原断言。
- 发布合同首次 1 failed / 3 passed：隔离 PYTHONPYCACHEPREFIX 拼接候选路径超过 Windows 长度上限。改用独立短路径，仅调整证据 runner，不改产品/测试；最终 4 passed。
- 学习 host probe 首次所有行为断言通过，但临时 SQLite 建表连接未显式关闭，Windows 清理时 WinError 32。临时脚本 finally 关闭连接后 rc=0；不能把第一次 rc=1 记成探针通过。
- 两个 Python runtime 都未安装 Ruff；随后仅向本轮临时工具目录安装 Ruff，不改项目 requirements 或全局环境，最终 lint rc=0。
- Red 日志已完整保存，但初次证据 runner 用 GBK 输出解码替换字符触发 UnicodeEncodeError；修正本轮临时 runner 输出编码，不修改测试或业务。red.json 保留测试自身 rc=1。

全仓技能 5 个既有错误：四个 `.agent/cloud_exports` 历史 BOM 语法错误，以及网络隔离测试故意验证 requests 的 E104。未修改检查器或删除历史文件隐藏错误。候选静态门禁需在后续新正式候选上重新执行，不沿用旧包通过数字。

## 发布基线与禁止越界

`release-baseline-inventory.json` 逐路径对照 HEAD/blob、当前工作树和现有 420 文件包：HEAD 与包有 178 个原始字节差异，其中 155 个仅 BOM/CRLF、23 个真实内容差异。工作树与现有包规范化后的运行内容只有 evolution_manager 不同，这是本轮补丁，旧候选没有自动包含它。

HEAD 保持 `0a46dcb88edc5be501942756082b6e1a461094d5`。完整历史发布基线的审查/提交和干净 checkout 重建未完成，不把“读了清单”标成“可复现”。需要精确提交授权，不能 git add -A。该项与独立复验、正式候选、宿主 staging 分别保留待办。

kill switch 开启时阻断 DISCOVERY。后续代码部署验收应确认真实 backlog 扫描、正确 skipped reason、无新挖矿，不要求 mined group 日志；已有 pending 的正常重放允许。部署代码与关闭 kill switch 不能绑成一次变更。

存储完全不可用或 pending 满时，当前 bool 接口无法保证意图持久化；此次保证可观测且不主动重挖，不承诺跨重启 exactly-once。真实 provider/cursor/pattern/jargon 全链路、QQ 手机投递及既有回执缺口均未在本轮验证。

## 独立复验清单

1. 不复用开发 helper，真实 SQLite 主表/pending 回读 True/False/None，尤其 pending 同时失败。
2. 两个入口的成功/拒绝/真实失败；检查已完成成功不会回滚、空结果不产生成功间隔。
3. 结算 await 中取消与返回后取消分开，True/False 后不重复结算；取消继续传播。
4. 旧 token 不修改新 lease；实际 scheduler start/worker/stop 恢复，不仅调用 ledger。
5. 诊断有界、同 run 原 pipeline 保留、不同 run 无陈旧结果；报告/日志错误不改变业务。
6. nested package 实际调用、无新增运行期绝对 astrmai 导入；源/数据库/生产包边界复核。

```yaml
implementation_completed: true
local_regression_passed: true
handoff_status: ready_for_independent_revalidation
independent_revalidation_completed_this_round: false
release_baseline_committed_this_round: false
clean_checkout_rebuild_verified: false
new_formal_candidate_built: false
host_validation_completed: false
production_directory_modified: false
production_config_modified: false
production_database_modified: false
development_database_modified: false
learning_kill_switch_changed: false
committed: false
pushed: false
deployed: false
```
