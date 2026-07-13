# AstrMai 第四轮扫描报告 — 运维质量（错误处理/生命周期/跨插件/WebUI/资源）

> 扫描日期: 2026-07-02
> 方法: 8 个并行 explore agent 分领域扫描
> 覆盖: 错误处理、学习管线、插件生命周期、WebUI/API、跨插件交互、消息格式、日志可观测性、资源性能
> 总计: ~80 bugs

---

## 一、错误处理完整性 (16 bugs)

### G1 (CRITICAL) — sys2_process 异常静默吞没
**文件**: `gate.py:887-889`
```python
task.add_done_callback(lambda t: self._background_tasks.discard(t))
# 只 discard，不检查 task.exception()
```
System 2 回复路径主通道，异常静默丢失，bot 无视消息。

### G2 (CRITICAL) — 私聊消息异常后仍返回 "PRIVATE_WAIT"
**文件**: `gate.py:744-748`
`signal_new_message` 抛异常 → debug log → 仍返回 `"PRIVATE_WAIT"` → 消息被误标为已处理。

### G3 (CRITICAL) — DB 写入失败但内存状态已变更
**文件**: `chat_state_service.py:176-181` (及多处)
先改内存再写 DB，写失败后内存与 DB 不一致。重启后状态回退。

### G4 (CRITICAL) — 权限检查异常静默丢弃用户消息
**文件**: `message_entry.py:52-59`
`check_message_scope_access` 抛异常 → `stop_event()` → `return` 无 yield。

### G5 (CRITICAL) — Compaction 后台任务无错误回调
**文件**: `context_compaction.py:330-333`
`_create_task` 用裸 `asyncio.create_task()` 无回调，compaction 静默失败。

### G6–G16
- `planner_side_inputs.py:510`: 用户画像加载 `except: pass`
- `cognitive_loop.py:202`: 宽泛异常捕获返回 None
- `planner.py:610`: trace store 失败 `except: pass`
- `followup_manager.py:69`: create_task 无错误回调
- `persistence_schema.py:183`: init_ready.set() 在 init 失败时仍触发
- 等

---

## 二、学习管线 (9 bugs)

### BUG 1 (CRITICAL) — 自学习循环：bot 挖掘自己的消息
**文件**: `expression_miner.py:30-39`
`_normalize_messages()` 不按 sender 过滤，bot 的 `sender_name="SELF"` 消息进入挖掘管线。

### BUG 5 (CRITICAL) — 去重循环完全失效
**文件**: `reflector.py:205`
```python
if id(p) in to_remove_ids:  # id(p) 是内存地址，to_remove_ids 是 DB ID 字符串
```
`id(p)` 永远不会匹配 DB ID。去重检测运行了但零剔除。

### BUG 3 (HIGH) — write_pattern 权重无上界
**文件**: `expression_pattern_service.py:328`
`merged_weight = existing + incoming` — 重复重发现导致权重无限增长。

### BUG 7 (HIGH) — 挖掘冷却完全绕过
**文件**: `evolution_manager.py:231-232`
`recorder.record()` 返回值被忽略，`_try_trigger_mining()` 无冷却检查，每次 bot 回复都触发。

### BUG 8 (HIGH) — weight=0.0 序列化成 weight=1.0
**文件**: `expression_pattern_service.py:151`
`float(metadata.get("weight") or 1.0)` — Python `or` 将 0.0 当 falsy → 变成 1.0。

### BUG 5.1 (HIGH) — 字符级 Jaccard 对中文无用
**文件**: `reflector.py:275-283`
`你好世界` vs `你好宇宙` → Jaccard 0.43，远低于 0.8 阈值 → 漏检。

### BUG 9 (MEDIUM) — 行话检测器无常用词过滤
**文件**: `jargon_candidate_extractor.py:9-20`
`NOISE_TOKENS` 只有 10 个条目。`我们` `什么` `知道` 等全被当作候选项发给 LLM。

