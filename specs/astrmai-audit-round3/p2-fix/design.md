# Design Document — AstrMai P2 Fix Round-3

> 本文档对应 Spec `astrmai-audit-round3/p2-fix`，描述 1 项 P2 缺陷的修复设计方案。  
> 其余 39 项 P2 已在 Round-2 副作用/ponytail 注释/已有代码中解决。  
> 不包含 P0/P1/P3 修复、架构重构、新功能开发。

## 1. Overview

### 1.1 整体策略

39/40 P2 bugs are already resolved. Only P2.18 requires a minimal code change.

| 阶段 | 主要动作 | 改动文件 | 改动类型 |
|------|---------|---------|---------|
| ① 代码修复 | 为 `ContextCompactionEngine` 添加 `refresh_config` 方法 | `context_compaction.py` | 补丁 (patch) |
| ② 回归验证 | pytest + import check | — | 验证 |

### 1.2 设计边界

- 不创建新文件
- 不修改 `_conf_schema.json` 或 `metadata.yaml`
- 不修改 AstrBot 框架 API 调用签名
- 每处改动 ≤ 20 行

### 1.3 与现有机制的契合

`PluginFacade.apply_hot_config` 在 `astrmai/app/plugin_facade.py:98-116` 已遍历所有组件调用 `refresh_config(parsed_config)`。`ContextCompactionEngine` 是唯一缺少该方法的配置感知组件。

```
plugin_facade.apply_hot_config()
  └── for name, comp in components:
        if comp and hasattr(comp, "refresh_config"):
          comp.refresh_config(parsed_config)
```

## 2. Architecture

### 2.1 模块位置

```
conversation/attention/context_compaction.py:175
  class ContextCompactionEngine(CompactionProviderMixin)
    ├── __init__(..., provider_id)    # line 179-206
    ├── refresh_config(parsed_config) # ← 新增
    └── ...
```

### 2.2 数据流

```
WebUI 热重载
  → plugin_facade.apply_hot_config(config_dict, parsed_config)
    → comp.refresh_config(parsed_config)
      → self.provider_id = str(parsed_config.conversation.compaction_provider_id or "")
      → self.compaction_trigger_segments = ...
```

## 3. Detailed Design: P2.18 — refresh_config

### 3.1 当前状态

`ContextCompactionEngine.__init__` (line 195):
```python
self.provider_id = str(provider_id or "")
```

缺少 `refresh_config` 方法，热重载后 `provider_id` 不更新。

### 3.2 修改方案

在 `__init__` 之后添加 `refresh_config` 方法：

```python
def refresh_config(self, config) -> None:
    conversation = getattr(config, "conversation", None)
    if conversation is None:
        return
    self.compaction_trigger_segments = int(getattr(conversation, "compaction_trigger_segments", 40) or 40)
    self.compaction_trigger_tokens = int(getattr(conversation, "compaction_trigger_tokens", 1800) or 1800)
    self.compaction_keep_recent_segments = int(getattr(conversation, "compaction_keep_recent_segments", 16) or 16)
    self.compaction_summary_max_tokens = int(getattr(conversation, "compaction_summary_max_tokens", 450) or 450)
    self.provider_id = str(getattr(conversation, "compaction_provider_id", "") or "")
```

### 3.3 为何不修改 bootstrap.py

`bootstrap.py:232-248` 仅在首次启动时调用。热重载不走该路径，走 `apply_hot_config → refresh_config`。修复应在 `ContextCompactionEngine` 内部。

### 3.4 测试策略

- 现有 pytest 套件确认无回归
- `import astrmai` 确认无导入错误

## 4. Design Alternatives Considered

| 方案 | 描述 | 决策 |
|------|------|:----:|
| A: 添加 refresh_config | 10 行方法，更新 5 个字段 | ✅ 采用 |
| B: 在 apply_hot_config 中特殊处理 | 增加调用者分支逻辑 | ❌ 违反单一职责 |
| C: 热重载中重建 ContextCompactionEngine | 需重新注入所有引用 | ❌ 过度重构 |

## 5. Risk Assessment

| 风险 | 可能性 | 影响 | 缓解 |
|------|:------:|:----:|------|
| 热重载中 compaction 正在执行 | 低 | 低 | 仅更新配置引用，不影响执行中的操作 |
| 新字段名在未来 Config 版本中变更 | 低 | 低 | getattr with default fallback |
