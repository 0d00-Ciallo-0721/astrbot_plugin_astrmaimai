# 黑话与表达风格学习阶段审计（2026-08-12）

状态：**只读审计完成，尚未实施修复** ｜ 范围：学习输入过滤、候选提取、LLM 增强、去重、审核与持久化

## 审计目标

本次审计回答以下问题：

1. 一个词语为什么会成为黑话候选。
2. 黑话含义是依据什么证据推测出来的。
3. 群聊说话风格是怎样被发现和总结的。
4. 哪些结论来自确定性代码，哪些结论来自 LLM。
5. 当前质量门、审核与去重能否防止错误学习。

本文只审计“学习阶段”。学习结果如何被动注入聊天、如何由模型主动查询，见同目录的 `LEARNING-INJECTION-AUDIT-20260812.md`。

## 总体结论

当前两条学习链已经按职责分开：

- **黑话学习**：代码先发现重复词语，LLM 再判断它是否具有特殊含义并生成释义。
- **表达学习**：代码先发现重复口癖、语气词、句尾、符号、句式和节奏，LLM 再判断它是否是可迁移的群聊表达习惯并生成摘要。

共同的输入治理是正确的：两条链默认只学习群聊中的真人文本，排除 Bot、自身消息、私聊、命令、插件回执、撤回消息、工具消息和传输载荷。学习结果也不会直接成为在线知识，而是先进入待审状态。

但两条链的证据强度不同：

- 表达候选的大部分事实由确定性规则产生，LLM 主要负责分类和解释，原始表达不会被模型替换，整体更稳。
- 黑话候选本身由规则发现，但“含义、场景、别名”主要由 LLM 根据少量孤立例句推测。模型看不到完整对话关系，因此存在“解释听起来合理，但并非群里真实含义”的风险。

## 共同输入过滤

两条学习管道都经过 `LearningInputPolicy`：

```text
MessageLog
→ 校验发送者和 provenance
→ 排除 Bot / self / system / tool
→ 排除私聊、撤回、命令、卡片、媒体占位和协议载荷
→ 删除引用块、HTML、CQ 码、链接和 @ 标记
→ 输出可学习的群聊真人文本
```

源码证据：

- `astrmai/learning/mining/learning_input_policy.py:20`
- `astrmai/learning/mining/learning_input_policy.py:52`
- `astrmai/learning/mining/learning_input_policy.py:90`

这层能否正确工作，依赖 MessageLog 的 `role`、`provenance`、`is_bot`、`is_recalled` 和 scope 字段准确。若入口错误地把 Bot 回声标为真人消息，学习层无法通过文本内容完全补救。

## 黑话学习链

### 完整流程

```text
群聊真人文本
→ 正则提取词语片段
→ 排除常用词、昵称、链接碎片、协议词和表达口癖
→ 统计出现消息数、上下文数和说话人数
→ 至少在多个不同消息中重复出现
→ 排除已学习、待审、拒绝和处理中词条
→ LLM 推测是否为黑话、含义、场景、别名和置信度
→ review_pending 持久化到全局黑话域
→ 自动审核或人工审核
→ approved 后成为 active 全局黑话
```

### 候选词如何产生

候选提取不是 LLM 自由阅读整群聊天，而是确定性正则扫描：

- 英文、数字和下划线组合：长度 2～24。
- 连续中文片段：长度 2～8，后续通常再过滤超过 6 字的普通长片段。
- 同一词在同一条消息内只计一次。
- 默认至少出现在 2 条不同消息中。
- 记录不同发送者数量、最多 4 条完整消息样例和来源消息 ID。

源码证据：

- `astrmai/learning/mining/jargon_candidate_extractor.py:54`
- `astrmai/learning/mining/jargon_candidate_extractor.py:71`
- `astrmai/learning/mining/jargon_candidate_extractor.py:113`
- `astrmai/learning/mining/jargon_candidate_extractor.py:159`
- `astrmai/learning/mining/jargon_candidate_extractor.py:162`

