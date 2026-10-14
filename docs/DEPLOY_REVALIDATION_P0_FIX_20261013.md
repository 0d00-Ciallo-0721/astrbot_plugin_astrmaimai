# 部署复验 P0 修复与交接

日期：2026-10-13。独立复验补记已确认 P0 修复有效，状态推进到 `ready_for_target_host_validation`；不等于生产部署已验收或授权。

输入：独立报告 `C:/tmp/lr_reval_5ce40451/REVAL_REPORT_5ce40451.md`。旧 ZIP SHA-256 `09a235390ff2048220772d1a1cd74844829fb2ffc8b210e3a1901eb73b7ff1ce` 含初始化 P0，不能部署。本轮未改写它、旧 manifest 或旧验证记录；源码修复与回归完成后另建全新候选。

## 修复范围

1. `astrmai/app/bootstrap.py` 撤掉传入 `ContextCompactionEngine` 的无效 `group_lifecycle` 参数。引擎既有接口没有使用该依赖，不新增仅为吞掉参数的接口，不改变压缩策略或 provider 守卫。
2. `astrmai/app/runtime_context.py` 把字段从 `InteractionServices` 移至真实 `PluginRuntimeContext`。后者 **slots=True**，原来并非动态挂属性侥幸可用，而是 bootstrap.build 直接 AttributeError。增加 dataclass 字段和 registry 实例断言。
3. `astrmai/proactive/dispatcher.py` 生命周期查询 Exception 返回拒绝及 `group_lifecycle_unavailable`，不查活动、不排 synthetic event；CancelledError 不捕获。
4. `astrmai/proactive/review_dispatcher.py` 确认 retired 后复用 `ReflectTracker.clear_chat` 删除该群审核请求并 continue，不 requeue，不刷 degraded；读取失败仍保留现有重排路径且不发送。实际 tracker 用例验证其他群保留、重复扫描无退群请求复活。
5. `tests/unit/learning/test_gray_release_contracts.py` 真实迁移用例引用 `LATEST_ARCHITECTURE_SCHEMA_VERSION`（当前 177）。预检实现接受显式 expected_schema_version，本身没有写死 175；保留测试中合法的历史版本快照/manifest 合同，不批量替换数字。
6. `tests/regression/state/test_decay_service_migrated.py` 替件接受 `min_interval_sec` 并断言 86400，保留失败重试和成功节流断言。本轮不改变 decay 的异常处理产品合同。
7. `scripts/probe_host_import_shape.py` 在 nested namespace 下创建真实 slotted runtime、赋值 registry，并实际调用启用压缩的 `_build_context_compaction_service` 和真实构造器；不打开数据库、不调用模型、不启动后台任务。`tests/test_release_candidate_contract.py` 验证探针返回这些事实，避免仅 import 冒充初始化成功。

未改变 schema、配置、超时、并发、视觉静默策略、notice UMO 算法。保留此前工作树改动。

## Red / Green

基线命令：

```text
python -B -m pytest -p no:cacheprovider tests/unit/conversation/test_context_runtime_wiring.py tests/unit/learning/test_gray_release_contracts.py tests/regression/state/test_decay_service_migrated.py -q --tb=short
```

exit 1：5 failed / 48 passed。分别为 bootstrap_build_attaches_chat_loop_kernel 的 AttributeError、两条真实压缩构造的 TypeError、schema 177 != 175、decay 替件调用数 0 != 2。与独立报告一致。

新增生命周期回放 Red：`python -B -m pytest -p no:cacheprovider tests/regression/lifecycle/test_group_lifecycle_fence.py -q --tb=short`，exit 1，2 failed / 22 passed。真实 tracker 的 retired 请求仍残留；主动读取失败后错误继续到 attention_gate_unavailable。最初日志替件误用了 AsyncMock，产生未 await 告警，已改为同步 Mock；不改业务断言。

发布探针 Red：`python -B -m pytest -p no:cacheprovider tests/test_release_candidate_contract.py::ReleaseCandidateContractTests::test_candidate_speaker_prompt_uses_host_namespace -q --tb=short`，exit 1，1 failed：旧探针没有 runtime_lifecycle_field_declared 证据。增强探针后发布初始化组转绿。

专项 Green：原 5 红 + 生命周期组 77 passed；加压缩回归后 114 passed；发布合同 + 初始化 + 栅栏 50 passed。均 exit 0。相关组重叠，不能相加。

## 完整验证命令、退出码与可复核产物

工件根：`C:/tmp/astrmai_reval_fix_20261013_7ac819`。各 pytest 命令完整如下，均 exit 0：

