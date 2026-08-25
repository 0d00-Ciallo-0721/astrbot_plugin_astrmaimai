# AstrMai 真实 LLM 试跑需求与配置

本文档对应当前工作树中的两个入口：

- `tests/manual/live_llm_probe.py`：Provider 裸 API / LLM 协议探针，`measurement_scope=provider_probe`。
- `tests/manual/astrmai_host_probe.py`：AstrMai Host/Gateway 场景探针，`measurement_scope=astrmai_host`。

本轮目标如果只是比较模型耗时、并发、超时、失败和协议行为，先使用 Provider probe；它不经过 AstrMai Gateway、Attention、Executor、Memory 或后台预算。只有配置真实 Host 事件 adapter/URL 后，才运行 Host probe。

## 必需文件

Provider probe 至少需要：

1. AstrBot Host 配置文件：`cmd_config.json`，包含启用的 `provider_sources`、API base、Provider key 和模型条目。
2. AstrMai 活动配置文件：`astrmai_config.json`，包含 `provider.task_models`、`agent_models`、`fallback_models`、`vision_models`、`embedding_models`、`infra` 和 `timing`。配置文件名跟随实际插件目录名 `astrmai`。
3. 有效的 Provider API key。推荐放在独立的 `astrmai_live_secrets.json`，产物只保存 fingerprint。

Host probe 额外需要：

1. 可访问 AstrMai 管理 API 的 Host 地址。
2. `ASTRMAI_HOST_EVENT_ADAPTER` 或 `ASTRMAI_HOST_EVENT_URL` 其中之一。
3. adapter 能返回真实 Host 调用证据，而不是本地生成的占位 ID。

## 配置变量

完整无密钥模板见同目录的 `llm_live_run.example.env`。

Provider probe：

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `ASTRMAI_HOST_CMD_CONFIG` | 是 | AstrBot `cmd_config.json` 的绝对路径 |
| `ASTRMAI_PLUGIN_CONFIG` | 是 | AstrMai JSON 配置的绝对路径 |
| `ASTRMAI_LIVE_API_KEY` | 是 | 本轮有效 API key；不写入产物 |
| `ASTRMAI_LIVE_SECRETS_FILE` | 推荐 | 独立密钥 JSON；优先级低于环境变量、高于旧配置内嵌 key |
| `ASTRMAI_LIVE_BASE_URL` | 否 | 覆盖 Host provider source 的 API base；覆盖时必须确认一致性 |
| `ASTRMAI_LIVE_MODEL` | 否 | 单模型探测；不设置时使用 task pool 第一个模型 |
| `ASTRMAI_LIVE_ENVIRONMENT` | 否 | 产物环境标签，例如 `staging` |
| `ASTRMAI_LIVE_REGION` | 否 | 产物区域标签 |

Host probe：

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `ASTRMAI_HOST_BASE_URL` | 是 | AstrBot/Host HTTP 根地址，例如 `http://127.0.0.1:PORT` |
| `ASTRMAI_HOST_API_PREFIX` | 否 | 默认 `/astrmai/admin` |
| `ASTRMAI_HOST_API_KEY` | 视 Host 鉴权 | 只保存 fingerprint |
| `ASTRMAI_HOST_EVENT_ADAPTER` | 与 event URL 二选一 | `module:function`，推荐真实进程内 adapter |
| `ASTRMAI_HOST_EVENT_URL` | 与 adapter 二选一 | 明确提供的 JSON POST 事件入口；不会猜私有路由 |
| `ASTRMAI_HOST_TIMEOUT_SEC` | 否 | Host 管理 API 超时，默认 10 秒 |
| `ASTRMAI_HOST_ADAPTER_TIMEOUT_SEC` | 否 | Python/HTTP 事件调用超时，默认跟随 Host timeout |
| `ASTRMAI_HOST_SAMPLE_INTERVAL_SEC` | 否 | Runtime 采样周期，默认 1 秒 |

## Provider 试跑命令

先做单模型短请求：

```powershell
python tests/manual/live_llm_probe.py `
  --model openai/deepseek-v4-flash `
  --levels 1 `
  --calls-per-level 5 `
  --max-calls 5 `
  --timeout-sec 45
```

并发阶梯：

```powershell
python tests/manual/live_llm_probe.py `
  --model openai/deepseek-v4-flash `
  --levels 1,2,3,4,8 `
  --calls-per-level 20 `
  --max-calls 100 `
  --context-profile medium `
  --rounds 4 `
  --max-tokens 512 `
  --timeout-sec 45
```

协议场景按需分开运行，避免无法区分失败来源：

```powershell
python tests/manual/live_llm_probe.py --model openai/deepseek-v4-flash --stream --levels 1,2,3 --calls-per-level 5 --max-calls 15
python tests/manual/live_llm_probe.py --model openai/deepseek-v4-flash --json --levels 1,2 --calls-per-level 5 --max-calls 10
python tests/manual/live_llm_probe.py --model openai/deepseek-v4-flash --tool-call --levels 1,2 --calls-per-level 5 --max-calls 10
python tests/manual/live_llm_probe.py --model openai/deepseek-v4-flash --vision --levels 1,2 --calls-per-level 5 --max-calls 10
```

Provider 产物目录为：

```text
artifacts/live_validation/<run_id>/
  run.json
  calls.jsonl
  summary.json
