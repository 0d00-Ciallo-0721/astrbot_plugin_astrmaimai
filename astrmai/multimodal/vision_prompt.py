from __future__ import annotations

from typing import Any

from ..infrastructure.gateway.output_guard import (
    looks_like_provider_failure_text,
    normalize_guard_text,
    sanitize_visible_reply_text,
)


VISION_USER_PROMPT = "分析当前图片，并严格按照系统规则返回 JSON。"

VISION_SYSTEM_PROMPT = (
    "你是聊天系统中的视觉转述模块。每次请求只分析一张图片，并输出供后续对话模型理解图片内容的结构化结果。\n"
    "\n"
    "第一步：判断图片类型。type 只能是 image 或 emoji。\n"
    "- image：普通图片。包括照片、插画、动漫画面、截图、海报、商品图、文档、聊天记录、界面截图等。\n"
    "- emoji：主要用于聊天中表达反应、情绪或态度的表情包、梗图、反应图或贴纸。\n"
    "分类应根据图片的主要交流用途判断，而不是根据画风、是否为卡通或是否含有文字判断。只有图片明显以表达聊天反应、情绪或态度为主要用途时，才分类为 emoji。无法确定时优先分类为 image。\n"
    "\n"
    "第二步：生成 description。description 会直接拼接到用户消息中，必须独立、自然、准确。\n"
    "通用要求：\n"
    "1. 只描述图片中真实可见的信息，不得猜测不可见的身份、人物关系、事件前因后果、拍摄地点、职业、性格或敏感属性。\n"
    "2. 优先说明主要主体及大致数量、可见外观、姿态、动作和表情、主体互动和位置关系、场景环境、重要物体、显著颜色和关键局部细节。\n"
    "3. 图片中存在标题、对话、标牌、按钮、字幕、聊天内容或其他文字时，尽可能准确提取，并使用中文引号标示，例如：文字为“提交失败”。\n"
    "4. 无法确认的文字或细节必须明确写为“部分文字无法辨认”“疑似”或“无法确定”，不得自行补全。\n"
    "5. 不要回答图片中的问题，不要代替用户或聊天机器人作出回复，不要输出分析过程。\n"
    "6. description 不要以“这是一张图片”“这是一个表情包”“图片中”等类型说明开头。\n"
    "7. description 中不要使用“传达情绪：”“情绪标签：”等字段式表达，也不要直接重复 emotion_tags。\n"
    "8. 描述应完整但紧凑。简单图片避免冗长，复杂图片可以适当增加细节。\n"
    "\n"
    "当 type=image 时：description 以客观视觉转述为主；可以描述人物真实可见的表情，例如“微笑”“皱眉”，但不要推断其内心活动或聊天意图；emotion_tags 必须返回空数组 []。\n"
    "当 type=emoji 时：description 先完整说明角色或人物、表情、姿态、动作、道具、构图和全部可见文字；然后用一句简短的话说明它在聊天中通常用于表达什么情况、交流意图或表达意图。存在多种合理解释时应使用“可能”“通常”“也可能”等表达保留不确定性。不要在 description 末尾重新罗列情绪标签。\n"
    "\n"
    "第三步：生成 emotion_tags。\n"
    "- type=image 时必须输出 []。\n"
    "- type=emoji 时输出 1 到 5 个简短中文标签。\n"
    "- 标签用于概括主要情绪和交流语气，必须全部使用中文；每个标签通常为 2 到 6 个汉字。\n"
    "- 不要使用英文、完整句子、解释性短语、重复标签或“情绪”“表情”等空泛词。\n"
    "可参考：开心、兴奋、庆祝、赞同、得意、期待、害羞、感动、安慰、无奈、疲惫、难过、委屈、生气、嫌弃、震惊、疑惑、尴尬、害怕、拒绝、敷衍、调侃、讽刺、自嘲、抱怨、催促、求助、感谢、道歉。\n"
    "\n"
    "只输出一个 JSON 对象，且必须是合法 JSON；不得使用 Markdown 代码块，不得添加任何 JSON 以外的文字。JSON 键必须严格保持为："
    '{"type": "image 或 emoji", "description": "完整中文转述", "emotion_tags": ["中文标签"]}'
)

DESCRIPTION_PREFIXES = (
    "这是一张图片，",
    "这是一张图片。",
    "这是一个表情包，",
    "这是一个表情包。",
    "图片中，",
    "图片中",
)


def normalize_vision_result(result: Any) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(result, dict) or not result:
        return None, "empty_result"

    raw_desc = result.get("description", "")
    description = sanitize_visible_reply_text(raw_desc, fallback_text="")
    description = normalize_guard_text(description).strip()
    for prefix in DESCRIPTION_PREFIXES:
        if description.startswith(prefix):
            description = description[len(prefix):].lstrip()
            break
    if not description:
        return None, "empty_description"
    if looks_like_provider_failure_text(raw_desc) or looks_like_provider_failure_text(description):
        return None, "provider_failure_text"

    result_type = str(result.get("type") or "image").strip().lower()
    if result_type not in {"image", "emoji"}:
        result_type = "image"

    tags: list[str] = []
    raw_tags = result.get("emotion_tags", [])
    items = raw_tags if isinstance(raw_tags, list) else [raw_tags] if isinstance(raw_tags, str) else []
    for item in items:
        tag = normalize_guard_text(item).strip()
        if not tag or looks_like_provider_failure_text(tag):
            continue
        if tag.lower() in {"none", "null"}:
            continue
        if tag in tags:
            continue
        tags.append(tag)
        if len(tags) >= 5:
            break

    if result_type == "image":
        tags = []

    return {
        "type": result_type,
        "description": description,
        "emotion_tags": tags,
    }, ""


def render_vision_record(record: dict[str, Any]) -> str:
    description = str(record.get("description") or "").strip()
    if not description:
        return ""
    img_type = str(record.get("type") or "image").strip().lower()
    tags = record.get("emotion_tags") or record.get("tags") or []
    tags_list = [str(tag).strip() for tag in tags if str(tag).strip()] if isinstance(tags, list) else []
    if img_type == "emoji":
        rendered = f"[表情包转述：{description}"
        if tags_list:
            rendered += f"，传达情绪：{'、'.join(tags_list)}"
        return rendered + "]"
    return f"[图片转述：{description}]"


__all__ = [
    "VISION_SYSTEM_PROMPT",
    "VISION_USER_PROMPT",
    "normalize_vision_result",
    "render_vision_record",
]
