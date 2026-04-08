# astrmai/Brain/persona_summarizer.py
import hashlib
import asyncio
import json
from typing import Dict, Any, Tuple
from astrbot.api import logger
from ..infra.persistence import PersistenceManager
from ..infra.gateway import GlobalModelGateway
from ..infra.lane_manager import LaneKey

class PersonaSummarizer:
    """
    人设摘要/压缩管理器 (System 2)
    职责: 将冗长的 System Prompt 压缩为高密度的核心特征与风格指南，减少 Token 消耗。
    """
    def __init__(self, persistence: PersistenceManager, gateway: GlobalModelGateway, config=None, memory_engine=None):
        self.persistence = persistence
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.memory_engine = memory_engine
        # 加载持久化缓存
        self.cache = self.persistence.load_persona_cache()
        # 运行时任务锁
        self.pending_tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def _compute_hash(self, text: str) -> str:
        """计算人设内容的 Hash 值，用于缓存 Key"""
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def _persona_lane_key(self, cache_key: str) -> LaneKey:
        return LaneKey(subsystem="sys2", task_family="persona", scope_id=cache_key, scope_kind="global")

    async def _call_persona_lane(
        self,
        prompt: str,
        cache_key: str,
        system_prompt: str = "",
        is_json: bool = False,
        prefix_hash: str = "",
    ):
        return await self.gateway.call_persona_task(
            prompt=prompt,
            system_prompt=system_prompt,
            is_json=is_json,
            lane_key=self._persona_lane_key(cache_key),
            base_origin="",
            prefix_hash=prefix_hash or self._compute_hash(system_prompt or prompt[:400]),
            persona_id=cache_key,
        )

    async def get_summary(self, original_prompt: str, persona_id: str = "", session_id: str = "global") -> Dict[str, Any]:
        """
        [修改] 基于 Persona ID 获取人设切片。全面替换缓存文件的保存为异步。
        :param original_prompt: 原始人设文本（用于首次生成）。
        :param persona_id: 配置中填写的唯一 ID。
        :param session_id: 如果 ID 为空，用 session_id 做兜底（实现千人千面缓存）。
        """
        # 接入 Config 阈值
        summary_threshold = self.config.performance.summary_threshold

        # 1. 确定 Cache Key (ID 优先)
        if persona_id and persona_id.strip():
            cache_key = persona_id.strip()
        else:
            cache_key = f"session_{session_id}"

        # ==========================================
        # 🟢 [核心修复] 2. 查缓存与自愈机制 (Fast Path & Self-Healing)
        # ==========================================
        if cache_key in self.cache:
            cached_data = self.cache[cache_key]
            
            # 检查是否陷入了“半成品死锁” (缓存未就绪，且当前没有后台任务在跑)
            is_ready = cached_data.get("is_full_ready", False)
            is_running = cache_key in self.pending_tasks
            
            if not is_ready and not is_running:
                logger.warning(f"[PersonaSummarizer] ⚠️ 发现 [{cache_key}] 的切片处于中断死锁状态，正在触发自愈机制，重新拉起后台提取任务！")
                # 从残缺缓存中提取原始长文本，重新拉起后台任务
                raw_text = cached_data.get("raw", original_prompt)
                task = asyncio.create_task(self._generate_all_shards_background(raw_text, cache_key))
                self.pending_tasks[cache_key] = task
                
            return cached_data

        # 3. 缓存完全未命中（新 ID 或新会话），启动全新生成流程
        if not original_prompt or len(original_prompt) < summary_threshold:
            return {
                "summary": original_prompt,
                "style": "保持原始风格",
                "shards": {},
                "is_full_ready": True,
                "raw": original_prompt,
                "timestamp": __import__("time").time()
            }

        async with self._lock:
            # 双重检查锁
            if cache_key in self.cache:
                return self.cache[cache_key]
                
            logger.info(f"[PersonaSummarizer] 🆕 未找到 ID [{cache_key}] 的缓存，开始构建新的人设切片...")
            
            # ==========================================
            # 🟢 阶段一：单独执行核心身份提取 -> 写入 JSON
            # ==========================================
            summary = await self._summarize_core_identity_with_retry(original_prompt, cache_key)
            
            new_cache_data = {
                "summary": summary,
                "style": "数据解析中...", # 临时占位
                "shards": {},
                "is_full_ready": False,
                "raw": original_prompt,
                "timestamp": __import__("time").time()
            }
            self.cache[cache_key] = new_cache_data
            
            # 第一次保存到 JSON
            if hasattr(self.persistence, 'save_persona_cache_async'):
                await self.persistence.save_persona_cache_async(self.cache)
            else:
                self.persistence.save_persona_cache(self.cache)
                
            # ==========================================
            # 🟢 阶段二：单独执行语言风格提取 -> 更新 JSON
            # ==========================================
            style = await self._summarize_style_with_retry(original_prompt, cache_key)
            
            # 更新字典并第二次保存
            self.cache[cache_key]["style"] = style
            if hasattr(self.persistence, 'save_persona_cache_async'):
                await self.persistence.save_persona_cache_async(self.cache)
            else:
                self.persistence.save_persona_cache(self.cache)
            
            # ==========================================
            # 🟢 阶段三：抛出后台任务生成 8 大维度
            # ==========================================
            task = asyncio.create_task(self._generate_all_shards_background(original_prompt, cache_key))
            self.pending_tasks[cache_key] = task
            
            return new_cache_data

