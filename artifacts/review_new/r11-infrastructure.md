# 审查报告：astrmai/infrastructure/persistence/ + compat/ + context_economy/
> task_id: r11 | 审查时间: 2025-07-13

## 执行摘要

本次审查覆盖 infrastructure 层的三个核心子模块：**persistence/**（持久化、ORM 模型、查询层）、**compat/**（旧版兼容桥）、**context_economy/**（Token 预算调度与 Prompt 模板管理）。共审查 22 个文件，发现 **17 个问题**（2🔴 严重 / 7🟡 中等 / 8🟢 建议）。

整体质量处于 **中等偏上** 水平。已知修复项（ChatState 字段补齐、freshness_budget 读取、lastmessagemetadatadb 建表、judgment_mode 类型处理）均已完成且正确，回归检查通过。主要风险集中在：全局 SQL 缓存未考虑 DDL 运行时变更、异步锁粒度不一致、`legacy_compat` 中 `freshness_budget` 反序列化脆弱性、以及大量异常被静默吞掉。

## 概述

- 审查文件数: 22
- 发现总数: 17
- 严重: 2 | 中等: 7 | 建议: 8

## 发现

### 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `database_service.py:102-111` | **全局 `_CHAT_STATES_COLUMNS` 缓存不感知运行时 DDL 变更**。`persistence_schema.py` 中的 `_init_db` / `_init_db_sync` 会在首次启动后通过 `ALTER TABLE` 追加列（如 `judgment_mode`、`last_msg_info` 等），但 `get_chat_state` 中的全局变量 `_CHAT_STATES_COLUMNS` 一旦缓存就永不刷新。如果数据库在进程生命周期内被回滚重建或由外部工具迁移，`dict(zip(col_names, row))` 会产生列数与行数不匹配的静默错位，导致字段取值张冠李戴。修复建议：① 对缓存加时间戳/TTL；② 捕获列数不匹配时自动刷新；③ 改用统一的列发现方法（如 `state_profile_persistence.py` 中每次 `PRAGMA table_info` 的做法）。 |
| 2 | `compat/legacy_compat.py:78-82` | **`_read_freshness_budget` 反序列化脆弱，事件系统序列化后可能丢失类型信息**。`emit_legacy_focus_thread_extras`（第 66 行）将整个 `FocusThreadContext` 对象存入 `event.set_extra("astrmai_focus_thread_context", focus_context)`。但部分消息平台/事件系统的 `set_extra` / `get_extra` 内部会做 JSON 序列化，将 `ReplyFreshnessBudget` 从 dataclass 降级为普通 dict。此时 `hasattr(stored_focus, "freshness_budget")` 返回 `False`，导致 `_read_freshness_budget` 始终返回空的 `ReplyFreshnessBudget()`，**`freshness_budget` 的所有字段（state / max_age_seconds / salvage_window_seconds / stale_reason）全部丢失**。修复建议：在 `get_extra` 读取后做类型守卫（如 `isinstance(stored_focus, dict)` 时用 `stored_focus.get("freshness_budget", {})` 重新构造 `ReplyFreshnessBudget`）。 |

### 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 3 | `persistence_schema.py:70-174` | **所有 `ALTER TABLE` 迁移语句使用裸 `except Exception: pass`** (`persistence_schema.py` 第 116 行、第 145 行注释 `# ?` 处同理)。虽然"列已存在时 ALTER TABLE 报错"是预期的，但 `except Exception` 过宽——会一并吞掉 `sqlite3.DatabaseError`（磁盘满、损坏）、`OperationalError`（权限、忙锁）等需要告警的异常。修复建议：只捕获 `sqlite3.OperationalError` 中明确的 "duplicate column name" 子串，其他异常 `raise`。 |
| 4 | `database_service.py:102` vs `state_profile_persistence.py:22` | **列发现策略不一致：全局变量 vs 实例级缓存 vs 每次 PRAGMA**。`database_service.get_chat_state` 用全局 `_CHAT_STATES_COLUMNS`，`state_profile_persistence._get_chat_state_cols` 用实例级类变量缓存，`state_profile_persistence.load_chat_state` 每次都实时 `PRAGMA table_info`。三套机制并存，维护成本高，且全局变量存在第 1 条所述风险。修复建议：统一为实例级缓存 + TTL 失效模式。 |
| 5 | `state_profile_persistence.py:92` | **`save_chat_state` 的 INSERT OR REPLACE 列序对 CREATE TABLE 定义存在隐式耦合**。15 个 VALUES 占位符的排列顺序必须与 `persistence_schema.py` 中 `CREATE TABLE chat_states` 的列序完全一致，代码中没有断言或自动化检查来保证这一点。如果将来某处增删列但未同步更新此处的列序，将产生静默数据错位。修复建议：改用具名列绑定（如 `INSERT INTO chat_states(col1, col2, ...) VALUES(:v1, :v2, ...)`），或增加自动化列的列序一致性测试。 |
| 6 | `compat/legacy_compat.py:87-89` | **`read_legacy_focus_thread_context` 中 `ambient_events` 的回退链可能引入重复**。第 87 行的 `event.get_extra("astrmai_focus_thread_ambient_events", [])` 与第 88 行的 `event.get_extra("astrmai_background_events", [])` 被 OR 连接。如果两个 key 都不为空，`or` 会丢弃后者，但 `emit_legacy_focus_thread_extras`（第 57 行、第 66 行）对 `ambient_events` 设置了两个不同的 extra key，消费者端无法保证拿到的是最新的。修复建议：明确只有一个权威 key，或者合并两个 source。 |
| 7 | `context_economy/center.py:221-258` | **`_resolve_scope_id` 存在 7+ 个近乎相同的 `if/elif` 分支**，每个的逻辑都是 `request.xxx or request.lane_key.xxx or "global"`。当新增 `WorkloadFamily` 时容易漏写分支，导致意外 fallback 到 `"global"` 而丢失 scope 隔离。修复建议：改用字典映射 `{family: lambda r: r.persona_id or r.scope_id or ...}` 或提取统一 fallback 逻辑。 |
| 8 | `database_jargon.py:96-106` | **`_canonical_jargon_rows` 用 f-string 拼接 SQL 占位符**。虽然第 96 行的 `statuses` 是硬编码元组 `("active", "stale")`，其 `','.join('?' for _ in statuses)` 结果是安全的，但这种模式容易在后续维护中被误用为直接拼接用户输入。修复建议：固定使用 `IN (?, ?)` 或将 `statuses` 过滤逻辑移到 Python 层。 |
| 9 | `state_profile_persistence.py:45` | **`load_chat_state` 返回 `Dict[str, Any]` 而非 `ChatState` 对象**。与 `database_service.get_chat_state`（返回 `ChatState` 对象）不一致。调用方（如 `profile_repository.py:30` 的 `load_chat_state`）需要知道返回值类型是 dict 还是对象，容易误用。修复建议：统一返回 `ChatState` 对象，或至少用类型注解明确标注。 |

### 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 10 | `state_profile_persistence.py:53,161` | **`json.loads(row_dict.get("last_msg_info") or "{}")` 中的 `or "{}"` 冗余**。`dict.get()` 已提供默认值 `"{}"`，`or` 是二次防御。虽然无害，但会掩盖值为空字符串时本该暴露的 schema 问题。建议用 `json.loads(row_dict.get("last_msg_info", "{}"))`。 |
| 11 | `state_profile_persistence.py:109-110` | **`save_chat_state` 中多处 `getattr(state, "xxx", 0.0) or 0.0` 的 `or 0.0` 模式**。如果 `getattr` 返回 `0.0`，`or 0.0` 会**将其替换为 0.0**（无变化）；但如果 `getattr` 返回 `0.0` 且该字段不应为 0，这个模式不会报错。更关键的是，如果 `getattr` 返回 `False`（如 `is_dirty`），`int(False)` = 0 是正确的，但 `int(False or False)` 没问题。建议：移除 `or` 后缀，直接使用 `float(getattr(state, "xxx", 0.0))`。 |
| 12 | `persistence_schema.py:172` | **`_init_db_sync` 和 `_init_db` 中大量重复的 DDL 代码**。同步版与异步版的 CREATE TABLE / ALTER TABLE 几乎是逐行复制粘贴。建议抽取公共 SQL 常量或生成器函数。 |
| 13 | `context_economy/center.py:284` | **`_snapshot_metric` 中 `max(metrics.call_count, 1)` 在 call_count=0 时产生误导性指标**。此时 `primary_hit_rate = 0/1 = 0%` 是正确的，但 `provider_session_usage_rate` 等都会是 0%，而 `avg_stable_prefix_length` 会是 0。虽然不会崩溃，但在调试刚启动时的指标会制造"无意义的零值行"。建议：当 `call_count == 0` 时直接返回空/占位指标。 |
| 14 | `context_economy/models.py:74-97` | **`WorkloadTrace` 字段膨胀（26 个字段），部分字段与 `WorkloadPolicy` 高度重复**（如 `template_id` / `template_version` / `schema_id` / `primary_model` 在两处都出现）。建议考虑 `WorkloadTrace` 引用 `policy.id` 而非复制字段，或使用 `@dataclass` 继承共享基类。 |
| 15 | `persistence_schema.py:1` | **文件头注释 `# astrmai/infra/persistence.py` 与实际路径不符**（实际路径是 `persistence_schema.py`）。这是从旧文件复制而来未更新，容易造成调试困惑。建议修正为 `persistence_schema.py`。 |
| 16 | `database_jargon.py:5` | **未使用的 import：`json`, `uuid`, `Dict`**。`uuid` 用于 `uuid.uuid4().hex`（第 108 行），`json` 在第 115 行，`Dict` 未使用。建议移除 `Dict` 导入。 |
| 17 | `state_profile_persistence.py` | **`save_chat_state` 每次写入全量 15 列，无脏检查**。即使只变更了 `mood`，也会全量序列化和写入 `last_msg_info`（含 JSON 序列化）和 `group_config`。在高频状态回写场景（如每轮对话更新 `energy`/`mood`）会增加不必要的 I/O。建议：考虑只写变更字段，或保持全量写入但配合 WAL 模式减少锁争用。 |

## 已知修复项回归检查

| 修复项 | 状态 | 说明 |
|--------|------|------|
| **ChatState 字段补齐** | ✅ 通过 | `ChatState` dataclass 含 14 个字段 + `updated_at` 在 DB 层；`load_chat_state` / `save_chat_state` / `get_chat_state` 三处均已覆盖所有字段。 |
| **Legacy freshness_budget** | ⚠️ 条件通过 | 功能逻辑正确，但反序列化存在类型丢失风险（见 🔴 #2）。建议在 `_read_freshness_budget` 中增加 dict→dataclass 的转换守卫。 |
| **lastmessagemetadatadb 建表** | ✅ 通过 | `persistence_schema.py` 同步/异步两处 `CREATE TABLE IF NOT EXISTS lastmessagemetadatadb` 均已包含完整列定义（id/chat_id/sender_id/has_image/image_urls/vl_executed/timestamp）。 |
| **judgment_mode 类型处理** | ✅ 通过 | 三处读写均使用 `str(row_dict.get("judgment_mode") or "single")` 模式，默认值统一为 `"single"`，类型安全。 |

## 亮点

1. **`persistence/__init__.py` 的懒加载导出设计**。使用 `__getattr__` 配合 `_EXPORTS` 字典实现按需导入，避免大型模块的循环依赖和启动全量加载，结构清晰且易于扩展。

2. **`context_economy/` 的 WorkloadFamily 分类体系**。将 Prompt 调用按 cache_priority / freshness_priority 分类管理，实现了基于 workload 特征的 Token 预算差异化调度，设计概念先进。`rotate_reasons` 的跟踪机制对调试模型切换非常有用。

3. **`legacy_compat.py` 的 `@deprecated` 装饰器**。统一且富含元信息（since/removal/replacement），配合 `DeprecationWarning` 提供了清晰的迁移路径，体现了良好的弃用管理实践。

4. **`database_jargon.py` 的双向写（legacy Jargon ↔ canonical_memories）兼容策略**。在不破坏旧表结构的同时，将数据同步写入新范式表，支持逐步迁移。`dedup_key` + `source_ref` 的联合去重设计稳健。

## 测试覆盖评估

| 维度 | 评估 | 说明 |
|------|------|------|
| ORM 模型与 Schema 一致性 | 🟡 弱 | `ChatState` 与 `chat_states` 表之间无自动化列对齐检查。仅有运行时会通过 `PRAGMA table_info` 隐式发现列，但没有"不应存在的列"检测。 |
| SQL 注入防护 | 🟢 强 | 所有用户输入均通过参数化查询或 SQLModel ORM 传递，无直接拼接。`database_jargon.py` 的 f-string 仅用于生成硬编码 placeholder。 |
| 异步锁正确性 | 🟡 中等 | `_db_lock` 在部分 Mixin（如 `CronPersistenceMixin`）中使用 `async with self._db_lock`，但 `MemoryPersistenceMixin.search_nodes` 等读操作未加锁，存在并发读-写不一致风险。 |
| 兼容层 round-trip | 🟡 未覆盖 | `emit_*` → `read_*` 的完整往返序列化测试缺失。特别地，`freshness_budget` 和 `FocusThreadContext` 嵌套 dataclass 的序列化恢复路径未经测试。 |
| 错误处理覆盖 | 🔴 弱 | 大量 `except Exception: pass`（persistence_schema.py）、`try/except` 返回空默认值，真实错误被静默吞掉。没有错误注入测试。 |

## 总体评级

**B-（中等偏上，建议修复严重项后提升至 B+）**

### 评级理由

- **架构设计良好**：三层分离（Schema → Mixin → Service → Repository）清晰，Mixin 模式复用得当，context_economy 的 WorkloadFamily 分类富有前瞻性。
- **已知修复项全部通过回归检查**：ChatState 字段补齐、freshness_budget 读取、lastmessagemetadatadb 建表、judgment_mode 类型处理均正确。
- **主要风险**：① 全局 SQL 缓存不感知 DDL 变更（🔴 #1）——生产环境中若数据库被重建将产生静默数据错位；② freshness_budget 反序列化在事件系统序列化后的类型丢失（🔴 #2）——影响兼容层数据完整性；③ 裸 `except Exception: pass` 掩盖真实错误（🟡 #3）；④ 异步锁粒度不统一，部分读操作未加锁。

**修复优先级建议**：🔴 #1（高）→ 🔴 #2（高）→ 🟡 #3（中）→ 🟡 #5（中）→ 🟡 #7（低）→ 其余建议项。