```text
python -B -m pytest -p no:cacheprovider tests/unit -q --tb=short --junitxml=C:/tmp/astrmai_reval_fix_20261013_7ac819/unit.xml
python -B -m pytest -p no:cacheprovider tests/regression -q --tb=short --junitxml=C:/tmp/astrmai_reval_fix_20261013_7ac819/regression.xml
python -B -m pytest -p no:cacheprovider tests/integration tests/original_ported -q --tb=short --junitxml=C:/tmp/astrmai_reval_fix_20261013_7ac819/integration_ported.xml
$topLevelTests = @(Get-ChildItem -LiteralPath tests -Filter 'test_*.py' -File | Sort-Object Name | ForEach-Object { $_.FullName })
python -B -m pytest -p no:cacheprovider @topLevelTests -q --tb=short --junitxml=C:/tmp/astrmai_reval_fix_20261013_7ac819/top_level.xml
python -B -m pytest -p no:cacheprovider tests/unit/conversation/test_context_runtime_wiring.py tests/unit/learning/test_gray_release_contracts.py tests/regression/state/test_decay_service_migrated.py tests/regression/lifecycle/test_group_lifecycle_fence.py tests/unit/conversation/test_group_dialogue_store_and_compaction.py -q --tb=short --junitxml=C:/tmp/astrmai_reval_fix_20261013_7ac819/targeted.xml
python -B -m pytest -p no:cacheprovider tests/test_release_candidate_contract.py tests/unit/conversation/test_context_runtime_wiring.py tests/regression/lifecycle/test_group_lifecycle_fence.py -q --tb=short --junitxml=C:/tmp/astrmai_reval_fix_20261013_7ac819/release_initialization.xml
```

| 分块 | passed | skipped | failed/error | 秒 |
|---|---:|---:|---:|---:|
| unit | 1674 | 0 | 0 | 235.59 |
| regression | 681 | 0 | 0 | 73.00 |
| integration + original_ported | 197 | 1 | 0 | 30.30 |
| tests 根目录 | 1684 | 0 | 0 | 145.28 |
| 专项（重叠） | 114 | 0 | 0 | 8.22 |
| 发布初始化（重叠） | 50 | 0 | 0 | 59.83 |

以上是本报告首次落盘前的四个分块实测：**4236 passed / 1 skipped / 0 failed / 0 error**，共收集 4237 条。不是单进程全仓结果，也不能作为文档落盘后的最终树全绿声明。随后写入本报告时引入两处本机路径，未重跑路径卫生门禁，是开发交接遗漏。独立复验对最终树收集 4237 条，出现 2 failed / 1 skipped：一条由本文路径引起，另一条是压力测试墙钟断言；单独重跑三次通过的证据来自验收方。本次只修正文档并重跑相关门禁，不把局部重跑冒称为新一轮全仓通过。

其他检查：

```text
$env:PYTHONPYCACHEPREFIX='C:/tmp/astrmai_reval_fix_20261013_7ac819/compile_cache'
python -m compileall -q astrmai scripts/probe_host_import_shape.py
python -I -B scripts/probe_host_import_shape.py <repo-root>
git diff --check
```

均 exit 0。编译缓存只写新隔离目录。probe 在隔离 ASTRBOT_ROOT 导入 main 并调用真实压缩构造；本机 **Python 3.11.4 / AstrBot 4.14.6**，不是目标宿主运行证明。

技能根运行 `python -B scripts/check_plugin.py <repo-root>`：exit 1，**5 error / 1062 warning**。五个 error 与前轮集合一致：`.agent/cloud_exports` 四个历史 BOM 文件以及 `tests/test_local_acceptance_network_isolation.py` 的 requests；没有冒称全仓静态通过。对本轮十个 Python 文件调用同检查器 check_python：exit 0，**0 error / 8 warning**（测试导入提示）。完整输出保存 static_repository.log / static_modified.log。

## 再复验清单与边界

