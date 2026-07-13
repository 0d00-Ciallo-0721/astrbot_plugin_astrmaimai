# AstrMai 第八轮深度审计报告 — 安全/死代码/类型/默认配置/契约/异常

> 日期: 2026-07-03 | 6 领域 | QQ Only | ~30 bugs

---

## 一、安全漏洞 (5 bugs)

### 1. [CRITICAL] ComputerAgent 宿主机任意代码执行 — `computer_agent.py:52-53`
```python
return ToolSet([
    LocalPythonTool(),          # 宿主机执行任意Python
    ExecuteShellTool(is_local=True),  # 宿主机执行任意Shell
])
```
- 一旦 `sandbox_enabled=True`，任何能触发 Sys3 工作模式的用户可执行任意代码
- 仅由全局开关控制，无每用户授权检查
- `_conf_schema.json` 注明"此功能在宿主机直执"

### 2. [HIGH] ASTRMAI_DB_PATH 绕过路径校验 — `db.py:15-16`
```python
if os.getenv("ASTRMAI_DB_PATH"):
    return os.path.realpath(raw)  # 跳过 plugin_data 目录边界检查
```
设置环境变量后完全跳过路径验证，可读写文件系统任意 SQLite 数据库。

### 3. [MEDIUM] SQL 注入潜伏 — `dashboard_repository.py:17`
```python
async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
```
`table` 参数直接拼接到 SQL。当前调用方硬编码，但函数设计危险。需添加表名白名单。

### 4. [MEDIUM] 三环境变量控制关键文件路径 — `paths.py:24-33`
`ASTRMAI_DB_PATH`、`ASTRMAI_CONFIG_PATH`、`ASTRMAI_PERSONA_CACHE_PATH`。DB 路径校验被绕过（见 #2）。

### 5. 安全状况总体良好
✅ 无 `eval/exec/compile` ✅ 无 `pickle/yaml.load` 不安全反序列化 ✅ 无 `os.system` ✅ 无硬编码 token/password ✅ 无 `subprocess`

---

## 二、死代码/不可达路径 (5 bugs)

### 1. [HIGH] 整个 `infrastructure/security/` 包(~113行)无人导入
`security/__init__.py`、`output_guard.py`、`input_sanitizer.py`、`rate_limiter.py` — 四个文件无人导入。`InputSanitizer` 和 `TokenBucket` 从未被实例化。

### 2. [MEDIUM] `looks_like_harmful_content()` — `output_guard.py:19`
检测 NSFW/自残/PII 内容的函数，零调用点——非内部调用，非外部调用。

### 3. [MEDIUM] `_build_summary_with_provider()` v1 — `compaction_providers.py:121`
~80行方法仅测试文件引用。生产代码独占使用 v2 版本。

### 4. [LOW] `PLUGIN_DIR`/`DATA_DIR` 死导出 — `meme_config.py:7-8`
在 `__all__` 中但从未被 meme 包外部导入。

### 5. [LOW] `HelpCommandView` 再导出 — `presentation/__init__.py`
仅在 presentation 包内部使用，从未通过 `__init__.py` 表面导入。

---

## 三、类型安全 (5 bugs)

### 1. [HIGH] `str(None)` → `"None"` 注入 wait targets — `reply_artifact_builder.py:316`
```python
at_targets = [str(action.get("target_id")) for action in actions if ...]
```
`action.get("target_id")` 无默认值 → `None` → `str(None)` = `"None"` → `Comp.At(qq="None")` → SDK 可能崩溃或发送格式错误的 @ 消息。

### 2. [HIGH] 同样模式在 `pfc_tools.py:270` — 去重检测破损
```python
matcher=lambda item: item.get("action") == "at" and str(item.get("target_id")) == target_id
```
当 `item["target_id"]` 为 `None`，比较 `"None" == target_id` 永远 False → 重复动作漏网。

### 3. [MEDIUM] `str(None)` → `"None"` 污染运行时状态 — `reply_artifact_builder.py:324`
`str(action.get("target_name"))` → `"None"` 字面量 → `emit_legacy_reply_runtime_extras()` 存储。

### 4. [MEDIUM] `personas` 类型检查无 else 回退 — `context_engine.py:371-379`
`isinstance(personas, dict)` / `isinstance(personas, list)`。若 personas 两者都不是（第三方插件返回自定义容器），静默返回空字符串，无诊断。

### 5. [LOW] `tool_state.get()` 无默认值 — `planner_side_inputs.py:494-498`
`state`/`profile`/`relationship_vec` 键缺失时返回 `None`。下行代码有守卫但其他调用路径可能无。

---

## 四、默认配置行为 (5 bugs)

### 1. [CRITICAL] 空 provider 模型池 — 插件完全不可用 — `config.py:19-23`
`fallback_models: []`, `agent_models: []`, `task_models: []`, `vision_models: []`, `embedding_models: []` — 全部默认空。`gateway_call.py:142-149` → 空 attempt_queue → `LLMCascadeFailureException("empty_model_pool")`。用户每条消息只看到回退文本 `"（陷入了短暂的沉默..."`。

