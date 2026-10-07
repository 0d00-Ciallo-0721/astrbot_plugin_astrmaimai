# QQ 引用视觉误判与重复正文修复：审计及交接

本文位于可追踪的 `docs/`，避免 `/plan/` 的既有忽略规则遗漏交接；未修改忽略规则。

```yaml
status: ready_for_independent_revalidation
independent_validation_passed: false
production_directory_modified: false
server_modified: false
database_modified: false
configuration_modified: false
existing_candidate_modified: false
candidate_built: false
real_qq_messages_sent: false
git_committed: false
git_pushed: false
deployed: false
```

## 1. 依据及环境

读取了 AstrBot 插件开发技能及 `references/events-filters-hooks.md`，以及
`AstrMai_QQ引用视觉误判与重复回复_全量只读审查报告_2026-10-06.md`。
启动时开发工作区干净；基线 commit 为 `16cc463`。本轮仅修改开发工作区。
本机 Python 3.11，已安装 AstrBot **4.14.6**；报告目标为 AstrBot **4.26.4** / NapCat **4.10.9**。
没有目标宿主源码或目标平台执行证据，不将本机离线回放表述为目标平台验证。

启动指纹与报告部署指纹一致（SHA-256）：

| 文件 | 启动 SHA-256 |
| --- | --- |
| `sensors.py` | `0e87f4d5b6a5af80a9588015136ae1f7aba63a9ed9852b7cf439a57b369cc8fd` |
| `reply_service.py` | `796098cdea42011eb4a2d0b2c051584a1b6a62ab2c7f86a0f30df863b25b4b32` |
| `qq_action_dispatcher.py` | `b754f692e268bf2866ad0c8568a4a794ef7a41a3ce8775f36223e3c79d4ebf68` |
| `turn_outcome.py` | `5c8bab206ef0498673373e6322ba8fda8d4a61207e5dc153b33b4cf9b0d4a684` |

修复后 `executor.py` 仍为 `6100bf15ccc1a2d982428508c05926132f93be4525cb43d8e507ca64ffebac88`，
`turn_outcome.py` 仍为上述启动指纹，未修改视觉失败产品策略或终态合同。

## 2. 修改范围与合同

| 文件 | 修改原因 |
| --- | --- |
| `astrmai/conversation/ingress/sensors.py` | Reply ID 本身不作为图片证据；未知目标最多查询一条，单次等待 1 秒，仅确认图片后建立候选。 |
| `astrmai/multimodal/napcat_image_resolver.py` | 将现有图片引用提取器改为 classmethod，供入站有限探测复用，不实例化 resolver 或写入缓存。 |
| `astrmai/conversation/execution/reply_artifact_builder.py` | 有效引用动作成为唯一正文、单段带 Reply 发送；沿用 send claim，明确处理未发送取消及不确定投递。 |
| `astrmai/conversation/execution/reply_service.py` | 支持 quote-only，引用沿用正文输出所有权、回执和历史提交，不再经 QQ 动作链另发正文。 |
| `astrmai/conversation/execution/qq_action_dispatcher.py` | 校验并选择最后一条有效引用动作、记录投递结果；commit 仅委托引用，其他非正文动作保留。 |
| `tests/test_sensors_refactor.py` | 更正无图片证据仍建候选的旧断言，补纯文本、真图、API 失败、探测上限及取消。 |
| `tests/test_reply_service_refactor.py` | 同一 trace/attempt/generation 的唯一正文、失败、取消、实际 history/claim 回归。 |
| `tests/test_qq_action_dispatcher.py` | 引用委托合同；使用非正文 like 动作继续验证动作 ledger 防重试。 |
| `tests/test_pfc_tools_chat_extensions_refactor.py` | 更正独立 dispatcher 再发引用正文的旧断言；保留 target/text，断言零额外 API 发送。 |
| `tests/helpers/qq_reply_real_components_replay.py` | 安装版本的真实 Reply/At/Plain/Image 组件离线回放，共 12 个场景。 |
| `tests/integration/test_qq_reply_real_components.py` | 隔离解释器运行真实组件回放，避免已有测试替身改变真实组件来源。 |
| 本文档 | Red/Green、验证结果、限制及独立验收清单。 |

