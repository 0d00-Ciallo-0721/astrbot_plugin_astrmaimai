# Round-4 P3 代码质量修复 (32 项)

## P3.1 persona_summarizer.py — exc_info=True 冗余 (8x)
- File: `astrmai/memory/persona/persona_summarizer.py:469,507,540,573,606,637,669,702`
- Action: `logger.exception()` 自带 exc_info，显式传 True 无害但冗余 → ponytail 注释
- Disposition: READ-ONLY AUDIT (无害冗余，不改代码)

## P3.2 topic_summarizer.py:366 — local import json
- File: `astrmai/memory/services/topic_summarizer.py:366`
- Action: 局部 `import json` → 移到模块顶部
- Disposition: FIX

## P3.3 utils.py:76 — RRFFusion metadata "first wins"
- File: `astrmai/memory/utils.py:76-82`
- Action: BM25 metadata 覆盖 vector metadata → ponytail 注释说明设计意图
- Disposition: FIX (ponytail 注释)

## P3.4 evolution_manager.py:293 — __import__("re")
- File: `astrmai/learning/evolution_manager.py:293`
- Action: 动态 `__import__("re")` → 检查模块级是否已有 `import re`
- Disposition: FIX (如果模块级没有就加)

## P3.5 reflect_tracker.py:171 — monkey-patch 改返回类型
- File: `astrmai/learning/review/reflect_tracker.py:171-192`
- Action: 已有 ponytail 注释说明
- Disposition: READ-ONLY AUDIT (已标注)

## P3.6 evolution_manager.py:119 — 重复 Normalize 逻辑
- File: `astrmai/learning/evolution_manager.py:119-134`
- Action: 重复逻辑暂不合并，ponytail 注释
- Disposition: READ-ONLY AUDIT

## P3.7 gateway_lane.py:413 — ~200 行重复
- File: `astrmai/infrastructure/gateway/gateway_lane.py:413+`
- Action: tool_chat_in_lane_result 与 _elastic_call_result 大量重复 → ponytail 注释
- Disposition: READ-ONLY AUDIT

## P3.8 event_bus.py:169 — _workers_started 永不为 False
- File: `astrmai/infrastructure/runtime/event_bus.py:169`
- Action: 安全守卫，调试时有用 → ponytail 注释
- Disposition: READ-ONLY AUDIT

## P3.9 judge.py:26 — group mutex 用纯 set()
- File: `astrmai/conversation/decision/judge.py:26,305-309`
- Action: asyncio 中用无锁 set → ponytail 注释
- Disposition: FIX (ponytail 注释)

## P3.10 planner.py:87 — planning history 全局
- File: `astrmai/conversation/planning/planner.py:87-89`
- Action: 全局 history → ponytail 注释
- Disposition: READ-ONLY AUDIT

## P3.11 followup_manager.py:15 — 零值歧义
- File: `astrmai/conversation/execution/followup_manager.py:15`
- Action: `or 0.0` 使 0 与"未设置"不可区分 → ponytail 注释
- Disposition: FIX (ponytail 注释)

## P3.12 followup_manager.py:15 — 同上重复 (bug 报告重复)
- Same as P3.11
- Disposition: resolved by P3.11

## P3.13 executor.py:431 — async 中用 sync tempfile
- File: `astrmai/conversation/execution/executor.py:431-434`
- Action: `tempfile.mkstemp()`/`os.fdopen()` → ponytail 注释
- Disposition: FIX (ponytail 注释)

## P3.14 conversation_continuity.py:213 — 轻量设计未注释
- File: `astrmai/conversation/planning/conversation_continuity.py:213-219`
- Action: 代码中已有中文注释 → READ-ONLY AUDIT
- Disposition: READ-ONLY AUDIT (已有注释)

## P3.15 context_engine.py:316 — 每次 prompt 打 warning
- File: `astrmai/conversation/planning/context_engine.py:316-322`
- Action: 降级到 debug 级别并加 ponytail 注释
- Disposition: FIX (降级日志)

