# astrmai/Heart/visual_cortex.py
import asyncio
import base64
import io
import time
import json
import numpy as np
from typing import Optional
from PIL import Image
from astrbot.api import logger
from ..infra.datamodels import VisualMemory

class VisualCortex:
    """
    多模态视觉皮层后台服务 (Phase 3)
    负责异步非阻塞地处理图像，调用 VLM，并将结果写入 VisualMemory。
    """
    def __init__(self, gateway, db_service):
        self.gateway = gateway
        self.db_service = db_service
        self.queue = asyncio.Queue()
        self._worker_task = None

    # [新增] 启动后台守护进程
    def start(self):
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())
            logger.info("[AstrMai-VisualCortex] 👁️ 异步视觉皮层已启动并监听任务队列。")

    # [新增] 停止守护进程
    def stop(self):
        if self._worker_task:
            self._worker_task.cancel()

    # [新增] 后台消费队列
    async def _worker(self):
        while True:
            try:
                picid, base64_data = await self.queue.get()
                await self.process_image_async(picid, base64_data)
                self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[AstrMai-VisualCortex] ❌ 视觉处理队列异常: {e}", exc_info=True)

    # [新增] 向队列投递任务
    def submit_task(self, picid: str, base64_data: str):
        self.queue.put_nowait((picid, base64_data))

    # [新增] 静态方法：GIF 关键帧横向拼接降维
    @staticmethod
    def _transform_gif(gif_base64: str, similarity_threshold: float = 1000.0, max_frames: int = 15) -> Optional[str]:
        """将GIF转换为水平拼接的静态图像, 跳过相似的帧以降低 VLM 理解难度"""
        try:
            if isinstance(gif_base64, str):
                gif_base64 = gif_base64.encode("ascii", errors="ignore").decode("ascii")
            
            gif_data = base64.b64decode(gif_base64)
            gif = Image.open(io.BytesIO(gif_data))

            all_frames = []
            try:
                while True:
                    gif.seek(len(all_frames))
                    frame = gif.convert("RGB")
                    all_frames.append(frame.copy())
            except EOFError:
                pass

            if not all_frames:
                return None

            selected_frames = []
            last_selected_frame_np = None

            for i, current_frame in enumerate(all_frames):
                current_frame_np = np.array(current_frame)
                if i == 0:
                    selected_frames.append(current_frame)
                    last_selected_frame_np = current_frame_np
                    continue

                if last_selected_frame_np is not None:
                    mse = np.mean((current_frame_np - last_selected_frame_np) ** 2)
                    if mse > similarity_threshold:
                        selected_frames.append(current_frame)
                        last_selected_frame_np = current_frame_np
                        if len(selected_frames) >= max_frames:
                            break

            if not selected_frames:
                return None

            frame_width, frame_height = selected_frames[0].size
            target_height = 200
            if frame_height == 0: return None
            
            target_width = int((target_height / frame_height) * frame_width)
            if target_width == 0: target_width = 1

            resized_frames = [
                frame.resize((target_width, target_height), Image.Resampling.LANCZOS) for frame in selected_frames
            ]

            total_width = target_width * len(resized_frames)
            if total_width == 0: return None

            combined_image = Image.new("RGB", (total_width, target_height))
            for idx, frame in enumerate(resized_frames):
                combined_image.paste(frame, (idx * target_width, 0))

            buffer = io.BytesIO()
            combined_image.save(buffer, format="JPEG", quality=85)
            return base64.b64encode(buffer.getvalue()).decode("utf-8")
            
        except Exception as e:
            logger.error(f"[AstrMai-VisualCortex] GIF 转换失败: {e}")
            return None

