# OPT-09 Provider 与模型池健壮性（not-found / 能力解析 / 级联副作用）

状态：代码完成 ｜ 优先级：P1 ｜ 依赖：无 ｜ 覆盖发现：RT-07(P3，已吸收 PL-07)、TG-02(P1)、RT-08(P2)、TL-04(P1/LIKELY) ｜ 配置漂移已在生产发生（openai/deepseek-v4-pro 不存在仍被引用），失败处理链条上有三处接缝。

## 完成记录

**2026-07-26 代码侧完成**：

- TG-02：`gateway_policy._is_fatal_failure` 增补 not-found 关键字族（含中文"没有找到"）——不存在的 provider 单次尝试即切下一模型，不再 backoff 空转；失败矩阵测试锚定（not-found×3 fatal / 502/连接重置/客户端超时 non-fatal / 429/quota 既有语义不变）。
- RT-08：`resolve_provider_capabilities` 重写——完整模型 ID 按 '/' 前缀降级：对象查找（全 ID→前缀）→ `get_all_providers` 注册 ID 前缀扫描 → 字符串前缀家族回退（gemini/xx 正确落 gemini 家族），终结 1005/1005 全 unknown。
- RT-07：compaction 配置 provider 一次性存在校验（实例级缓存，只查一次、只告警一次），无效剔除；校验接口异常时不拦截保持旧行为。
- TL-04：executor 工具级联新增**真实副作用护栏**——`_side_effect_footprint`（只计 pending_actions + cross_session_sends，修正了 gateway 侧把纯查询也当副作用的口径）；失败时足迹超过进入循环前基线 → 停止级联 + 清空待提交动作 + 打 `astrmai_side_effect_cascade_stop` 标记，防止跨模型重放真实发送。**决策记录**：不在级联层强制 fatal 终止——gateway 的 fatal 语义是"本模型别重试"，换模型（可能换 provider）对 429/quota 类是合法救济。
- 测试：`tests/regression/architecture/test_provider_pool_robustness.py` 10 条（stash 红验证 6 红）；compaction/gateway_policy 相关 47 项既有测试全绿。
- 待部署验收：ProviderNotFoundError 不再出现 backoff 重试序列；GatewayUsage/trace provider 家族分布非 unknown；灰度观察 cache_control 启用后 429 情况。

## 目标

- 不存在的 provider 不再被空转重试：not-found 立即切下一模型（当前按可重试错误处理，重试 max_retries+1 次夹 backoff）。
- compaction 首试必败消除：启动期校验 provider id 存在性，无效即 WARN 并剔除。
- provider 能力解析恢复（当前 1005/1005 全 unknown）：cache_control/远程会话特性不再形同虚设，成本归因可按 provider 家族分析。
- 工具副作用与模型级联的边界收紧：已执行真实副作用（发私聊/戳人）的失败轮不再整轮换模型重跑。

## 基线证据

- **TG-02**：`gateway_policy.py:169-191` `_is_fatal_failure` 关键字表（429/403/quota/timeout/408/504…）不含任何 not-found 类；`_classify_failure_kind` 归 UNKNOWN。实证 ProviderNotFoundError 模型尝试 3 次、star.context WARN 4 条。失败矩阵 (timeout|5xx|not-found)×(primary|fallback) 零测试。
- **RT-07**：`compaction_providers.py:24-28` 把配置的 `compaction_provider_id` 无验证放候选首位；服务器残留旧 id `openai/deepseek-v4-pro` → 每次压缩首试必败。
- **RT-08**：`provider_capabilities.py:107-121` 先调可能缺失的 `get_provider_by_id`，回退时把完整模型 ID 当 provider type 匹配家族表 → 必然 unknown；`supports_cache_control/supports_remote_session` 恒 False。
- **TL-04**（LIKELY）：`executor.py:1065-1082` `except Exception → continue` 换模型重跑**整个工具循环**（副作用在 try 内已执行）；主控复核加重证据：L1075 计算了 `fatal=_is_executor_failure_fatal(...)` 但**只写日志从不终止级联**；`space_transition` 去重键为 (target_id, 精确文本)，跨模型措辞不同必失配；`pending_actions` 失败后仍留共享 extras 随任意成功发送被提交。16h 无 space_transition 实例（故 LIKELY），但同构触发（工具轮失败→换模型）日志 3 次。

## 实施步骤

1. **先补失败矩阵测试**（TG-02，先锚定现状再改）：参数化 (timeout|5xx|not-found)×(primary|fallback)，断言各组合 attempt 次数、backoff、最终选中模型。
2. `_is_fatal_failure` 加 not-found 关键字（`没有找到`/`not found`/`provider`——或独立 `FailureKind.PROVIDER_NOT_FOUND`，断言单次尝试即切换）。
3. RT-07：bootstrap 或 compaction 初始化对 `compaction_provider_id` 做 `get_provider_by_id` 校验，不存在则一次性 WARN 并从候选剔除。
4. RT-08：能力解析回退前按 `/` 拆 provider 段并查 `context.get_all_providers()` 按 id 前缀匹配 provider 对象/type；部署后灰度观察 429（家族识别变化会改变 cache_control 行为）。
5. TL-04：先写级联测试（工具 call 写入 cross_session_sends 后 gateway 抛级联异常，断言第二模型未被调用/未重放 send）；然后 executor except 分支检查 `_tool_side_effect_count(event)` 超过进入循环前基线时停止级联、改走诚实降级；`_handle_fatal_fallback` 前清空或标记 pending_actions；同时让 L1075 的 fatal 分类真正参与终止决策。注意 `_tool_side_effect_count` 当前把纯查询工具也计入（gateway_lane.py:187-193），需先修计数口径再做判定，避免一次查询就禁掉 gateway 级重试。

## 验收标准

- 失败矩阵测试 + 级联副作用测试全绿；全量 pytest 绿。
- 部署后：配置假 provider id 启动只见一次性 WARN、压缩不再首试必败、not-found 单次尝试即切换；GatewayUsage/trace 的 provider 字段出现真实家族（unknown 占比 ≈0）；无副作用重放事故。

## 风险与回退

- 改 fatal 判定影响所有失败路径——矩阵测试先行锚定，改动后逐条对比。
- TL-04 **过度收紧的风险**：副作用后的可恢复失败（如输出格式错）会直接放弃回复。缓解：仅禁"整轮重跑"，保留基于已得工具结果的文本重写路径。
- RT-08 中风险：能力识别变化启用 cache_control 后需灰度观察 provider 429。
- 各项独立提交可单独 revert。

## 完成记录

（完成后填写：失败矩阵测试清单、provider 归因修复后的家族分布、级联行为变更说明）
