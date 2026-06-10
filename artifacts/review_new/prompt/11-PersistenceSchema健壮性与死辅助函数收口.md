# Prompt 11：`PersistenceSchema` 健壮性与死辅助函数收口

## 任务目标
收口 M6 当前最明确的两个问题：
- 兼容迁移只窄处理 `duplicate column name`
- `_dedupe_sqlmodel_metadata_indexes()` 未接线

## 必读报告
- `artifacts/review_new/06-模块-M6-基础设施与运行时支撑.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

## 必读代码
- `astrmai/infrastructure/persistence/persistence_schema.py`
- 调用初始化链的相关入口
- 持久化相关测试

## 必须完成的修复
1. 提升 `persistence_schema.py` 对异常 schema 状态的健壮性与可诊断性。
2. 对 `_dedupe_sqlmodel_metadata_indexes()` 做明确处理：
   - 要么接入真实初始化链
   - 要么删除，避免误导
3. 不要把这轮扩大成整个 persistence 子系统重构。

## 实施要求
- 优先最小修复。
- 如果你增强了日志或异常分层，保持与项目现有错误处理风格一致。

## 验证要求
至少执行：
- persistence / state 持久化相关测试
- 一次最小初始化验证

## 完成标准
- schema 迁移不再只靠“吞 duplicate column name”这一层脆弱假设
- 未接线 helper 有了明确归宿
- 更新：
  - `artifacts/review_new/06-模块-M6-基础设施与运行时支撑.md`

