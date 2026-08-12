import inspect
import random
import re
import time
from typing import List
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
import astrbot.api.message_components as Comp
from astrbot.core.star.command_management import list_commands

from ...shared.helpers.plugin_helpers import (
    event_mentions_actor,
    get_event_self_id,
    is_at_message_component,
    resolve_at_target_id,
)
from ..vision_state import derive_vision_state, user_asked_about_image
from .poke_play import PokePlaybook

class PreFilters:
    """
    感知与过滤器 (System 1: Fused Version)
    职责: 构建指令防火墙，执行严格的消息清洗与拦截。
    """
    def __init__(self, config):
        self.config = config
        self.foreign_commands = set()
        self._commands_loaded = False 
        self._poke_playbook = PokePlaybook()
        self._recent_counter_pokes: dict[str, float] = {}
        self._recent_peer_poke_joins: dict[str, float] = {}

    def refresh_config(self, config):
        self.config = config

    def _record_counter_poke_echo(self, chat_id: str, bot_id: str, target_id: str) -> None:
        now = time.time()
        key = f"{chat_id}:{bot_id}:{target_id}"
        self._recent_counter_pokes[key] = now
        cutoff = now - 12.0
        for stale_key, seen_at in list(self._recent_counter_pokes.items()):
            if seen_at < cutoff:
                self._recent_counter_pokes.pop(stale_key, None)

    def _is_counter_poke_echo(self, chat_id: str, sender_id: str, target_id: str, bot_id: str) -> bool:
        key = f"{chat_id}:{bot_id}:{target_id}"
        seen_at = self._recent_counter_pokes.get(key)
        if not seen_at:
            return False
        if time.time() - seen_at > 12.0:
            self._recent_counter_pokes.pop(key, None)
            return False
        return sender_id == bot_id or not sender_id or sender_id == target_id

    def _peer_poke_join_allowed(self, chat_id: str, sender_id: str, target_id: str) -> bool:
        key = f"{chat_id}:{sender_id}:{target_id}"
        seen_at = self._recent_peer_poke_joins.get(key)
        if not seen_at:
            return True
        if time.time() - seen_at > 30.0:
            self._recent_peer_poke_joins.pop(key, None)
            return True
        return False

    def _record_peer_poke_candidate(self, chat_id: str, sender_id: str, target_id: str) -> None:
        now = time.time()
        self._recent_peer_poke_joins[f"{chat_id}:{sender_id}:{target_id}"] = now
        cutoff = now - 90.0
        for stale_key, seen_at in list(self._recent_peer_poke_joins.items()):
            if seen_at < cutoff:
                self._recent_peer_poke_joins.pop(stale_key, None)

    async def load_foreign_commands(self):
        """公开接口：异步动态加载系统内所有注册指令（D59：替代 hasattr 私有方法访问）"""
        return await self._load_foreign_commands()

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
            # ponytail: cap foreign_commands at 10K to prevent unbounded growth
            if len(self.foreign_commands) > 10000:
                self.foreign_commands = set(list(self.foreign_commands)[-8000:])
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
        
        if event.get_extra("is_virtual_poke"):
            return True

        # OPT-03/PL-01+ID-03: 后台合成事件（主动开口候选/群友互动叙事）只有 message_str、
        # 无消息组件，不能被下方"组件文本为空"的过滤绞杀；是否回应交给 judge/playbook 决定
        if event.get_extra("astrmai_is_proactive_event") or event.get_extra("astrmai_interaction_kind"):
            return True

        # =================================================================
        # 🚀 [新增] 媒体链接白名单防护层 (AstrBot Parser 兼容补丁)
        # =================================================================
        raw_msg = event.message_str or ""
        url_pattern = r"(?i)\b(?:https?://|www\.)[^\s()<>]+(?:\([\w\d]+\)|([^[:punct:]\s]|/))"
        urls = re.findall(url_pattern, raw_msg)
        
        if urls:
            # parser 插件支持的所有媒体域名特征库
            parser_domains = [
                "acfun.cn", "bilibili.com", "b23.tv", 
                "douyin.com", "iesdouyin.com", "v.douyin.com",
                "instagram.com", "instagr.am",
                "kuaishou.com", "v.kuaishou.com", "chenzhongtech.com",
                "tiktok.com", "x.com", "twitter.com",
                "weibo.com", "weibo.cn", "video.weibo.com", "mapp.api.weibo.cn",
                "xiaohongshu.com", "xhslink.com",
                "youtube.com", "youtu.be",
                "163.com", "zhihu.com"
            ]
            
            # 检查文本中是否包含 parser 的特征域名
            if any(domain in raw_msg.lower() for domain in parser_domains):
                logger.debug(f"[AstrMai-Sensor] 🛑 检测到媒体解析链接，主动放弃拦截权交予 Parser 插件处理。")
                return False
        # =================================================================

        # 2. 深度清洗文本与负载检测
        clean_text_parts = []
        has_payload = False
        image_urls = []
        
        # ==========================================
        # 阶段一：特征探针提取 (主脑视觉直通车)
        # ==========================================
        has_at_bot = False
        reply_image_urls = []
        raw_image_component_count = 0
        reply_image_component_count = 0
        bot_id = get_event_self_id(event)
        # 判断私聊环境 (如果不存在 group_id 则为私聊)
        is_private = not bool(event.get_group_id())
        reply_cls = getattr(Comp, "Reply", None)
        plain_cls = getattr(Comp, "Plain", None)
        image_cls = getattr(Comp, "Image", None)
        media_classes = tuple(
            cls
            for cls in (
                image_cls,
                getattr(Comp, "Video", None),
                getattr(Comp, "Record", None),
                getattr(Comp, "File", None),
            )
            if cls is not None
        )

        async def _extract_image_ref(image_component):
            file_path = getattr(image_component, "file", None) or getattr(image_component, "path", None)
            if file_path:
                return str(file_path)
            # non-HTTP URLs (WeChat wxfile://, QQ local references, etc.) may be extractable
            image_url = getattr(image_component, "url", None) or getattr(image_component, "image_url", None) or getattr(image_component, "src", None)
            if image_url and not str(image_url).startswith(("http://", "https://")):
                return str(image_url)
            file_to_base64 = getattr(image_component, "file_to_base64", None)
            if callable(file_to_base64):
                try:
                    encoded = file_to_base64()
                    if inspect.isawaitable(encoded):
                        encoded = await encoded
                    if encoded:
                        return f"data:image/jpeg;base64,{encoded}"
                except Exception:
                    pass
            return ""

        async def _scan_reply_chain(chain):
            """递归扫描引用链中的图片"""
            nonlocal reply_image_component_count
            urls = []
            if not chain:
                return urls
            for c in chain:
                if image_cls is not None and isinstance(c, image_cls):
                    reply_image_component_count += 1
                    image_ref = await _extract_image_ref(c)
                    if image_ref:
                        urls.append(image_ref)
                elif reply_cls is not None and isinstance(c, reply_cls) and hasattr(c, 'chain'):
                    urls.extend(await _scan_reply_chain(c.chain))
            return urls

        if event.message_obj and event.message_obj.message:
            for seg in event.message_obj.message:
                # 探针：检测消息体内是否 @ 了 Bot
                at_target_id = resolve_at_target_id(seg)
                if at_target_id == bot_id:
                    has_at_bot = True
                    
                # 探针：检测引用组件，并递归挖掘被引用消息中的图片
                if reply_cls is not None and isinstance(seg, reply_cls):
                    if hasattr(seg, 'chain'):
                        scanned_reply_images = await _scan_reply_chain(seg.chain)
                        if scanned_reply_images:
                            has_payload = True
                            reply_image_urls.extend(scanned_reply_images)
                    continue # 忽略引用组件对纯文本的干扰
                    
                # 忽略艾特对纯文本的干扰
                if is_at_message_component(seg):
                    continue 
                
                # 提取纯文本
                if plain_cls is not None and isinstance(seg, plain_cls):
                    text = seg.text.replace('\u200b', '').strip()
                    if text: 
                        clean_text_parts.append(text)
                        
                # 【核心逻辑】：必须检测 Comp.Image 及其他媒体载荷
                if media_classes and isinstance(seg, media_classes):
                    has_payload = True
                    
                # 顺手提取 URL
                if image_cls is not None and isinstance(seg, image_cls):
                    raw_image_component_count += 1
                    image_ref = await _extract_image_ref(seg)
                    if image_ref:
                        image_urls.append(image_ref)
                    
        # 封装着色逻辑：主脑视觉直通车
        direct_vision_urls = []
        
        if is_private and image_urls:
            direct_vision_urls.extend(image_urls)
        elif has_at_bot:
            if image_urls:
                direct_vision_urls.extend(image_urls)
            if reply_image_urls:
                direct_vision_urls.extend(reply_image_urls)
        
        if direct_vision_urls:
            direct_vision_urls = list(dict.fromkeys(direct_vision_urls))
        vision_cfg = getattr(self.config, "vision", None)
        vision_enabled = bool(getattr(vision_cfg, "enable_vision", True))
        probability = getattr(vision_cfg, "image_recognition_probability", 1.0)
        try:
            probability = float(probability)
        except (TypeError, ValueError):
            probability = 1.0
        probability = max(0.0, min(1.0, probability))
        force_direct_vision = bool(direct_vision_urls) and (is_private or has_at_bot)

        selected_direct = bool(direct_vision_urls)
        vision_direct_skip_reason = ""
        if direct_vision_urls and not vision_enabled:
            selected_direct = False
            vision_direct_skip_reason = "disabled"
            logger.debug("[AstrMai-Sensor] vision direct path skipped: disabled")
        elif direct_vision_urls and not force_direct_vision and random.random() > probability:
            selected_direct = False
            vision_direct_skip_reason = "probability_gate"
            logger.debug(
                "[AstrMai-Sensor] vision direct path skipped by probability gate "
                f"(p={probability:.2f}, candidates={len(direct_vision_urls)})"
            )
        elif direct_vision_urls:
            event.set_extra("direct_vision_urls", direct_vision_urls)
            event.set_extra("direct_image_refs", direct_vision_urls)
            logger.debug(
                "[AstrMai-Sensor] vision direct path selected "
                f"(urls={len(direct_vision_urls)}, probability={probability:.2f}, "
                f"forced={force_direct_vision})"
            )
        else:
            vision_direct_skip_reason = "not_direct_path"

        event.set_extra("vision_direct_selected", selected_direct)
        event.set_extra("vision_direct_skip_reason", vision_direct_skip_reason)
        event.set_extra("astrmai_is_direct_vision_request", selected_direct)
        event.set_extra(
            "astrmai_is_passive_image_share",
            bool(image_urls) and not selected_direct and not is_private and not has_at_bot,
        )
        if has_at_bot:
            event.set_extra("astrmai_at_bot_wakeup", True)
            if not is_private:
                event.set_extra("astrmai_group_direct_wakeup", True)
        # ==========================================

        clean_text = " ".join(clean_text_parts).strip().lower()
        
        # 记录提取到的所有图片足迹；reply-image 也应保留给后续主链使用
        extracted_image_urls = list(dict.fromkeys(list(image_urls or []) + list(reply_image_urls or [])))
        event.set_extra("extracted_image_urls", extracted_image_urls)
        event.set_extra("extracted_image_refs", extracted_image_urls)
        total_image_components = raw_image_component_count + reply_image_component_count
        resolved_image_count = len(extracted_image_urls)
        placeholder_count = max(0, total_image_components - resolved_image_count)
        asked_about_image = user_asked_about_image(clean_text or raw_msg)
        vision_state = derive_vision_state(
            raw_count=total_image_components,
            resolved_count=resolved_image_count,
        )
        image_focus_allowed = bool(resolved_image_count)
        if image_focus_allowed:
            image_focus_reason = "resolvable_media"
        elif asked_about_image and total_image_components:
            image_focus_reason = "explicit_question_without_resolvable_media"
        elif total_image_components:
            image_focus_reason = "placeholder_ignored"
        else:
            image_focus_reason = "no_media"
        event.set_extra("astrmai_vision_state", vision_state)
        event.set_extra("astrmai_image_event_count", int(bool(total_image_components or resolved_image_count)))
        event.set_extra("astrmai_image_raw_component_count", total_image_components)
        event.set_extra("astrmai_image_resolved_count", resolved_image_count)
        event.set_extra("astrmai_image_placeholder_count", placeholder_count)
        event.set_extra("astrmai_user_asked_about_image", asked_about_image)
        event.set_extra("astrmai_image_focus_allowed", image_focus_allowed)
        event.set_extra("astrmai_image_focus_reason", image_focus_reason)
        
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
        if not clean_text and not has_payload and not has_at_bot:
            return False

        # 5. 昵称点名提权
        # 此处直接使用开头已获取的 raw_msg
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

        return event_mentions_actor(event, bot_self_id)

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

                elif hasattr(Comp, "Poke") and isinstance(component, Comp.Poke):
                    to_user = getattr(component, 'target_id', getattr(component, 'qq', None))
                    if to_user and not callable(to_user):
                        to_user = str(to_user)
                        if to_user and to_user != from_user:
                            relations.append((from_user, to_user, "poke", 0.2))
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
            try:
                bot_id = str(event.get_self_id())
            except Exception:
                logger.debug("[AstrMai-Sensor] Failed to read bot self_id from event", exc_info=True)
                
        if not bot_id: bot_id = "unknown"

        event_sender_id = str(event.get_sender_id())
        sender_id = event_sender_id
        if sender_id == bot_id: return

        is_poke = False
        target_id = ""
        group_id = str(event.get_group_id() or "")
        raw_sender_id = ""
        actor_confident = False
        
        # 🟢 [深度修复 1] 递归查找 OneBot 底层 payload，突破封装层，解决 target_id 丢失
        def _find_poke_payload(obj, depth=0):
            if depth > 8: return {}
            if isinstance(obj, dict) and (
                str(obj.get("sub_type", "")).lower() == "poke"
                or str(obj.get("notice_type", "")).lower() == "poke"
                or (
                    str(obj.get("notice_type", "")).lower() == "notify"
                    and str(obj.get("sub_type", "")).lower() in {"poke", "戳一戳"}
                )
            ):
                return obj
            if hasattr(obj, "__dict__"):
                for k, v in obj.__dict__.items():
                    res = _find_poke_payload(v, depth+1)
                    if res: return res
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    res = _find_poke_payload(v, depth+1)
                    if res: return res
            elif isinstance(obj, (list, tuple)):
                for v in obj:
                    res = _find_poke_payload(v, depth+1)
                    if res: return res
            return {}

        raw = _find_poke_payload(event)

        # 1. 优先通过原生 raw_event 判断
        if raw and (
            str(raw.get("sub_type", "")).lower() == "poke"
            or str(raw.get("notice_type", "")).lower() in {"poke", "notify"}
        ):
            if "target_id" in raw or "user_id" in raw:
                is_poke = True
                raw_sender_id = str(raw.get("user_id", "") or "")
                if raw_sender_id and raw_sender_id.isdigit():
                    sender_id = raw_sender_id
                    actor_confident = True
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
                    actor_confident = bool(sender_id and sender_id.isdigit())
                    break
        
        if not is_poke: return
        if sender_id == bot_id: return

        if target_id and not target_id.isdigit() and target_id != bot_id:
            target_id = ""

        if not target_id or target_id == "0":
            # OPT-03/ID-10: 目标缺失时不得伪装成"戳 bot"——旧兜底会导致无端回戳
            # 并给发起者误结算好感；记为 unknown-target 并跳过整个互动处理
            logger.debug(
                f"[AstrMai-Sensor] poke target unresolved (sender={sender_id}); skip interaction handling"
            )
            event.set_extra("astrmai_interaction_target_unknown", True)
            return
        if self._is_counter_poke_echo(chat_id=str(getattr(event, "unified_msg_origin", "") or group_id), sender_id=sender_id, target_id=target_id, bot_id=bot_id):
            logger.debug("[AstrMai-Sensor] ignored recent counter-poke echo")
            return
        
        # 🟢 [深度修复 2] 异步闭包：带【并发安全回写】的数据自愈逻辑
        sender_name = event.get_sender_name() if actor_confident and sender_id == event_sender_id else ""
        
        async def _resolve_name(uid, current_name):
            # 判断当前名是否已经足够真实
            valid_name = ""
            if current_name and current_name != uid and current_name != "未知用户" and not current_name.startswith("群友"):
                valid_name = current_name
                
            profile = None
            is_anonymous_uid = str(uid or "").startswith("80000000")
            if not is_anonymous_uid and hasattr(attention_gate, 'state_engine'):
                profile = await attention_gate.state_engine.get_user_profile(uid)
                
            # 如果 event 没带有效名字，向数据库画像借用
            if not valid_name and profile and profile.name and profile.name != "未知用户" and not profile.name.startswith("群友"):
                valid_name = profile.name
                
            # 如果数据库也没有，强行调用 API 溯源
            if not valid_name and not is_anonymous_uid:
                client = getattr(event, 'bot', None)
                if client and hasattr(client, 'api') and group_id:
                    try:
                        info = await client.api.call_action('get_group_member_info', group_id=int(group_id), user_id=int(uid))
                        if isinstance(info, dict):
                            valid_name = info.get('card') or info.get('nickname') or ""
                    except Exception as e:
                        logger.debug(f"[AstrMai-Sensor] 📡 API 拉取用户 {uid} 信息失败: {e}")
                        
            # OPT-03/ID-10: 群名片可能被设成签名样长文本（如"…的季节里，希望你别中暑了~"），
            # 混入互动叙事观感极差；超长或含换行的候选名一律弃用
            if valid_name and (len(valid_name) > 20 or "\n" in valid_name):
                valid_name = ""

            # 最终兜底
            final_name = valid_name if valid_name else f"群友{uid[-4:]}"
            
            # 🟢 【核心闭环】如果得到了真实姓名，且数据库为空/不符，立刻加锁执行回写覆盖
            if profile and final_name and final_name != f"群友{uid[-4:]}":
                if profile.name != final_name:
                    try:
                        # 严格遵守 Phase 2 的并发控制规范，获取原子锁防脏写
                        if hasattr(attention_gate.state_engine, "apply_profile_name"):
                            await attention_gate.state_engine.apply_profile_name(uid, final_name, source="sensor_resolve")
                        else:
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

        sender_name = await _resolve_name(sender_id, sender_name) if actor_confident else "有人"

        bot_name = "我"
        if hasattr(self, 'config') and self.config and hasattr(self.config, 'system1'):
            if self.config.system1.nicknames:
                bot_name = self.config.system1.nicknames[0]
                
        target_is_bot = target_id == bot_id
        target_name = bot_name if target_is_bot else await _resolve_name(target_id, "")
        actor_label = (
            f"{sender_name}({sender_id})"
            if actor_confident and sender_id and sender_id not in str(sender_name)
            else sender_name
        )
        target_label = f"{target_name}({target_id})" if target_id and target_id not in str(target_name) else target_name
        occurred_at = time.time()
        relative_age_label = "刚刚"

        state_engine = getattr(attention_gate, "state_engine", None)
        relationship_engine = getattr(state_engine, "relationship_engine", None)
        social_score = 0.0
        if actor_confident and relationship_engine and hasattr(relationship_engine, "get_social_score"):
            try:
                social_score = float(relationship_engine.get_social_score(sender_id) or 0.0)
            except Exception:
                logger.debug("[AstrMai-Sensor] failed to read relationship score for poke", exc_info=True)

        chat_id = str(getattr(event, "unified_msg_origin", "") or group_id or target_id or sender_id)
        play_sender_id = sender_id if actor_confident else "unknown"
        poke_play = self._poke_playbook.build(
            chat_id=chat_id,
            sender_id=play_sender_id,
            sender_name=sender_name,
            target_id=target_id,
            target_name=target_name,
            target_is_bot=target_is_bot,
            social_score=social_score,
            group_id=group_id,
            now=occurred_at,
        )

        if actor_confident and target_is_bot and state_engine and hasattr(state_engine, "calculate_and_update_affection"):
            try:
                await state_engine.calculate_and_update_affection(
                    user_id=sender_id,
                    group_id=chat_id,
                    mood_tag="happy" if social_score >= -20 else "neutral",
                    intensity=0.35,
                    message_text="戳一戳",
                    event_type="normal_chat",
                )
            except Exception:
                logger.debug("[AstrMai-Sensor] poke affection settlement failed", exc_info=True)

        virtual_text = poke_play.narrative
        logger.info(f"[AstrMai-Sensor] 👉 捕获互动事件: {virtual_text}")
        
        if actor_confident and target_is_bot and poke_play.should_counter_poke:
            try:
                client = getattr(event, 'bot', None)
                if client and hasattr(client, 'api'):
                    if group_id:
                        await client.api.call_action('send_poke', user_id=int(sender_id), group_id=int(group_id))
                    else:
                        await client.api.call_action('send_poke', user_id=int(sender_id))
                    self._record_counter_poke_echo(str(getattr(event, "unified_msg_origin", "") or group_id), bot_id, sender_id)
                    logger.info(f"[AstrMai-Sensor] 👈 已回戳反击用户: {sender_name}")
            except Exception as e:
                logger.debug(f"[AstrMai-Sensor] 回戳操作发生异常: {e}")

        peer_join_allowed = True
        if not target_is_bot:
            peer_join_allowed = (
                actor_confident
                and self._peer_poke_join_allowed(chat_id, sender_id, target_id)
                and poke_play.cooldown_seconds <= 0
            )
            self._record_peer_poke_candidate(chat_id, sender_id, target_id)

        event.message_str = virtual_text
        event.set_extra("is_virtual_poke", target_is_bot)
        event.set_extra("astrmai_interaction_kind", "poke" if target_is_bot else "peer_poke")
        event.set_extra("astrmai_lightweight_event", True)
        event.set_extra("astrmai_rich_text", virtual_text)
        event.set_extra("astrmai_reply_mode", "playful_interaction" if target_is_bot else "peer_interaction_observer")
        event.set_extra("astrmai_interaction_actor_id", sender_id if actor_confident else "")
        event.set_extra("astrmai_interaction_actor_name", sender_name)
        event.set_extra("astrmai_interaction_actor_display_name", actor_label)
        event.set_extra("astrmai_interaction_actor_confident", actor_confident)
        event.set_extra("astrmai_interaction_target_id", target_id)
        event.set_extra("astrmai_interaction_target_name", target_name)
        event.set_extra("astrmai_interaction_target_display_name", target_label)
        event.set_extra("astrmai_interaction_target_is_bot", target_is_bot)
        event.set_extra("astrmai_interaction_occurred_at", occurred_at)
        event.set_extra("astrmai_interaction_relative_age_label", relative_age_label)
        event.set_extra("astrmai_poke_intent", poke_play.intent)
        event.set_extra("astrmai_poke_intensity", poke_play.intensity)
        event.set_extra("astrmai_poke_streak_count", poke_play.streak_count)
        event.set_extra("astrmai_poke_relationship_level", poke_play.relationship_level)
        event.set_extra("astrmai_poke_reply_hint", poke_play.reply_hint)
        event.set_extra("astrmai_poke_social_signal", poke_play.social_signal)
        event.set_extra("astrmai_poke_cooldown_seconds", poke_play.cooldown_seconds)
        event.set_extra("astrmai_poke_group_focus_hint", poke_play.group_focus_hint)
        event.set_extra("astrmai_peer_poke_join_allowed", peer_join_allowed)
        event.set_extra(
            "astrmai_peer_poke_allowed_target_ids",
            [item for item in (sender_id if actor_confident else "", target_id) if item],
        )
        event.set_extra(
            "astrmai_peer_poke_allowed_target_names",
            [item for item in (sender_name if actor_confident else "", target_name) if item],
        )
        event.set_extra("astrmai_bonus_score", 2.0 if target_is_bot else 0.2)
        if target_is_bot and poke_play.should_force_meme:
            event.set_extra("astrmai_force_meme", True)
            event.set_extra("astrmai_bypass_mood_analysis", poke_play.meme_tag)
        
        await attention_gate.process_event(event)
