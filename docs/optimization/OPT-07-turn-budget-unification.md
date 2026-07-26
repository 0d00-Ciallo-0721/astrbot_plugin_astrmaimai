# OPT-07 延迟预算统一（tool / vision 纳入 turn 预算）

状态：代码完成（待 420s 级事故样本复采归零） ｜ 优先级：P1 ｜ 依赖：OPT-02（已完成） ｜ 覆盖发现：RT-04(P2)、RT-05(P1)、TG-03(P1) ｜ 预算体系目前三态并存：chat 路径 clamp 错轮、tool 路径完全不受约束、vision 旁路无上限——420s 的 turn 7edddd 是三者叠加的完整事故样本。

## 完成记录

**2026-07-26 代码侧完成**：

- **预算语义定稿**（写入代码注释与 schema hint）：主回复（gateway.tool）clamp 到剩余预算但以 `main_reply_reserve_sec` 兜底（预算耗尽仍允许在保留额内完成主回复）；vision/后台路径 clamp 无兜底，耗尽即止。
- 改动：`gateway_lane._tool_loop_total_timeout` 接入 `remaining_turn_budget` + reserve 兜底（RT-04）；`executor` 视觉旁路新增 `_vision_side_path_timeout_override()`（min(配置图片分析超时, 剩余预算)，≤0.5s 跳过整个旁路）并给 `call_vision_task` 传 `timeout_override`（RT-05①③）；`private_turn_coordinator.prepare_batch` 增加 `deadline` 参数 + `vision_total_budget_sec()` 公共访问器，gate 合并循环把 burst deadline 持久化到 `SessionContext.vision_burst_deadline`（跨迭代不重置，批次派发时清零；对旧签名 stub 保留 TypeError 回退）（RT-05②）；`message_entry._configure_turn_budget` 接线失败提级 WARN 并以 360/90 默认兜底（TG-03①）。
- 测试：新增 `tests/test_turn_budget_e2e_refactor.py` 9 条（接线成功/失败兜底、tool 环预算矩阵 4 态、vision 超时 2 态、coordinator 总额访问器）；**stash 红验证 4 条变红**；受影响套件 123 passed。
- 待部署验收：不再出现 `exhausted=true` 后主回复仍执行的 turn；vision 轮 `turn_total_elapsed_ms` p95 <120s；gateway.chat max 从 122.8s 回落；fatal_no_send 计数不升（灰度观察 clamp 误杀）。

## 目标

- turn 预算（total 360s / 主回复保留 90s）成为**真实上限**：主回复（gateway.tool）与视觉链路都受预算约束。
- 单张图片不再可能烧掉整轮预算：三层重试相乘（框架 5 × 网关 3 × 池 7 模型）被总额封顶。
- 预算体系三个执法点（接线/网关耗尽/judge 耗尽降级）有端到端测试守护——当前零测试，接线异常被 `except: logger.debug` 静默吞掉。

## 基线证据

- **RT-04**：`gateway_lane.py:182-185` `_tool_loop_total_timeout = max(api_timeout, tool_timeout)`，全程无 `clamp_timeout_to_turn_budget`；trace 7edddd：`budget.remaining_ms=0, exhausted=true` 后 dialog 照常 8.3s 成功，总轮长 420s。
- **RT-05**：`executor.py:682-688` 视觉旁路 `call_vision_task` 裸调用无 timeout_override 无总额；4da2910 的屏障策略只覆盖 coordinator 路径，且 `gate.py:1310-1315` 合并循环每次迭代重新起算 180s；vision 池是唯一逐模型全遍历的任务。实证：7edddd 三条 vision ledger 109.1s/71.0s/122.8s + 6 条 deadline 秒败；[Gemini] request_retry 43 条同窗（15:25-15:29）。
- **TG-03**：`message_entry.py:145-156` `_configure_turn_budget` 整体 try/except→debug 日志——config.timing 字段改名即预算静默失效、clamp 全变 no-op 且测试全绿；`turn_total_budget_sec` 在 tests/ 零出现。生产 remaining_ms p05=0 说明预算确实被顶到耗尽边界。

## 方案决策

预算语义需要一次明确定稿（当前"未经设计确认"）：**推荐** —— 主回复路径 clamp 但以 `main_reply_reserve_sec`(90s) 为下限兜底（预算耗尽时仍允许在保留额内完成主回复，而不是直接失败）；vision/后台路径 clamp 到剩余预算无兜底。把这条语义写进 config hint 与测试断言。

## 实施步骤

1. **先补测试**（TG-03 三点，先红后绿）：① message_entry 接线断言 `snapshot['budget'].total_budget_sec` 来自 config.timing（并把接线失败从 debug 提为 WARN+默认值兜底）；② 慢模型（可控 sleep）下 dialog 调用被 clamp 到剩余预算且 exhausted 标记正确；③ judge 在预算=0 时走 `judge_budget_exhausted` 降级（decision_router.py:123-128）。
2. RT-04：`tool_chat_in_lane_result` attempt 循环前 clamp（`reserve_for_reply=False`，它本身就是 reply），预算 < 保留额时以保留额兜底。
3. RT-05 三缺口：① executor 视觉旁路传 timeout_override 并套用 vision_barrier_total 同款总额；② coordinator 屏障 deadline 存到 session/事件级，合并迭代不重置；③ `call_vision_task` 对同池顺序尝试加轮预算 clamp。
4. 模拟 502 provider 重放图片消息：断言总视觉耗时 ≤ 配置总额、轮长 ≤ budget、mood/cognitive/memory 不再被拖到集体 deadline 秒败。

## 验收标准

- 新增预算 e2e 测试三条 + vision 总额测试全绿；全量 pytest 绿。
- 部署复采：不再出现 `exhausted=true` 后主回复仍执行的 turn；vision 轮 `turn_total_elapsed_ms` p95 < 120s（基线单例 420s）；gateway.chat max 从 122.8s 显著回落。

## 风险与回退

- **中风险**：clamp 过严会误杀长工具链/慢 provider 下的图片轮。缓解：主回复保留额兜底语义 + vision 保留 timeout_fallback 策略；灰度期监控 fatal_no_send 计数（基线 7/518）不升。
- 预算语义定稿后写入 `_conf_schema.json` hint，避免后续理解漂移。
- 回退：三处独立提交；revert 后回到"预算只约束部分路径"的现状。

## 完成记录

（完成后填写：预算语义定稿文本、vision 轮时延分布前后、budget 测试清单）
