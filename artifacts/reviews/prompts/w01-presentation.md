# 开发窗口 01：Presentation/Events 修复

## 必须先读取的审查报告
1. `artifacts/reviews/r09-presentation.md` — 完整发现清单（1🔴 3🟡 6🟢）
2. `artifacts/reviews/r15-master.md` — 总报告，了解全局上下文

## 目标文件
- `astrmai/presentation/events/message_entry.py` — 主消息入口（核心）
- `astrmai/presentation/commands/review_commands.py` — 审查命令
- `astrmai/presentation/events/startup_hooks.py` — 启动钩子
- `astrmai/presentation/events/error_interceptor.py` — 错误拦截
- `astrmai/presentation/events/result_sniffer.py` — 结果嗅探
- `astrmai/presentation/commands/mai_help.py` — 帮助命令
- `astrmai/presentation/dto/command_models.py` — 命令 DTO
- `astrmai/presentation/dto/message_scope.py` — 消息作用域 DTO

## 依赖
无底层依赖。Presentation 是最底层入口模块，只依赖 facade 接口。

---

## 🔴 严重（1 项）

### P1-1：自消息检查滞后于防抖和权限检查
- **文件**：`astrmai/presentation/events/message_entry.py:40`
- **当前代码**：`scope.sender_id == scope.self_id`（跳过机器人自己发出的消息）在第 40 行
- **问题**：第 31 行的 `check_message_dedup` 和第 34-38 行的 poke/命令/权限检查在 self-message 检查之前执行。如果框架回环注入机器人自己的回复，防抖（TTL 1.5s）可能无法完全拦截，导致不必要的计算后才被拦截。
- **最小修复**：将 self-message 检查移至第 31 行 `check_message_dedup` 之后（约第 31-32 行之间），确保在处理任何业务逻辑之前就拦截自消息。

---

## 🟡 中等（3 项）

### P1-2：主流水线全路径缺少错误处理
- **文件**：`astrmai/presentation/events/message_entry.py:27-55`
- **问题**：`handle_global_message` 依次调用 `handle_poke`、`check_message_dedup`、`check_framework_command`、`check_message_scope_access`、`handle_group_reply_wait`、`try_consume_reflect_feedback`、`record_and_dispatch_attention`、`suppress_default_llm_if_engaged` 等 8 个外部方法，**没有任何一个调用被 `try/except` 包裹**。任一方法抛出异常都会导致整条消息静默丢弃且无日志。
- **最小修复**：在关键段添加统一的 `try/except` 捕获，至少 `logger.exception` 记录上下文。不需要包裹所有 8 个调用——优先覆盖最可能失败的 3-4 个（如 `record_and_dispatch_attention`、`suppress_default_llm_if_engaged`）。

### P1-3：review_commands 输入校验缺失
- **文件**：`astrmai/presentation/commands/review_commands.py:10-20`
- **问题**：
  - `get_review_detail` 的 `pattern_id: str` 未做空字符串/格式校验，直接传给 facade
  - `submit_review` 的 `weight_delta: float` 未做范围限制（如 [-1.0, 1.0]）
  - `replacement_expression`/`style`/`reason` 未做长度限制
- **最小修复**：在 presentation 层添加轻量防御性校验（拒绝空 pattern_id、clamp weight_delta、限制字符串长度 ≤ 1000）

---

## 🟢 建议（6 项）

次要代码整洁项，详见 `r09-presentation.md` 的 🟢 部分：
- `message_entry.py` 中 `msg` 与 `msg_str` 重复计算
- `event` 参数缺少类型标注
- 三个事件入口缺少 docstring
- `handle_mai_help` 不必要的 async generator
- `command_models.py:38` 的 `object` → `Any`
- `message_scope.py:30` 的 `umo` 命名晦涩

---

## 验证命令
```powershell
$env:PYTHONPATH='.'
python -m pytest tests/test_attention_gate_refactor.py tests/test_reply_service_refactor.py tests/test_legacy_compat_refactor.py -q
```

## 成功标准
- 🔴 P1-1 修复：自消息检查提前至防抖之后
- 🟡 P1-2 修复：主流水线至少 3 个关键调用有异常捕获
- 🟡 P1-3 修复：review_commands 有基本输入校验
- 所有测试通过（无新增回归）