### 2. [HIGH] 空 persona_id → 标识范围到垃圾键 — `config.py:38-41`
`persona_id = ""` (falsy) → `context_engine.py:314` 跳过 persona 加载 → 落到通用 `DEFAULT_PERSONA_PROMPT`。整个代码库中 `persona_id=""` 用作记忆/泳道/lore/日记的范围键——所有数据累积到空字符串键，后续设置真实 persona_id 后不可检索。

### 3. [HIGH] 空 `wakeup_words`/`nicknames` → 自然名称识别破损 — `config.py:55-56`
`sensors.py:278-280` → `if nicknames and raw_msg:` → False → 名称提及检测跳过。`judge.py:312-316` → `wakeup_words=[]` → 唤醒词检测死代码。Bot 仅响应 @mention。

### 4. [MEDIUM] `stale_reply_max_age_sec=0.0` → 推导超时 37.5 秒过窄 — `config.py:133`
`reply_freshness.py:16-20` 和 `executor.py:309-312` → `max(30, min(90, 15*2.5))` = 37.5 秒。慢速 LLM/高负载下静默丢弃。

### 5. [MEDIUM] `auto_recall_probability=0.0` → 概率记忆自动召回关闭 — `config.py:185`
仅关键词触发召回（`之前`、`记得` 等）有效。随机路径关闭。结合 Bug #2，bot 在默认配置下无有效持久记忆。

---

## 五、API 契约违规 (5 bugs)

### 1. [MEDIUM] MemoryQuery 废弃死字段 — `memory_query.py:16-19,32-38`
`include_feedback` 和 `retrieve_keys` 存在于公共 API 但**从未被读取**。类文档注明"声明用于 API 兼容但不被任何检索路径读取。设置无效。"

### 2. [MEDIUM] FrequencyController 废弃模块 — `frequency_controller.py:5-8`
模块文档"DEPRECATED: 注入到 AttentionGate 但当前管道从未调用。" 完整 210 行实现注册为活跃依赖但从未执行。

### 3. [MEDIUM] `should_reply()` message_text 未使用参数 — `frequency_controller.py:77,87`
`message_text: str = ""` 在签名中，文档说"暂未使用，预留关键词分析"。参数接受但静默忽略。

### 4. [LOW] `_compat_instant_llm_last_check` 标注"禁止读取" — `summarizer.py:29-31`
字段存在但值显式非权威——"运行时状态权威在 MemoryTurnPipeline，禁止读取此字段。"

### 5. [LOW] `has_latest_assistant` 过期字段契约 — `group_dialogue_store.py:44-49`
字段默认为 False，在 `_build_warm_quotes` 重算前包含不可靠数据。文档警告"不要依赖此前的过期值。"

---

## 六、异常处理特异性 (5 bugs)

### 1. [HIGH] 裸 `except:` 捕获所有 — `hybrid_retriever.py:89`
```python
try: meta = json.loads(meta)
except: meta = {}  # 捕获 KeyboardInterrupt/SystemExit/CancelledError
```
**最严重**——阻止干净关闭。**修复**: `except (json.JSONDecodeError, TypeError):`

### 2. [HIGH] `except Exception: pass` 静默吞错 — `sensors.py:376`
```python
try: bot_id = str(event.get_self_id())
except Exception: pass  # 零日志, 零回退, 零追踪
```
**修复**: 至少 `logger.debug(..., exc_info=True)` 或捕获特定类型。

### 3. [MEDIUM] `except Exception:` 静默掩藏 LLM 失败 — `memory_claim_service.py:117`
关键数据管道中 LLM 提取失败静默返回 `[]`，上游代码无法区分"无 claims"和"提取失败"。

### 4. [MEDIUM] `except Exception` 在 HTTP 下载中太宽 — `vision_binding.py:52`
Python ≤3.8 上捕获 CancelledError。3.9+ 上也捕获 MemoryError 等非网络异常。

### 5. [MEDIUM] 嵌套 `except Exception` 混淆 json 解析与 ORM 错误 — `context_engine.py:568,575`
内层应该是 `json.JSONDecodeError`，外层在生产环境仅 debug 级别记录，掩盖真正的数据库错误。

---

## 全局 Top 5

| # | 严重度 | 领域 | Bug | 文件 |
|---|--------|------|-----|------|
| 1 | CRITICAL | 默认 | 空 provider 池 → 插件完全不可用 | `config.py:19-23` |
| 2 | CRITICAL | 安全 | RCE 宿主机直执 | `computer_agent.py:52` |
| 3 | HIGH | 异常 | 裸 except: 捕获 KeyboardInterrupt | `hybrid_retriever.py:89` |
| 4 | HIGH | 默认 | 空 persona_id → 垃圾键 | `config.py:38` |
| 5 | HIGH | 类型 | str(None) → "None" 注入 | `reply_artifact_builder.py:316` |