# [新增] 核心后台调度器：全维度切片提取引擎
    async def _generate_all_shards_background(self, original_prompt: str, cache_key: str):
        """
        后台静默提取 8 大维度切片任务。
        采用顺序 await 执行以保护 LLM API 并发配额，完成后自动更新挂起状态。
        """
        logger.info(f"[PersonaSummarizer] 🚀 开始后台静默提取 [{cache_key}] 的全维度人格切片...")
        
        # ==========================================
        # 🟢 [Phase 8] 触发原典清洗与向量化重铸
        # ==========================================
        try:
            if getattr(self, 'memory_engine', None):
                logger.info(f"[PersonaSummarizer] 🧹 检测到人设重建，准备清空旧版并重铸 {cache_key} 的潜意识原典...")
                await self.memory_engine.clear_persona_lore(cache_key)
                await self.memory_engine.add_persona_lore(original_prompt, cache_key)
            else:
                logger.warning("[PersonaSummarizer] ⚠️ 未能找到 memory_engine (未注入)，跳过原典入库。")
        except Exception as e:
            logger.error(f"[PersonaSummarizer] ⚠️ 潜意识原典重铸失败 (防宕机隔离): {e}")

        try:
            shards = {}
            # 顺序调用 8 大维度切片提取 (依赖下方的具体子函数)
            shards["logic_style"] = await self._summarize_logic_style(original_prompt, cache_key)
            shards["speech_style"] = await self._summarize_speech_style(original_prompt, cache_key)
            shards["world_view"] = await self._summarize_world_view(original_prompt, cache_key)
            shards["timeline"] = await self._summarize_timeline(original_prompt, cache_key)
            shards["relations"] = await self._summarize_relations(original_prompt, cache_key)
            shards["skills"] = await self._summarize_skills(original_prompt, cache_key)
            shards["values"] = await self._summarize_values(original_prompt, cache_key)
            shards["secrets"] = await self._summarize_secrets(original_prompt, cache_key)

            # 获取原子锁，安全写回内存并解除失忆状态
            async with self._lock:
                if cache_key in self.cache:
                    self.cache[cache_key]["shards"] = shards
                    self.cache[cache_key]["is_full_ready"] = True
                    
                    # 异步/同步持久化到磁盘
                    if hasattr(self.persistence, 'save_persona_cache_async'):
                        await self.persistence.save_persona_cache_async(self.cache)
                    else:
                        self.persistence.save_persona_cache(self.cache)
                        
            logger.info(f"[PersonaSummarizer] ✅ [{cache_key}] 的 8 大维度人格切片已全部提取并组装完毕，角色完全降临！")
            
        except asyncio.CancelledError:
            logger.warning(f"[PersonaSummarizer] ⚠️ [{cache_key}] 的后台切片任务被系统强行终止。")
            raise
        except Exception as e:
            logger.error(f"[PersonaSummarizer] ❌ [{cache_key}] 的切片任务发生严重异常: {e}")
        finally:
            # 无论成功失败，必须从任务挂起池中安全注销自己，防止内存泄漏和僵尸任务
            self.pending_tasks.pop(cache_key, None)