### A：图片真实性

已加载纯文本 Reply 保留引用、@ 和文字，图片候选为零，不调用视觉 provider。
Reply 内嵌 Image、当前消息 Image 保持识别。Reply 同时缺少 chain 与 message_str 才作为未知目标；
@Bot 场景最多对一个目标做 `get_msg` 有界探测，不下载图片。
API 拒绝、异常、超时或未知类型不被归类为已确认图片，也不生成识图失败。
外部 `CancelledError` 继续传播。探测结果记录在 `astrmai_reply_image_probe`。

旧测试“无 chain 仍建刷新候选”锁定了误判，已明确改为“Reply ID 不是图片证据”。
保留图像组件和 OneBot image 段的原有解析合同，没有新增 NapCat 原生 picElem 兼容层。

### B：唯一正文发送权

有效 `quote_reply_action` 的 target 与 text 在 artifact 准备阶段进入现有 `QUOTE_REPLY` 路线，
覆盖同回合普通正文，并合成一个正文段。QQ dispatcher 不再 transport 引用正文。
无引用时普通回复/分段规则不变；无效引用保留普通回复；poke 等非正文动作可独立提交。
引用路线禁用可选 TTS，避免另一个正文载体。

引用即使普通 send-claim 开关关闭，仍需通过现有 TurnOutcome 正文所有权。
不是用文本 hash 消重，也未另建第二套发送器。

| 阶段/结果 | 所有权及回执/历史 |
| --- | --- |
| prepare 取消 | 释放本次正文 ownership，零 transport、零 history。 |
| 已取得 send claim、transport 前取消 | 标记本次 send_key failed，释放正文 ownership；不清理其他 key。 |
| Host 返回 False | failed、未 sent、无 history、不走 QQ 引用补发。 |
| Host 返回 True | 确认发送，True 不作为平台 message ID。 |
| transport 异常/发送中取消 | 投递不确定，保留非可重发 send claim，并关闭同回合 fallback；不伪造 sent/history。 |
| 成功后父任务在 settlement 取消 | shielded 自有结算任务继续，提交已发送正文的历史，再传播取消；retry 不再发送。 |
| settlement 抛异常 | 已发送正文/history 保留，不恢复为可重发请求。 |
| settlement 任务自身取消 | 记录 settlement_cancelled，不能伪报 committed；保留已发送 history 并传播取消。 |
| 无效引用 + 普通分段部分发送 | 只提交实际成功段，不额外发送合并引用正文。 |

stale、shutdown、target expired/cross-group 等既有保护保留。

## 3. Red / Green 证据

以下早期记录继承自同一任务的执行摘要；其中 A Red 与两个追加边界 Red 的完整筛选参数未在摘要保留，
不把重构出的命令冒充原始命令。对应失败用例及最终可重跑命令均保留在测试树中。

| 闭环 | Red（exit=1） | Green（exit=0） |
| --- | --- | --- |
| 基线四个专项文件 | 无新增失败，132 passed / 75 warnings | 基线，不代表修复验收。 |
| A 图片候选真实性 | sensors 新用例 4 failed / 24 passed | sensors + resolver 40 passed。 |
| B 唯一引用正文 | 4 failed / 1 passed / 76 deselected | reply service + dispatcher 100 passed / 84 warnings。 |
| transport 取消及失败 | 3 failed / 81 deselected | 定向 8 项通过。 |
| prepare / pretransport 所有权边界 | 3 failed / 3 passed / 84 deselected；其中一项是测试把真实 claimed 状态误写 pending，已更正，另两项为实际泄漏 | 后续 quote 专项通过。 |
| settlement 自身取消误报 committed | 1 failed / 92 deselected / 2 warnings | quote 专项 18 passed / 75 deselected / 31 warnings。 |
| 扩大回归发现旧独立引用 transport 断言 | 1 failed / 419 passed / 107 warnings，失败在 PFC dispatcher quote 测试 | 更新唯一正文合同后 420 passed / 107 warnings。 |

可确认的 B Red 命令：

