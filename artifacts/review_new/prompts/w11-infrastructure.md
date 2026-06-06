# 开发窗口 11：Infrastructure — 缓存失效 + 序列化安全

## 必须先读取的审查报告
1. `artifacts/review_new/r11-infrastructure.md` — 2🔴 7🟡 8🟢

## 审查范围
`astrmai/infrastructure/persistence/` + `compat/` + `context_economy/`（22 个源文件）

---

## 🔴 严重（2 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `database_service.py:102-111` | **全局 `_CHAT_STATES_COLUMNS` 缓存不感知运行时 DDL 变更**。数据库被重建后列数错位，静默数据损坏。**修复**：缓存加 TTL + 列数不匹配时自动刷新。 |
| 2 | `compat/legacy_compat.py:78-82` | **`_read_freshness_budget` 反序列化脆弱**。事件系统 JSON 序列化后 `ReplyFreshnessBudget` dataclass 降级为 dict，`hasattr` 返回 False，所有字段丢失。**修复**：`isinstance(stored_focus, dict)` 时从 dict 重建。 |

---

## 🟡 中等（重点 5 项）

| # | 文件:行号 | 描述 |
|---|----------|------|
| 3 | `persistence_schema.py:70-174` | 所有 ALTER TABLE 裸 `except Exception: pass` — 只捕获 `OperationalError` "duplicate column" |
| 4 | `database_service.py:102` vs `state_profile_persistence.py:22` | 列发现三套策略不一致，统一为实例级缓存 + TTL |
| 5 | `state_profile_persistence.py:92` | INSERT OR REPLACE 列序与 CREATE TABLE 隐式耦合，改用具名列绑定 |
| 6 | `compat/legacy_compat.py:87-89` | `ambient_events` 双 key 回退链可能重复，明确唯一权威 key |
| 7 | `state_profile_persistence.py:45` | `load_chat_state` 返回 dict vs `get_chat_state` 返回对象不一致 |

---

## 🟢 建议（选做）

- `persistence_schema.py` DDL 代码重复（sync/async 逐行复制）→ 提取常量
- `context_economy/models.py` WorkloadTrace 26 字段膨胀
- `state_profile_persistence.py:109-110` `or 0.0` 冗余模式清理

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/unit/state/ tests/regression/persistence/ tests/regression/state/ tests/test_database_adapters_refactor.py -q
```

## 成功标准
- 🔴 2 项全部修复
- 🟡 #3 #6 #7 修复
- 相关测试通过（含已知修复回归）
