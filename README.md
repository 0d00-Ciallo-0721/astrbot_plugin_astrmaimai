# AstrMai

AstrMai 是面向 AstrBot 的事件驱动型角色聊天插件，提供对话注意力、人格压缩、长期记忆、图片理解、主动互动、QQ 工具调用和管理页面。

## 运行要求

- AstrBot `>= 4.26.4, < 5`
- 已验证 AstrBot `4.26.4` 与 `4.26.6`
- Python 3.12
- 可用的文本模型；图片理解需要额外配置视觉模型
- FAISS、TTS、Sys3、Computer、Cron 均为可选能力，缺失时插件会按功能降级

依赖列在 `requirements.txt`。AstrBot 安装插件时可自动安装，也可以在 AstrBot 的 Python 环境中执行：

```bash
pip install -r requirements.txt
```

## 安装

1. 将发布包目录命名为 `astrmai`。
2. 放入 AstrBot 的 `data/plugins/` 目录。
3. 在 AstrBot WebUI 中启用插件。
4. 等待日志出现：

```text
[AstrMai] boot complete — runtime running
```

不要把源码仓库整体复制到生产环境。正式发布包不应包含 `tests/`、`.agent/`、`.claude/`、虚拟环境、数据库、日志或本机缓存。

## 首次配置

至少需要确认：

- **模型池**：Judge、对话、记忆、视觉等任务使用的模型。
- **接管范围**：私聊是否启用、群聊范围和注意力策略。
- **人格**：AstrBot 当前人格已可读取；启动时会先生成并持久化核心人格。
- **时间设置**：模型请求、整轮预算、视觉等待和回复过期时间。
- **记忆**：召回数量、混合检索、向量模型和维护策略。

配置项均在 AstrBot 插件配置页显示中文名称和说明。旧配置中的越界值会被记录并回退到安全默认值，不应导致整个插件拒绝加载。

### 群聊身份、续聊与昵称唤醒

本专题开发改动等待独立复验。本轮新增的空正文 Reply 修复尚未纳入既有发布包。当前消息的发言人、引用作者与被 @ 的人分别进入上下文，引用第三方不会替换当前发言人的身份。

| 配置项 | 默认值 | 含义 |
|---|---|---|
| `conversation.group_history_readable_ttl_sec` | `7200` | 群聊旧话题超过确认窗口后，仍可按当前消息的承接证据读取有限历史 |
| `conversation.group_history_persist_ttl_sec` | `86400` | 可读期后仅保留少量背景；超过此时长清理 continuity 历史，并非删除数据库 |
| `conversation.restore_recent_message_limit` | `20` | 从现有群聊快照恢复的最近消息条数，范围 5–100；不表示恢复完整数据库历史 |
| `system1.nickname_wakeup_threshold` | `0.7` | 正数昵称分数达到此值才成为强唤醒；零阈值不会让无昵称消息提权 |
| `system1.nickname_at_others_with_question_score` | `0.7` | 明确先呼叫机器人昵称，再 @ 他人并提出问题或请求时的分数 |
| `system1.nickname_prefix_score` | `0.8` | 无 @ 他人干扰时，昵称开头呼叫的分数 |
| `system1.nickname_with_question_score` | `0.6` | 昵称出现在正文中并带疑问或请求语气时的分数 |
| `system1.nickname_mention_only_score` | `0.3` | 单纯提及昵称的分数 |

`@他人 + 机器人昵称` 不会仅因带问号而成为强唤醒；结构化 @ 机器人沿用现有唤醒链。未成为强唤醒的消息仍按普通群聊注意力规则处理，不能将“无强唤醒”理解为保证不回复。

引用机器人但没有新增有效 Plain/图片等媒体正文时，仅保留引用上下文，不因引用本身强唤醒。引用中的原文、@ 他人的展示文字、空白和零宽字符不算追问；单字追问（如“？”）仍有效，结构化 @Bot 仍独立触发。

话题活跃期、确认证据窗口和历史读取时长是不同边界。默认超过 30 分钟后，有承接证据的历史以 `recent_only` 提供；超过 2 小时以 `background` 提供，均不恢复隐藏 provider/lane 会话。快照按条数恢复、不按消息年龄丢弃，但恢复历史的首次读取有有限温区窗口，撤回状态及群隔离仍保留。

## 数据目录

运行数据由 AstrBot 写入插件数据目录，典型容器路径为：

```text
/AstrBot/data/plugin_data/astrmai/
```

其中可能包含：

- `astrmai.db`：会话、记忆、画像、学习和视觉记录。
- `persona_cache.json`：人格核心、说话方式、第一人称改写和八维切片。
- `cache/`：上下文快照和可观测账本。
- 表情包及其他运行资源。