```powershell
python -B -m pytest -p no:cacheprovider tests/test_reply_service_refactor.py -k 'queued_quote or invalid_quote_target or quote_body_and_non_body' -q
```

最后一个取消观测 Red 及 Green：

```powershell
python -B -m pytest -p no:cacheprovider tests/test_reply_service_refactor.py -k cancelled_settlement -q
python -B -m pytest -p no:cacheprovider tests/test_reply_service_refactor.py -k quote -q
```

旧 dispatcher 合同更正后局部验证：

```powershell
python -B -m pytest -p no:cacheprovider tests/test_pfc_tools_chat_extensions_refactor.py -k dispatcher_delegates_quote -q
```

结果 exit=0，1 passed / 75 deselected。

## 4. 真实组件离线链路及证据边界

回放加载本机实际 AstrBot 组件，不安装 AstrBot 替身。
链路为 `PreFilters -> AttentionGate.PerceptionBuilder -> ConcurrentExecutor -> ReplyService -> mock Context.send_message -> ChatRuntimeCoordinator / GroupDialogueStore`。
模型、get_msg、Host 发送均是离线替身；图片是隔离目录的本地 8×8 PNG，无外部请求、真实 QQ 消息或生产数据。
引用-only/False 两个场景直接走 ReplyService；其余经过 executor。
候选存在时测试手工设置 `astrmai_final_vision_target`，未运行完整 message_entry/facade/planner 调度。

| 场景 | perception 图片数 | 视觉调用 | Host 正文调用 | send claim / history |
| --- | --- | --- | --- | --- |
| loaded_text：真实 Reply.chain 为 Plain | 0 | 0 | 1 | committed / 1 |
| fetched_text：get_msg 为纯文本，picElem=None | 0 | 0 | 1 | committed / 1 |
| unknown：目标类型未知 | 0 | 0 | 1 | committed / 1 |
| api_failure：get_msg 异常 | 0 | 0 | 1 | committed / 1 |
| embedded_image：Reply 内嵌真实 Image | 1 | 1 | 1 | committed / 1 |
| fetched_image：get_msg 确认 image 段 | 1 | 1 | 1 | committed / 1 |
| inline_image：当前消息真实 Image | 1 | 1 | 1 | committed / 1 |
| quote：普通正文 + 有效引用动作 | 0 | 0 | 1，Reply(88)+引用正文 | committed / 1 |
| quote_only：只有引用正文 | 0 | 0 | 1，Reply(88)+引用正文 | committed / 1 |
| quote_false：Host 明确拒绝 | 0 | 0 | 1 次尝试，无成功 | failed / 0 |
| image_timeout_text：真图解析失败 + 独立文本 | 1 | 0 | 1，正常文本 | committed / 1 |
| image_timeout_dependent：真图解析失败 + 依赖图片问题 | 1 | 0 | 1，现行失败提示 | committed / 1 |

纯文本/未知/API 失败没有固定识图失败提示；成功真图有一次视觉调用。
所有引用场景 QQ `call_action` 零发送，唯一发送携带 Reply(88) 与引用正文。
确认发送时 history 与实际 body、receipt 一致，True 回执的 outbound_message_ids 为空。

真实组件测试在最终综合回归后，仅增强了 raw replyElement 形态和 perception 图片数断言，
再独立运行下列命令：exit=0，1 passed（内部 12 场景），7.62s。

```powershell
python -B -m pytest -p no:cacheprovider tests/integration/test_qq_reply_real_components.py -q
```

## 5. 二阶 timeout_fallback 评估

报告 §4.4 的策略冲突不能用本轮候选修复直接宣称全部解决。
已单独用确有图片的失败回放证明：可独立回答的文本走 `continue_text_only`；
依赖图片的问题维持 `notify_failure`。没有把所有视觉失败统一忽略，也没有修改 executor。
混合文本中“是否可独立回答”的覆盖仍有限；更多误分类需要真实事件证据及独立产品决策。
该项作为后续评估保留，不阻塞 A/B 的独立复验，也不意味着生产已获发布授权。

## 6. 最终验证命令与结果

工作目录为开发根目录，shell 为 PowerShell 7。综合命令先出现上述旧合同失败，更新后重跑：

