# 审查报告：astrmai/infrastructure/compat/ + astrmai/infrastructure/persistence/
> task_id: r12-infrastructure | 审查时间: 2025-07-16

## 概述
- 审查文件数: 14
- 发现总数: 12
- 严重: 3 | 中等: 4 | 建议: 5

## 发现

### 🔴 严重

| # | 文件:行号 | 描述 |
|---|----------|------|
| 1 | `orm_models.py:60-82` → `state_profile_persistence.py:30-60` | **ChatState 持久化严重不完整**。`ChatState` 数据类定义了 12+ 个字段，但 `chat_states` 表（`persistence_schema.py:49-69`）只建了 9 列。字段 **`total_messages`**、**`judgment_mode`**、**`last_msg_info`**（含 sender_id/has_image/image_urls/vl_executed 嵌套）、**`last_access_time`**、**`next_wakeup_timestamp`**、**`is_dirty`** 在入库/出库时被静默丢弃。`load_chat_state()` 返回的字典只包含 7 个业务字段，其余均回退为数据类默认值，导致状态轮转时数据丢失。 |
| 2 | `orm_models.py:77-78` | **`last_msg_info`（LastMessageMetadata）完全未持久化**。`ChatState` 通过嵌套数据类 `LastMessageMetadata`（sender_id, has_image, image_urls, vl_executed）记录上条消息元信息，但 `chat_states` 表没有对应列。单独的 `lastmessagemetadatadb` 表（`LastMessageMetadataDB`）虽有定义，但从未与 chat_states 做 JOIN 或关联查询——`load_chat_state` / `get_chat_state` 均不读取该表。调用者通过 `add_last_message_meta` 写入后无法与 chat_state 一同回读，造成多模态上下文丢失。 |
| 3 | `state_profile_persistence.py:174` | **`add_last_message_meta` 硬编码表名 `lastmessagemetadatadb` 依赖 SQLModel 命名约定**。表由 SQLModel 的 `metadata.create_all()` 隐式创建（`persistence_manager.py:25`），若 SQLModel 未来修改命名策略（如支持 `__tablename__`），此原始 SQL 会静默失败或写入错误表。且该表未出现在 `_init_db_sync` / `_init_db` 的 `CREATE TABLE IF NOT EXISTS` 中，若 SQLModel 初始化先于 `metadata.create_all()` 调用，会导致 `no such table` 运行时错误。 |

### 🟡 中等

| # | 文件:行号 | 描述 |
|---|----------|------|
| 4 | `persistence_schema.py:42-145` vs `:158-245` | **同一套 DDL 逻辑在 sync/async 中完全重复**。`_init_db_sync()` 和 `_init_db()` 包含约 80 行完全相同的建表和 ALTER TABLE 代码（chat_states, user_profiles, cronsnapshot 以及 expressionpattern 列迁移）。任何 schema 变更都必须在两处同步修改，极易遗漏导致环境间不一致。建议提取公共 `_DDL_STATEMENTS` 常量。 |
| 5 | `state_profile_persistence.py:26-27, 72-73` | **每次 load 都执行 `PRAGMA table_info`**。`load_chat_state` 和 `load_user_profile` 每次读取单行数据时都额外执行 `PRAGMA table_info` 获取列名，引入了不必要的 SQLite 元数据查询开销。列名是静态已知的，应当在类级别或模块级别缓存。 |
| 6 | `legacy_compat.py:88` | **`read_legacy_focus_thread_context` 始终使用默认 `ReplyFreshnessBudget()`**。不从 event extras 中反序列化 `freshness_budget` 的任何字段（state, created_at, max_age_seconds, salvage_window_seconds, latest_activity_ts, stale_reason）。如果旧事件中存储了新鲜度状态，读取后全部丢失，始终回退为 `FreshnessState.FRESH`。 |
| 7 | `state_profile_persistence.py:30-60` vs `database_service.py:155-185` | **`load_chat_state` 和 `get_chat_state` 用两套独立逻辑解析同表**。前者在 `StateProfilePersistenceMixin` 中用 aiosqlite + PRAGMA，后者在 `DatabaseService` 中用同步 sqlite3 + PRAGMA。两者返回的数据结构不完全一致（如 `database_service.py:164-178` 不包含 `group_config` JSON 解析？实际上有解析）。重复维护成本高，且在 `state_profile_persistence.py:37` 返回 dict 而在 `database_service.py` 返回 `ChatState` 对象——调用方需处理两种返回类型。 |

