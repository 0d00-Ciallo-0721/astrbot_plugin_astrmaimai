# 开发窗口 02：Infrastructure/Persistence 修复

## 必须先读取的审查报告
1. `artifacts/reviews/r10-infrastructure.md` — 完整发现清单（3🔴 4🟡 5🟢）
2. `artifacts/reviews/r15-master.md` — 总报告
3. `artifacts/reviews/r13-session-fixes.md` — 了解本轮已修复内容（避免重复）

## 目标文件
- `astrmai/infrastructure/persistence/orm_models.py` — ChatState 等表定义
- `astrmai/infrastructure/persistence/persistence_schema.py` — 建表 SQL
- `astrmai/infrastructure/persistence/state_profile_persistence.py` — 状态/Profile 持久化
- `astrmai/infrastructure/compat/legacy_compat.py` — 兼容层

## 依赖
无底层依赖。Persistence 是最底层模块。

---

## 🔴 严重（3 项）

### P2-1：ChatState 持久化严重不完整
- **文件**：`astrmai/infrastructure/persistence/orm_models.py:60-82` + `state_profile_persistence.py:30-60`
- **问题**：`ChatState` 数据类定义了 12+ 字段，但 `chat_states` 表只建了 9 列。以下字段在入库/出库时被**静默丢弃**：
  - `total_messages` — 消息计数
  - `judgment_mode` — 判定模式
  - `last_msg_info` — 最后消息信息（含嵌套 sender_id/has_image/image_urls/vl_executed）
  - `last_access_time` — 最后访问时间
  - `next_wakeup_timestamp` — 下次唤醒时间戳
  - `is_dirty` — 脏标记
- **后果**：`load_chat_state()` 返回的字典只包含 7 个业务字段，其余回退为数据类默认值，状态轮转时数据丢失。
- **最小修复**：
  1. 在 `persistence_schema.py` 的 `chat_states` 建表 SQL 中补充缺失列
  2. 在 `state_profile_persistence.py` 的 `load_chat_state()` 中读取新列
  3. 在 `save_chat_state()` 中写入新列

### P2-2：chat_states 表缺 last_reply_time 列
- **文件**：`astrmai/infrastructure/persistence/persistence_schema.py:49-69`
- **问题**：建表语句缺少 `last_reply_time` 列，导致 3 个测试（T9/T10/T11）`sqlite3.OperationalError: table chat_states has no column named last_reply_time`
- **最小修复**：在建表 SQL 中添加 `last_reply_time REAL DEFAULT 0.0`

### P2-3：详见 r10-infrastructure.md（legacy_compat 兼容层边界条件）

---

## 🟡 中等（4 项）

详见 `r10-infrastructure.md`，重点：
- `state_profile_persistence.py` 序列化/反序列化键名一致性（`trust`/`relationship_vector`）
- `legacy_compat.py` 废弃 API 标注完整性
- 数据库连接生命周期管理

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/unit/state/test_chat_state_persistence_migrated.py tests/unit/state/test_relationship_profile_roundtrip_migrated.py -q
```

## 成功标准
- 🔴 P2-1：ChatState 全部 12+ 字段 roundtrip 无丢失
- 🔴 P2-2：T9/T10/T11 3 个 schema 测试通过
- 无新增回归