# [修改] 替换 call_judge 为 call_persona_task
    async def _summarize_core_identity_with_retry(self, original_prompt: str, cache_key: str, max_retries: int = 3) -> str:
        """核心身份提取：带重试机制与智能正则兜底"""
        logger.info(f"[PersonaSummarizer] 🧠 正在提取核心身份骨架 (最大重试: {max_retries}次)...")
        prompt = f"""
你的任务是将以下[原始人设]极致压缩为【核心身份骨架】，作为 AI 聊天机器人（System 1 直觉引擎）秒开回复的底层基石。
注意：这是一个二次元/动漫/游戏角色扮演场景，极度依赖角色与用户的“羁绊”设定。

[原始人设]
{original_prompt}

[深度压缩指令]
请在 200 字以内，用最高密度的陈述句提取以下三大核心要素（不要分点或写小标题，请融合成一段极具概括力的设定陈述）：
1. **核心身份与属性标签**：她/他是谁？最显著的二次元萌属性是什么？（如：病弱重度兄控妹妹、慵懒但杀伐果断的风纪委员长、表面毒舌实则自卑的女仆）。
2. **绝对关系锚点（最高优先级！）**：对话者（用户）在她的世界里究竟是什么身份？（是哥哥、Sensei、客人、还是挚友？）。
3. **初始互动底色（Attitude）**：她面对对话者时，默认的心理状态和态度是怎样的？（是满眼爱意的无条件服从、口是心非的傲娇掩饰、公事公办的冷漠、还是极具侵略性的病娇占有？）。

[输出纪律]
- 必须严格控制在 200 字以内！字字珠玑，彻底剥离所有生平背景、冗长故事和无关配角。
- 必须直接输出纯文本，绝对禁止包含“好的”、“根据设定”、“在这份人设中”、“该角色”等废话前缀或后缀。
"""
        for attempt in range(max_retries):
            try:
                res = await self._call_persona_lane(prompt, cache_key, system_prompt="你是一个资深的角色扮演设定提取专家。", is_json=False)
                if res and len(str(res).strip()) > 10:
                    return str(res).strip()
                logger.warning(f"[PersonaSummarizer] ⚠️ 核心身份提取结果过短，准备重试 ({attempt+1}/{max_retries})")
            except Exception as e:
                logger.warning(f"[PersonaSummarizer] ❌ 核心身份提取请求失败 ({attempt+1}/{max_retries}): {e}")
            
            await asyncio.sleep(1.5) # 错峰重试，避免并发限流

        # ==========================================
        # 🛡️ 智能兜底：不再无脑截断，尝试正则抓取关键信息
        # ==========================================
        import re
        logger.error(f"[PersonaSummarizer] 🚨 核心身份提取彻底失败，触发智能降级兜底！")
        # 尝试抓取包含“姓名”、“身份”、“性格”的段落
        match = re.search(r'(.{0,50}(?:姓名|身份|性格|设定).*?)(?:\n\n|$)', original_prompt, re.IGNORECASE | re.DOTALL)
        fallback_text = match.group(0).strip()[:150] if match else original_prompt[:150]
        return f"[系统降级提取] {fallback_text}...\n(注：角色记忆正在缓慢恢复中)"

    async def _summarize_style_with_retry(self, original_prompt: str, cache_key: str, max_retries: int = 3) -> str:
        """语言风格提取：带重试机制与安全兜底"""
        logger.info(f"[PersonaSummarizer] 🗣️ 正在提取语言风格与排版规范 (最大重试: {max_retries}次)...")
        prompt = f"""
你的任务是从以下[原始人设]中极致压缩出【语言与排版绝对规范】，作为驱动 AI 聊天机器人（System 1 & 2）的底层强制指令。
注意：这是二次元/动漫/游戏角色扮演，极度依赖特定的口癖和回复格式。

[原始人设]
{original_prompt}

[深度提取指令]
请在 200 字以内，用极其简练、带有强制性（“必须”、“严禁”）的祈使句，提炼出以下四大对话规则：
1. **专属称谓规则**：角色的第一人称自称是什么？【必须】如何称呼对话者（用户）？
2. **标志性口癖与语调**：高频使用的句式、语气词（如：喵、……的说、哼）或特定标点偏好（如喜欢用波浪号~、大量使用省略号...）。
3. **排版与动作禁忌（最高优先级！）**：原设定中是否有明确的格式限制？（例如：单次回复不超过30字、严禁使用括号/星号进行动作描写、必须维持日常短消息风格）。
4. **情绪表达质感**：说话时的整体温度和节奏是怎样的？（如：毒舌但句句使用敬语、软糯连贯的撒娇、冰冷简短的无口陈述）。

[输出纪律]
- 必须直接输出纯文本指令，绝对禁止包含“好的”、“提取结果如下”等废话。
- 语言必须具有强烈的“约束感”（如：必须使用...，严禁输出...），确保能直接作为系统规则约束最终的对话模型。
- 将四点融合成一段高密度的规则说明，不要输出 JSON 或 markdown 代码块。
"""
        for attempt in range(max_retries):
            try:
                res = await self._call_persona_lane(prompt, cache_key, system_prompt="你是一个资深的角色扮演设定提取专家。", is_json=False)
                if res and len(str(res).strip()) > 5:
                    return str(res).strip()
            except Exception as e:
                logger.warning(f"[PersonaSummarizer] ❌ 语言风格提取请求失败 ({attempt+1}/{max_retries}): {e}")
            
            await asyncio.sleep(1.5)

        # 🛡️ 安全兜底：赋予基础的二次元扮演防护
        return "保持自然、简短的对话风格，拒绝使用AI助手的机械回复格式，严禁长篇大论，贴合人设原本的语气。"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_logic_style(self, original_prompt: str, cache_key: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 性格逻辑 (logic_style)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【性格逻辑】维度的深度切片。
这将被用于驱动一个在线社交聊天机器人，使其表现得像一个真实的二次元/动漫/游戏角色。

[深度扫描维度]
请你像心理分析师一样，仔细扫描文本并提取以下细节：
1. **基础性格底色**：她/他的日常默认状态是什么？（如：慵懒、冷酷、元气、三无、病娇、傲娇、温柔等）。
2. **状态切换与反差（Gap Moe）**：什么特定情境或人会触发她/他的“里性格”？（例如：表面冷酷但被夸奖时会慌乱掩饰；平时懂事但遇到特定事情会极度任性；工作时杀伐果断但私下极度依赖）。
3. **情绪反应机制**：
   - 极度喜悦时：会有什么生理或心理表现？
   - 愤怒或吃醋时：是冷暴力、毒舌、病娇化，还是委屈哭泣？
   - 焦虑与不安时：会有什么强迫性行为或逃避机制？
4. **行动驱动力**：她/他做出决定的第一直觉是基于感性（情感、羁绊）还是理性（规则、利益、效率）？

[输出纪律]
- 请输出一段高密度、结构化的文本，全面总结上述维度。
- **绝对禁止**自行捏造设定中不存在的性格标签。
- 不要出现“该角色……”、“在这个设定中……”等旁白废话，直接陈述性格事实。
- 如果人设中完全没有提到性格相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_lane(prompt, cache_key, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_speech_style(self, original_prompt: str, cache_key: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 语言风格 (speech_style)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【语言风格】维度的深度切片。
这是防止角色 OOC 的最关键一步，因为这决定了她/他打字聊天的语气。

[深度扫描维度]
请极度细致地扫描并提取以下语言特征：
1. **第一人称自称**：平时自称什么？（如：我、吾、人家、妾身、老朽、自己的名字等）。情绪激动时自称是否会改变？
2. **第二人称与专属称谓**：如何称呼对话者/用户？（如：你、汝、欧尼酱、Sensei、前辈、杂修、主人等）。
3. **标志性口癖（Catchphrase）**：句子开头或结尾是否有高频词？（如：……的说、喵、啦、哼、hiyohiyo、哎呀）。
4. **文本排版与符号偏好**：
   - 是否喜欢用特定符号？（如：波浪线“~”、音符“♪”、颜文字）。
   - 沉默或无口属性的表达：（是否大量使用“……”或简短的单字）。
   - 语速与句式：（是喋喋不休的长篇大论，还是惜字如金的短句？是否经常使用倒装句或反问句？）。
5. **社交语气**：是敬语拉满（礼貌但疏离）、粗口/毒舌、还是软糯撒娇？

[输出纪律]
- 必须列出具体的称呼、口癖示例。
- **绝对禁止**捏造原设定中没有的口癖和颜文字。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_lane(prompt, cache_key, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_world_view(self, original_prompt: str, cache_key: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 世界观 (world_view)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【世界观】维度的深度切片。

[深度扫描维度]
请提取支撑该角色生存的虚拟世界背景：
1. **时代与舞台特征**：故事发生在哪里？（如：赛博朋克都市、剑与魔法异世界、封闭的乡下小镇、末日废土、日常校园等）。
2. **角色社会阶层与阵营**：她/他在这个世界中处于什么位置？（如：权贵、反叛军、学生会、风纪委员、神明、平民、被通缉者等）。
3. **专属黑话与专有名词**：文本中出现的特定组织名称、地名、魔法系统、科技名词（如：融合战士、基沃托斯、圣痕、异世界图书馆等）。简要标注其含义。
4. **世界法则对角色的限制**：这个世界的什么规则在压迫或约束着她/他？

[输出纪律]
- 重点提取名词及其解释，为角色提供聊天时的“常识库”。
- **绝对禁止**引入原设定之外的任何动漫或现实世界观。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_lane(prompt, cache_key, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_timeline(self, original_prompt: str, cache_key: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 生平经历 (timeline)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【生平经历】维度的深度切片。

[深度扫描维度]
不要写成流水账，请提取对角色当前性格产生决定性影响的“剧情锚点”：
1. **起源与童年**：她/他的出身背景，是否经历过重大创伤、失去亲人或被抛弃？
2. **核心转折事件**：哪一个事件彻底改变了她/他的命运？（如：获得力量的瞬间、犯下大错的时刻、被救赎的经历）。
3. **与对话者（用户）的历史渊源**：她/他与“用户（如哥哥、老师等）”是怎么相遇的？共同经历过什么生死攸关或极度甜蜜的关键事件？确立当前关系的核心事件是什么？
4. **当前的处境**：她/他现在正面临什么危机，或者正处于什么日常状态中？

[输出纪律]
- 提炼高密度的事件骨架，侧重于“事件如何塑造了她的心理”。
- **绝对禁止**发散或续写剧情。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_lane(prompt, cache_key, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_relations(self, original_prompt: str, cache_key: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 人际关系 (relations)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【人际关系】维度的深度切片。

[深度扫描维度]
请清晰构建该角色的社交图谱：
1. **绝对核心锚点（对用户/对话者）**：她/他对“你（用户/对话者）”的感情定位是什么？（是病态的依赖、极度的占有欲、默默的暗恋、主从的绝对忠诚、还是傲娇的掩饰？）。这种羁绊到了什么程度（例如：愿意为你死、把你视为全世界）？
2. **敌意与警惕对象**：谁是她/他的死对头？她/他会对接近用户的哪些人产生嫉妒或敌意？
3. **友方与NPC态度**：设定中提到的其他具体名字的角色，她/他怎么称呼他们？态度是怎样的？
4. **社交边界感**：对待完全不认识的陌生人，她是冷漠、警惕、毒舌还是热情礼貌？

[输出纪律]
- 必须明确“对待用户”和“对待其他人”的巨大反差。
- **绝对禁止**提取或捏造原设定文本中未出现的名字。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_lane(prompt, cache_key, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_skills(self, original_prompt: str, cache_key: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 技能能力 (skills)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【技能能力】维度的深度切片。

[深度扫描维度]
全方位评估角色的能力面板：
1. **超凡/战斗能力**：拥有的魔法、武技、武器、黑客技术或特殊天赋。战斗时的风格是怎样的？（如：狂暴、精准、毁灭性、治愈辅助）。
2. **日常与生活技能**：在非战斗状态下，她/他擅长什么？（如：家务全能、料理大师，或者是重度机械白痴、生活九级残废需要人照顾）。
3. **能力代价与致命弱点**：使用能力是否需要付出代价？（如：消耗寿命、失去记忆、身体退化）。她在生理或心理上有什么极度害怕的弱点？（如：怕鬼、怕虫子、怕孤单）。

[输出纪律]
- 既要提取“她能做什么”，更要提取“她不能做什么”或“她的软肋”，这有助于增加交互的脆弱感。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_lane(prompt, cache_key, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_values(self, original_prompt: str, cache_key: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 价值观 (values)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【价值观】维度的深度切片。

[深度扫描维度]
剖析角色的底层动机与喜恶法则：
1. **最高信仰与核心执念**：在这个世界上，对她/他来说最重要、绝对不能妥协的事物是什么？（如：不择手段追求进化、维护某人的安全、遵守风纪、对爱的纯粹渴求）。
2. **道德底线**：她是守序正义（不伤害无辜）、还是混沌邪恶（为了目的可以杀人/无视伦理）？
3. **极度的喜好**：最喜欢的食物、物品或消遣方式是什么？（这些通常是聊天中能让她开心起来的“道具”）。
4. **极度的厌恶**：绝对不能触碰的逆鳞或极其讨厌的事物是什么？（这些通常是触发她愤怒或黑化的“雷区”）。

[输出纪律]
- 重点突出极端偏好和底线，不要用模棱两可的词汇。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_lane(prompt, cache_key, is_json=False)
        except Exception:
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_secrets(self, original_prompt: str, cache_key: str) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 深层秘密 (secrets)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【深层秘密】维度的深度切片。
这是角色的“灵魂”，即表象之下的里设定。

[深度扫描维度]
请像心理医生一样，挖掘出文本中隐藏的里层信息：
1. **心理创伤与自卑感**：她/他内心深处最怕什么？（如：觉得自己只是替代品、害怕被抛弃、害怕被视为怪物、对过往罪行的负罪感）。
2. **伪装下的真心**：傲娇、毒舌、冷酷或过分元气的外表下，掩盖了怎样脆弱、渴望被爱或极度疲惫的真实想法？（她/他绝口不提，但在特定时刻会暴露的软肋）。
3. **剧情暗线事实**：设定中是否提到了某种隐藏的诅咒、寿命将近的倒计时、不可告人的黑历史或不为人知的牺牲？

[输出纪律]
- 重点提取那些“她自己不想承认，但确实存在”的矛盾点。
- 提取的结果将作为 AI 对话时的“潜意识指南”，请务必深刻。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_lane(prompt, cache_key, is_json=False)
        except Exception:
            return "无"