### BUG 6 (MEDIUM) — Rejected 模式永远无法恢复
**文件**: `reflector.py:180` + `expression_pattern_service.py:74`
`_merge_review_status` 保留 `"rejected"` 优于 `"pending"` → 低权重模式永久死亡。

---

## 三、插件生命周期 (17 bugs)

### BUG 14 (HIGH) — 9 个 AstrBot filter hook 卸载时未注销
**文件**: `main.py:100-214`
`terminate()` 只调 `facade.terminate()`，不注销 `on_llm_request`、`on_llm_response`、`command("mai")` 等。卸载后 AstrBot 仍调用已死的 handler。

### BUG 15 (HIGH) — 85 条 admin API route 卸载时不清理
**文件**: `main.py:67` → `plugin_pages.py:567-701`
重载时注册 +85 重复路由，旧路由指向死 facade。

### BUG 4 (HIGH) — ProactiveTask.stop() fire-and-forget
**文件**: `proactive_task.py:225-228`
`stop()` 只 cancel+flag 不 await，后台 worker 可能仍在运行。

### BUG 10 (HIGH) — EventBus 单例热重载后旧 worker 存活
**文件**: `event_bus.py:12-18`
`__new__` 类级 `_instance` → reload 后旧 worker 任务引用自身无法 GC → 永久运行。

### BUG 11 (HIGH) — ACTIVE_FACADE 模块级全局重载时被重置
**文件**: `plugin_api.py:20`
reload 把 `ACTIVE_FACADE` 重置为 None → 旧 facade 未 terminate → 双实例。

### BUG 2 (HIGH) — 热重载组件列表缺 10+ 服务
**文件**: `plugin_facade.py:98-110`
`apply_hot_config` 只刷新 11 个组件。`visual_cortex`、`system2_planner`、`prompt_refiner`、`context_engine` 等不刷新。

### BUG 1 (MEDIUM) — Gateway semaphore 热应用不重刷新
**文件**: `model_gateway.py:38`
`_global_semaphore` 初始化一次，`refresh_config` 不重建。改 `max_concurrent_llm_calls` 无效。

### BUG 8 (MEDIUM) — on_program_start 无重入守卫
**文件**: `lifecycle.py:50`
多次调用 → 内存引擎二次初始化、主动服务重复启动。

### BUG 6 (MEDIUM) — safe_create_task 创建分离 event loop
**文件**: `plugin_helpers.py:43`
无 running loop 时创建新 loop → 协程无法访问主 loop 资源。

### BUG 7 (MEDIUM) — set_active_facade 用 asyncio.run() 终止
**文件**: `plugin_api.py:48-54`
新建 loop 跑 `terminate()` → 任务创建自主 loop → RuntimeError。

---

## 四、WebUI / Plugin Pages (10 bugs)

### 10b (HIGH) — 独立 FastAPI server on port 8765 无认证
**文件**: `server.py:10-34`
CORS allow `*` methods，bind `0.0.0.0` → 暴露所有 API 无认证。

### 1c (HIGH) — user_slice 操作 ValueError 未捕获 → 500
**文件**: `plugin_pages.py:528-560`
`UserUiService.add_slice()` 抛 `ValueError` 未在 handler 捕获 → 500 crash。

### 7c (MEDIUM) — persona_ui_service 泄露完整人格
**文件**: `persona_ui_service.py:40-55`
8 个 shard（含深层秘密）+ first_person_rewrite + summary 全量返回。

### 8a (MEDIUM) — 无认证中间件 (文档声明)
**文件**: `plugin_pages.py:567-574`
85 条 API 全无认证，依赖 AstrBot WebUI 的 admin panel login 门控。

### 3c (MEDIUM) — base.html 破损 CDN 路径
**文件**: `data/t2i_templates/base.html:6-7`
`/path/to/highlight.min.js` — 占位符路径，生产 404。

