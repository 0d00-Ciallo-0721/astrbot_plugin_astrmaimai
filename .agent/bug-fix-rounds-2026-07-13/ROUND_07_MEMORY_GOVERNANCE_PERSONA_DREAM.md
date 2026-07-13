# Round 07：记忆治理、Persona 与 Dream

数量：9。依赖：Round 06。

完成标准：迁移可续跑，写入配置生效，人工替换稳定，Persona/Dream 生命周期和协议闭环，损坏文本不再进入新数据。

## R07-01 / P1：每日 canonical memory decay 缺参数且失败后推进 24h 标记
- 原始 ID：`AM-MEM-06-03`, `AM-LP-10-05`；验证级别：A。
- 主文件：`astrmai/proactive/decay_service.py`, `astrmai/memory/services/memory_engine.py`。
- 修复边界：传当前 decay_rate，仅成功后推进 `_last_memory_decay`，失败保留重试。
- 回归目标：首次执行不 TypeError；失败后下一维护周期可重试；成功后 24h 节流。

## R07-02 / P2：三类 legacy import 只处理 1000 条就永久标记完成
- 原始 ID：`AM-MEM-06-05`；验证级别：B（条件型）。
- 主文件：`astrmai/memory/services/memory_engine.py`。
- 修复边界：分页/游标直至 exhausted，再写完成标记；重复运行幂等。
- 回归目标：1001+ memory events/jargon/expression 全量迁移，无重复。

## R07-03 / P2：`min_memory_confidence` 未进入统一写入决策
- 原始 ID：`AM-MEM-06-06`；验证级别：B。
- 主文件：`config.py`, `_conf_schema.json`, `astrmai/memory/services/memory_write_service.py`。
- 修复边界：统一 write service 读取 live config；0 关闭门控，非零拒绝低 confidence，并返回可解释结果。
- 回归目标：阈值边界上下写入行为明确，所有写入来源一致。

## R07-04 / P2：表达人工 replacement 改 content 不改 dedup key
- 原始 ID：`AM-MEM-06-07`；验证级别：B。
- 主文件：`astrmai/memory/services/expression_pattern_service.py`, `v2_store.py`。
- 修复边界：replacement 与 canonical dedup identity 原子迁移，处理新键冲突和旧键 tombstone/alias。
- 回归目标：旧表达再出现不覆盖人工结果，新表达累积到同一记录。

## R07-05 / P2：Persona prompt 同 ID 更新仍永久命中旧摘要缓存
- 原始 ID：`AM-MEM-06-08`；验证级别：B。
- 主文件：`astrmai/memory/persona/persona_summarizer.py`, `astrmai/conversation/planning/context_engine.py`。
- 修复边界：cache hit 校验 raw hash/version；变化时失效 summary、shards、self-lore 并避免旧任务回写。
- 回归目标：同 persona ID 改 prompt 后产生新 summary；未改变 prompt 保持缓存命中。

## R07-06 / P2：Persona shard task 未纳入插件 lifecycle
- 原始 ID：`AM-MEM-06-09`；验证级别：B。
- 主文件：`astrmai/memory/persona/persona_summarizer.py`, `astrmai/app/lifecycle.py`, `astrmai/shared/helpers/plugin_helpers.py`。
- 修复边界：实现统一 background task owner/stop，shutdown cancel+await pending tasks。
- 回归目标：生成中重载后旧实例不再调用 LLM/写 cache，新实例无并发覆盖。

## R07-07 / P2：Dream 事实产出格式与 promotion 消费协议断开
- 原始 ID：`AM-MEM-06-10`；验证级别：B。
- 主文件：`astrmai/memory/dream/dream_agent.py`, `dream_generator.py`, `promotion_engine.py`, `astrmai/proactive/dream_scheduler.py`。
- 修复边界：定义一个结构化事实 contract，并确保 Agent 产出、Generator 解析、Promotion metadata 使用同一 schema。
- 回归目标：重复事实可形成 candidate 并通过阈值晋升；普通思考文本不误晋升。

## R07-08 / P2：Dream 默认 style mojibake 进入 prompt 和可见 fallback
- 原始 ID：`AM-MEM-06-11`；验证级别：B。
- 主文件：`astrmai/memory/dream/dream_generator.py`。
- 修复边界：修正源字符串并对 style 做受控有效值校验；不迁移已有用户数据。
- 回归目标：默认 prompt/fallback 不含损坏字符，显式合法 style 保持可用。

## R07-09 / P3：InstantMemoryGate 把固定乱码前缀写入 canonical content
- 原始 ID：`AM-MEM-06-12`；验证级别：B。
- 主文件：`astrmai/memory/services/instant_memory_gate.py`。
- 修复边界：新写入使用可读稳定标签或清洁事实；历史污染是否清理另立迁移任务，不在本轮批量改数据。
- 回归目标：新 canonical content/索引/prompt 不含 `[????|...]`。