候选路由会把明显口癖、语气词、重复符号和句尾词送往表达学习，把普通交互词直接拒绝；未被识别的剩余词默认更倾向黑话。这能覆盖新词，但也会让普通名词和半截中文短语进入黑话候选。

源码证据：`astrmai/learning/mining/candidate_router.py`。

### 黑话含义如何推测

LLM 对每个候选实际看到的主要信息是：

- 候选词本身；
- 最多 3 条独立消息样例；
-原始上下文摘要；
- 出现次数。

提示词要求模型判断：

- 是否存在区别于字面意义的稳定特殊含义；
- 是否属于黑话，而非口癖、普通词、专有名词或命令；
- `meaning`、`scene`、`aliases`、`confidence`、`semantic_novelty` 等字段。

源码证据：

- `astrmai/learning/mining/jargon_enricher.py:33`
- `astrmai/learning/mining/jargon_enricher.py:44`
- `astrmai/learning/mining/jargon_enricher.py:115`

模型**没有看到**：

- 样例消息的前后聊天轮次；
- 某句话回复了谁；
- 谁提出该词、谁解释过它；
- 说话人身份和稳定关系；
- 不同群中相同词语的对照含义；
- 反例或明确否定该释义的消息。

因此当前释义不是事实检索，而是“基于少量孤立样例的受限语义推测”。例如某词只有在上一句问题中才具备特殊含义，模型看不到上一句时，很容易根据常识补出一个看似合理的错误解释。

### 黑话接受条件

Enricher 解析后，只要模型返回：

- `is_jargon=true`；
- `term_type=jargon`；
- `semantic_novelty=true`；
- `meaning` 非空；

该候选就可进入后续持久化。当前没有额外的确定性“释义必须被样例蕴含”校验，也没有硬性的最低置信度门槛。

模型返回的 `examples` 还会与真实样例合并。这意味着模型补写的例句可能进入候选证据，后二次审核无法明确区分哪些是聊天原文、哪些是模型生成文本。

源码证据：

- `astrmai/learning/mining/jargon_enricher.py:128`
- `astrmai/learning/mining/jargon_enricher.py:130`

### 黑话去重和全局语义

黑话保存到 `GLOBAL_JARGON_SESSION_ID`，来源群写入 metadata。指纹只基于规范化词语，不包含群聊和含义：

```text
jargon_fingerprint(normalized_term)
```

源码证据：

- `astrmai/learning/dedup/normalization.py:8`
- `astrmai/learning/dedup/normalization.py:57`
- `astrmai/learning/evolution_manager.py:814`
- `astrmai/learning/evolution_manager.py:884`

这符合“黑话作为全局知识库”的产品要求，但当前等价于“一词一义”。如果同一个词在两个群中具有不同含义，系统会合并来源和样例，而不是建立两个独立语义或按语境消歧。

### 黑话二次审核

自动审核再次调用 LLM，根据词语、当前释义、场景、最多 5 条样例、出现次数和置信度，输出：

- `approved`：进入 active；
- `rejected`：保持不可用；
- `revision_needed`：进入人工待审，并可重写释义。

源码证据：`astrmai/learning/review/jargon_auto_check_task.py`。

二次审核可以挡住明显普通词和垃圾解释，但它没有重新读取完整对话，而且输入样例可能已混入第一次模型生成的例句。因此它不是独立事实核验，只是对同一份压缩材料进行第二次语义判断。

## 表达风格学习链

### 完整流程

```text
群聊真人文本
→ 规则识别重复整句、口癖、语气词、句尾、符号、叠字和短句节奏
→ 聚合当前群内的不同消息证据
→ 达到最少不同消息数
→ LLM 判断是否属于可迁移的共同说话方式
→ 保留原始表达，生成 summary / style / situation
→ review_pending 或 pending_human 持久化到当前群
→ 自动审核或人工审核
→ approved 后成为当前群 active 表达模式
```

