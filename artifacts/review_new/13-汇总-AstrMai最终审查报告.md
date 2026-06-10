# AstrMai 最终审查报告

## 说明
- 本报告已按当前仓库代码重新校正。
- 本次仅修正报告内容，不修改任何业务代码。
- 旧报告中部分问题仍然真实存在，部分已因后续重构而过时；本版只保留当前仍有参考价值的结论。

## 总体结论
- 当前仓库**不是“完全无问题”状态**。
- 插件主入口 `main.py -> PluginFacade -> Plugin Page` 主链路整体仍可用。
- 但仓库中仍存在一组**真实运行级问题**，主要集中在：
  - `config.py` 与 `_conf_schema.json` 的配置映射断裂
  - 若干并发与运行时防护不足
  - 记忆写入内容过滤过严

## 已确认仍然真实存在的问题

### P1

#### 1. 配置链断裂
以下问题仍需关注，但本轮已收敛掉一部分已知缺口：

- `conversation` 配置段与 `AstrMaiConfig.conversation` 的承接已修复
- `deep_temporal_*` / `maintenance_*` 已从 `global_settings` 归位到 `memory`
- `performance.summary_threshold` 已补齐 schema 暴露并接入运行时读取
- `evolution.jargon_min_count` 已补齐 schema 暴露并与下游读取对齐
- 当前仍建议继续排查其余 schema 覆盖缺口，避免出现新的“模型里有、schema 未暴露”项

影响判断：
- 历史上这类问题确实会导致参数静默失效；本轮已关闭其中 4 个已知缺口，但配置层仍值得继续专项复查

#### 2. 并发与运行时防护不足
以下问题当前仍然成立或基本成立：

- `astrmai/learning/evolution_manager.py`
  - `get_active_patterns_canonical()` 在 loop 活跃时会直接抛 `RuntimeError`
- `astrmai/state/energy/frequency_controller.py`
  - 声明了 `_records_lock`，但当前未实际用于 `_records` 的保护
- `astrmai/state/group_wait/group_reply_wait_manager.py`
  - 正常 `_states` 读写路径未完整加锁
- `astrmai/proactive/wakeup_service.py`
  - 仍直接访问 `self.config.life.*`

影响判断：
- 这些问题更偏向“特定条件触发”的运行时风险
- 不一定启动即炸，但不能视为安全

#### 3. 记忆写入过滤过严
- `astrmai/memory/services/memory_write_service.py`
  - `should_skip_content()` 仍然直接跳过以 `{` 开头的内容

影响判断：
- 可能误杀本应写入记忆的正常文本内容

## 已校正的旧结论

以下内容不应再继续算作“当前仓库仍存在的问题”：

- standalone FastAPI backend route/service 显性断裂
  - Prompt03/04 已修复构造参数断裂、缺失方法和 route/service 签名不匹配
  - 已通过 backend route 相关测试与最小路由调用复核
- standalone 前端残留
  - `astrmai/webui/frontend/` 当前已删除
- mock server 链路
  - `astrmai/webui/mock_frontend_server.py` 当前已删除
- 远程图片 URL 安全链路
  - `astrmai/infrastructure/security/url_validator.py` 当前已删除
  - `REMOTE_IMAGE_ALLOWLIST.md` 当前已删除

## 对当前运行状态的更准确判断

### 插件加载
- 当前 `main.py` 插件入口仍然完整
- `@register`、`PluginFacade`、`register_astrmai_admin_pages(...)` 仍然连通
- 从入口代码看，插件**大概率可以正常加载**

### 主消息链
- `main.py -> facade -> ingress/attention/planning/execution` 主链路未发现“已删除模块仍被直接引用”的明显断点
- 但并发与运行时防护问题仍可能在特定条件下触发异常

### Plugin Page / 页面入口
- 当前主支持入口是 AstrBot Plugin Page
- 相关主链路没有被这次复核直接判定为整体断裂
- 此前确认的 standalone FastAPI backend route/service 断裂已收口
- 但这不自动等于整个 standalone backend 已完成全量发布级验证

### 配置系统
- 配置系统的主干断裂已收敛一部分：`conversation`、`deep_temporal_*` / `maintenance_*`、`performance.summary_threshold`、`evolution.jargon_min_count` 已完成对齐
- 但 `config.py` 与 `_conf_schema.json` 仍建议继续做覆盖性复查，避免残留其他未暴露字段

## 建议的真实优先级

### 第一优先级
- 修 `config.py` 与 `_conf_schema.json` 的映射断裂

### 第二优先级
- 修 `frequency_controller.py` / `group_reply_wait_manager.py` 的并发保护
- 修 `evolution_manager.py` 的运行时 worker/loop 调用路径

### 第三优先级
- 重新评估 `memory_write_service.py` 的 `{` 过滤规则

## 最终结论
- 旧版总报告中的“有问题”方向并不是错的。
- 但旧版把“当前真实问题”和“已删除旧链路的历史问题”混在了一起。
- 当前应以本版为准：
  - **仍有真实 bug**
  - **主插件入口大概率可加载**
  - **standalone FastAPI backend 的已知 route/service 断裂已修复**
  - **配置链与部分运行时链路仍需继续修复**
