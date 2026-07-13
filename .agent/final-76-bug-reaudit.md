# AstrMai 上线前 76 条 Bug 最终复审报告

> 复审日期：2026-07-03
> 项目：AstrMai - AstrBot 事件驱动对话插件
> 路径：项目仓库根目录
> 复审方式：只读源码、调用链、配置约束、现有测试契约与最小运行时验证

## 1. 执行摘要

原审计报告将 76 项全部列为 Bug，并给出 `1 CRITICAL + 7 HIGH + 28 MEDIUM + 40 LOW` 的严重度分布。逐项复审后，结论如下：

| 最终结论 | 数量 | 占比 |
|---|---:|---:|
| 确认缺陷 | 20 | 26.3% |
| 部分成立或定性错误 | 8 | 10.5% |
| 误判或设计行为 | 24 | 31.6% |
| 不存在或当前调用链不可达 | 24 | 31.6% |
| 合计 | 76 | 100% |

核心结论：

1. 原报告中的 `CRITICAL` 项不构成 CRITICAL。原 #1 是有限触发窗口问题，且当前工作区已经修正为凌晨 3-5 点。
2. 原报告真正保留为 HIGH 的只有 #3。
3. 复审发现一个原报告未准确识别的 HIGH 根因：Reflector 在权重更新异常时可能连续删除两个批次。
4. #44 的问题比原描述严重：SQLite FTS5 BM25 的排序和归一化方向整体相反，应从 LOW 上调为 MEDIUM。
5. #50 与 #13、#66 与 #65、#72 与 #32 属于重复根因，不应作为独立修复项重复统计。
6. 76 个报告项中约 63% 不是可直接修复的真实运行时缺陷。

## 2. 修订后的风险清单

### 2.1 HIGH

| 项目 | 文件 | 结论 |
|---|---|---|
| #3 | `astrmai/infrastructure/gateway/gateway_call.py` | 模型成功返回后，usage、trace 或 benchmark 记录异常会落入失败分支，导致成功结果丢失并错误扣减模型健康分 |
| 新发现 | `astrmai/learning/review/reflector.py` | LLM 成功后先删除当前批次；后续权重更新异常时，外层 `except` 再删除队首，误删下一批 |

### 2.2 MEDIUM

| 项目 | 说明 |
|---|---|
| #2 | 表达模式写入被过滤时，后续查询可能返回 `None` 并触发 `.id` 异常 |
| #6 | 反思任务在网络或处理异常时主动丢弃批次 |
| #12 | `detected_facts` 路径无法由当前 DreamAgent 产出，原报告给出的方括号原因不准确 |
| #13 / #50 | 侮辱词使用裸子串匹配，误伤“摇滚”“passbook”等正常文本 |
| #15 | 最老 lane lock 长时间占用时，锁字典可持续增长 |
| #23 | 昵称 JSON 解析失败后，完整模型原始输出会被当作昵称 |
| #24 | async 路径中的同步 DB fallback 会阻塞事件循环 |
| #27 | `PluginFacade` 没有 `config` 属性，消息入口配置的 fallback text 实际不会生效 |
| #34 | 热配置刷新部分失败后，运行时可能同时持有新旧配置 |
| #44 | FTS5 BM25 按错误方向排序并反向归一化，影响记忆召回相关性 |

### 2.3 LOW

建议处理的 LOW 根因：

- #10：Dream legacy 查询吞掉 DB 异常并伪装为“未找到”。
- #14：群回复等待信息混用 `monotonic()` 和 `time.time()`。
- #16：database review 的后台保存任务未纳入生命周期管理。
- #20：目标字段接受 list/dict 并通过 `str()` 转为目标文本。
- #40：记忆注入 trace 持久化失败完全不可见。
- #42：极小剩余预算会产生纯 `...` 记忆行。
- #53：未知画像类别全部落入 `speech_style_points`。
- #65：active state/profile 获取异常会中断本轮全部衰减处理。
- #67：单个文件系统或 psutil 调用失败会令 dashboard snapshot 整体失败。

## 3. 原 #1-36 逐项结论