### 规则层学什么

规则层识别以下类型：

| 类型 | 说明 |
|---|---|
| `catchphrase` | 重复出现的完整口癖或短句 |
| `particle` | 语气词 |
| `ending` | 句末习惯 |
| `symbol` | 颜文字、重复标点或符号组合 |
| `repetition` | 叠字、拉长音等重复形式 |
| `sentence_pattern` | 重复句式片段 |
| `rhythm` | 短句比例和短时间连续发送节奏 |

源码证据：

- `astrmai/learning/mining/expression_candidate_extractor.py:118`
- `astrmai/learning/mining/expression_candidate_extractor.py:165`
- `astrmai/learning/mining/expression_candidate_extractor.py:197`
- `astrmai/learning/mining/expression_candidate_extractor.py:265`

默认至少需要 3 条不同消息证据，配置位于 `config.py:208`。证据按群聚合，不保存个人表达档案，符合“学习群聊共同风格、注入当前群”的产品边界。

### 说话风格如何总结

表达 Enricher 实际看到：

- 规则提取出的原始表达；
- 确定性推断的习惯类型；
- 初步 situation 和 style；
- 出现次数和不同消息数；
- 最多 4 条原始消息样例；
- 当前群作用域。

LLM 的任务不是从整群聊天自由创作一份人格，而是判断这个候选是否属于可迁移的共同表达习惯，并生成：

- `summary`：对这种表达习惯的简短描述；
- `style`：语气或形式特征；
- `situation`：适用场景；
- `habit_type`：规范化类别；
- `confidence`：模型置信度。

源码证据：

- `astrmai/learning/mining/expression_pattern_enricher.py:77`
- `astrmai/learning/mining/expression_pattern_enricher.py:95`
- `astrmai/learning/mining/expression_pattern_enricher.py:136`

原始 `expression` 由规则层保留，模型不能替换它。例如：

```text
真实消息多次以“呀”结尾
→ 规则候选：expression=“呀”, habit_type=ending
→ LLM 摘要：句末使用“呀”，语气更轻松亲近
```

因此表达风格总结是“规则发现事实 + 模型解释语义”的混合流程，不是纯模型归纳。

### 节奏候选的特殊问题

节奏规则会统计当前批次所有可学习群消息：

- 短消息占比；
- 相邻消息时间差；
- 是否出现数秒内连续补充。

满足条件后生成：

- “偏好短句连发，常在数秒内连续补充”；或
- “偏好简短单句回复”。

源码证据：`astrmai/learning/mining/expression_candidate_extractor.py:422`。

当前相邻消息没有先按发送者或回复链分组。在高活跃群里，甲、乙、丙三个人依次发送短消息，也可能被统计为群体“短句连发”风格。由于产品要求按群学习，群级聚合本身正确；但“群体风格”应来自多个用户各自重复表现，而不应直接等同于群流量高。

### 表达审核和去重

表达指纹由群作用域、习惯类型和规范化表达组成。同一个群中相同表达不会重复创建，不同群可以拥有各自模式。

源码证据：`astrmai/memory/services/expression_pattern_service.py:51`。

自动挖掘不能直接把候选设为 active；模型即使输出 approved，也会被规范化为待审。后续自动审核根据表达、场景、风格、样例和次数决定批准、拒绝或转人工。

源码证据：

- `astrmai/learning/evolution_manager.py:700`
- `astrmai/learning/review/expression_auto_check_task.py`

与黑话相同，模型补写的 `content_samples` 会合并到真实样例，存在证据来源混淆。此外，已 rejected 的表达也参与候选预加载和去重，后续即使出现更强的新证据，通常仍会被抑制，不会自动重新学习。

## 风险清单

### P1：黑话释义缺少对话关系证据

候选可靠不等于释义可靠。当前 LLM 看不到前后轮、回复目标和别人对该词的解释，无法区分真实群义与常识补全。