### 🟢 建议

| # | 文件:行号 | 描述 |
|---|----------|------|
| 8 | `legacy_compat.py:35-36` | **`astrmai_focus_thread_reason` 与 `astrmai_focus_reason` 值相同**。在 `emit_legacy_focus_thread_extras` 中，第 35 行设置 `astrmai_focus_reason = focus_context.focus_reason`，第 63 行又设置 `astrmai_focus_thread_reason = focus_context.focus_reason`（同一个字段）。若意图是区分"本事件焦点原因"与"线程根原因"，则后者应使用 `focus_context.root_reason`。当前设计会造成下游消费者困惑。 |
| 9 | `legacy_compat.py:114-144` | **`emit_legacy_prompt_envelope_extras` 未覆盖 `PromptEnvelope` 新增字段**。新 `PromptEnvelope` 有 40+ 个字段（如 `state_block`, `memory_block`, `background_memory_block`, `cognitive_drive_block`, `soft_background_block`, `guidance_lines` 等），但旧接口只序列化了约 15 个字段。虽然兼容层标记为 deprecated，但现存消费者（`planner_prompt_context.py`, `prompt_refiner.py`）仍通过 `read_legacy_prompt_envelope` 读取，会丢失大量上下文。建议评估是否可移除这些遗留消费者。 |
| 10 | `state_profile_persistence.py:47` | **`save_chat_state` 的 SQL 列顺序与建表语句列顺序不一致**。建表顺序为 `(chat_id, energy, mood, group_config, last_reset_date, total_replies, last_reply_time, last_passive_decay_time, updated_at)`，与 INSERT 的 VALUES 占位符顺序一致，但与列清单顺序一致是好的——但少了 `total_messages`, `judgment_mode` 等字段的列。建议统一补齐或明确标记为有意省略。 |
| 11 | `persistence_schema.py:89-103, 203-217` | **ALTER TABLE ADD COLUMN 使用 try/except pass 静默忽略所有错误**。若列已存在则抛出 `OperationalError` 被吞掉，逻辑正确，但若因其他原因（如数据库只读、磁盘满）失败也会被静默忽略，造成持久层静默退化。建议区分 `duplicate column` 异常与其他异常。 |
| 12 | `orm_models.py:10` | **`LastMessageMetadataDB` 表名与功能不匹配**。类名含 `DB` 后缀而其他 SQLModel 表类（如 `ExpressionPattern`, `MessageLog`）均无此后缀，命名不一致。且按 SQLModel 约定表名变为 `lastmessagemetadatadb`，可读性差。建议统一命名风格，或显式声明 `__tablename__ = "last_message_metadata"`。 |

## 亮点

- **`legacy_compat.py` 的 deprecation 机制设计成熟**：统一的 `@deprecated` 装饰器 + 模块级 docstring 标明迁移路径与消费者列表，为渐进式重构提供了清晰的路线图。
- **`persistence_manager.py` 的 MRO 多继承组合设计**：将 `PersistenceSchemaMixin`、`PersonaCacheMixin`、`StateProfilePersistenceMixin` 组合成单一 `PersistenceManager`，职责分离清晰，扩展性好。
- **`orm_models.py` 中的 `_dedupe_sqlmodel_metadata_indexes`**：在热重载场景下主动清理 SQLModel 元数据中的重复索引，体现了对动态重载环境的深入理解，是一个少见但实用的防御性设计。

## 总结

总体而言，`infrastructure/compat/` 和 `infrastructure/persistence/` 模块代码结构清晰，deprecation 机制和模块组合设计值得肯定。但**持久化层存在 3 个严重问题**：`ChatState` 有 6 个字段（含 `last_msg_info` 嵌套结构）在序列化时被完全静默丢弃，导致状态数据在 save-load 轮转后不可逆丢失；`last_msg_info` 虽然有独立表但从未被关联读取，多模态上下文无法还原。中等问题集中在**代码重复**（sync/async DDL 复制粘贴、两套独立的 chat_states 解析逻辑）和**性能**（每次 load 查 PRAGMA）。兼容层作为 v2.0 deprecated 桥接，功能覆盖合理，但 `PromptEnvelope` 的新增字段缺口较大，建议评估是否可直接淘汰遗留消费者。下一阶段建议将 `ChatState` 的完整字段集对齐到表结构中，统一 `load_chat_state` / `get_chat_state` 为单一实现，并抽取共享 DDL 常量消除重复。
