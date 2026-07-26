# OPT-05 记忆数据质量与维护调度

状态：代码完成（purge 默认关，观察一周后手动开启） ｜ 优先级：P1 ｜ 依赖：无（WU-07 的墓碑过期依赖本 OPT） ｜ 覆盖发现：ML-03(P1)、ML-04(P1)、ML-10(P2)、WU-04(P1) ｜ 两条随运行时间单调恶化的数据损耗 + 治理自愈通道的结构性断裂。

## 完成记录

**2026-07-26 代码侧完成**：

- ML-03：`instant_memory_gate._authority_dedup_key`——like/dislike 多值属性追加归一化 value 片段（去空白/小写/32 字符），不同偏好共存、同偏好复述仍去重；display_name/server_count 等单值属性保持 attribute 级覆盖语义。
- ML-04：`memory_index_projector.cleanup_deleted` 改为**先** `FaissVecDB.delete(doc_id)`（内部按 doc_id 反查 int id 并同步删 embedding）**后**兜底 SQL 行删除——顺序关键，反过来 faiss 查不到 int id；faiss 不可用时回退旧 SQL 路径；FTS 键仍用 int id。审计时发现的"rebuild 无人调"由 WU-04 调度一并解决。
- ML-10：实际缓冲格式比审计更糟（`用户/旁白：{text}` 连 sender 都没有）；改为结构化条目（sender_id/text），join 时渲染成摘要解析器**已认识**的 `[序号] 发送者: 内容` 格式——解析器零改动，speaker_ids 落到 QQ 号；热更前的旧字符串条目原样透传。
- WU-04：`proactive_task._run_maintenance_cycle` 接入 `_run_memory_store_maintenance`（每日节流、INFO 报告）；**purge 分步启用**用调度侧保守策略实现——`memory.maintenance_purge_enabled=False`（默认）时各类宽限期推到 1e12 秒，索引一致性修复照跑、零物理删除，服务端 run_once 零改动；管理页记忆质量面板新增"执行维护"按钮接通既有端点。新配置两枚（schema+pydantic 同步，纯中文文案）。
- 过程事故记录：首次插入把 signin/digest 块截进了新方法尾部，被既有隔离测试 `test_proactive_task_maintenance_cycle_isolates_subservice_failures` 当场抓获——该测试正是 OPT-13 主张的"装配断言"价值实证。
- 测试：新增 `tests/regression/memory/test_memory_data_quality_hotfix.py` 12 条（stash 红验证 11/12）；受影响套件 287 passed。
- 待部署验收：embedding 与 documents 行数差值不再增长；新群摘要 `speaker_ids` 非空；维护日志每日出现且 purge 报告为零删除；一周后核对报告再开 `maintenance_purge_enabled`；偏好共存 SQL 采样。

## 目标

- 用户的多个偏好可共存（"喜欢咖啡"和"喜欢猫"都记住），不再互相覆盖。
- 删除/覆盖记忆时向量索引真正回收，FAISS 幽灵向量停止累积。
- 群摘要记忆能归属到具体说话人（当前全部 unknown）。
- 索引一致性修复 + 黑话/表达积压过期清理有真实调度方，治理闭环自愈。

## 基线证据

- **ML-03**：`instant_memory_gate.py:153` `dedup_key=f"{subject_id}:{entity}:{attribute}"` 不含 value；authority 路径 `mark_superseded_by_key`（`v2_store.py:900-913`）把同 key 旧记录全部 supersede 并移出索引 → 新"喜欢"顶掉旧"喜欢"。
- **ML-04**：`memory_index_projector.py:105-110` 清理只删 documents 表 + FTS 行，**从不调用** AstrBot `FaissVecDB.delete`（唯一会同步 `embedding_storage.delete` 的 API）→ 每次覆盖/软删/隔离都留幽灵向量，top-k 名额被死条目挤占，向量召回率单调下降。
- **ML-10**：`memory_turn_pipeline.py:136-139` buffer 行格式是 `用户/旁白：{sender}: {text}`，而 `session_memory_summarizer.py:364` 只解析 `[time] sender: content` → 不匹配全落 `sender=unknown`，speaker_ids 恒空。
- **WU-04**：`MemoryMaintenanceService.run_once`（索引修复+积压清理+墓碑 purge）全仓唯一调用方是前端从不调用的 WebUI 端点（契约 diff 证实）；调度侧只挂了 `DecayService.run_once`（`proactive_task.py:787`，仅每日衰减）。
- DB 取证 SQL（本地无库，上线验证用）：`SELECT dedup_key, COUNT(*) n, SUM(status='superseded') dead FROM canonical_memories WHERE dedup_key LIKE '%:preference:%' GROUP BY dedup_key HAVING n>1;`；幽灵向量数 = embedding 存储行数 − documents 行数。

## 实施步骤

1. ML-03：偏好类（like/dislike 等多值属性）dedup_key 追加 **value 归一片段**，或在 `MemoryConflictResolver` 对多值属性禁用 authority_override（走普通 dedup）。测试：连续处理"我喜欢咖啡""我喜欢猫"，断言两条 active；同 value 重复表述仍去重。
2. ML-04：`cleanup_deleted` 查出 `documents.doc_id` 后改调 `engine.faiss_db.delete(doc_id)`（faiss 未初始化时保留现 SQL 路径作降级）；`rebuild` 路径同步。集成测试：写入→supersede→断言 faiss 检索不返回且 embedding 行减少。
3. ML-10：`record_turn` 存结构化 dict（sender_id/sender_name/text）替代拼接字符串；或 `_build_topic_messages` 增加 `用户/旁白：sender: text` 解析分支（推荐前者，顺带解决检索层 sender 过滤缺参数的远期需求）。
4. WU-04：在 `proactive_task`（或 dream_scheduler）的日常低峰任务里调用 `memory_engine.maintenance_service.run_once()`；同时在管理页记忆质量面板加"执行维护"按钮接通现有端点。**首次接通前必须在真实库 dry-run**：核对 `protected_*` 保护参数，确认 purge 范围只含过期 pending/rejected。
5. 部署后 DB 采样验证（上面两条 SQL + `/memories/quality/overview` 的 index 异常数趋势）。

## 验收标准

- 四项单测/集成测试绿；全量 pytest 绿。
- 部署后：偏好共存 SQL 采样无"多值属性互相 supersede"新增；embedding 与 documents 行数差值不再增长；新产出的群摘要 `metadata.speaker_ids` 非空；`/memories/quality/overview` 索引异常数在维护跑过后归零、review_pending 超期条目开始减少。

## 风险与回退

- ML-04 中风险：faiss 删除路径需处理未初始化/异常降级，保留 SQL 路径兜底；出问题最多回到"只删文档不删向量"的现状。
- WU-04 **中风险（本 OPT 最高）**：run_once 含物理删除（purge）。缓解：dry-run 先行 + 首周调度只开 `index_repair`（purge 用开关分步启用）。
- ML-03 中风险：dedup_key 变更影响后续写入的去重语义，不迁移旧数据（旧 superseded 记录不复活，只保证增量正确）。
- 各项独立提交可单独 revert。

## 完成记录

（完成后填写：DB 采样前后数据、幽灵向量差值、首次维护 dry-run 报告与正式运行报告）
