# Agent 09

Agent ID:
`019e71c0-9ba0-7660-bff5-027cf247b16e`

状态：
已完成

发现：
1. `[P1]` 命令入口仍绕过统一权限闸门，未授权会话依然能执行 `/mai` 和 `/work`。`check_message_scope_access()` 只在全局消息入口和外部结果嗅探里生效，[message_entry.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/presentation/events/message_entry.py:33) 与 [external_result_bridge.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/ingress/external_result_bridge.py:61) 都会过权限判断，但 [main.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/main.py:105) / [main.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/main.py:123) 的 `@filter.command` 直接调用 presentation handler，没有走 [permission_guard.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/ingress/permission_guard.py:6)；而 [plugin_facade.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/plugin_facade.py:195) 的 `/work` 实现也没有二次校验。最小 stub 复现里，`default:GroupMessage:forbidden-group` 上 `handle_mai_help()` 仍直接返回结果。
2. `[P1]` 去重在 poke 归一化之前执行，且空事件签名统一塌缩成 `obj_empty`，会把不同 notice/poke 事件误判成重复消息。链路上 [message_entry.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/presentation/events/message_entry.py:25) 先跑 `check_message_dedup()`，之后才到 [poke_handler.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/ingress/poke_handler.py:14)；而 [dedupe.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/ingress/dedupe.py:12) 对空 `message_str` 且空 `message_obj.message` 一律生成 `obj_empty`。最小复现里，同一会话同一发送者的两个不同空事件得到相同签名，第二个直接被 `duplicate_message` 拦掉。
3. `[P2]` 外部插件结果嗅探仍保留一套分叉的命令识别逻辑，职责越界且和主 ingress 口径不一致。正常入口通过 [command_guard.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/ingress/command_guard.py:6) 调 [plugin_facade.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/app/plugin_facade.py:137) 的统一判定；但 [external_result_bridge.py](C:/Users/zlj/Desktop/mai/astrmai_plugin_refactored_final/astrmai/conversation/ingress/external_result_bridge.py:33) 又私下实现了 `_is_framework_command()`，在 host facade 不可用时退化到 `sensors.is_command_sync()`。最小对比里，同一运行时下 `PluginFacade.is_framework_command("/help") == True`，但 bridge fallback 返回 `False`。

补充：
`error_interceptor` 本身目前看起来是薄封装，未看到同级别剩余问题。验证时参考测试需带 `PYTHONPATH=.` 才能正常收集。