## P3.16 bootstrap.py:90 — task_models[0] 不检查空列表
- File: `astrmai/app/bootstrap.py:90`
- Action: `(task_models or ["Unconfigured"])[0]` 已通过 or 保护 → READ-ONLY AUDIT
- Disposition: READ-ONLY AUDIT (已有保护)

## P3.17 bootstrap.py:192 — trace_cache_dir 路径
- File: `astrmai/app/bootstrap.py:192`
- Action: 默认路径回退 → ponytail 注释
- Disposition: FIX (ponytail 注释)

## P3.18 bootstrap.py:504 — 闭包引用循环
- File: `astrmai/app/bootstrap.py:504-510`
- Action: runtime → interaction → bridge → runtime 循环 → ponytail 注释
- Disposition: READ-ONLY AUDIT

## P3.19 lifecycle.py:126 — 启动任务不确认
- File: `astrmai/app/lifecycle.py:126-129`
- Action: 不验证任务成功 → ponytail 注释
- Disposition: FIX (ponytail 注释)

## P3.20 lifecycle.py:252 — 10+ flag 无原子性
- File: `astrmai/app/lifecycle.py:252-270`
- Action: 顺序 flag 设置 → ponytail 注释
- Disposition: READ-ONLY AUDIT

## P3.21 lifecycle.py:229 — dict.fromkeys 依赖 Task hashable
- File: `astrmai/app/lifecycle.py:229`
- Action: CPython asyncio.Task is hashable → ponytail 注释
- Disposition: READ-ONLY AUDIT

## P3.22 lifecycle.py:85 — host() weakref 重复解引用
- File: `astrmai/app/lifecycle.py:85-91`
- Action: `host()` 调两次 → 存为局部变量
- Disposition: FIX

## P3.23 lifecycle.py:115 — visual_cortex.start() 同步
- File: `astrmai/app/lifecycle.py:115-124`
- Action: 如果是协程会被泄露 → ponytail 注释
- Disposition: FIX (ponytail 注释)

## P3.24 plugin_facade.py:331 — 私有 AstrBot API
- File: `astrmai/app/plugin_facade.py:331-395`
- Action: 使用 `_collect_descriptors` → ponytail 注释
- Disposition: FIX (ponytail 注释)

## P3.25 plugin_facade.py:22 — WebUI 注册失败静默
- File: `astrmai/app/plugin_facade.py:22-28`
- Action: 已有 warning 日志 → ponytail 注释
- Disposition: FIX (ponytail 注释)

## P3.26 runtime_context.py:81 — threading.Lock 混 asyncio
- File: `astrmai/app/runtime_context.py:81-82`
- Action: 已有 ponytail 注释 → READ-ONLY AUDIT
- Disposition: READ-ONLY AUDIT (已有注释)

## P3.27 runtime_context.py:140 — sync_host_compat_attrs 部分失败
- File: `astrmai/app/runtime_context.py:140-148`
- Action: 循环 setattr 可能部分成功 → ponytail 注释
- Disposition: FIX (ponytail 注释)

## P3.28 runtime_context.py:420 — 29 LEGACY_RUNTIME_ATTRS 死代码
- File: `astrmai/app/runtime_context.py:420-456`
- Action: 兼容 shim → ponytail 注释
- Disposition: READ-ONLY AUDIT (已标注)

## P3.29 runtime_facade_protocol.py:15 — @runtime_checkable 不必要
- File: `astrmai/app/runtime_facade_protocol.py:15-16`
- Action: 显式继承不需要 runtime_checkable → ponytail 注释
- Disposition: FIX (ponytail 注释)

## P3.30 main.py:41 — Plugin Pages API 无运行时守卫
- File: `main.py:41-47`
- Action: 已有 ponytail 注释 → READ-ONLY AUDIT
- Disposition: READ-ONLY AUDIT (已有注释)

## P3.31 main.py:134 — priority=10 可被低优先插件静默
- File: `main.py:134` (actually line 165: priority=10)
- Action: 优先级较低可能被静默 → ponytail 注释
- Disposition: FIX (ponytail 注释)

## P3.32 cross-cutting — 无 MCP 连接管理
- File: N/A (cross-cutting)
- Action: 架构缺失 → ponytail 注释
- Disposition: READ-ONLY AUDIT
