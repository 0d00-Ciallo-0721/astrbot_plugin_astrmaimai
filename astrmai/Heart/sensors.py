# astrmai/Heart/sensors.py
import re
from typing import List
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
import astrbot.api.message_components as Comp
from astrbot.core.star.command_management import list_commands

class PreFilters:
    """
    感知与过滤器 (System 1: Fused Version)
    职责: 构建指令防火墙，执行严格的消息清洗与拦截。
    """
    def __init__(self, config):
        self.config = config
        self.foreign_commands = set()
        self._commands_loaded = False 

    async def _load_foreign_commands(self):
        """异步动态加载系统内所有注册指令"""
        if self._commands_loaded:
            return

        try:
            from astrbot.core.star.command_management import list_commands
            all_cmds = await list_commands()
            if all_cmds:
                for cmd in all_cmds:
                    main_cmd = cmd.get("effective_command")
                    if main_cmd: 
                        self.foreign_commands.add(main_cmd.lower())
                    for alias in cmd.get("aliases", []):
                        self.foreign_commands.add(alias.lower())
            
            # [修复点] 兼容强类型 Config 对象，不再使用 dict.get()
            extra_cmds = []
            if hasattr(self.config, "system1") and hasattr(self.config.system1, "extra_command_list"):
                 extra_cmds = self.config.system1.extra_command_list
                 
            for extra in extra_cmds:
                if extra:
                    self.foreign_commands.add(extra.lower())

            self._commands_loaded = True
            logger.debug(f"[AstrMai-Sensor] 🛡️ 成功加载外部系统指令隔离名单 ({len(self.foreign_commands)} 条)")
        except Exception as e:
            logger.warning(f"[AstrMai-Sensor] ⚠️ 加载外部指令列表失败: {e}")

    async def should_process_message(self, event: AstrMessageEvent) -> bool:
        """
        核心网关：判断是否应该处理这条消息，并打上指令隔离标签。
        """
        await self._load_foreign_commands()

        # 1. 忽略 Bot 自身发出的消息
        if event.get_sender_id() == event.get_self_id():
            return False

        # 2. 深度清洗文本与负载检测
        clean_text_parts = []
        has_payload = False
        image_urls = []
        
        if event.message_obj and event.message_obj.message:
            for seg in event.message_obj.message:
                if isinstance(seg, (Comp.At, Comp.Reply)):
                    continue 
                if isinstance(seg, Comp.Plain):
                    text = seg.text.replace('\u200b', '').strip()
                    if text: 
                        clean_text_parts.append(text)
                if isinstance(seg, (Comp.Image, Comp.Video, Comp.Record, Comp.File)):
                    has_payload = True
                if isinstance(seg, Comp.Image) and seg.url:
                    image_urls.append(seg.url)
        
        clean_text = " ".join(clean_text_parts).strip().lower()
        
        # 记录提取的图片信息，供 AttentionGate 放入 LastMessageMetadata
        event.set_extra("extracted_image_urls", image_urls)
        
        # 3. 🚨 核心指令拦截防火墙 🚨
        if clean_text:
            words = clean_text.split()
            if words:
                first_word = words[0]
                cmd_key_no_prefix = first_word[1:] if first_word.startswith("/") else first_word
                
                if (first_word in self.foreign_commands or cmd_key_no_prefix in self.foreign_commands):
                    logger.debug(f"[AstrMai-Sensor] 🛑 隔离网关：精准识别到指令 [{first_word}]，彻底拦截。")
                    event.set_extra("astrmai_is_command", True)
                    return False

        # 4. 空消息检查
        if not clean_text and not has_payload:
            return False

        # 5. 昵称点名提权
        raw_msg = event.message_str or ""
        nicknames = self.config.system1.nicknames
        if nicknames and raw_msg:
            for nickname in nicknames:
                if nickname and nickname in raw_msg:
                    logger.debug(f"[AstrMai-Sensor] 🔔 触发昵称点名: {nickname}")
                    event.set_extra("astrmai_bonus_score", 1.0) 
                    return True

        return True

    def is_wakeup_signal(self, event: AstrMessageEvent, bot_self_id: str) -> bool:
        """检测是否为强唤醒信号 (@Bot)"""
        if event.get_extra("astrmai_is_command"):
            return False

        if not event.message_obj or not event.message_obj.message:
            return False
            
        try:
            for component in event.message_obj.message:
                if isinstance(component, Comp.At):
                    if str(component.qq) == str(bot_self_id):
                        return True
        except Exception:
            pass
            
        return False

    async def is_command(self, text: str) -> bool:
        """
        [新增] 判断文本是否命中指令防火墙
        """
        if not text: return False
        
        # 1. 检查基础指令前缀 (接入 Config)
        if any(text.startswith(prefix) for prefix in self.config.global_settings.command_prefixes):
            return True
            
        # 2. 检查动态加载的系统指令库
        first_word = text.split()[0].lower()
        if self.foreign_commands and first_word in self.foreign_commands:
            return True
            
        return False


    def extract_social_relations(self, event: AstrMessageEvent, group_id: str) -> list:
        """
        [新增] 静默抽取社交互动关系，构建群组社交图谱
        返回格式: [(from_user, to_user, relation_type, strength_delta)]
        """
        relations = []
        if not event.message_obj or not event.message_obj.message:
            return relations
            
        from_user = event.get_sender_id()
        
        try:
            for component in event.message_obj.message:
                # 检测 @ 提及行为 (Mention)
                if isinstance(component, Comp.At):
                    to_user = str(component.qq)
                    if to_user and to_user != from_user:
                        relations.append((from_user, to_user, "mention", 0.1))
                
                # 检测回复行为 (Reply) - 如果消息包含引用组件
                elif isinstance(component, Comp.Reply):
                    # 注意：部分平台的 Reply 组件可能不直接提供 target_user_id，这里作示意
                    # 如果有具体的目标用户ID可以提取，则计入强度更高的回复关系
                    # 假设 AstrBot 的某些适配器在 Reply 中有 sender_id
                    to_user = getattr(component, 'sender_id', None) 
                    if to_user and to_user != from_user:
                        relations.append((from_user, to_user, "reply", 0.3))
        except Exception as e:
            logger.debug(f"[AstrMai-Sensor] 抽取社交关系失败: {e}")
            
        return relations    
    
    async def process_poke_event(self, event: AstrMessageEvent, context, attention_gate):
        """
        [优化版] 拦截底层戳一戳事件，执行回戳，并生成虚拟消息送入注意力门控
        """
        # 尝试提取底层 raw_event 数据 (适用于 OneBot/Aiocqhttp 等标准协议)
        raw = getattr(event.message_obj, "raw_event", {})
        
        if raw.get("notice_type") == "notify" and raw.get("sub_type") == "poke":
            sender_id = str(raw.get("user_id", ""))
            target_id = str(raw.get("target_id", ""))
            group_id = raw.get("group_id", "")
            bot_id = str(event.get_self_id())
            
            # 尝试获取发送者名称，若无则降级为 QQ 号
            sender_name = event.get_sender_name() or sender_id
            
            # 获取当前机器人的配置昵称
            bot_name = "我"
            if hasattr(self, 'config') and self.config and hasattr(self.config, 'system1'):
                if self.config.system1.nicknames:
                    bot_name = self.config.system1.nicknames[0]
                    
            target_name = bot_name if target_id == bot_id else target_id
            
            # 1. 捏造虚拟互动标记 (供大模型产生画面感)
            virtual_text = f"(Interaction: {sender_name} -> {target_name})"
            logger.info(f"[AstrMai-Sensor] 👉 捕获互动事件: {virtual_text}")
            
            # 2. 如果被戳的是自己，执行回戳反击逻辑 (采用更现代、兼容性更强的 API)
            if target_id == bot_id:
                try:
                    client = getattr(event, 'bot', None)
                    if client and hasattr(client, 'api'):
                        if group_id:
                            # 群聊回戳需要带上 group_id
                            await client.api.call_action('send_poke', user_id=int(sender_id), group_id=int(group_id))
                        else:
                            # 私聊回戳只需 user_id
                            await client.api.call_action('send_poke', user_id=int(sender_id))
                        logger.info(f"[AstrMai-Sensor] 👈 已回戳反击用户: {sender_name}")
                    else:
                        logger.warning("[AstrMai-Sensor] 无法获取底层 bot 实例，跳过回戳")
                except Exception as e:
                    logger.debug(f"[AstrMai-Sensor] 回戳操作发生异常 (可能由于平台暂不支持): {e}")

            # 3. 伪装事件并强制推入滑动窗口 (无论是否回戳，都让大模型知道自己被戳了)
            event.message_str = virtual_text
            event.set_extra("is_virtual_poke", True)
            event.set_extra("astrmai_bonus_score", 2.0) # 赋予高优权重，提高 AI 理睬的概率
            
            await attention_gate.process_event(event)  