| # | 最终判定 | 修订级别 | 复审说明 |
|---:|---|---|---|
| 1 | 部分成立，当前已修 | LOW/MEDIUM | 原窗口只有 hour=3，但仍有一小时，不是“几乎从不触发”，更不是 CRITICAL |
| 2 | 确认 | MEDIUM | write 返回空字符串后，dedup 查询可能仍为 `None` |
| 3 | 确认 | HIGH | 成功结果后的辅助记录异常会进入失败处理 |
| 4 | 部分成立 | 无直接级别 | 每个模型只试一次，但会切换其他模型；Tool Loop 重试还存在重复副作用风险 |
| 5 | 部分成立 | 技术债 | 重复代码属实，但不是 HIGH 运行时 Bug |
| 6 | 确认 | MEDIUM | 异常批次会被主动删除；同时存在更严重的双批次误删问题 |
| 7 | 不存在 | - | AstrBot message component 按契约返回类或 `None` |
| 8 | 部分成立 | 技术债 | 两个方法确实未使用，但死代码不是 HIGH Bug |
| 9 | 不存在 | - | gather 中五个 worker 均自行捕获 `Exception` |
| 10 | 确认 | LOW | DB 错误被吞并返回 `None` |
| 11 | 误判 | - | detected fact 本身不是 canonical memory，没有 source memory ID 可回写 |
| 12 | 部分成立 | MEDIUM | 路径确实不可工作，但 DreamAgent 根本不生成 `[fact]`，不是中英文括号不匹配 |
| 13 | 确认 | MEDIUM | 裸子串匹配会误判“摇滚” |
| 14 | 确认 | LOW | remaining seconds 使用了错误的时钟基准 |
| 15 | 确认 | MEDIUM | 最老锁持续占用时无法有效清理 |
| 16 | 确认 | LOW | fire-and-forget 保存任务未被生命周期追踪 |
| 17 | 不存在 | - | cooldown 只面向限流、配额和权限；超时由重试及健康分处理 |
| 18 | 误判 | - | 默认目标是有日志的明确降级行为 |
| 19 | 不存在 | - | 两个 attention window 列表只由同一组件同步修改 |
| 20 | 确认 | LOW | 非字符串 goal 会被接受并字符串化 |
| 21 | 误判 | - | 队列裁剪已有 warning，不是静默行为 |
| 22 | 不存在 | - | 调用方明确检查 `prompt is None` |
| 23 | 确认 | MEDIUM | 解析失败后原始响应直接作为 preferred nickname |
| 24 | 确认 | MEDIUM | 同步 DB fallback 位于 async 方法中 |
| 25 | 不存在 | - | 有效 GIF/PIL frame 不会产生零高度帧 |
| 26 | 不存在 | - | `AstrMessageEvent` 保证所需属性和方法 |
| 27 | 确认 | MEDIUM | `facade.config` 不存在，导致配置 fallback 被忽略 |
| 28 | 不存在 | - | `dream_interval_min` 受 Pydantic `ge=1` 约束 |
| 29 | 误判 | - | 无 amount 调用是旧版接口兼容 fallback |
| 30 | 不存在 | - | `meme_probability` 是受约束的整数配置 |
| 31 | 误判 | - | 无事件循环时同步执行是该 helper 的明确兜底语义 |
| 32 | 误判 | - | ComputerAgent 无工具时返回明确的 `SUBAGENT_DECLINE` |
| 33 | 不存在 | - | 当前代码已有 `run_at.timestamp() if run_at else None` |
| 34 | 确认 | MEDIUM | 热配置部分成功后不回滚 |
| 35 | 不存在 | - | 生产 `PersistenceManager` 始终初始化 `cache_dir` |
| 36 | 不存在 | - | 实际宿主对象允许这些兼容属性写入 |

## 4. 原 #37-76 逐项结论

| # | 最终判定 | 修订级别 | 复审说明 |
|---:|---|---|---|
| 37 | 不存在 | - | 非纯数字平台 ID 保留为字符串是跨平台兼容行为，负数不必强转 int |
| 38 | 误判 | - | AstrMai 设计上允许注意力系统选择回复未被 @ 的群消息 |
| 39 | 不存在 | - | 状态修改不跨 await，事件循环内同步 `len()` 不构成竞态 |
| 40 | 确认 | LOW | `_persist_trace` 使用裸 `except Exception: pass` |
| 41 | 不存在 | - | 两个生产入口均构造 float timestamp |
| 42 | 确认 | LOW | budget 剩余 1-2 字符时可生成纯 `...` 行 |
| 43 | 误判 | - | epoch 0 被视为未访问并回退到更新时间符合当前语义 |
| 44 | 确认，且上调 | MEDIUM | `bm25()` 越小越相关，但查询使用 DESC，归一化也按相反方向 |
| 45 | 误判 | - | 失败已有 warning；是否升级属于告警策略 |
| 46 | 误判 | - | 50 以下最多保留少量池，实际 pool name 来自有限任务族 |
| 47 | 误判 | - | 属性文档明确说明是 non-blocking approximate |
| 48 | 不存在 | - | worker 创建失败会向上传播；正常创建的 task 会被加入追踪集合 |
| 49 | 误判 | - | 未来一年阈值是宽松策略，不是计算错误 |
| 50 | 确认但重复 | MEDIUM | 与 #13 是同一个裸子串匹配根因 |
| 51 | 误判 | - | 高信任衰减更慢是注释明确说明的关系模型设计 |
| 52 | 误判 | - | 乱码占位符用于匹配旧数据中的编码损坏值 |
| 53 | 确认 | LOW | 未识别类别统一落入 speech-style 桶 |
| 54 | 不存在 | - | 当前代码已有 `recovery_anchor > 0` 守卫 |
| 55 | 不存在 | - | 当前代码使用 `str(result or "")`，None 不会变成 `"None"` |
| 56 | 误判 | - | sync-only API 主动拒绝事件循环调用，并提供 async API 和测试 |
| 57 | 误判 | - | bool 可数值化是 Python 类型语义，接口未禁止 |
| 58 | 误判 | - | 可选表情目录缺失时跳过属于正常降级 |
| 59 | 不存在 | - | AstrMessageEvent 保证 UMO；send 异常也会由 await 向上传播 |
| 60 | 不存在 | - | 该解析器只从 `@filter.command("work")` 入口调用 |
| 61 | 误判 | - | 空 reason 没有违反任何现有决策不变量 |
| 62 | 误判 | - | 顶层 config 导入符合当前 AstrBot 插件加载结构 |
| 63 | 误判 | - | 无 UMO 时的输入是 group_id，审核请求本身面向群组 |
| 64 | 不存在 | - | `AttentionGate.__init__` 已初始化 `_proactive_dispatching` |
| 65 | 确认 | LOW | state/profile 获取异常会跳过本轮其余衰减流程 |
| 66 | 部分成立，重复 | LOW | 与 #65 是同一问题 |
| 67 | 确认 | LOW | dashboard 系统调用无局部降级保护 |
| 68 | 部分成立，当前不可达 | LOW/潜在 | 方法确实忽略 MemoryEvent where，但当前没有该调用 |
| 69 | 误判 | - | 列名严格受 `SLICE_FIELDS` 白名单约束 |
| 70 | 不存在 | - | agent 受 FunctionTool `call` 方法契约约束 |
| 71 | 部分成立 | LOW/诊断 | 首次 TypeError 会被兼容重试覆盖，但第二次 TypeError 会正常向上传播 |
| 72 | 误判，重复 | - | 与 #32 相同，是明确的优雅降级 |
| 73 | 不存在 | - | 两个后台循环都在每次迭代内捕获异常并继续 |
| 74 | 误判 | - | asyncio Task 按对象身份可哈希，不是偶然 CPython 细节 |
| 75 | 不存在 | - | Protocol、docstring 和唯一调用方均明确使用 `async for` |
| 76 | 误判 | - | 初始化失败会记录 degraded 状态并明确输出 `vision disabled` |