建议：为每个候选构造小型证据窗口，包含命中消息前后 1～2 条、回复目标、发送者匿名 ID、时间和同词不同场景；要求释义逐条引用 `evidence_message_ids`。

### P1：节奏学习混淆群活跃度与表达习惯

多人同时刷短句可能被总结成“喜欢连续补充”。

建议：先按发送者计算连续短句，再以多个贡献者的独立行为汇总为群风格；至少要求两个用户分别命中，或明确标记为 `group_flow_pattern`，不要伪装成个人式回复节奏。

### P1：全局黑话缺少多义词机制

同词在不同群的不同含义会被一词一义指纹合并。

建议：保持全局词典，但将实体拆成 `term + senses[]`；每个 sense 保存释义指纹、来源群和证据，检索时先精确命中词，再结合当前语境选择 sense。

### P2：模型生成样例与事实证据混合

第一次模型补写的例句可能被第二次审核当作真实证据。

建议：严格拆分 `source_examples` 与 `model_examples`。审核、置信度和晋升只允许引用 source；model examples 只能作为展示建议，不能计入证据数。

### P2：没有确定性语义支持度门槛

模型只要声称是黑话并给出非空释义即可进入待审。

建议：增加最低语境一致率、至少两个独立上下文、至少一个非模型来源解释信号；低证据候选只进入人工待审，不进入自动审核批准路径。

### P2：拒绝项会长期抑制重新学习

旧弱证据导致 rejected 后，新证据也可能因为 dedup 直接跳过。

建议：拒绝记录保留冷却期和证据版本。当新消息数、独立群数或语义簇发生显著变化时，允许创建 revision，而不是创建重复实体。

## 推荐修复顺序

1. 证据与模型生成内容分栏，禁止模型样例参与审核证据。
2. 黑话候选增加前后轮、回复目标和 message_id 引用。
3. 修正节奏统计，区分单人行为、多人共同习惯和群活跃度。
4. 为全局黑话增加多义 sense 和语境消歧。
5. 增加 rejected 候选的证据版本与重新评估机制。
6. 补充学习阶段结构化 trace，记录候选来源数、证据窗口、模型判断、审核结果和去重原因。

## 建议新增回归场景

1. 同一黑话在完整回复链中含义明确，孤立消息含义不明确：必须根据回复链释义。
2. 相同词在两个群含义不同：应建立两个 sense，不得静默覆盖。
3. LLM 返回虚构 examples：不得计入 source evidence。
4. 三个不同用户在两秒内各发一句短消息：不得判为单人连续补充风格。
5. 两个以上用户分别长期使用相同句尾：可以形成群级 ending 风格。
6. rejected 候选获得新的高质量证据：允许进入 revision，不创建重复 active 记录。
7. Bot 回声和插件回执混入 MessageLog：不得进入任何学习候选。

## 本轮验证

执行：

```text
python -m pytest \
  tests/unit/learning/test_expression_style_learning.py \
  tests/unit/learning/test_expression_enrichment_pipeline.py \
  tests/unit/learning/test_jargon_pipeline_migrated.py \
  tests/unit/learning/test_learning_input_policy.py \
  tests/regression/learning/test_round9_learning_review.py -q
```

结果：`54 passed`，仅有 3 条第三方依赖弃用警告。

测试证明当前学习链按上述设计运行，但尚未覆盖以下关键风险：黑话回复链语义约束、模型样例与事实证据隔离、多义词、多人高活跃导致的节奏误判，以及 rejected 候选的新证据重评。

## 最终判断

当前学习系统不是完全依靠模型乱猜：候选发现、真人消息过滤、去重和待审治理都已有明确代码边界。表达学习尤其接近可靠的“规则发现 + 模型解释”。

真正需要优先治理的是黑话语义证据。当前模型能判断“这个词看起来像什么意思”，但还不能证明“群里的人确实用它表达这个意思”。后续优化应先增强证据结构和可追溯性，再调整提示词或模型；仅更换模型无法解决上下文缺失。
