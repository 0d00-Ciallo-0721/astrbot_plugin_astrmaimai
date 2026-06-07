# P3 — 技术债偿还（~8h）

> 基于终审报告未修复项 | 5 项 | 不阻塞发布

---

## #1 `context_compaction.py` — 拆分 God Class（最大单件）

**文件：** `astrmai/conversation/attention/context_compaction.py`（1722 行，单类 ~1600 行）
**问题：** `ContextCompactionEngine` 职责过多，50+ 方法。
**修复：** 提取三个子组件：

| 组件 | 职责 |
|------|------|
| `CompactionSafetyAnalyzer` | 安全窗口检测、风险分析、冷却期管理 |
| `CompactionWindowSelector` | 候选消息选择、去重 |
| `CompactionExecutor` | 压缩执行、结果合并、后处理 |

`ContextCompactionEngine` 保留为编排层。

**风险：** 高（影响 attention 核心路径）  
**验证：** `python -m pytest tests/test_attention_gate_refactor.py tests/regression/attention/ -q`

---

## #2 `admin_ui_service.py` — 继续拆分

**文件：** `astrmai/webui/backend/services/admin_ui_service.py`（800+ 行）
**问题：** God Object，虽已提取 PersonaUiService、LearningUiService 等，仍剩余多个职责。
**修复：** 继续提取 `ObservabilityUiService`、`SchedulerUiService`。
**风险：** 中

---

## #3 `planner.py` — 拆分巨型方法

**文件：** `astrmai/conversation/planning/planner.py:890-1319`
**问题：** `plan_and_execute` ~430 行。
**修复：** 提取：

| 方法 | 职责 |
|------|------|
| `_prepare_plan_context()` | 上下文收集、装配 |
| `_invoke_planning_llm()` | LLM 调用 + 重试 |
| `_parse_plan_result()` | 结果解析、校验 |

**风险：** 高  
**验证：** `python -m pytest tests/test_planner_cognitive_loop_refactor.py -q`

---

## #4 `bootstrap.py` — 封装属性赋值

**文件：** `astrmai/app/bootstrap.py:200-203`
**问题：** `_build_proactive_task` 通过 `runtime.system2_planner.heartflow_manager = ...` 等直接赋值修改外部对象内部状态。
**修复：** 已有 `_nullify_proactive_refs` 文档标注路线图：

```python
# ProactiveTask 新增方法
def configure(self, deps: ProactiveDeps):
    self.heartflow_manager = deps.heartflow_manager
    self.dream_scheduler.dream_visible = deps.dream_visible
```

**风险：** 中

---

## #5 `chat_loop_kernel.py` — 配置去重

**文件：** `astrmai/conversation/loop/chat_loop_kernel.py:45-110`
**问题：** `SCHEDULER_POLICY_PROFILES` 三组配置大量重复字段。
**修复：**

```python
base_profile = { "idle_backoff": ..., "maintenance_interval": ... }
profiles = {
    "default": { **base_profile, ... },
    "aggressive": { **base_profile, ... },
    "conservative": { **base_profile, ... },
}
```

**风险：** 低  
**验证：** `python -m pytest tests/test_chat_loop_kernel_refactor.py -q`
