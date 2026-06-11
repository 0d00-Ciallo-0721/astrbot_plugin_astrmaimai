# 窗口11-Prompt12-结构收口与主链验收补测审查报告

## 审查范围
- `astrmai/conversation/contracts/message_scope.py`
- `astrmai/conversation/execution/text_segmenter.py`
- `tests/test_message_scope_contract_refactor.py`
- `tests/integration/host/test_host_mock_validation.py`
- `artifacts/review_new/03-模块-M3-规划执行与主对话链.md`
- `artifacts/review_new/08-模块-M8-表现层多模态与对外命令.md`
- `artifacts/review_new/09-模块-M9-测试与运行验证契约.md`
- `artifacts/review_new/12-全局审计-残留引用.md`
- `artifacts/review_new/13-汇总-AstrMai最终审查报告.md`

## 审查方法
- 通读本轮 diff，并检查相邻调用点与热路径，而不是只看改动文本本身。
- 复核 `message_scope` 权威定义、`semantic_chunk()` 删除后的仓内引用状态，以及入口 smoke 是否真实经过 `main.py -> PluginFacade.on_global_message() -> yield reply`。
- 重新执行与本窗口直接相关的测试集合，确认结构收口、入口链路和既有 `main.py` / reply 相关回归未受破坏。

## 审查结论
- 本轮范围内**未发现新的实现缺陷、结构回退或报告失真问题**。
- `message_scope` 已收口为单权威实现；`conversation/contracts/message_scope.py` 当前仅为兼容 re-export，不再构成第二实现源。
- `TextSegmenter.semantic_chunk()` 已删除，仓内未发现遗留调用；“死且错”的误导性能力已清理。
- 新增 smoke 测试真实经过 `main.AstrMaiPlugin.on_global_message()` 与 `PluginFacade.on_global_message()`，能够把事件送入入口主链并产出可见 `yield` 结果；同时报告中也如实保留了“这不是完整宿主 / planner-executor 全链验收”的边界说明。
- 五份 `review_new` 文档与当前代码状态一致，没有把“已收口问题”继续写成待修项，也没有把这轮 smoke 夸大成全量验收闭环。

## 验证记录
- `python -m unittest tests.test_message_scope_contract_refactor tests.integration.host.test_host_mock_validation tests.test_reply_service_refactor tests.test_main_reverse_session_hook_refactor -q`
  - 结果：`28 passed, 1 skipped`
- `python -m unittest tests.test_main_reverse_session_hook_refactor tests.test_main_reply_request_trace_refactor tests.test_main_reply_live_providers_refactor tests.test_main_reply_cache_replay_live_refactor tests.test_presentation_commands_refactor -q`
  - 结果：`18 passed`
- 额外搜索确认：
  - 仓内未发现 `semantic_chunk()` 的残留生产/测试调用
  - 仓内未发现生产代码继续把 `conversation/contracts/message_scope.py` 当作独立权威实现使用

## 最终判定
- 本窗口在当前审查范围内可判定为**无历史遗留问题**。
- 后续若继续推进相关子域，下一步重点应转向更高层级的真实宿主验收或 planner/executor 深链验证，而不是回头重开 Prompt 12 已收口的 `message_scope` / `semantic_chunk()` 问题。