```

重点字段：`model_id`、`provider_id`、`request_started_at`、`completed_at`、`total_elapsed_ms`、`status`、`error_class`、`finish_reason`、`response_nonempty`、`retry_count`、`fallback`、`stream_*`、`json_*`、`tool_call_*`。
长上下文轮次额外记录：`context_profile`、`context_chars_requested`、`prompt_chars`、`conversation_rounds` 和 `usage.*` token 统计。

推荐使用矩阵运行器编排中长上下文测试。它默认只生成计划，不会发起请求；
只有显式加上 `--execute` 才会执行，并在每个阶段前检查全局调用上限：

```powershell
python tests/manual/run_provider_matrix.py `
  --profiles medium:4,long:8,xlong:8 `
  --levels 1,2,3,4 `
  --calls-per-level 10 `
  --max-total-calls 120 `
  --max-tokens 512 `
  --output-dir artifacts/live_validation
```

确认预算和 Key 后再追加 `--execute`。任一阶段失败时矩阵停止，后续阶段不
自动继续；矩阵 manifest 会记录计划调用数、实际启动数和停止原因。

## Host/Gateway 试跑命令

Host 入口需要先配置真实 adapter 或事件 URL：

```powershell
$env:ASTRMAI_HOST_BASE_URL = "http://127.0.0.1:PORT"
$env:ASTRMAI_HOST_API_KEY = "<host-key>"
$env:ASTRMAI_HOST_EVENT_ADAPTER = "tests.helpers.real_host_adapter:inject"
$env:ASTRMAI_HOST_ADAPTER_TIMEOUT_SEC = "45"
$env:ASTRMAI_HOST_SAMPLE_INTERVAL_SEC = "1"
```

然后按阶段执行：

```powershell
python tests/manual/astrmai_host_probe.py --scenario main_reply_private --repeat 3
python tests/manual/astrmai_host_probe.py --scenario multi_group_queue_b01 --repeat 1
python tests/manual/astrmai_host_probe.py --scenario judge_b05 --repeat 1
python tests/manual/astrmai_host_probe.py --scenario tool_loop --repeat 1
python tests/manual/astrmai_host_probe.py --scenario memory_b07 --repeat 1
python tests/manual/astrmai_host_probe.py --scenario background_b08 --repeat 1
```

Host adapter 的最小结果合同：

```json
{
  "status": "completed",
  "final_status": "completed",
  "host_event_id": "event_from_host",
  "host_chat_id": "chat_from_host",
  "host_event_type": "group_message",
  "injected_at": "2026-01-01T00:00:00Z",
  "host_turn_id": "turn_from_host",
  "trace_id": "trace_from_host",
  "metrics": {
    "turn_total_elapsed_ms": 4200,
    "gateway_queue_wait_ms": null
  }
}
```

完成态的 ID 必须来自 Host/Trace。`accepted`、`queued`、`pending` 或缺少 Trace 会被标记为 `measurement_incomplete`。无法从 Runtime/Trace 取得的值必须为 `null`，不能填 `0` 或 `false`。

场景专属证据：

- B01：`gateway_queue_wait_ms`、`semaphore_wait_ms`、`lane_wait_ms`、`sys2_lock_wait_ms`、`executor_lock_wait_ms`。
- B05：`judge_called`、`judge_skipped`、`filter_reason`、`expected_action`、`actual_action`。
- B07：`vector_status`、`index_generation`、`faiss_latency_ms`、`fallback_source`、`outbox_pending_count`。
- B08：`background_active`、`queue_wait_ms`、`execution_timeout`、`late_completed`。

Host 产物目录为：

```text
artifacts/live_validation/<run_id>/
  run.json
  summary.json
  host_requests.jsonl
  runtime_samples.jsonl
  turns.jsonl
  stages.jsonl
  report.md
```

## 通过条件

Provider probe：

- HTTP 200 和非空回复达到目标比例；
- 记录 P50/P95/P99、超时、失败类别和协议合同结果；
- 不把裸 Provider 结果解释成 AstrMai Gateway 结果。

Host probe：

- 所有目标场景 `scenario_status == passed`；
- 无 `not_configured`、`measurement_incomplete`、`timeout`、`skipped`、`budget_exhausted` 或 `degraded`；
- 完成态均有 Host event/turn/trace 关联；
- B01/B05/B07/B08 证据可追溯；
- Gateway 队列不可得时明确记录 `null`，不解释为队列为空；
- 产物不含 API key、Authorization、完整 prompt、消息正文或响应正文。

独立密钥文件示例见 `llm_live.secrets.example.json`。复制为目标机上的 `astrmai_live_secrets.json` 后填写 Key，不要提交到 Git。

注意：Provider 探针可以直接读取该文件；AstrBot 核心仍按自身规则读取 `cmd_config.json`。完整 Host 运行前，需要通过启动前注入/临时运行配置把同一组 Key 提供给 AstrBot，不能只在 AstrMai 插件配置中声明而期待核心自动读取。
