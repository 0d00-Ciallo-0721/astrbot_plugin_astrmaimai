# 窗口10-Prompt11-PersistenceSchema健壮性与死辅助函数收口审查报告

## 审查范围
- `astrmai/infrastructure/persistence/persistence_schema.py`
- `astrmai/infrastructure/persistence/persistence_manager.py`
- `tests/regression/persistence/test_persistence_regressions_migrated.py`
- `tests/unit/state/test_chat_state_persistence_migrated.py`
- `tests/unit/state/test_relationship_profile_roundtrip_migrated.py`
- `artifacts/review_new/06-模块-M6-基础设施与运行时支撑.md`

## 审查结论
- 本窗口最终无历史遗留问题。
- `persistence_schema.py` 已不再把兼容迁移失败统一收敛成“只吞 duplicate column name、其余模糊报错”的脆弱路径。
- `_dedupe_sqlmodel_metadata_indexes()` 已确认接入 `PersistenceManager` 初始化链，且 reload 重复索引场景有回归证明，不再属于“未接线 helper”。

## 已确认事实
1. sync / async 两条初始化路径都已改为复用统一 schema patch helper，保持行为一致。
2. `"duplicate column name"` 仍是唯一被静默兼容的已知历史迁移场景，其余 `sqlite3.OperationalError` 会带着 scope、失败 SQL 和原始 sqlite 原因进入诊断消息。
3. `PersistenceManager()` 的同步初始化、事件循环内的异步初始化、以及 reload 后的 SQLModel metadata 重复索引收口都已有实测覆盖。
4. `artifacts/review_new/06-模块-M6-基础设施与运行时支撑.md` 已同步校正为当前真实状态，不再残留“helper 未接线”或“本窗口问题仍未收口”的旧结论。

## 审查验证
- `python -m pytest tests/regression/persistence/test_persistence_regressions_migrated.py tests/unit/state/test_chat_state_persistence_migrated.py tests/unit/state/test_relationship_profile_roundtrip_migrated.py -q`
- `python -m pytest tests/regression/persistence/test_persistence_regressions_migrated.py -q -k "sync_init_without_running_loop_creates_session_id_column or async_init_with_running_loop_schedules_task or reload_datamodels_does_not_duplicate_indexes_on_create_all or schema_patch_helpers_swallow_duplicate_columns_and_wrap_other_errors"`

## 备注
- 审查中未发现新的未修复回归，也未发现本窗口范围内仍需继续实现的遗留项。
- 上述测试存在若干既有 warning（主要来自 SQLModel reload 场景和测试环境协程告警），但本轮未新增相关失败，且不构成本窗口的阻断问题。