### 2a (MEDIUM) — API_PREFIX 潜在路径不匹配
**文件**: `app.js:1` vs `plugin_pages.py:26`
前端 `"admin"` vs 后端 `"/astrmai/admin"` → 依赖 bridge 自动补前缀。

### 10a (MEDIUM) — _call_facade 静默丢弃关键字参数
**文件**: `plugin_api.py:197`
`return method(*args)` — 只传位置参数，kwarg 静默丢弃。

---

## 五、跨插件交互 (9 bugs)

### 1 (HIGH) — 11 处 stop_event() 阻断后续插件
**文件**: `message_entry.py` + `main.py` + `outbound_error_policy.py`
AstrMai 调用 `event.stop_event()` 时阻止所有下游插件处理该事件。

### 2 (HIGH) — 外部结果桥接吸收其他插件的输出
**文件**: `external_result_bridge.py:31-64`
不检查来源插件 → 任意插件输出进入 AstrMai attention gate → 对话记忆污染。

### 5 (HIGH) — 命令名碰撞 ("mai"、"work")
**文件**: `main.py:174,208`
短通用命令名无命名空间。`plugin_facade.is_framework_command` 扫描全局注册命令，匹配到其他插件命令时仍 `stop_event()`。

### 8 (HIGH) — on_llm_request 修改全局 request.system_prompt
**文件**: `main.py:120`
`inject_gemini_reverse_session` 对所有 LLM 请求追加 system_prompt → 修改其他插件的上下文。

### 9 (HIGH) — Cron job 无命名空间
**文件**: `cron_agent.py:54` + `heartbeat.py:106`
使用 AstrBot 共享 cron 命名空间 → LIST/DELETE 可操作其他插件的任务。

### 6 (MEDIUM) — priority=10 低于多数插件
**文件**: `main.py:194`
`on_global_message` priority=10 → 多数插件先处理 → AstrMai 可能收不到消息。

### 7 (MEDIUM) — event.set_result(None) 擦除其他插件输出
**文件**: `outbound_error_policy.py:34`
错误拦截时 `set_result(None)` → 擦除另一个插件的错误消息。

---

## 六、消息格式边界 (20+ bugs)

### 1B (HIGH) — Forward/小程序/AppMsg 内容丢失
**文件**: `sensors.py:179-180`
只提取 `Comp.Plain.text`，合并转发、微信卡片等 structured 组件内容丢失。

### 6A (HIGH) — @all/@everyone 和微信文本 @ 漏检
**文件**: `sensors.py:162`
只检查 `seg.qq == bot_id`，QQ `@all` (qq="all"/"") 漏检，微信文本 @ 无结构化组件。

### 5A (HIGH) — 微信图片 url 未被 sensors 提取
**文件**: `sensors.py:131`
只检查 `file`/`path` 属性 → 微信远程图片使用 `url` 字段 → 漏提取。

### 2A (MEDIUM) — Unicode 控制字符未处理
**文件**: `sensors.py:180`
只 strip `\u200b`，不处理 ZWJ/ZWNJ/BOM/LRM/RLM 等控制字符。

### 9A (MEDIUM) — 零宽字符消息突破过滤器
**文件**: `sensors.py:268-270`
`strip()` 结果为空但 `raw_msg` 可能仍含零宽字符 → `is_wakeup_signal` 可能误触。

### 10B (MEDIUM) — 连续不同图片被误判重复
**文件**: `gate.py:609`
`f"{msg_str}|{bool(extracted_images)}"` → 图片不同但 hash 相同。

### 8C (MEDIUM) — 深度嵌套回复丢失上下文
**文件**: `gate.py:279`
只提取 `sender_id`，不提取被回复消息的实际文本。

### 7B (MEDIUM) — 毫秒 Unix 时间戳解析失败
**文件**: `judge.py:117-118`
`float(timestamp) > 0` → 毫秒级时间戳(>10^10)被误判为秒级 → 错误时间。

---

## 七、日志与可观测性 (14 bugs)

