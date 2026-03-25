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
        
        # ==========================================
        # [新增] 阶段一：特征探针提取 (主脑视觉直通车)
        # ==========================================
        has_at_bot = False
        reply_image_urls = []
        bot_id = str(event.get_self_id())
        # 判断私聊环境 (如果不存在 group_id 则为私聊)
        is_private = not bool(event.get_group_id())

        def _scan_reply_chain(chain):
            """递归扫描引用链中的图片"""
            urls = []
            if not chain: return urls
            for c in chain:
                if isinstance(c, Comp.Image) and getattr(c, 'url', ''):
                    urls.append(c.url)
                elif isinstance(c, Comp.Reply) and hasattr(c, 'chain'):
                    urls.extend(_scan_reply_chain(c.chain))
            return urls

        if event.message_obj and event.message_obj.message:
            for seg in event.message_obj.message:
                # [新增] 探针：检测消息体内是否 @ 了 Bot
                if isinstance(seg, Comp.At) and str(seg.qq) == bot_id:
                    has_at_bot = True
                    
                # [新增] 探针：检测引用组件，并递归挖掘被引用消息中的图片
                if isinstance(seg, Comp.Reply):
                    if hasattr(seg, 'chain'):
                        reply_image_urls.extend(_scan_reply_chain(seg.chain))
                    continue # 忽略引用组件对纯文本的干扰
                    
                # 忽略艾特对纯文本的干扰
                if isinstance(seg, Comp.At):
                    continue 
                
                # 提取纯文本
                if isinstance(seg, Comp.Plain):
                    text = seg.text.replace('\u200b', '').strip()
                    if text: 
                        clean_text_parts.append(text)
                        
                # 【核心逻辑】：必须检测 Comp.Image 及其他媒体载荷
                if isinstance(seg, (Comp.Image, Comp.Video, Comp.Record, Comp.File)):
                    has_payload = True
                    
                # 顺手提取 URL
                if isinstance(seg, Comp.Image) and getattr(seg, 'url', ''):
                    image_urls.append(seg.url)
                    
        # [新增] 封装着色逻辑：主脑视觉直通车
        direct_vision_urls = []
        
        if is_private and image_urls:
            # 1. is_private: 私聊环境且存在图片
            direct_vision_urls.extend(image_urls)
        elif has_at_bot:
            # 2. is_at_with_image: 群聊环境 + @Bot + 存在图片
            if image_urls:
                direct_vision_urls.extend(image_urls)
            # 3. is_reply_with_image: 群聊环境 + @Bot + 引用历史中存在图片
            if reply_image_urls:
                direct_vision_urls.extend(reply_image_urls)
                
        # 一旦命中上述条件，进行去重并注入高优先级特权标志
        if direct_vision_urls:
            unique_direct_urls = list(dict.fromkeys(direct_vision_urls))
            event.set_extra("direct_vision_urls", unique_direct_urls)
            logger.debug(f"[AstrMai-Sensor] 👁️ 捕获主脑直通车视觉特征！提取直通 URL 数: {len(unique_direct_urls)}")
        # ==========================================

        clean_text = " ".join(clean_text_parts).strip().lower()
        
        # 记录提取的图片 URL，供后续模块备份足迹使用
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

        # 4. 空消息兜底检查
        # 【关键】：如果是一张纯图片，clean_text 为空，但 has_payload 为 True，消息将被安全放行！
        if not clean_text and not has_payload:
            return False

        # 5. 昵称点名提权
        raw_msg = event.message_str or ""
        nicknames = self.config.system1.nicknames if hasattr(self.config.system1, 'nicknames') else []
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
        if not text:
            return False

        # 确保外部指令库已加载，避免冷启动时首条指令漏检
        await self._load_foreign_commands()
        
        # 1. 检查基础指令前缀 (接入 Config)
        if any(text.startswith(prefix) for prefix in self.config.global_settings.command_prefixes):
            return True
            
        # 2. 检查动态加载的系统指令库
        first_word = text.split()[0].lower()
        cmd_key_no_prefix = first_word[1:] if first_word.startswith("/") else first_word
        if self.foreign_commands and (first_word in self.foreign_commands or cmd_key_no_prefix in self.foreign_commands):
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
        [优化版] 拦截底层戳一戳事件，执行回戳，并生成虚拟消息送入注意力门控 (带数据自愈闭环)
        """
        # 1. 🟢 安全获取 bot_id
        bot_id = ""
        if hasattr(event.message_obj, 'self_id'):
            bot_id = str(event.message_obj.self_id)
        elif hasattr(event, 'bot') and hasattr(event.bot, 'self_id'):
            bot_id = str(event.bot.self_id)
            
        if not bot_id and hasattr(event, "get_self_id"):
            try: bot_id = str(event.get_self_id())
            except Exception: pass
                
        if not bot_id: bot_id = "unknown"

        sender_id = str(event.get_sender_id())
        if sender_id == bot_id: return

        is_poke = False
        target_id = ""
        group_id = str(event.get_group_id() or "")
        
        # 🟢 [深度修复 1] 递归查找 OneBot 底层 payload，突破封装层，解决 target_id 丢失
        def _find_poke_payload(obj, depth=0):
            if depth > 3: return {}
            if isinstance(obj, dict) and (obj.get("sub_type") == "poke" or obj.get("notice_type") == "notify"):
                return obj
            if hasattr(obj, "__dict__"):
                for k, v in obj.__dict__.items():
                    res = _find_poke_payload(v, depth+1)
                    if res: return res
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    res = _find_poke_payload(v, depth+1)
                    if res: return res
            return {}

        raw = _find_poke_payload(event)

        # 1. 优先通过原生 raw_event 判断
        if raw and (raw.get("sub_type") == "poke" or raw.get("notice_type") == "notify"):
            if "target_id" in raw or "user_id" in raw:
                is_poke = True
                sender_id = str(raw.get("user_id", sender_id))
                target_id = str(raw.get("target_id", ""))
                group_id = str(raw.get("group_id", group_id))
        
        # 2. 降级：通过 AstrBot 的 Comp.Poke 提取
        if not is_poke and event.message_obj and event.message_obj.message:
            for c in event.message_obj.message:
                if isinstance(c, Comp.Poke):
                    is_poke = True
                    extracted = getattr(c, 'target_id', getattr(c, 'qq', ''))
                    # 防止提取到 built-in id 函数
                    if not callable(extracted):
                        target_id = str(extracted) if extracted else ""
                    break
        
        if not is_poke: return
        if sender_id == bot_id: return

        if target_id and not target_id.isdigit():
            target_id = ""

        if not target_id or target_id == "0": 
            target_id = bot_id 
        
        # 🟢 [深度修复 2] 异步闭包：带【并发安全回写】的数据自愈逻辑
        sender_name = event.get_sender_name()
        
        async def _resolve_name(uid, current_name):
            # 判断当前名是否已经足够真实
            valid_name = ""
            if current_name and current_name != uid and current_name != "未知用户" and not current_name.startswith("群友"):
                valid_name = current_name
                
            profile = None
            if hasattr(attention_gate, 'state_engine'):
                profile = await attention_gate.state_engine.get_user_profile(uid)
                
            # 如果 event 没带有效名字，向数据库画像借用
            if not valid_name and profile and profile.name and profile.name != "未知用户" and not profile.name.startswith("群友"):
                valid_name = profile.name
                
            # 如果数据库也没有，强行调用 API 溯源
            if not valid_name:
                client = getattr(event, 'bot', None)
                if client and hasattr(client, 'api') and group_id:
                    try:
                        info = await client.api.call_action('get_group_member_info', group_id=int(group_id), user_id=int(uid))
                        if isinstance(info, dict):
                            valid_name = info.get('card') or info.get('nickname') or ""
                    except Exception as e:
                        logger.debug(f"[AstrMai-Sensor] 📡 API 拉取用户 {uid} 信息失败: {e}")
                        
            # 最终兜底
            final_name = valid_name if valid_name else f"群友{uid[-4:]}"
            
            # 🟢 【核心闭环】如果得到了真实姓名，且数据库为空/不符，立刻加锁执行回写覆盖
            if profile and final_name and final_name != f"群友{uid[-4:]}":
                if profile.name != final_name:
                    try:
                        # 严格遵守 Phase 2 的并发控制规范，获取原子锁防脏写
                        lock_func = getattr(attention_gate.state_engine, '_get_user_lock', None)
                        if lock_func:
                            async with lock_func(uid):
                                profile.name = final_name
                                profile.is_dirty = True
                        else:
                            # 降级直接赋值并打脏标记
                            profile.name = final_name
                            profile.is_dirty = True
                        logger.debug(f"[AstrMai-Sensor] 💾 数据自愈: 成功将用户 {uid} 的真实昵称 '{final_name}' 同步落盘至 UserProfile 画像库。")
                    except Exception as e:
                        logger.error(f"[AstrMai-Sensor] ⚠️ 回写用户画像昵称失败: {e}")
                        
            return final_name

        sender_name = await _resolve_name(sender_id, sender_name)

        bot_name = "我"
        if hasattr(self, 'config') and self.config and hasattr(self.config, 'system1'):
            if self.config.system1.nicknames:
                bot_name = self.config.system1.nicknames[0]
                
        target_name = bot_name if target_id == bot_id else await _resolve_name(target_id, "")
        
        virtual_text = f"(Interaction: {sender_name} -> {target_name})"
        logger.info(f"[AstrMai-Sensor] 👉 捕获互动事件: {virtual_text}")
        
        if target_id == bot_id:
            try:
                client = getattr(event, 'bot', None)
                if client and hasattr(client, 'api'):
                    if group_id:
                        await client.api.call_action('send_poke', user_id=int(sender_id), group_id=int(group_id))
                    else:
                        await client.api.call_action('send_poke', user_id=int(sender_id))
                    logger.info(f"[AstrMai-Sensor] 👈 已回戳反击用户: {sender_name}")
            except Exception as e:
                logger.debug(f"[AstrMai-Sensor] 回戳操作发生异常: {e}")

        event.message_str = virtual_text
        event.set_extra("is_virtual_poke", True)
        event.set_extra("astrmai_bonus_score", 2.0) 
        
        await attention_gate.process_event(event)