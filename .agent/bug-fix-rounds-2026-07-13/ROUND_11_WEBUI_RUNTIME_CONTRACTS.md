# Round 11：WebUI 与运行时数据契约

数量：8。依赖：前 10 轮 runtime API 稳定。

完成标准：Plugin Page 展示真实 live state；写操作与 runtime cache 一致；列表、字段、分页和创建/读取集合闭环。

## R11-01 / P1：WebUI profile mutation 绕过 live cache 并可能被旧对象覆盖
- 原始 ID：`12-01`；验证级别：B。
- 主文件：`astrmai/webui/backend/services/user_ui_service.py`, `astrmai/state/user_profile_service.py`, `astrmai/webui/plugin_pages.py`。
- 修复边界：写操作走 runtime profile service 或原子 invalidate/replace cache；manual locks 同步到 live object。
- 回归目标：active user 更新/删除/slice 变更下一轮对话立即生效，旧 dirty object 不回写覆盖。

## R11-02 / P2：Plugin Page 对 bridge 已解包结果再次读取 `.data`
- 原始 ID：`12-02`；验证级别：B。
- 主文件：`pages/admin/app.js`, `astrmai/webui/plugin_pages.py`。
- 修复边界：统一 frontend API adapter 的 unwrap contract，所有调用只消费一次业务对象。
- 回归目标：health/observability/scheduler/chat state 正常渲染，失败仍进入 degraded UI。

## R11-03 / P2：Persona diagnostics 与 live summarizer 使用不同 cache 文件
- 原始 ID：`12-03`；验证级别：B。
- 主文件：`astrmai/webui/backend/paths.py`, `persona_ui_service.py`, `astrmai/infrastructure/persistence/persistence_manager.py`。
- 修复边界：页面从 bound runtime persistence 读取，或与 live `cache/persona_cache.json` 单一来源一致。
- 回归目标：生成 persona shards 后页面显示同一 summary/hash/readiness，无 stale sibling file。

## R11-04 / P2：Dashboard 查询不存在的 `UserProfile` 表并把错误伪装成 0
- 原始 ID：`12-04`；验证级别：B。
- 主文件：`astrmai/webui/backend/services/dashboard_repository.py`, `dashboard_service.py`, `astrmai/infrastructure/persistence/persistence_schema.py`。
- 修复边界：使用真实 `user_profiles` schema；query failure 保留 degraded signal，不能等同空表。
- 回归目标：有 N 个 profile 显示 N；缺表/SQL 错误显示 degraded 而非 0。

## R11-05 / P2：Review 页面遗漏 canonical `expression` 字段
- 原始 ID：`12-05`；验证级别：B。
- 主文件：`pages/admin/app.js`, `astrmai/webui/backend/services/review_ui_service.py`。
- 修复边界：前端优先 `expression`，再兼容 legacy aliases；不改变审核动作 API。
- 回归目标：pending/all review 内容可见，approve/reject 对应同一 record。

## R11-06 / P2：Review 过滤/total 仅针对 bounded prefix，页面无导航
- 原始 ID：`12-06`；验证级别：B。
- 主文件：`astrmai/webui/backend/services/review_ui_service.py`, `pages/admin/app.js`。
- 修复边界：全量条件 count/filter 后 limit/offset；页面保存 page/page_size/total 并提供导航。
- 回归目标：第 51 条之后可访问，关键词命中后页记录，total 与全数据集一致。

## R11-07 / P3：Dashboard DB size producer/consumer 字段名不一致
- 原始 ID：`12-07`；验证级别：B。
- 主文件：`astrmai/webui/backend/services/dashboard_service.py`, `pages/admin/app.js`。
- 修复边界：统一 `db_size_kb` 与单位，保留必要旧字段兼容时只在 adapter 层处理。
- 回归目标：非空 DB 显示数值和 KB 单位，0 也不被 `||` 误判成缺失。

## R11-08 / P3：创建 memory event 写 canonical，但 paired list 只读 legacy
- 原始 ID：`12-08`；验证级别：B。
- 主文件：`astrmai/webui/backend/services/memory_ui_service.py`, `astrmai/webui/plugin_pages.py`, `pages/admin/app.js`。
- 修复边界：create/list 使用同一 resource collection，或页面明确跳转 canonical list；不能成功后不可见。
- 回归目标：POST 创建后对应 GET/list 立即可见且可管理，legacy 数据兼容不丢。
