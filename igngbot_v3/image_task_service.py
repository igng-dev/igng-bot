import asyncio
import base64
import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime

from .agent_loop import OpenAIToolAgent, ToolSpec
from .api_clients import (
    IMAGE_ASPECT_RATIOS,
    IMAGE_QUALITIES,
    IMAGE_QUALITY_LABELS,
    call_ccode_image,
    image_file_to_data_url,
    normalize_aspect_ratio,
    normalize_image_quality,
    resolution_label,
)
from .chat_service import ChatService, MessageContext
from .call_log_db import insert_call_log
from .context_manager import estimate_tokens
from .db import DBHandler
from .onebot_api import send_group_image, send_group_text
from .prompt_rules import RISK_SPEECH_CONSTRAINTS

logger = logging.getLogger(__name__)


class ImageTaskService:
    """Persistent conversations that can chat and use image/function tools."""

    IMAGE_PROGRESS_INTERVAL = 180

    _TASK_RE = re.compile(r"^/任务(?:\s+(.*))?$", re.DOTALL)
    _CREATE_RE = re.compile(r"^创建(?:\s+(.+))?$", re.DOTALL)
    _RENAME_RE = re.compile(r"^命名\s+#?(\d+)\s+(.+)$", re.DOTALL)
    _SUMMARY_RE = re.compile(r"^总结\s+#?(\d+)$", re.DOTALL)
    _CONTINUE_RE = re.compile(r"^#?(\d+)\s+(.+)$", re.DOTALL)
    _CONTINUOUS_RE = re.compile(r"^连续\s+#?(\d+)$", re.DOTALL)
    _EXIT_CONTINUOUS_RE = re.compile(r"^退出(?:\s+#?(\d+))?$", re.DOTALL)
    _SIZE_MAP = {
        "1k": "1024x1024",
        "2k": "2048x2048",
        "4k": "3840x2160",
    }
    TASK_CONTEXT_MAX_TOKENS = 200_000
    TASK_CONTEXT_KEEP_RECENT_RATIO = 0.15

    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.agent = OpenAIToolAgent(config)
        self.chat_tools = ChatService(config, db)
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._active_task_ids = set()
        self._activity_guard = threading.Lock()
        self._continuous_sessions = {}
        self._continuous_guard = threading.Lock()

    def start(self):
        logger.info("Image task service started")

    def _task_lock(self, task_id):
        with self._locks_guard:
            return self._locks.setdefault(int(task_id), threading.RLock())

    def _try_start_task_execution(self, task_id):
        with self._activity_guard:
            task_id = int(task_id)
            if task_id in self._active_task_ids:
                return False
            self._active_task_ids.add(task_id)
            return True

    def _finish_task_execution(self, task_id):
        with self._activity_guard:
            self._active_task_ids.discard(int(task_id))

    def _is_task_active(self, task_id):
        with self._activity_guard:
            return int(task_id) in self._active_task_ids

    def _send_text(self, task, text):
        import requests
        group_id = int(task["group_id"])
        if group_id < 0:
            endpoint = "send_private_msg"
            target = {"user_id": -group_id}
        else:
            endpoint = "send_group_msg"
            target = {"group_id": group_id}
        response = requests.post(
            f"{self.config.ONEBOT_HTTP_URL}/{endpoint}",
            json={**target, "message": text},
            headers={"Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}"},
            timeout=30,
        )
        try:
            result = response.json()
            message_id = (result.get("data") or {}).get("message_id")
            if message_id is not None and not self.db.get_message_by_msg_id(task["group_id"], message_id):
                self.db.insert_message(
                    group_id=task["group_id"],
                    sender_id=self.config.BOT_USER_ID,
                    message_content=text,
                    reply_to_msg_id=None,
                    msg_id=str(message_id),
                    is_self=True,
                )
        except Exception as exc:
            logger.debug("Failed to record task reply message ID: %s", exc)

    def _get_onebot_message(self, group_id, message_id):
        """Fetch a message that may not have been captured in message_logs."""
        import requests

        try:
            response = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/get_msg",
                json={"message_id": int(message_id)},
                headers={"Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}"},
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("retcode") not in (None, 0) or result.get("status") not in (None, "ok"):
                logger.warning("OneBot get_msg rejected message %s: %s", message_id, result)
                return None
            data = result.get("data") or {}
            if int(group_id) < 0:
                is_bot_private_message = (
                    data.get("message_type") == "private"
                    and (
                        str(data.get("user_id")) == str(self.config.BOT_USER_ID)
                        or str(data.get("target_id")) == str(-int(group_id))
                    )
                )
                if not is_bot_private_message:
                    logger.info("Quoted private message %s does not belong to user %s", message_id, -int(group_id))
                    return None
            elif str(data.get("group_id")) != str(group_id):
                logger.info(
                    "Quoted message %s belongs to group %s, not group %s",
                    message_id,
                    data.get("group_id"),
                    group_id,
                )
                return None
            if str(data.get("user_id")) != str(self.config.BOT_USER_ID):
                logger.info(
                    "Quoted message %s belongs to user %s, not this bot",
                    message_id,
                    data.get("user_id"),
                )
                return None
            logger.info("Fetched quoted bot message %s from OneBot", message_id)
            return {
                "message_content": data.get("raw_message") or data.get("message") or "",
                "is_self": True,
                "message": data.get("message"),
            }
        except Exception as exc:
            logger.warning("Failed to fetch quoted message %s from OneBot: %s", message_id, exc)
            return None

    def _get_onebot_message_data(self, message_id):
        """Fetch any quoted group message for continuous-mode image input."""
        import requests

        try:
            response = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/get_msg",
                json={"message_id": int(message_id)},
                headers={"Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}"},
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()
            if result.get("retcode") not in (None, 0) or result.get("status") not in (None, "ok"):
                return None
            return result.get("data") or None
        except Exception as exc:
            logger.warning("Failed to fetch quoted image message %s from OneBot: %s", message_id, exc)
            return None

    @staticmethod
    def _image_urls_from_message(message):
        urls = []
        for item in (message or {}).get("files") or []:
            if item.get("type") != "image":
                continue
            value = item.get("stored_path") or item.get("url")
            if value:
                urls.append(str(value))
        attachments = (message or {}).get("attachments_json")
        if attachments:
            try:
                attachments = json.loads(attachments) if isinstance(attachments, str) else attachments
            except (TypeError, json.JSONDecodeError):
                attachments = []
        for item in attachments or []:
            if item.get("type") == "image":
                value = item.get("stored_path") or item.get("original_url")
                if value:
                    urls.append(str(value))
        for item in (message or {}).get("message") or []:
            if not isinstance(item, dict) or item.get("type") != "image":
                continue
            url = (item.get("data") or {}).get("url")
            if url:
                urls.append(str(url))
        return [url for index, url in enumerate(urls) if url and url not in urls[:index]]

    def _current_task_image_refs(self, parsed):
        refs = self._image_urls_from_message(parsed)
        reply_id = parsed.get("reply_to_msg_id")
        if reply_id:
            quoted = self.db.get_message_by_msg_id(parsed["group_id"], reply_id)
            if not quoted:
                quoted = self._get_onebot_message_data(reply_id)
                if quoted and int(parsed["group_id"]) < 0:
                    if (
                        quoted.get("message_type") == "private"
                        and str(quoted.get("user_id")) == str(-int(parsed["group_id"]))
                    ):
                        quoted["group_id"] = parsed["group_id"]
            if quoted and str(quoted.get("group_id")) == str(parsed["group_id"]):
                refs.extend(self._image_urls_from_message(quoted))

        unique = []
        for ref in refs:
            if ref and ref not in unique:
                unique.append(ref)
        return unique[:12]

    @staticmethod
    def _continuous_session_key(owner_igng_id, group_id):
        return f"{owner_igng_id}:{int(group_id)}"

    def _get_continuous_session(self, owner_igng_id, group_id):
        session_key = self._continuous_session_key(owner_igng_id, group_id)
        with self._continuous_guard:
            session = self._continuous_sessions.get(session_key)
            return dict(session) if session else None

    def _schedule_continuous_expiry(self, owner_igng_id, group_id, task_id, delay=600):
        timer = threading.Timer(
            delay,
            self._expire_continuous_session,
            args=(str(owner_igng_id), int(group_id), int(task_id)),
        )
        timer.daemon = True
        timer.start()
        return timer

    def _enter_continuous_session(self, owner_igng_id, task_id, group_id):
        session_key = self._continuous_session_key(owner_igng_id, group_id)
        with self._continuous_guard:
            previous = self._continuous_sessions.pop(session_key, None)
            if previous and previous.get("timer"):
                previous["timer"].cancel()
            session = {
                "task_id": int(task_id),
                "group_id": int(group_id),
                "last_message_at": time.monotonic(),
            }
            self._continuous_sessions[session_key] = session
            session["timer"] = self._schedule_continuous_expiry(
                owner_igng_id, group_id, task_id
            )

    def _touch_continuous_session(self, owner_igng_id, task_id, group_id):
        session_key = self._continuous_session_key(owner_igng_id, group_id)
        with self._continuous_guard:
            session = self._continuous_sessions.get(session_key)
            if not session or int(session["task_id"]) != int(task_id):
                return
            if session.get("timer"):
                session["timer"].cancel()
            session["last_message_at"] = time.monotonic()
            session["timer"] = self._schedule_continuous_expiry(
                owner_igng_id, group_id, task_id
            )

    def _exit_continuous_session(self, owner_igng_id, group_id, task_id):
        session_key = self._continuous_session_key(owner_igng_id, group_id)
        with self._continuous_guard:
            session = self._continuous_sessions.get(session_key)
            if not session or int(session["task_id"]) != int(task_id):
                return None
            self._continuous_sessions.pop(session_key, None)
            if session.get("timer"):
                session["timer"].cancel()
            return session

    def _expire_continuous_session(self, owner_igng_id, group_id, task_id):
        session_key = self._continuous_session_key(owner_igng_id, group_id)
        with self._continuous_guard:
            session = self._continuous_sessions.get(session_key)
            if not session or int(session["task_id"]) != int(task_id):
                return
            elapsed = time.monotonic() - session["last_message_at"]
            if elapsed < 600:
                session["timer"] = self._schedule_continuous_expiry(
                    owner_igng_id, group_id, task_id, 600 - elapsed
                )
                return
            if self._is_task_active(task_id):
                session["timer"] = self._schedule_continuous_expiry(
                    owner_igng_id, group_id, task_id, 30
                )
                return
            self._continuous_sessions.pop(session_key, None)
        self._send_text(
            {"group_id": session["group_id"]},
            f"任务 #{task_id} 已闲置超过10分钟，已自动退出连续对话模式。",
        )

    def _available_image_models(self, user_id):
        return self.db.get_image_models_for_user(user_id)

    @staticmethod
    def _conversation(row):
        try:
            value = json.loads(row.get("conversation_json") or "[]")
            return value if isinstance(value, list) else []
        except (TypeError, json.JSONDecodeError):
            return []

    def _save_conversation(self, task_id, conversation, owner_igng_id):
        self.db.update_task(
            task_id,
            owner_igng_id,
            json.dumps(conversation, ensure_ascii=False),
        )

    def _append_message(self, task_id, message, owner_igng_id):
        with self._task_lock(task_id):
            row = self.db.get_task(task_id, owner_igng_id)
            if not row:
                return None
            conversation = self._conversation(row)
            conversation.append(message)
            self._save_conversation(task_id, conversation, owner_igng_id)
            return row

    def _system_prompt(self, task):
        available_models = self._available_image_models(task.get("requester_id"))
        model_text = "、".join(available_models) if available_models else "（当前没有可用模型）"
        tool_names = [tool.name for tool in self._tools(task)]
        tool_text = "、".join(tool_names) if tool_names else "（当前没有可用工具）"
        base_prompt = str(task.get("system_prompt") or "").strip()
        if not base_prompt:
            base_prompt = self.db.get_system_prompt("task")
        context_summary = str(task.get("context_summary") or "").strip()
        summary_section = (
            "\n# 已压缩的任务上下文\n"
            "以下摘要来自本任务较早的对话，作为长期背景使用；最新消息和未压缩的近期对话优先。\n"
            f"{context_summary}\n"
            if context_summary else ""
        )
        return f"""{base_prompt}

{summary_section}

{RISK_SPEECH_CONSTRAINTS}

# 工具使用
当前实际注册的工具只有：{tool_text}。
工具的名称、参数、枚举值和返回内容以本次请求中传入的工具定义为准。普通功能工具包括聊天历史、MC 服务器信息、玩家处罚、假人、领地、IGNG 用户和文章查询，以及发送已有表情。需要真实数据时优先调用对应工具，再根据结果回答。

不要把普通聊天问题强行改写成工具调用。头像和表情目录仅支持查询或发送已有内容，任务中不能创建、修改或删除头像、表情，也不能调用头像或表情专用生图流程。

# 图像生成工具
generate_image 是普通图片生成工具，不是任务的默认目的。只有用户明确要求生成图片，或当前上下文已经明确需要立即生成时才调用；如果用户只是讨论构图、风格或想法，先正常聊天。

涉及政治煽动、露骨色情、极端主义、仇恨、暴力犯罪或其他违法违规内容的图片请求，一律不调用 generate_image。直接向用户说明“这个做不到”，不要协助改写、规避或拆分这类请求。

当前用户确切可用的生图模型：{model_text}。
- 调用 generate_image 时必须明确指定 model，且只能从工具定义提供的模型枚举中选择。
- gpt-image-2-plus 支持 1k、2k、4k；gpt-image-2-fast 只能使用 1k。
- aspect_ratio 用于指定图像比例，支持 1:1、5:4、9:16、21:9、16:9、4:3、3:2、4:5、3:4、2:3；未指定时使用 1:1。
- quality 用于切换图像质量，支持 low、medium、high；默认使用 high（高质量），推荐保持高质量。
- 用户消息包含参考图 ID 时，先调用 query_reference_images 查询这些图；工具返回的图片会在本轮后续请求中继续作为多模态输入。
- 用户要求保存当前消息中的图片时，调用 save_reference_image，并根据工具返回的唯一 ID 告知用户。
- 当前用户消息中的图片会标记为“当前任务消息图片 #编号”，保存参考图时使用对应编号。
- 生图需要参考图时，通过 reference_image_ids 指定已经查询或保存的参考图 ID；不要根据消息引用关系自动猜测参考图。
- 用户未明确指定分辨率时使用 1k；普通用户没有可用模型时不要调用生图工具，应说明当前没有可用模型。
- prompt 必须是中文、自然流畅、可以直接交给图片模型的完整画面描述，包含主体、动作、表情、服装、构图、环境、光线、画风和必要限制；不要写成 JSON、字段清单、关键词堆砌、英文模板或“请生成……”这类半成品。
- 每张生成图片都会记录图像 ID、作者、提示词、模型、尺寸和 NAS 路径。
- 一旦调用 generate_image，就代表用户已同意立即生成，不要再询问确认。工具执行期间只需说明已经开始；工具返回后根据实际结果说明完成或失败。"""

    def _latest_task_input_images(self, task):
        for item in reversed(self._conversation(task)):
            if item.get("role") == "user":
                return list(item.get("image_refs") or [])
        return []

    def _materialize_reference_image(self, image_ref, owner_igng_id):
        import requests

        image_ref = str(image_ref or "")
        if os.path.isfile(image_ref):
            return image_ref
        content = None
        extension = ".png"
        if image_ref.startswith("data:") and "," in image_ref:
            header, encoded = image_ref.split(",", 1)
            content = base64.b64decode(encoded)
            extension = ".jpg" if "jpeg" in header else ".png"
        elif image_ref.startswith(("http://", "https://")):
            response = requests.get(image_ref, timeout=(15, 60))
            response.raise_for_status()
            content = response.content
            content_type = response.headers.get("Content-Type", "")
            if "jpeg" in content_type:
                extension = ".jpg"
            elif "webp" in content_type:
                extension = ".webp"
        if not content:
            return None
        directory = os.path.join(self.config.IMAGE_STORAGE_PATH, "references", str(owner_igng_id))
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"reference_{uuid.uuid4().hex}{extension}")
        with open(path, "wb") as image_file:
            image_file.write(content)
        return path

    def _reference_images_by_ids(self, owner_igng_id, reference_ids):
        rows = self.db.get_reference_images(owner_igng_id, reference_ids)
        selected = []
        for row in rows:
            path = row.get("file_path")
            if path and os.path.isfile(path):
                selected.append(self._image_input_url(path))
        return selected

    def _tools(self, task):
        async def save_reference_image(description, image_indexes=None):
            refs = self._latest_task_input_images(task)
            indexes = image_indexes or ([1] if len(refs) == 1 else [])
            if not indexes:
                return "error: 当前消息没有可保存的图片，请指定 image_indexes。"
            saved = []
            for value in indexes:
                try:
                    index = int(value) - 1
                except (TypeError, ValueError):
                    continue
                if index < 0 or index >= len(refs):
                    continue
                path = await asyncio.to_thread(
                    self._materialize_reference_image,
                    refs[index],
                    task["owner_igng_id"],
                )
                if not path:
                    continue
                reference_id = self.db.insert_reference_image(
                    task["owner_igng_id"],
                    str(description or "未命名参考图")[:500],
                    path,
                    source_msg_id=task.get("current_msg_id"),
                )
                saved.append(f"#{reference_id}")
            return (
                f"已保存参考图：{'、'.join(saved)}。请把这些唯一 ID 告知用户。"
                if saved else "error: 参考图保存失败。"
            )

        async def query_reference_images(reference_image_ids):
            rows = self.db.get_reference_images(task["owner_igng_id"], reference_image_ids)
            if not rows:
                return "error: 没有找到属于当前用户的参考图 ID。"
            image_urls = []
            labels = []
            for row in rows:
                path = row.get("file_path")
                if not path or not os.path.isfile(path):
                    labels.append(f"#{row['id']}（文件不存在）")
                    continue
                labels.append(f"#{row['id']}：{row.get('description') or '未命名'}")
                image_urls.append(self._image_input_url(path))
            return {
                "text": f"已查询参考图：{'；'.join(labels)}。这些图片已加入本轮多模态上下文。",
                "image_urls": image_urls,
            }

        async def generate_image(
            prompt,
            model,
            size=None,
            aspect_ratio="1:1",
            quality="high",
            reference_image_ids=None,
        ):
            return await self._generate_image(
                task["id"],
                str(prompt or "").strip(),
                model=model,
                size=size,
                aspect_ratio=aspect_ratio,
                quality=quality,
                reference_image_ids=reference_image_ids or [],
                group_id=task["group_id"],
                requester_id=task["requester_id"],
                owner_igng_id=task["owner_igng_id"],
            )

        image_tools = [
            ToolSpec(
                name="save_reference_image",
                description="将当前用户消息中的一张或多张图片保存为当前用户的长期参考图，并返回唯一 ID。",
                parameters={
                    "type": "object",
                    "properties": {
                        "description": {"type": "string", "description": "参考图的自然语言描述。"},
                        "image_indexes": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                            "description": "当前消息图片编号，从 1 开始；只有一张图时可省略。",
                        },
                    },
                    "required": ["description"],
                    "additionalProperties": False,
                },
                handler=save_reference_image,
            ),
            ToolSpec(
                name="query_reference_images",
                description="按当前用户拥有的参考图 ID 查询图片；查询结果会作为图片加入本轮后续上下文。",
                parameters={
                    "type": "object",
                    "properties": {
                        "reference_image_ids": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                            "description": "要查询的一个或多个参考图唯一 ID。",
                        },
                    },
                    "required": ["reference_image_ids"],
                    "additionalProperties": False,
                },
                handler=query_reference_images,
            ),
            ToolSpec(
                name="generate_image",
                description="按用户意图生成一张普通图片。必须指定当前可用模型。",
                parameters={
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "中文自然语言的完整画面描述，可直接交给图片模型；不要使用 JSON、Markdown、字段清单或英文关键词",
                        },
                        "model": {
                            "type": "string",
                            "enum": self._available_image_models(task.get("requester_id")),
                            "description": "必须从当前确切可用模型列表中选择",
                        },
                        "size": {
                            "type": "string",
                            "enum": ["1k", "2k", "4k"],
                            "description": "分辨率。用户没有明确指定时使用 1k",
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "enum": list(IMAGE_ASPECT_RATIOS),
                            "description": "图像比例。未明确指定时使用 1:1。",
                        },
                        "quality": {
                            "type": "string",
                            "enum": list(IMAGE_QUALITIES),
                            "description": "图像质量。默认 high（高质量），推荐使用 high。",
                        },
                        "reference_image_ids": {
                            "type": "array",
                            "items": {"type": "integer", "minimum": 1},
                            "description": "需要参考图时填写一个或多个已保存参考图 ID；不需要时省略。",
                        },
                    },
                    "required": ["prompt", "model"],
                    "additionalProperties": False,
                },
                handler=generate_image,
            )
        ]
        context = MessageContext(
            group_id=task["group_id"], sender_id=task["requester_id"], sender_name="",
            msg_id=None, rich_text="", direct_mention=True, files=[],
        )
        return image_tools + self.chat_tools._build_tools(
            context,
            None,
            include_image_generation=False,
            search_mode="task",
        )

    def _image_prompt_original(self, task_id):
        row = self.db.get_task(task_id)
        for item in self._conversation(row or {}):
            if item.get("role") == "user":
                return item.get("content") or ""
        return ""

    @staticmethod
    def _task_message_tokens(message):
        content = str(message.get("content") or "")
        total = estimate_tokens(content) + 20
        image_refs = message.get("image_refs") or []
        total += len(image_refs) * 765
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += estimate_tokens(json.dumps(tool_calls, ensure_ascii=False))
        return total

    @classmethod
    def _task_conversation_tokens(cls, conversation):
        return sum(cls._task_message_tokens(item) for item in conversation)

    @staticmethod
    def _format_task_summary_messages(messages):
        lines = []
        for item in messages:
            if item.get("task_status"):
                continue
            role = "用户" if item.get("role") == "user" else "云萤"
            content = str(item.get("content") or "").strip()
            if not content and not item.get("image_refs"):
                continue
            if len(content) > 6000:
                content = content[:6000] + "..."
            if item.get("image_refs"):
                content = f"{content} [包含 {len(item['image_refs'])} 张可用图片]".strip()
            lines.append(f"[{role}] {content}")
        return "\n".join(lines)

    async def _compress_task_context(self, task):
        conversation = self._conversation(task)
        if len(conversation) < 4:
            return task
        system_prompt = self._system_prompt(task)
        tools = self._tools(task)
        used_tokens = (
            estimate_tokens(system_prompt)
            + sum(estimate_tokens(json.dumps(tool.to_openai_tool(), ensure_ascii=False)) for tool in tools)
            + self._task_conversation_tokens(conversation)
        )
        if used_tokens < self.TASK_CONTEXT_MAX_TOKENS:
            return task

        recent_budget = max(1, int(self._task_conversation_tokens(conversation) * self.TASK_CONTEXT_KEEP_RECENT_RATIO))
        recent_tokens = 0
        split_at = len(conversation)
        for index in range(len(conversation) - 1, -1, -1):
            item_tokens = self._task_message_tokens(conversation[index])
            if recent_tokens and recent_tokens + item_tokens > recent_budget:
                break
            recent_tokens += item_tokens
            split_at = index
        if split_at <= 0:
            split_at = 1
        old_messages = conversation[:split_at]
        recent_messages = conversation[split_at:]
        if not old_messages or not recent_messages:
            return task

        previous = str(task.get("context_summary") or "").strip() or "（暂无已有任务摘要）"
        summary_prompt = f"""[任务名称]
{task.get('task_name') or '未命名任务'}

[已有任务摘要]
{previous}

[需要合并的较早任务对话]
{self._format_task_summary_messages(old_messages)}

请更新任务长期上下文摘要，供后续 agent 继续工作。摘要必须：
- 保留用户目标、明确要求、已确认的事实、重要偏好、已完成结果、失败原因、未解决问题和下一步。
- 保留必要的任务编号、图像编号、模型、尺寸、比例、工具结果和关键参数。
- 删除寒暄、重复提示词和已经被后续消息覆盖的中间过程。
- 不要猜测图片内容；只记录对话中明确说明的图片信息。
- 使用简洁中文，控制在 6000 字以内。
只输出更新后的摘要正文，不要输出分析过程或标题。"""
        try:
            summary_text = await self.agent.summarize(
                system_prompt=(
                    "你是持久任务的上下文压缩器。你要像 Codex 的长期上下文摘要一样，"
                    "把旧对话压缩成准确、可执行、可继续使用的工作记忆。"
                    "摘要不是对话回复，不要向用户提问，也不要编造信息。"
                ),
                user_prompt=summary_prompt,
                max_tokens=1800,
                model=self.config.LLM_CLOUD_MODEL,
            )
            summary_text = summary_text.strip()
            if not summary_text:
                raise RuntimeError("task context summary is empty")
            through = int(task.get("context_summary_through") or 0) + len(old_messages)
            self.db.update_task_context(
                task["id"],
                task["owner_igng_id"],
                json.dumps(recent_messages, ensure_ascii=False),
                summary_text,
                through,
            )
            compressed = self.db.get_task(task["id"], task["owner_igng_id"])
            logger.info(
                "Task %s context compressed at approximately %s tokens; removed %s messages",
                task["id"],
                used_tokens,
                len(old_messages),
            )
            return compressed or task
        except Exception:
            logger.exception("Task %s context compression failed; keeping original conversation", task["id"])
            return task

    @classmethod
    def _resolve_size(cls, size):
        return cls._SIZE_MAP.get(str(size or "").strip().lower(), "1024x1024")

    async def _generate_image(self, task_id, prompt, model, size=None, aspect_ratio="1:1", quality="high", reference_image_ids=None,
                              group_id=None, requester_id=None, owner_igng_id=None):
        if not prompt:
            return "error: 生图提示词为空，请先补充画面要求。"
        task = self.db.get_task(task_id, owner_igng_id)
        if not task:
            return "error: 生图任务不存在。"
        task = dict(task)
        task["group_id"] = group_id
        task["requester_id"] = requester_id
        task["owner_igng_id"] = owner_igng_id

        available_models = self._available_image_models(task.get("requester_id"))
        if model not in available_models:
            return "error: 当前用户没有可用的生图模型。"
        requested_size = str(size or "1k").strip().lower()
        if model == "gpt-image-2-fast" and requested_size not in ("1k", "1024x1024"):
            return "error: gpt-image-2-fast 只能使用 1k。"
        aspect_ratio = normalize_aspect_ratio(aspect_ratio)
        quality = normalize_image_quality(quality)
        size = self._resolve_size(size)
        normalized_reference_ids = []
        for value in reference_image_ids or []:
            try:
                reference_id = int(value)
            except (TypeError, ValueError):
                continue
            if reference_id > 0 and reference_id not in normalized_reference_ids:
                normalized_reference_ids.append(reference_id)
        reference_images = self._reference_images_by_ids(
            owner_igng_id, normalized_reference_ids
        )
        reference_label = (
            "、".join(f"#{reference_id}" for reference_id in normalized_reference_ids)
            if normalized_reference_ids
            else "无"
        )
        notice = (
            f"正在使用模型 {model}，按以下提示词生图（{resolution_label(size)}，比例 {aspect_ratio}，"
            f"质量 {IMAGE_QUALITY_LABELS[quality]}，参考图 {reference_label}）：\n"
            f"{prompt}"
        )
        self._append_message(
            task_id,
            {
                "role": "assistant",
                "content": notice,
                "message_type": "system",
                "system_message": "image_generation_start",
            },
            owner_igng_id,
        )
        await asyncio.to_thread(self._send_text, task, notice)

        generated_at = datetime.now()
        progress_task = asyncio.create_task(
            self._image_progress_loop(
                task,
                task_id,
                prompt,
                model,
                requested_size,
                aspect_ratio,
                quality,
                normalized_reference_ids,
            )
        )
        try:
            logger.info(
                "Task %s requesting image generation: model=%s size=%s aspect_ratio=%s quality=%s references=%s",
                task_id,
                model,
                requested_size,
                aspect_ratio,
                quality,
                len(reference_images),
            )
            result = await asyncio.to_thread(
                self._call_image_api,
                prompt,
                size,
                reference_images,
                model=model,
                aspect_ratio=aspect_ratio,
                quality=quality,
            )
            if not result:
                logger.error("Task %s image API returned no result", task_id)
                return "error: 图片接口调用失败。"

            file_path = await asyncio.to_thread(
                self._save_generated_image,
                result.get("b64_json"),
                result.get("url"),
                generated_at,
            )
            if not file_path:
                logger.error("Task %s image API returned no usable image data", task_id)
                return "error: 图片已返回但没有可发送的内容。"
            image_id = self.db.insert_image(
                owner_igng_id,
                prompt,
                model,
                requested_size,
                file_path,
                generated_at=generated_at,
                task_id=task_id,
                aspect_ratio=aspect_ratio,
                quality=quality,
            )
            image_sent = await self._send_image_with_text(
                task,
                file_path,
                f"图片生成完成，图像 #{image_id}。",
                image_id=image_id,
            )
            text = (
                f"图片生成完成，图像 #{image_id}。"
                if image_sent
                else f"图片 #{image_id} 已生成，但发送失败，请稍后重试。"
            )

            with self._task_lock(task_id):
                latest = self.db.get_task(task_id, owner_igng_id)
                conversation = self._conversation(latest or {})
                self.db.update_task(
                    task_id,
                    task["owner_igng_id"],
                    json.dumps(conversation, ensure_ascii=False),
                )
            self._append_message(
                task_id,
                {
                    "role": "assistant",
                    "content": text,
                    "message_type": "system",
                    "system_message": "image_generation_complete",
                },
                owner_igng_id,
            )
            logger.info("Task %s image generation completed as image #%s", task_id, image_id)
            return f"success: {text}"
        except Exception as exc:
            logger.exception("Image task %s generation failed", task_id)
            return "error: 图片生成失败，请稍后重试。"
        finally:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task

    async def _image_progress_loop(
        self,
        task,
        task_id,
        prompt,
        model,
        size,
        aspect_ratio,
        quality,
        reference_image_ids=None,
    ):
        """Ask the cloud LLM for a user-facing status while image generation waits."""
        started_at = datetime.now()
        reference_label = (
            "、".join(f"#{reference_id}" for reference_id in (reference_image_ids or []))
            if reference_image_ids
            else "无"
        )
        try:
            while True:
                await asyncio.sleep(self.IMAGE_PROGRESS_INTERVAL)
                elapsed = int((datetime.now() - started_at).total_seconds())
                status_prompt = (
                    f"任务 #{task_id} 正在使用模型 {model} 生成图片，尺寸为 {size}、比例为 {aspect_ratio}、"
                    f"质量为 {IMAGE_QUALITY_LABELS.get(quality, quality)}、参考图为 {reference_label}。"
                    f"已经等待约 {elapsed // 60} 分钟。当前仍处于等待图像接口返回阶段。\n"
                    f"用户提示词：{prompt[:1200]}"
                )
                try:
                    status = await self.agent.summarize(
                        system_prompt=(
                            "你是任务进度播报器。根据给出的任务状态，向用户写一条简短中文进度消息，"
                            "不超过100字。只能说明正在等待图像接口返回、已等待多久、使用的模型、尺寸、比例、质量和参考图ID；"
                            "不要编造百分比、接口内部状态或完成结果，也不要让用户重新提交任务。"
                        ),
                        user_prompt=status_prompt,
                        max_tokens=160,
                        model=self.config.LLM_CLOUD_MODEL,
                    )
                    status = status.strip()
                except Exception as exc:
                    logger.warning("Task %s progress LLM failed: %s", task_id, exc)
                    status = ""
                if not status:
                    status = (
                        f"任务 #{task_id} 仍在等待图像接口返回，已等待约 {elapsed // 60} 分钟，"
                        f"当前使用模型 {model}（{size}，比例 {aspect_ratio}，质量 {IMAGE_QUALITY_LABELS.get(quality, quality)}，"
                        f"参考图 {reference_label}）。"
                    )
                self._append_message(
                    task_id,
                    {
                        "role": "assistant",
                        "content": status,
                        "task_status": True,
                        "message_type": "system",
                        "system_message": "image_generation_progress",
                    },
                    task["owner_igng_id"],
                )
                await send_group_text(self.config, task["group_id"], status)
                logger.info("Task %s progress status sent after %ss", task_id, elapsed)
        except asyncio.CancelledError:
            logger.info("Task %s progress reporter stopped", task_id)
            raise

    def _call_image_api(self, prompt, size, reference_images=None, model=None, aspect_ratio="1:1", quality="high"):
        return call_ccode_image(
            self.config,
            prompt,
            size,
            images=reference_images,
            timeout=self.config.CCODE_IMAGE_TIMEOUT,
            model=model,
            aspect_ratio=aspect_ratio,
            quality=quality,
        )

    def _save_generated_image(self, b64_data, image_url, generated_at):
        import requests

        try:
            if b64_data:
                if "," in b64_data and b64_data.lstrip().startswith("data:"):
                    b64_data = b64_data.split(",", 1)[1]
                content = base64.b64decode(b64_data)
            elif image_url:
                response = requests.get(image_url, timeout=(15, 300))
                response.raise_for_status()
                content = response.content
            else:
                return None
            date_dir = generated_at.strftime("%Y-%m")
            directory = os.path.join(self.config.IMAGE_STORAGE_PATH, date_dir)
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(
                directory,
                f"image_{generated_at.strftime('%Y%m%d_%H%M%S%f')}_{uuid.uuid4().hex[:8]}.png",
            )
            with open(path, "wb") as image_file:
                image_file.write(content)
            logger.info("Generated image saved: %s (%s bytes)", path, len(content))
            return path
        except Exception as exc:
            logger.error("Failed to save generated image: %s", exc)
            return None

    def _conversation_for_llm(self, row):
        conversation = []
        for item in self._conversation(row):
            if item.get("task_status"):
                continue
            message = dict(item)
            message.pop("task_status", None)
            message.pop("image_ref", None)
            image_refs = message.pop("image_refs", None) or []
            if image_refs:
                blocks = [{"type": "text", "text": str(message.get("content") or "")}]
                for index, image_url in enumerate(image_refs, start=1):
                    image_url = self._image_input_url(image_url)
                    blocks.append({"type": "text", "text": f"[当前任务消息图片 #{index}]"})
                    blocks.append({"type": "image_url", "image_url": {"url": image_url}})
                message["content"] = blocks
            conversation.append(message)
        return conversation

    @staticmethod
    def _image_input_url(image_ref):
        image_ref = str(image_ref or "")
        if image_ref.startswith(("data:", "http://", "https://")):
            return image_ref
        if os.path.isfile(image_ref):
            try:
                return image_file_to_data_url(image_ref)
            except Exception as exc:
                logger.warning("Failed to encode task reference image %s: %s", image_ref, exc)
        return image_ref

    async def _send_image_with_text(self, task, image_path, text, image_id=None):
        if str(image_path or "").startswith(("http://", "https://", "data:")):
            result = await send_group_image(
                self.config, task["group_id"], image_path, return_result=True
            )
        else:
            result = await send_group_image(
                self.config, task["group_id"], f"file://{image_path}", return_result=True
            )
        image_sent = bool(result.get("ok"))
        if image_sent:
            source_msg_id = str(result.get("message_id") or f"generated-{image_id}")
            attachments = [{
                "type": "image",
                "stored_path": image_path,
                "image_id": image_id,
            }]
            try:
                self.db.insert_message(
                    group_id=task["group_id"],
                    sender_id=self.config.BOT_USER_ID,
                    message_content="[图片]",
                    message_structure=json.dumps(
                        [{"type": "image", "image_id": image_id}], ensure_ascii=False
                    ),
                    attachments_json=json.dumps(attachments, ensure_ascii=False),
                    reply_to_msg_id=None,
                    msg_id=source_msg_id,
                    file_url=image_path,
                    file_type="image",
                    is_self=True,
                )
                self.db.append_task_message(
                    task["id"],
                    task["owner_igng_id"],
                    {
                        "role": "assistant",
                        "content": "[图片]",
                        "image_id": image_id,
                        "message_type": "system",
                        "system_message": "image_generation_image",
                    },
                    source_msg_id=source_msg_id,
                )
            except Exception:
                logger.exception("Failed to record generated task image message")
        await asyncio.to_thread(
            self._send_text,
            task,
            text if image_sent else "图片发送失败，请稍后重试。",
        )
        return image_sent

    def _run_llm(self, task_id, group_id, requester_id, owner_igng_id):
        worker_db = DBHandler(self.config)
        try:
            worker_db.connect()
            worker = ImageTaskService(
                self.config,
                worker_db,
            )
            worker._locks = self._locks
            worker._locks_guard = self._locks_guard
            worker._active_task_ids = self._active_task_ids
            worker._activity_guard = self._activity_guard
            worker._run_llm_inner(task_id, group_id, requester_id, owner_igng_id)
        except Exception as exc:
            logger.exception("Image task %s worker failed before LLM execution", task_id)
            try:
                task = self.db.get_task(task_id, owner_igng_id)
                if task:
                    task = dict(task)
                    task["group_id"] = group_id
                    message = "任务处理失败，请稍后重试。"
                    self._append_message(task_id, {"role": "assistant", "content": message}, owner_igng_id)
                    self._send_text(task, message)
            except Exception:
                logger.exception("Failed to report image task %s worker failure", task_id)
        finally:
            worker_db.close()
            self._finish_task_execution(task_id)
            logger.info("Task %s execution finished", task_id)

    def _run_llm_inner(self, task_id, group_id, requester_id, owner_igng_id):
        with self._task_lock(task_id):
            task = self.db.get_task(task_id, owner_igng_id)
            if not task:
                return
            task = dict(task)
            task["group_id"] = group_id
            task["requester_id"] = requester_id
            task["owner_igng_id"] = owner_igng_id
            logger.info("Task %s LLM worker started", task_id)
            system_prompt = ""
            conversation = []
            try:
                task = asyncio.run(self._compress_task_context(task))
                task = dict(task)
                task["group_id"] = group_id
                task["requester_id"] = requester_id
                task["owner_igng_id"] = owner_igng_id
                system_prompt = self._system_prompt(task)
                conversation = self._conversation_for_llm(task)
                result = asyncio.run(
                    self.agent.run(
                        system_prompt=system_prompt,
                        conversation=conversation,
                        tools=self._tools(task),
                        model=self.config.LLM_CLOUD_MODEL,
                        max_steps=self.config.CLOUD_LLM_MAX_STEPS,
                        max_tokens=self.config.AGENT_MAX_TOKENS,
                    )
                )
                self._record_task_call(
                    task_id=task_id,
                    group_id=group_id,
                    sender_id=requester_id,
                    message_text=self._latest_task_user_text(task),
                    system_prompt=system_prompt,
                    user_prompt=json.dumps(conversation, ensure_ascii=False),
                    result=result,
                )
                text = (result.get("text") or "").strip()
                generated = any(
                    call.get("function", {}).get("name") == "generate_image"
                    for message in result.get("messages", [])
                    for call in message.get("tool_calls", [])
                )
                tool_errors = [
                    str(message.get("content") or "")
                    for message in result.get("messages", [])
                    if message.get("role") == "tool"
                    and str(message.get("content") or "").startswith("error:")
                ]
                if generated and self._is_post_generation_confirmation(text):
                    text = ""
                if text:
                    self._append_message(task_id, {"role": "assistant", "content": text}, owner_igng_id)
                    task = self.db.get_task(task_id, owner_igng_id)
                    task = dict(task)
                    task["group_id"] = group_id
                    self._send_text(task, text)
                elif tool_errors:
                    error_text = tool_errors[-1].removeprefix("error:").strip()
                    message = f"任务工具执行失败：{error_text or '未返回具体错误。'}"
                    logger.warning("Task %s completed with tool error: %s", task_id, error_text)
                    self._append_message(task_id, {"role": "assistant", "content": message}, owner_igng_id)
                    task = dict(self.db.get_task(task_id, owner_igng_id) or task)
                    task["group_id"] = group_id
                    self._send_text(task, message)
                elif result.get("raw", {}).get("error") == "max_steps_reached":
                    message = "任务处理步骤已达到上限，请引用本任务消息继续。"
                    self._append_message(task_id, {"role": "assistant", "content": message}, owner_igng_id)
                    task = dict(self.db.get_task(task_id, owner_igng_id) or task)
                    task["group_id"] = group_id
                    self._send_text(task, message)
                else:
                    message = "任务已完成，但模型没有返回可发送的结果。请引用本任务消息重试。"
                    logger.warning("Task %s completed without text or tool error", task_id)
                    self._append_message(task_id, {"role": "assistant", "content": message}, owner_igng_id)
                    task = dict(self.db.get_task(task_id, owner_igng_id) or task)
                    task["group_id"] = group_id
                    self._send_text(task, message)
            except Exception as exc:
                logger.exception("Image task %s LLM failed", task_id)
                self._record_task_call(
                    task_id=task_id,
                    group_id=group_id,
                    sender_id=requester_id,
                    message_text=self._latest_task_user_text(task),
                    system_prompt=system_prompt,
                    user_prompt=json.dumps(conversation, ensure_ascii=False),
                    response_content="",
                    success=False,
                    error_message=str(exc),
                )
                message = "任务处理失败，请稍后重试。"
                self._append_message(task_id, {"role": "assistant", "content": message}, owner_igng_id)
                task = self.db.get_task(task_id, owner_igng_id)
                if task:
                    task = dict(task)
                    task["group_id"] = group_id
                    self._send_text(task, message)

        if self._auto_summary_needed(task_id, owner_igng_id):
            self._spawn_summary(task_id, group_id, owner_igng_id)

    @staticmethod
    def _latest_task_user_text(task):
        for item in reversed(ImageTaskService._conversation(task)):
            if item.get("role") == "user":
                return str(item.get("content") or "")[:2000]
        return ""

    @staticmethod
    def _task_tool_calls(result):
        calls = []
        for message in result.get("messages", []):
            for call in message.get("tool_calls", []) or []:
                function = call.get("function") or {}
                calls.append(
                    {
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "arguments": function.get("arguments"),
                    }
                )
        return calls

    def _record_task_call(
        self,
        task_id,
        group_id,
        sender_id,
        message_text,
        system_prompt,
        user_prompt,
        result=None,
        response_content="",
        success=True,
        error_message="",
    ):
        result = result or {}
        if not response_content:
            response_content = str(result.get("text") or "")
        try:
            asyncio.run(
                insert_call_log(
                    group_id=str(group_id),
                    sender_id=str(sender_id),
                    task_id=int(task_id),
                    message_text=message_text,
                    call_type="task",
                    model=self.config.LLM_CLOUD_MODEL,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    response_content=response_content,
                    tool_calls=self._task_tool_calls(result),
                    token_usage=result.get("token_usage"),
                    success=success,
                    error_message=error_message,
                )
            )
        except Exception:
            logger.exception("Failed to record task %s LLM call log", task_id)

    def _auto_summary_needed(self, task_id, owner_igng_id):
        task = self.db.get_task(task_id, owner_igng_id)
        if not task or task.get("task_name"):
            return False
        user_messages = sum(
            1 for item in self._conversation(task) if item.get("role") == "user"
        )
        return user_messages >= 3

    @staticmethod
    def _task_summary_prompt(task):
        messages = []
        for item in ImageTaskService._conversation(task):
            if item.get("task_status"):
                continue
            role = "用户" if item.get("role") == "user" else "云萤"
            content = str(item.get("content") or "").strip()
            if content:
                messages.append(f"{role}：{content}")
        transcript = "\n".join(messages)[-6000:]
        return f"""请根据下面的任务对话生成一个简短任务名。
只输出任务名本身，不要引号、标点前后缀、解释或换行。
任务名不超过20个汉字，准确概括任务的主要主题或目标。

对话：
{transcript}"""

    @staticmethod
    def _clean_task_name(value):
        name = re.sub(r"\s+", " ", str(value or "")).strip()
        name = re.sub(r"^(任务名称|任务名|名称)\s*[:：]\s*", "", name)
        name = name.strip(" \t\r\n\"'“”‘’《》【】[]()（）：:，。.!！")
        return name[:50]

    def _spawn_summary(self, task_id, group_id=None, owner_igng_id=None):
        threading.Thread(
            target=self._run_summary,
            args=(task_id, group_id, owner_igng_id),
            daemon=True,
        ).start()

    def _run_summary(self, task_id, group_id=None, owner_igng_id=None):
        worker_db = DBHandler(self.config)
        try:
            worker_db.connect()
            worker = ImageTaskService(
                self.config,
                worker_db,
            )
            worker._locks = self._locks
            worker._locks_guard = self._locks_guard
            worker._summarize_task_inner(task_id, group_id, owner_igng_id)
        except Exception as exc:
            logger.exception("Task %s summary failed", task_id)
            task = self.db.get_task(task_id, owner_igng_id)
            if task:
                delivery = dict(task)
                if group_id is not None:
                    delivery["group_id"] = group_id
                self._send_text(delivery, f"任务 #{task_id} 总结失败：{exc}")
        finally:
            worker_db.close()

    def _summarize_task_inner(self, task_id, group_id=None, owner_igng_id=None):
        with self._task_lock(task_id):
            task = self.db.get_task(task_id, owner_igng_id)
            if not task:
                return
            name = asyncio.run(
                self.agent.summarize(
                    system_prompt=(
                        "你是任务命名器。只根据提供的任务对话生成简短、准确的中文任务名。"
                        "只输出任务名，不要解释，不要使用 Markdown。"
                    ),
                    user_prompt=self._task_summary_prompt(task),
                    max_tokens=40,
                    model=self.config.LLM_CLOUD_MODEL,
                )
            )
            name = self._clean_task_name(name)
            if not name:
                raise RuntimeError("任务名称为空")
            self.db.rename_task_global(owner_igng_id, task_id, name)
            delivery = dict(task)
            if group_id is not None:
                delivery["group_id"] = group_id
            self._send_text(delivery, f"任务 #{task_id} 总结完成，名称：{name}")

    @staticmethod
    def _is_post_generation_confirmation(text):
        normalized = re.sub(r"[，。！？、：；,.!?;:\s]", "", str(text or "")).lower()
        if not normalized:
            return False
        confirmation_terms = (
            "确认",
            "确定",
            "是否使用",
            "要不要使用",
            "是否生成",
            "要不要生成",
            "是否开始",
            "要不要开始",
        )
        return any(term in normalized for term in confirmation_terms)

    def _spawn(self, task_id, group_id, requester_id, owner_igng_id):
        threading.Thread(
            target=self._run_llm,
            args=(task_id, group_id, requester_id, owner_igng_id),
            daemon=True,
        ).start()

    def _resolve_task_owner(self, parsed):
        owner_igng_id = self.db.resolve_bound_igng_account_id(parsed["sender_id"])
        if owner_igng_id is None:
            self._send_text(
                {"group_id": parsed["group_id"]},
                "你的 QQ 尚未绑定 IGNG 账号，请先在 IGNG 网站绑定 QQ。",
            )
        return owner_igng_id

    def _handle_task_command(self, parsed, argument, owner_igng_id):
        argument = (argument or "").strip()
        create = self._CREATE_RE.fullmatch(argument)
        if create:
            task_name = (create.group(1) or "").strip()
            if not task_name:
                self._send_text(
                    {"task_type": "chat", "group_id": parsed["group_id"]},
                    "用法：/任务 创建 <任务名>",
                )
                return True
            self._create_chat_task(parsed, task_name, owner_igng_id)
            return True

        continuous = self._CONTINUOUS_RE.fullmatch(argument)
        if continuous:
            task_id = int(continuous.group(1))
            task = self.db.get_task(task_id, owner_igng_id)
            if not task:
                self._send_text({"group_id": parsed["group_id"]}, f"未找到任务 #{task_id}。")
                return True
            self._enter_continuous_session(owner_igng_id, task_id, parsed["group_id"])
            self._send_text(
                {"group_id": parsed["group_id"]},
                f"已进入 #{task_id} 任务的连续对话模式。后续消息会发送给该任务；使用 /任务 退出退出。",
            )
            return True

        exit_continuous = self._EXIT_CONTINUOUS_RE.fullmatch(argument)
        if exit_continuous:
            requested_task_id = exit_continuous.group(1)
            session = self._get_continuous_session(owner_igng_id, parsed["group_id"])
            task_id = int(requested_task_id) if requested_task_id else (
                int(session["task_id"]) if session else None
            )
            if task_id is not None and self._exit_continuous_session(
                owner_igng_id, parsed["group_id"], task_id
            ):
                message = f"已退出 #{task_id} 任务的连续对话模式。"
            else:
                message = "当前未处于连续对话模式。"
            self._send_text({"group_id": parsed["group_id"]}, message)
            return True

        summary = self._SUMMARY_RE.fullmatch(argument)
        if summary:
            task_id = int(summary.group(1))
            task = self.db.get_task(task_id, owner_igng_id)
            if not task:
                self._send_text(
                    {"task_type": "chat", "group_id": parsed["group_id"]},
                    f"未找到任务 #{task_id}。",
                )
                return True
            self._send_text(
                {"task_type": "chat", "group_id": parsed["group_id"]},
                f"开始总结任务 #{task_id}，请稍候。",
            )
            self._spawn_summary(task_id, parsed["group_id"], owner_igng_id)
            return True

        rename = self._RENAME_RE.fullmatch(argument)
        if rename:
            task_id, name = int(rename.group(1)), rename.group(2).strip()
            ok = self.db.rename_task_global(owner_igng_id, task_id, name)
            self._send_text(
                {"task_type": "chat", "group_id": parsed["group_id"]},
                f"任务 #{task_id} 已命名为：{name}" if ok else f"未找到任务 #{task_id}。",
            )
            return True

        continue_match = self._CONTINUE_RE.match(argument)
        if continue_match:
            task_id = int(continue_match.group(1))
            content = continue_match.group(2).strip()
            task = self.db.get_task(task_id, owner_igng_id)
            if not task:
                self._send_text(
                    {"task_type": "chat", "group_id": parsed["group_id"]},
                    f"未找到任务 #{task_id}。",
                )
                return True
            task = dict(task)
            task["group_id"] = parsed["group_id"]
            task["current_msg_id"] = parsed.get("msg_id")
            self._continue_task(
                task,
                content,
                image_refs=self._current_task_image_refs(parsed),
                requester_id=parsed["sender_id"],
                owner_igng_id=owner_igng_id,
            )
            return True
        if argument in ("列表", "list"):
            rows = self.db.list_named_tasks_global(owner_igng_id)
            lines = ["任务列表："]
            for row in rows:
                lines.append(f"#{row['id']} {row.get('task_name') or '未命名'}")
            self._send_text({"task_type": "chat", "group_id": parsed["group_id"]}, "\n".join(lines) if rows else "没有已命名的任务。")
            return True
        self._send_text(
            {"task_type": "chat", "group_id": parsed["group_id"]},
            "用法：/任务 创建 <任务名>、/任务 #id <对话>、/任务 命名 #id <名称>、/任务 总结 #id、/任务 列表",
        )
        return True

    def _create_chat_task(self, parsed, task_name, owner_igng_id):
        conversation = []
        task_id = self.db.create_task(
            owner_igng_id,
            task_name,
            json.dumps(conversation, ensure_ascii=False),
        )
        task = self.db.get_task(task_id, owner_igng_id)
        task = dict(task)
        task["group_id"] = parsed["group_id"]
        task["owner_igng_id"] = owner_igng_id
        intro = f"任务 #{task_id} 已创建，名称：{task_name}。请使用 /任务 #{task_id} <对话> 开始。"
        conversation.append(
            {
                "role": "assistant",
                "content": intro,
                "message_type": "system",
                "system_message": "task_created",
            }
        )
        self.db.update_task(
            task_id,
            owner_igng_id,
            json.dumps(conversation, ensure_ascii=False),
        )
        self._send_text(task, intro)

    def handle(self, parsed):
        content = (parsed.get("message_content") or "").strip()
        content = re.sub(r"^@[^\s/]+\s*", "", content).strip()
        task_match = self._TASK_RE.fullmatch(content)
        if task_match:
            owner_igng_id = self._resolve_task_owner(parsed)
            if owner_igng_id is None:
                return True
            return self._handle_task_command(parsed, task_match.group(1), owner_igng_id)

        owner_igng_id = self.db.resolve_bound_igng_account_id(parsed["sender_id"])
        continuous = (
            self._get_continuous_session(owner_igng_id, parsed["group_id"])
            if owner_igng_id is not None
            else None
        )
        if continuous:
            task = self.db.get_task(continuous["task_id"], owner_igng_id)
            if not task:
                self._exit_continuous_session(
                    owner_igng_id, parsed["group_id"], continuous["task_id"]
                )
                self._send_text(
                    {"group_id": parsed["group_id"]},
                    f"任务 #{continuous['task_id']} 已不存在，已退出连续对话模式。",
                )
                return True
            task = dict(task)
            task["group_id"] = parsed["group_id"]
            task["owner_igng_id"] = owner_igng_id
            task["current_msg_id"] = parsed.get("msg_id")
            self._touch_continuous_session(owner_igng_id, task["id"], parsed["group_id"])
            self._continue_task(
                task,
                content or "[空消息]",
                image_refs=self._current_task_image_refs(parsed),
                requester_id=parsed["sender_id"],
                owner_igng_id=owner_igng_id,
            )
            return True

        reply_id = parsed.get("reply_to_msg_id")
        if reply_id and content:
            logger.info(
                "Checking task reply: group=%s message=%s reply_to=%s",
                parsed["group_id"],
                parsed.get("msg_id"),
                reply_id,
            )
            referenced = self.db.get_message_by_msg_id(parsed["group_id"], reply_id)
            if not referenced:
                logger.info("Quoted message %s is absent from message_logs; querying OneBot", reply_id)
                referenced = self._get_onebot_message(parsed["group_id"], reply_id)
            if not referenced or not referenced.get("is_self"):
                logger.info("Quoted message %s is not a task-eligible bot message", reply_id)
                return False
            owner_igng_id = self._resolve_task_owner(parsed)
            if owner_igng_id is None:
                return True
            task = None
            task = self.db.find_task_by_assistant_text_global(
                owner_igng_id,
                referenced.get("message_content", ""),
                source_msg_id=reply_id,
            )
            if not task:
                logger.info(
                    "Quoted bot message %s is not part of an active task for IGNG user %s",
                    reply_id,
                    owner_igng_id,
                )
                return False
            if task:
                task = dict(task)
                task["group_id"] = parsed["group_id"]
                task["owner_igng_id"] = owner_igng_id
                task["current_msg_id"] = parsed.get("msg_id")
                self._continue_task(
                    task,
                    content,
                    requester_id=parsed["sender_id"],
                    owner_igng_id=owner_igng_id,
                )
                return True
            logger.info("No task found for IGNG user %s after quoted message %s", owner_igng_id, reply_id)
        return False

    def _continue_task(self, task, content, image_refs=None, requester_id=None, owner_igng_id=None):
        content = (content or "").strip()
        if not content:
            return
        if not self._try_start_task_execution(task["id"]):
            logger.info("Task %s continuation rejected because execution is active", task["id"])
            self._send_text(task, f"任务 #{task['id']} 正在进行，请稍候再试。")
            return
        acknowledgement = f"已收到任务 #{task['id']} 的补充内容，正在处理。"
        try:
            with self._task_lock(task["id"]):
                conversation = self._conversation(task)
                user_message = {
                    "role": "user",
                    "content": content,
                    "user_id": str(requester_id or owner_igng_id or task["owner_igng_id"]),
                }
                if image_refs:
                    user_message["image_refs"] = list(image_refs)
                conversation.append(user_message)
                conversation.append(
                    {
                        "role": "assistant",
                        "content": acknowledgement,
                        "task_status": True,
                        "message_type": "system",
                        "system_message": "task_acknowledgement",
                    }
                )
                self.db.update_task(
                    task["id"],
                    owner_igng_id or task["owner_igng_id"],
                    json.dumps(conversation, ensure_ascii=False),
                )
            self._send_text(task, acknowledgement)
            self._spawn(
                task["id"],
                task["group_id"],
                requester_id,
                owner_igng_id or task["owner_igng_id"],
            )
        except Exception:
            self._finish_task_execution(task["id"])
            raise