- 新候选目录 `C:/tmp/astrmai_deploy_20261013_p0fix_7ac819/candidate`，构建前目标不存在；ZIP `astrmai_candidate_20261013_p0fix.zip`、配套 manifest 与 VERIFICATION.json 均在该根目录。422 文件、7,753,337 bytes；相对退回候选只修改 bootstrap/runtime_context/proactive dispatcher/review_dispatcher 四个运行文件，无新增/删除。
- content SHA-256 `0931529c7d812c4cbd3787c880649d6cee98dd2a72455adb3639a86f5521ed4a`；stable digest `51799b4617489e3a1cfccf4d2924e43d3b0cad758de86538fcb7a85c292398f5`；manifest SHA-256 `0c8cc4a55609168a94cd66c1ebb3d9c187af9830f0f2b865419030f445569df2`；ZIP SHA-256 `6db7a359b425708b441ead827653ffe01574125b6d7d6d9c1befe27b9d6e7de6`。
- 构建命令 `python scripts/build_release_candidate.py C:/tmp/astrmai_deploy_20261013_p0fix_7ac819/candidate --baseline-manifest C:/tmp/astrmai_deploy_20261013_5ce40451/candidate.candidate_manifest.json`，exit 0。`python -B C:/tmp/astrmai_deploy_20261013_p0fix_7ac819/verify_and_archive.py`，exit 0；调用的完整命令/cwd/退出码已入 VERIFICATION.json，包含全部 pytest selector 与 JUnit 路径。
- 候选目录/manifest validator、源码对应字节、ZIP 422 条目哈希及大小均通过；技能候选 **0 error / 19 warning**。候选副本隔离 compileall/main import/nested 压缩构造探针通过，正式候选及 manifest 检查后未变。构建验证期间源码 git status 完全一致；本地生产树验证前后逐文件 hash 一致，未覆盖。测试/文档不包含于运行候选。
- 不能继续部署旧 09a23539 ZIP。新包独立复验见 `C:/tmp/lr_reval_7ac819/REVAL_ADDENDUM_7ac819.md`：422/422 字节与工作树一致、AST 实参漂移门无违规、目标 4.26.4 / 3.12.13 真实压缩构造成功；判定允许进入目标宿主验证。尚未获得生产运行/投递验收或部署授权，不将单个构造成功写为完整插件启动验证。
- 目标宿主下一步验证完整初始化及生命周期入口/出口；独立验收方对真实构造与 namespace 导入的证据已经承接，不重复列为完全未验证。
- 真机采集 notice 与普通消息 UMO，逐字符核对。缺失真机样本，尚不能宣布栅栏端到端验收。
- 独立报告的生产库副本 175→177 迁移证据属于验收方，本轮没有连接服务器或复核真实库。本轮临时迁移测试通过，未改 schema。
- 本轮未处理非阻断的 classifier 词表分叉、delivery classifier 冗余 retcode 条件、异常重包装分类脆弱性；应独立定向验证，不能扩大此次 P0 修补为未经评审的分类重构。
- 不把探针声明为全插件真实启动：它覆盖实际压缩 builder 和 runtime 字段，完整 host 生命周期、QQ 投递仍待目标实例复核。
- 压缩 provider 的无参守卫只检查进程级状态。撤掉不受支持的构造参数没有移除原有按群能力，因为引擎原本未接受或使用该依赖；入站拒绝和清对话态不等于取消所有在途压缩请求。退群时已排队/进行中的压缩是资源边界待验项，不宣称已完全栅栏化。
- 压力测试稳定性作为独立后续项：用同步屏障、进度和取消状态验证正确性，延迟单列测量；本轮不改硬阈值、不 skip/xfail、不改业务调度。

## 独立复验后的文档收尾（2026-10-13）

仅局部修正本文两处本机路径、测试时序说明及交接状态；未改业务源码、测试断言、候选字节或原 VERIFICATION.json。原始验证记录继续保留为历史证据，本节和独立补记说明其时序限制，不覆盖历史记录。

修正后实际执行：

```text
python -B -m pytest -p no:cacheprovider tests/regression/architecture/test_import_boundaries_refactor.py -q --tb=short --junitxml=C:/tmp/astrmai_doc_closure_20261013_7ac819/path_hygiene.xml
python -B -m pytest -p no:cacheprovider tests/test_attention_gate_refactor.py -k test_concurrent_process_event_pressure_shutdown_reload_resume -q --tb=short --junitxml=C:/tmp/astrmai_doc_closure_20261013_7ac819/pressure_recheck.xml
```

路径/架构组 **5 passed / 1 warning，exit 0**；原样压力用例 **1 passed / 106 deselected / 1 warning，exit 0**。压力单跑通过不证明负载敏感性已消除，亦不证明整仓最终树全绿。本节落盘后仍须重复路径组（JUnit 使用 path_hygiene_final.xml）及 git diff --check，不再在最终门禁后写入未经检查的仓库文档。

候选按 manifest 再次回读 **422 文件，sha/size mismatch=0**，未新增文件；部署 ZIP、manifest、原 VERIFICATION.json 保持独立复验身份。没有重打包、同步生产、连接服务器、调整配置/kill switch、提交或推送。

```yaml
handoff_status: ready_for_target_host_validation
independent_revalidation_completed: true
deployment_status: pending_target_host_validation
production_deployment_authorized: false
source_p0_fixed_locally: true
pre_document_pytest_blocks_passed: true
final_tree_full_repository_green_claimed: false
full_repository_skill_check_passed: false
new_formal_candidate_built: true
candidate_verified_locally: true
old_candidate_deployable: false
target_host_revalidated_this_round: false
production_directory_modified: false
production_config_modified: false
server_accessed: false
kill_switch_changed: false
committed: false
pushed: false
deployed: false
```