发布包不携带这些文件。新安装时数据目录应由 AstrBot 和插件自行创建。

## 管理页面

插件提供 AstrBot 原生 Plugin Page：

- 页面目录：`pages/admin/`
- 入口：AstrBot WebUI -> 插件 -> `astrmai` -> 插件页面 -> `admin`

管理页用于查看运行状态、主动学习、表达与黑话审核、记忆网络、用户画像和角色切片。页面必须从已登录的 AstrBot WebUI 打开，不能直接双击本地 HTML。

## 可选能力与降级

- **视觉**：可选择“等待图片识别完成”或“超时后以 `[图片]` 继续”。视觉模型不可用时不应阻塞纯文本聊天。
- **向量检索**：embedding 或 FAISS 不可用时，记忆召回降级到 canonical FTS/BM25 与 fallback。
- **TTS**：通过独立 TTS 插件增强；失败时保留文本回复，不向用户发送内部错误。
- **Sys3 / Computer / Cron**：未启用或宿主能力缺失时保持聊天模式。
- **QQ 工具**：由 NapCat/AstrBot 提供实际能力；查询或发送失败时返回真实原因，不凭空构造成功结果。

## 升级

升级前同时备份：

1. 当前插件目录。
2. `data/plugin_data/astrmai/` 数据目录。
3. 当前插件配置。

然后用新的干净发布包替换插件业务文件。不要覆盖数据目录，也不要把发布包内不存在的数据目录解释为需要删除生产数据。

升级后检查：

```text
Plugin astrmai (...)
[AstrMai] memory skeleton initialized
[AstrMai] persona core ready and persisted
[AstrMai] boot complete — runtime running
```

再检查最近日志中没有 AstrMai 相关的 `Traceback`、`ValidationError`、`ModuleNotFoundError` 或 `AttributeError`。

## 回滚

1. 在 AstrBot WebUI 中禁用 AstrMai。
2. 恢复升级前备份的插件目录。
3. 仅在发生数据结构不兼容且确认需要时恢复数据目录备份。
4. 重新启用插件并检查启动日志。

代码回滚与数据回滚应分开处理，避免用旧数据库覆盖升级后产生的新聊天记录。

## 构建发布候选

在源码仓库中执行：

```bash
python scripts/build_release_candidate.py C:\tmp\astrmai-release-candidate
```

输出目录必须位于源码仓库之外。构建器使用白名单复制运行文件，并拒绝数据库、日志、缓存、测试和虚拟环境进入候选包。

构建目标必须是事先不存在的新目录；构建器会清理已存在的输出目录。运行包内禁止绝对 `astrmai` 导入，包括函数内惰性导入；根入口的 standalone 兼容分支保留。

上线前还须在目标宿主的 Python/依赖环境中，对 staging 副本执行宿主包名探针：

```bash
python -I -B scripts/probe_host_import_shape.py /path/to/staging/plugin
```

探针按 `data.plugins.astrmai.astrmai.*` 导入并实际调用引用身份分支，要求顶层 `astrmai` 不可见。脚本不在运行包内，须单独携带；它隔离临时数据根、不初始化插件、不发送消息。该检查只证明此装载与调用边界，不替代真实宿主事件及 QQ 投递验收。

## 验证

开发环境建议执行：

```bash
python -B -m pytest -p no:cacheprovider tests --ignore=tests/manual -q
python -m compileall -q main.py config.py astrmai pages scripts
git diff --check
```

真实环境验收还应覆盖私聊、群聊、图片、记忆召回、工具调用、插件禁用后重新启用和管理页。

Prompt Context 的本地回归和测试隔离已完成，结果与限制见 [本地验收记录](docs/PROMPT_CONTEXT_LOCAL_ACCEPTANCE_20261011.md)。测试使用隔离的宿主数据根，逐用例恢复 AstrBot/插件模块替身；目标 Python 3.12 / AstrBot 4.26.4 与真实 QQ 投递仍待验收。

## 故障排查

- **插件无法加载**：先查看完整 `ValidationError` 或导入堆栈，不要只看最后一行。
- **回复很慢**：核对“时间设置”中的整轮预算、模型请求超时、视觉策略和回复过期时间。
- **图片未理解**：确认视觉模型池可用，并检查视觉策略是否允许超时降级。
- **记忆无结果**：检查写入记录、索引投影状态、embedding/FAISS 可用性和召回 trace。
- **管理页空白或 500**：确认插件运行时已完成启动，并从 AstrBot Plugin Page 入口打开。
- **其他插件命令无响应**：AstrMai 对其他插件命令应只跳过自身处理，不应调用 `event.stop_event()`。