## 5. 新发现与扩展结论

### 5.1 Reflector 双批次误删

位置：`astrmai/learning/review/reflector.py`

执行顺序：

1. LLM 返回并成功解析评分。
2. 当前批次从 `_pending_reflections` 中删除。
3. 逐条更新表达权重。
4. 若任一权重更新抛异常，进入外层 `except`。
5. `except` 再次从当前队首删除 `len(batch)` 条，误删下一批。

这比原 #6 描述的“失败批次被丢弃”更严重，应单独作为 HIGH 修复。

### 5.2 BM25 排序方向反转

位置：`astrmai/memory/retrieval/bm25.py`

SQLite FTS5 `bm25()` 返回负数，数值越小表示匹配越相关。当前实现存在两层反转：

1. SQL 使用 `ORDER BY score DESC`，把较不相关结果放在前面。
2. 多结果归一化使用 `(score - min) / range`，把较大原始分数映射为更高相关度。

本地 SQLite 最小验证：

```text
apple apple apple -> -1.4193548387096774e-06
apple banana      -> -1e-06
```

按相关性应以前者优先，但 `DESC` 会首先返回后者。

## 6. 推荐修复顺序

### Wave 1：结果正确性与数据安全

1. #3：隔离模型成功结果与 usage/trace/benchmark 记录异常。
2. Reflector 双批次误删：只确认删除原始批次一次，权重更新失败不得删除后续数据。
3. #44：修正 BM25 SQL 排序和归一化方向。

### Wave 2：可触发的 MEDIUM

1. #2：使用 write 返回值或安全处理 dedup 查询为空。
2. #6：为反思失败设计有限重试或 dead-letter 状态。
3. #12：明确 detected facts 的真实生产协议。
4. #13/#50：按词边界、中文上下文或规则表匹配侮辱词。
5. #15、#23、#24、#27、#34。

### Wave 3：低风险稳健性

处理 #10、#14、#16、#20、#40、#42、#53、#65、#67；#68 和 #71 可随相关模块维护处理。

## 7. 验证记录与边界

- 复审基于当前工作区源码，而不是仅根据原审计描述判断。
- 已核对实际调用方、Pydantic 配置约束、AstrBot 事件契约和现有测试。
- 使用本地 SQLite FTS5 最小查询验证了 #44 的排序语义。
- 本轮复审未修改业务代码。
- 状态文件记录的基线为 `990 passed, 1 skipped` 和 `compileall` 通过；本轮只读复审未重新执行全量 pytest。
- 当前工作区本身存在未提交修改；#1 在当前工作区已经修正，因此报告同时保留“原始问题成立”和“当前已修”的状态。

## 8. 最终判定

AstrMai 不存在原报告所称的 CRITICAL 上线阻断项，但仍有两个应在上线前处理的 HIGH 根因：

1. Gateway 成功调用被辅助记录异常反向标记为失败。
2. Reflector 权重更新异常导致双批次误删。

同时，BM25 排序方向反转会系统性降低记忆召回质量，建议与 HIGH 项同一批处理。其余确认缺陷可以按 MEDIUM、LOW 分波次修复，不建议对误判项、设计行为或当前不可达项进行防御性大改。