### 1 (CRITICAL) — Energy 耗尽无日志
**文件**: `energy_manager.py:35-49`
`should_drop_by_energy()` 返回 True（bot 决定不回复）→ 零日志。

### 2 (CRITICAL) — executor core crash 丢失 stack trace
**文件**: `executor.py:875`
`logger.error(f"...crashed: {exc}")` → 无 `exc_info=True` → 丢失完整堆栈。

### 3 (CRITICAL) — 核心状态转换不可见
Mood/energy/compaction 触发仅 debug 日志 → 生产不可见。

### 4 (CRITICAL) — 非 JSON LLM 调用不记录延迟
**文件**: `gateway_call.py:286`
text reply 的 `_log_usage` 无 `latency_ms` → 90%+ LLM 调用延迟不可测。

### 5 (CRITICAL) — 消息发送成功无日志
**文件**: `reply_artifact_builder.py:349-382`
`_send_segments()` 成功不记录 → 无法确认 bot 是否真的发了回复。

### 9 (HIGH) — trace_id 未传播到 95% 日志消息
`debug_trace()` 有 trace_id，但 `logger.error/warning` 只有 chat_id → 无法关联。

### 8 (MEDIUM) — 误导性日志说"成功"实际上失败
多处 compaction 恢复路径用 `debug` 记录实际上是失败 → 设为 info/warning。

### 6 (MEDIUM) — 昂贵 f-string 无条件求值
**文件**: `main.py:155`
`str(response.completion_text)[:200]` 在 debug 关闭时仍求值。

### 7 (MEDIUM) — LLM 回复文本泄露到日志
**文件**: `main.py:155` + `gate.py:580`
助手回复 200 字和用户消息 100 字出现在日志中。

---

## 八、资源与性能 (15 bugs)

### 1-9 (HIGH) — 9 个无界字典（内存泄漏）
| 文件 | 行 | 字典 |
|------|------|------|
| `user_profile_service.py` | 32 | `user_profiles` |
| `user_profile_service.py` | 33 | `_user_locks` |
| `chat_state_service.py` | 30 | `chat_states` |
| `executor.py` | 57-58 | `_chat_locks`, `_chat_pending_count` |
| `gate.py` | 86 | `_proactive_injection_lock` |
| `dispatcher.py` | 67 | `_cooldowns` |
| `feedback_bridge.py` | 20-21 | `_last_flush_ts`, `_last_pulse_ts` |
| `topic_digest_service.py` | 22 | `_last_digest_by_chat` |
| `state_store.py` | 10 | `_states` |

### 10 (HIGH) — ProactiveTask 孤立 future + 无指数退避
**文件**: `proactive_task.py:223,204`
`asyncio.ensure_future(self.start())` 从非 async 线程回调 → 孤立。崩溃重试固定 5 秒无退避无上限。

### 11 (MEDIUM) — deepcopy 在热路径
**文件**: `chat_state_service.py:303`
`copy.deepcopy(state)` 每次情绪分析 → 高开销。

### 13-14 (LOW) — PIL Image 未关闭
**文件**: `executor.py:428`, `image_pipeline.py:27`
打开 PIL Image 读格式后丢弃对象。

### 15 (LOW) — re.findall 每回复编译
**文件**: `reply_artifact_builder.py:85`
句子分割用裸 `re.findall(raw_pattern)` → 改为 `re.compile` 模块级。

---

## 修复优先级建议

### 第一批 (崩溃/安全)
- G1: gate.py sys2_process 异常吞没
- G2: 私聊消息误标已处理
- BUG 14: filter hook 卸载不注销

### 第二批 (内存/资源)
- 1-9: 9 个无界字典
- G5: compaction task 无错误回调
- BUG 5: 学习去重失效

### 第三批 (数据完整性)
- G3: DB 写入不一致
- BUG 3/8: 权重溢出/序列化
- BUG 5.1: 中文去重失效

### 第四批 (可观测性)
- 1-5: 关键路径无日志

---
*扫描完成。*
