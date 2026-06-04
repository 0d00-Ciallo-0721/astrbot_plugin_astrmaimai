# 开发窗口 08：Gateway/Runtime 残差修复

## 必须先读取的审查报告
1. `artifacts/reviews/r02-gateway-runtime.md` — 完整发现清单（2🔴 8🟡 5🟢）
2. `artifacts/reviews/r15-master.md` — 总报告
3. `artifacts/reviews/r13-session-fixes.md` — **重要**：窗口 4 的 D1-D5/D14-D17 已修复，本窗口只修审查**新发现**的残差

## 目标文件
- `astrmai/infrastructure/gateway/gateway_lane.py` — Gateway Lane Mixin
- `astrmai/infrastructure/gateway/gateway_call.py` — Gateway Call Mixin
- `astrmai/infrastructure/gateway/gateway_result.py` — Cache Observation
- `astrmai/infrastructure/gateway/model_router.py` — Model Router
- `astrmai/infrastructure/gateway/gateway_policy.py` — Gateway Policy
- `astrmai/infrastructure/runtime/lane_manager.py` — Lane Manager
- `astrmai/infrastructure/runtime/lane_storage.py` — Lane Storage

## 依赖
窗口 03（state）+ 窗口 04（memory）

---

## 🔴 严重（2 项）

### P8-1：chat_in_lane_result 双重冷却过滤导致 trace 日志与执行不一致
- **文件**：`astrmai/infrastructure/gateway/gateway_lane.py:217-293`
- **根因链**：
  1. 行 203-208: `_filter_cooldown_attempt_queue()` 获第一次过滤快照 → `skipped_cooldown_models`
  2. 行 234: 调 `_elastic_call_result(models=models)` — 用**原始 models** 而非过滤后的 `attempt_queue`
  3. `_elastic_call_result` 内部（`gateway_call.py:131-140`）**再次过滤** — 这是第二次过滤
  4. 行 281-293: `append_trace_stage(skipped_cooldown_models=...)` 用的是**第一次**（过期的）快照
- **后果**：若两次过滤间有模型解除冷却，trace 会错误地将该模型标记为"被跳过"
- **最小修复**（方案 A — 推荐）：
  - `chat_in_lane_result` 不再做预过滤，删除行 197-210 的 `_build_attempt_queue`/`_filter_cooldown_attempt_queue`
  - `model_hint` 改为在 `_elastic_call_result` 返回后从 `result.model_id` 获取
  - trace 的 `skipped_cooldown_models`/`cooldown_overridden` 通过 `_elastic_call_result` 的新返回值获取
- **备选方案 B**：
  - `_elastic_call_result` 暴露 `skipped_cooldown_models`/`cooldown_overridden` 到返回值
  - `chat_in_lane_result` 使用返回值中的信息做 trace

### P8-2：详见 r02-gateway-runtime.md

---

## 🟡 中等（8 项）

详见 `r02-gateway-runtime.md`，重点：
- `_build_cache_observation` 剩余 4 个 cache reason 不可达（`prefix_stable`/`provider_visible_hash_stable`/`cache_affinity_enabled`/`cached_usage_supported`）— D18 已修复最关键 2 个
- `DEFAULT_POLICIES` 中 `("sys2","dialog")` 存储策略与其他 lane 差 4 倍（D40）
- `gateway_call.py` 与 `gateway_lane.py` 中成功路径代码重复（D15/D16 建议）
- `gateway_result.py` 中 `_build_cache_observation` 从序列化 meta 反向推导（D18 已改善）

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_gateway_context_passthrough_refactor.py tests/test_main_reply_request_trace_refactor.py tests/test_context_economy_refactor.py -q
```

## 成功标准
- 🔴 P8-1：双重过滤不一致修复（trace 与实际执行一致）
- 🔴 2 项全部修复
- Gateway/Runtime 23 个测试全部通过