```powershell
python -B -m pytest -p no:cacheprovider tests/test_sensors_refactor.py tests/unit/multimodal/test_napcat_image_resolver.py tests/test_reply_service_refactor.py tests/test_qq_action_dispatcher.py tests/test_executor_vision_refactor.py tests/test_turn_outcome_refactor.py tests/test_executor_refactor.py tests/test_lifecycle_shutdown_regression.py tests/regression/architecture/test_reply_commit_outbox.py tests/regression/architecture/test_committed_reply_writeback.py tests/test_pfc_tools_chat_extensions_refactor.py tests/integration/test_qq_reply_real_components.py -q
```

exit=0，**420 passed，107 warnings，51.60s**。警告包括既有 pkg_resources 与 legacy runtime extras 弃用。

编译：

```powershell
python -m compileall -q astrmai
```

首次 PYTHONPYCACHEPREFIX 在长的系统临时路径，三处内置 webui venv 文件的 pyc 写入超过 Windows 路径限制，exit=1。
换用短的隔离临时前缀后同命令 exit=0；无源码改动，不向工作区写入字节码。

main smoke（仅 cwd 为新建临时目录；DEV_ROOT 是开发目录实参）：

```powershell
python -B -c "import sys; sys.path.insert(0,sys.argv[1]); import main; print('main_import_ok')" DEV_ROOT
```

exit=0，输出 `main_import_ok`；默认运行文件只可能生成在隔离 cwd，没有在真实 data/ 执行。

技能根目录静态命令：

```powershell
python -B scripts/check_plugin.py DEV_ROOT
```

exit=1，**15 error / 1019 warning，2048 Python 文件**，不能称为全仓通过。
错误为既有 U+FEFF 文件（含 cloud_exports 副本、think_level_policy/v2_store/plugin_pages 及测试）
与 manual/group_trace_audit、test_local_acceptance_network_isolation 的 requests 引用。
扫描还包含既有 webui/venv、cloud_exports 和测试文件，不是候选包验证。
错误文件不在本轮修改列表，未顺手改动。

另外从同一技能脚本调用 `check_python(path, root)`，以 git diff 与 untracked 列出的 11 个 Python 文件为输入：
exit=0，**0 error / 10 warning**。警告均 W120，包含项目模块 astrmai/tests 与既有 sqlalchemy 导入检查。
该结果仅表示修改范围的 Python 静态规则通过，不能替代全插件 scan 或独立验收。

`git diff --check` 已 exit=0，只有 Git LF/CRLF 转换提示；最终交接后再次核对。
本轮未删除既有缓存。编译/import 临时目录与回放图片不在工作树；未生成发布包。

## 7. 独立复验清单与剩余限制

1. 重跑第 6 节综合回归和真实组件回放，核对结果而非仅采信开发自测。
2. 用目标 AstrBot 4.26.4 源码核对 Reply 组件、Context.send_message 的 False/True/None/其他返回合同；目前只具备本机版本组件证据。
3. 接入完整 message_entry/facade/planner，验证 Reply/@/文字和选图链，不能把测试手动设置 final target 当作 planner 实测。
4. 核对同一 trace/attempt/generation/send_key 的唯一正文、quote-only、无 quote、无效 target、普通部分发送及非正文动作。
5. 逐窗口取消：prepare、取得 claim 后、transport、成功 receipt 后 settlement；验证不确定投递不重试、不误报 committed。
6. 补多次取消、owner_registry shutdown 与任意 history 提交点取消的综合回放；本轮未覆盖所有历史 await 的取消窗口。
7. 在目标宿主核对 OneBot get_msg payload、多个 Reply/图片、超时及实际延迟；本轮上限为一次查询/1 秒等待，尚无真实平台延迟证据。
8. 原生 NapCat picElem-only 且无 OneBot image/真实 Image 的真图适配没有扩大，需按目标实际 payload 另核。
9. 两个 Bug 的目标平台投递、QQ 客户端引用呈现及长期行为仍需另行验证，必须独立复验和部署授权后安排。

当前仅交回独立验收：**ready_for_independent_revalidation**。不构建/替换候选包，不同步生产。