# [修改] 核心处理流程：单通 VLM 识别并入库
    async def process_image_async(self, picid: str, base64_data: str):
        import tempfile
        import os
        temp_file_path = None
        
        try:
            # 🟢 [核心修复] 缓存拦截屏障：如果数据库中已经存在该图片，直接跳过识别
            with self.db_service.get_session() as session:
                if session.get(VisualMemory, picid):
                    logger.info(f"[AstrMai-VisualCortex] ⚡ 命中视觉记忆缓存，跳过重复解析: {picid}")
                    return

            image_bytes = base64.b64decode(base64_data)
            image_format = Image.open(io.BytesIO(image_bytes)).format.lower()
            
            # GIF 降维处理
            if image_format in ["gif", "webp"]:
                transformed_b64 = self._transform_gif(base64_data)
                if transformed_b64:
                    base64_data = transformed_b64
                    image_bytes = base64.b64decode(base64_data) # 重新获取降维后的字节流
                    image_format = "jpeg"
                else:
                    logger.warning(f"[AstrMai-VisualCortex] GIF 处理失败，跳过识别: {picid}")
                    return

            # ==========================================
            # 🔪 [核心修改] 拔除 Base64 拼接毒瘤，改为物理落盘
            # ==========================================
            # 废弃: image_uri = f"data:image/{image_format};base64,{base64_data}"
            
            fd, temp_file_path = tempfile.mkstemp(suffix=f".{image_format}")
            with os.fdopen(fd, 'wb') as f:
                f.write(image_bytes)
                
            logger.info(f"[AstrMai-VisualCortex] 💾 图片已物理落盘至临时文件: {temp_file_path}")

            system_prompt = (
                "你是一个群聊视觉分析助手。请分析图片内容，并严格使用且仅使用以下 JSON 格式输出结果，不要输出任何 Markdown 标记：\n"
                '{ "type": "image" 或 "emoji", "description": "详细的画面内容描述，如果是梗图请解释梗", "emotion_tags": ["情绪1", "情绪2"] }。\n'
                "注意：如果 type 是 image，emotion_tags 可以为空数组。"
            )

            logger.info(f"[AstrMai-VisualCortex] 🚀 正在异步解析视觉输入: {picid} ({image_format})")
            
            # 传递纯净的物理文件路径给网关
            result_dict = await self.gateway.call_vision_task(
                image_data=temp_file_path,
                prompt="请分析这幅图片/表情包。",
                system_prompt=system_prompt
            )

            if not result_dict:
                logger.warning(f"[AstrMai-VisualCortex] ⚠️ 视觉模型返回为空: {picid}")
                return

            img_type = result_dict.get("type", "image")
            description = result_dict.get("description", "无法识别内容的图片")
            emotion_tags = result_dict.get("emotion_tags", [])
            tags_json_str = json.dumps(emotion_tags, ensure_ascii=False) if isinstance(emotion_tags, list) else "[]"

            logger.info(f"[AstrMai-VisualCortex] ✅ 解析完成 {picid} | 类型:{img_type} | 描述:{description[:20]}... | 标签:{tags_json_str}")

            # 内部函数闭包处理 DB
            def _save_db():
                with self.db_service.get_session() as session:
                    import time
                    mem = session.get(VisualMemory, picid)
                    if not mem:
                        mem = VisualMemory(
                            picid=picid,
                            type=img_type,
                            description=description,
                            emotion_tags=tags_json_str,
                            timestamp=time.time()
                        )
                        session.add(mem)
                    else:
                        mem.type = img_type
                        mem.description = description
                        mem.emotion_tags = tags_json_str
                        mem.timestamp = time.time()
                    session.commit()
            
            import asyncio
            await asyncio.to_thread(_save_db)

        except Exception as e:
            logger.error(f"[AstrMai-VisualCortex] ❌ 处理图片 {picid} 时发生未捕获异常: {e}", exc_info=True)
            
        finally:
            # ==========================================
            # 🧹 [核心修改] 执行完毕后，安全销毁本地临时文件
            # ==========================================
            if temp_file_path and os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                    logger.debug(f"[AstrMai-VisualCortex] 🧹 已安全销毁临时视觉文件: {temp_file_path}")
                except Exception as e:
                    logger.error(f"[AstrMai-VisualCortex] ⚠️ 无法删除临时视觉文件 {temp_file_path}: {e}")