import json
import logging
import os
import re
import threading
import time
import base64
from datetime import datetime

import requests

from .db import DBHandler
from .api_clients import (
    call_ccode_image,
    cloud_chat_completion,
    extract_resolution,
    image_file_to_data_url,
    resolution_label,
)

logger = logging.getLogger(__name__)

STICKER_ANALYSIS_COOLDOWN = 60


class StickerGenerator:
    def __init__(self, config):
        self.config = config
        self._db = None
        self._thread = None
        self._running = False
        self._trigger = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._last_message = None
        self._last_analysis = {}
        self._sessions = {}

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Sticker generator started")

    def _ensure_db(self):
        if self._db is None:
            self._db = DBHandler(self.config)
            self._db.connect()

    # --- Auto-analysis trigger (bot's own messages only) ---

    def trigger(self, group_id, message_content, sender_id):
        if sender_id != self.config.BOT_USER_ID:
            return
        with self._lock:
            now = datetime.now()
            last = self._last_analysis.get(0)
            if last and (now - last).total_seconds() < STICKER_ANALYSIS_COOLDOWN:
                return
            self._last_analysis[0] = now
            self._last_message = (group_id, message_content, sender_id)
            self._trigger = True
        self._wake.set()

    # --- Background loop ---

    def _run_loop(self):
        self._ensure_db()

        while self._running:
            self._wake.wait()
            self._wake.clear()

            with self._lock:
                if not self._trigger:
                    continue
                self._trigger = False
                group_id, message_content, sender_id = self._last_message
                self._last_message = None

            try:
                self._analyze_and_generate(group_id, message_content)
            except Exception as e:
                logger.error(f"Sticker analysis error: {e}", exc_info=True)

    def _analyze_and_generate(self, group_id, message_content):
        self._ensure_db()
        stickers = self._db.get_all_sticker_names()
        existing_names = [s["name"] for s in stickers]

        result = self._call_cloud_analyze(message_content, existing_names)
        if result is None:
            return

        if not result.get("need_new"):
            return

        name = result.get("name", "").strip()
        description = result.get("description", "").strip()
        if not name or not description:
            logger.warning("LLM returned need_new but missing name/description")
            return

        logger.info(f"LLM suggests new sticker category: [{name}] {description[:80]}...")
        self._do_generate_and_notify(group_id, name, description)

    # --- Cloud sticker analysis ---

    def _call_cloud_analyze(self, message_content, existing_names):
        try:
            names_list = "、".join(existing_names) if existing_names else "（无）"

            system_msg = (
                "你是一个表情种类管理助手。根据bot发出的消息内容，判断是否需要添加一个新的表情种类。"
                "关键原则：只在消息表达了现有种类完全无法覆盖的新情绪/氛围时才建议新建。"
                "近义情绪应被已有种类覆盖，例如：已有「开心」则不需要「喜悦」「快乐」；"
                "已有「疑惑」则不需要「困惑」「不解」；已有「生气」则不需要「愤怒」「恼火」。"
                "表情名称用2-3字中文情绪词。描述只写动作和神态，严禁描述发色、瞳色、服装等外观。"
            )
            user_msg = (
                f"当前已有表情种类：{names_list}\n\n"
                f"Bot刚发送的消息：{message_content}\n\n"
                f"请判断是否需要为这条消息新建一个表情种类。\n"
                f"不需要：{{\"need_new\": false}}\n"
                f"需要：{{\"need_new\": true, \"name\": \"新种类名\", "
                f"\"description\": \"只描述动作神态，勿描述外观，40字以内\"}}\n"
                f"只输出JSON，不要包含其他内容。"
            )
            content = cloud_chat_completion(
                self.config,
                [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.3,
                max_tokens=300,
            )
            content = content or ""
            logger.info(f"Sticker analysis response: {content[:200]}")

            text = content.strip()
            text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
            text = re.sub(r'<[^>]+>', '', text)

            if "```json" in text:
                text = text.split("```json", 1)[1].split("```", 1)[0]
            elif "```" in text:
                text = text.split("```", 1)[1].split("```", 1)[0]

            json_start = text.find("{")
            json_end = text.rfind("}")
            if json_start != -1 and json_end != -1:
                text = text[json_start : json_end + 1]

            return json.loads(text.strip())
        except Exception as e:
            logger.error(f"Sticker analysis cloud LLM call failed: {e}")
            return None

    # --- Manual sticker generation (via command) ---

    def _llm_generate_description(self, name):
        """Ask LLM to generate a sticker description for a given emotion name."""
        system_msg = (
            "你是一个二次元表情包设计师。用户给出一个表情名称（情绪），"
            "你需要为这个表情创作具体的画面描述。"
            "规则：1) 用中文自然语言描述，50字以内；"
            "2) 只描述角色的表情、动作和神态，严禁描述发色、瞳色、服装等外观特征；"
            "3) 只输出描述本身，不要任何分析或解释。"
        )
        content = cloud_chat_completion(
            self.config,
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": f"请为表情\"{name}\"创作一个具体的画面描述。"},
            ],
            temperature=1.0,
            max_tokens=200,
        )
        content = content.strip()
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        return content if content else None

    # --- CCode image edit API ---

    def _call_image_edit_api(self, prompt, image_path, size):
        try:
            return call_ccode_image(
                self.config,
                prompt,
                size,
                images=[image_file_to_data_url(image_path)],
                timeout=300,
            )
        except Exception as e:
            logger.error(f"Image edit API call failed: {e}")
            return None

    # --- Download and store ---

    def _download_image(self, url, name, generated_at):
        try:
            safe_name = re.sub(r'[\\/:*?"<>|]', '_', name)
            dir_path = os.path.join(self.config.STICKER_STORAGE_PATH, safe_name)
            os.makedirs(dir_path, exist_ok=True)

            file_name = f"sticker_{generated_at.strftime('%Y%m%d_%H%M%S')}.png"
            file_path = os.path.join(dir_path, file_name)

            logger.info(f"Downloading sticker from: {url[:80]}...")
            headers = {"User-Agent": "Mozilla/5.0"}
            last_error = None
            for attempt in range(1, 4):
                try:
                    resp = requests.get(url, headers=headers, timeout=(15, 300), stream=True)
                    resp.raise_for_status()

                    with open(file_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(f"Sticker download attempt {attempt}/3 failed: {e}")
                    time.sleep(3 * attempt)

            if last_error is not None:
                raise last_error

            file_size = os.path.getsize(file_path)
            logger.info(f"Sticker saved: {file_path} ({file_size / 1024:.0f}KB)")
            return file_path
        except Exception as e:
            logger.error(f"Failed to download sticker image: {e}")
            return None

    def _save_image_from_b64(self, b64_data, name, generated_at):
        try:
            safe_name = re.sub(r'[\\/:*?"<>|]', '_', name)
            dir_path = os.path.join(self.config.STICKER_STORAGE_PATH, safe_name)
            os.makedirs(dir_path, exist_ok=True)

            file_name = f"sticker_{generated_at.strftime('%Y%m%d_%H%M%S')}.png"
            file_path = os.path.join(dir_path, file_name)

            if "," in b64_data and b64_data.lstrip().startswith("data:"):
                b64_data = b64_data.split(",", 1)[1]

            with open(file_path, "wb") as f:
                f.write(base64.b64decode(b64_data))

            file_size = os.path.getsize(file_path)
            logger.info(f"Sticker saved from base64: {file_path} ({file_size / 1024:.0f}KB)")
            return file_path
        except Exception as e:
            logger.error(f"Failed to save base64 sticker image: {e}")
            return None

    # --- Core generation ---

    def _do_generate(self, group_id, name, description, avatar_id, size):
        """Generate a sticker and store it. If group_id is None, don't send messages."""
        self._ensure_db()

        # Get the avatar to use
        if avatar_id is not None:
            avatar = self._db.get_avatar_by_id(avatar_id)
            if avatar is None:
                if group_id:
                    self._send_reply(group_id, f"头像 #{avatar_id} 不存在")
                return
        else:
            avatar = self._db.get_current_avatar()
            if avatar is None:
                if group_id:
                    self._send_reply(group_id, "没有当前正在使用的头像，请先生成头像")
                return

        avatar_path = avatar.get("file_path", "")
        if not avatar_path or not os.path.exists(avatar_path):
            if group_id:
                self._send_reply(group_id, "当前头像文件不存在，请先生成头像")
            return

        actual_avatar_id = avatar["id"]

        # Build the edit prompt: keep the character, apply the expression
        edit_prompt = (
            f"同一个二次元可爱美少女角色，{description}，"
            f"保持角色外观、发型、服装和画风不变，只改变表情和动作神态"
        )

        if group_id:
            self._send_reply(group_id, f"正在生成表情「{name}」，请稍候（最长5分钟）... 当前分辨率：{resolution_label(size)}")

        generated_at = datetime.now()
        result = self._call_image_edit_api(edit_prompt, avatar_path, size)
        if result is None:
            if group_id:
                self._send_reply(group_id, f"表情「{name}」生成失败（API调用失败）")
            return

        image_url = result.get("url")
        b64_data = result.get("b64_json")
        if image_url:
            file_path = self._download_image(image_url, name, generated_at)
        elif b64_data:
            file_path = self._save_image_from_b64(b64_data, name, generated_at)
        else:
            file_path = None
        if file_path is None:
            if group_id:
                self._send_reply(group_id, f"表情「{name}」图片保存失败")
            return

        sticker_id = self._db.insert_sticker(
            name=name,
            description=description,
            avatar_id=actual_avatar_id,
            file_path=file_path,
            image_url=image_url,
            generated_at=generated_at,
        )
        logger.info(f"Sticker #{sticker_id} [{name}] generated with avatar #{actual_avatar_id}")

        if group_id:
            self._send_image_with_text(
                group_id, file_path,
                f"表情「{name}」生成完成 ID: {sticker_id}\n分辨率: {resolution_label(size)}\n描述: {description}",
            )

    def _do_generate_and_notify(self, group_id, name, description):
        """Generate silently and notify admin via private message."""
        self._ensure_db()

        avatar = self._db.get_current_avatar()
        if avatar is None:
            logger.warning("Auto sticker generation skipped: no current avatar")
            return

        avatar_path = avatar.get("file_path", "")
        if not avatar_path or not os.path.exists(avatar_path):
            logger.warning("Auto sticker generation skipped: avatar file missing")
            return

        edit_prompt = (
            f"同一个二次元可爱美少女角色，{description}，"
            f"保持角色外观、发型、服装和画风不变，只改变表情和动作神态"
        )

        generated_at = datetime.now()
        result = self._call_image_edit_api(edit_prompt, avatar_path, self.config.CCODE_DEFAULT_IMAGE_SIZE)
        if result is None:
            logger.error(f"Auto sticker generation failed for [{name}]")
            return

        image_url = result.get("url")
        b64_data = result.get("b64_json")
        if image_url:
            file_path = self._download_image(image_url, name, generated_at)
        elif b64_data:
            file_path = self._save_image_from_b64(b64_data, name, generated_at)
        else:
            file_path = None
        if file_path is None:
            logger.error(f"Auto sticker save failed for [{name}]")
            return

        sticker_id = self._db.insert_sticker(
            name=name,
            description=description,
            avatar_id=avatar["id"],
            file_path=file_path,
            image_url=image_url,
            generated_at=generated_at,
        )
        logger.info(f"Auto sticker #{sticker_id} [{name}] generated")

        # Notify admin via private message with image + info
        admin_msg = (
            f"新表情种类「{name}」已自动生成\n"
            f"ID: {sticker_id}\n"
            f"分辨率: {resolution_label(self.config.CCODE_DEFAULT_IMAGE_SIZE)}\n"
            f"描述: {description}\n"
            f"触发消息: {group_id if group_id else 'N/A'}"
        )
        self._send_private_msg(self.config.ADMIN_USER_ID, file_path, admin_msg)

    # --- Command handler ---

    def _cleanup_sessions(self):
        now = time.time()
        expired = [
            uid for uid, s in self._sessions.items()
            if now - s["created_at"] > 600
        ]
        for uid in expired:
            del self._sessions[uid]

    def _cancel_session(self, sender_id):
        if sender_id in self._sessions:
            group_id = self._sessions[sender_id]["group_id"]
            self._sessions.pop(sender_id, None)
            self._send_reply(group_id, "之前的操作已取消")

    def handle_command(self, parsed):
        content = parsed.get("message_content", "").strip()
        content = re.sub(r"^@[^\s/]+\s*", "", content).strip()
        sender_id = parsed.get("sender_id", 0)
        group_id = parsed.get("group_id", 0)
        is_admin = self.config.is_admin_user(sender_id)

        self._cleanup_sessions()

        # Check session response commands
        session = self._sessions.get(sender_id)
        if session and session["group_id"] == group_id:
            clean_content, new_size, has_size = extract_resolution(
                content, session.get("size", self.config.CCODE_DEFAULT_IMAGE_SIZE)
            )
            if has_size:
                session["size"] = new_size
                session["created_at"] = time.time()
                content = clean_content
            # /手动 提示词
            m_manual = re.search(r'/手动\s+(.+)', content)
            if m_manual:
                return self._handle_session_cmd(sender_id, group_id, "手动", None, m_manual.group(1).strip())
            m = re.search(r'/(确认|重做|取消)\s*(\d*)', content)
            if m:
                cmd = m.group(1)
                num = int(m.group(2)) if m.group(2) else None
                return self._handle_session_cmd(sender_id, group_id, cmd, num, None)

        # /表情列表
        m = re.search(r'/表情列表', content)
        if m:
            self._cancel_session(sender_id)
            threading.Thread(target=self._handle_list_cmd, args=(group_id,), daemon=True).start()
            return True

        # /表情查看 表情名称
        m = re.search(r'/表情查看\s+(\S+)', content)
        if m:
            self._cancel_session(sender_id)
            name = m.group(1)
            threading.Thread(target=self._handle_view_cmd, args=(group_id, name), daemon=True).start()
            return True

        # /表情生成 表情名称 [头像id]
        m = re.search(r'/表情生成\s+(\S+)(?:\s+(\d+))?', content)
        if m:
            self._cancel_session(sender_id)
            if not is_admin:
                self._send_reply(group_id, "只有bot管理员可以使用此命令")
                return True
            cleaned, size, _ = extract_resolution(content, self.config.CCODE_DEFAULT_IMAGE_SIZE)
            m2 = re.search(r'/表情生成\s+(\S+)(?:\s+(\d+))?', cleaned)
            name = m2.group(1)
            avatar_id = int(m2.group(2)) if m2.group(2) else None
            threading.Thread(target=self._handle_generate_cmd, args=(group_id, sender_id, name, avatar_id, size), daemon=True).start()
            return True

        # /表情删除 id
        m = re.search(r'/表情删除\s+(\d+)', content)
        if m:
            self._cancel_session(sender_id)
            if not is_admin:
                self._send_reply(group_id, "只有bot管理员可以使用此命令")
                return True
            sticker_id = int(m.group(1))
            threading.Thread(target=self._handle_delete_cmd, args=(group_id, sticker_id), daemon=True).start()
            return True

        return False

    def _handle_session_cmd(self, sender_id, group_id, cmd, num, manual_prompt):
        session = self._sessions.get(sender_id)
        if not session or session["state"] != "awaiting_confirm":
            return False

        if cmd == "取消":
            self._cancel_session(sender_id)
            return True

        if cmd == "确认":
            descriptions = session.get("descriptions", [])
            if not descriptions:
                self._send_reply(group_id, "没有可用的描述")
                return True
            idx = max(0, min((num or 1) - 1, len(descriptions) - 1))
            if idx >= len(descriptions):
                self._send_reply(group_id, f"序号超出范围，共有 {len(descriptions)} 个描述")
                return True
            name = session["name"]
            description = descriptions[idx]
            avatar_id = session.get("avatar_id")
            self._sessions.pop(sender_id, None)
            label = f"第 {idx + 1} 个" if len(descriptions) > 1 else ""
            self._send_reply(group_id, f"已选择{label}描述，正在生成表情「{name}」... 接下来将使用{resolution_label(session['size'])}分辨率。")
            threading.Thread(
                target=self._do_generate, args=(group_id, name, description, avatar_id, session["size"]),
                daemon=True,
            ).start()
            return True

        if cmd == "重做":
            count = max(1, min(num or 1, 5))
            self._send_reply(group_id, f"正在生成 {count} 个表情描述，请稍候... 接下来将使用{resolution_label(session['size'])}分辨率。")
            descriptions = []
            for i in range(count):
                try:
                    d = self._llm_generate_description(session["name"])
                except Exception as e:
                    logger.error(f"Re-generate description {i + 1} failed: {e}")
                    d = None
                if d:
                    descriptions.append(d)

            if not descriptions:
                self._send_reply(group_id, "重新生成失败，请重试")
                return True

            session["descriptions"] = descriptions
            session["created_at"] = time.time()

            if len(descriptions) == 1:
                msg = (
                    f"表情「{session['name']}」新描述：\n{descriptions[0]}\n\n"
                    f"接下来将使用{resolution_label(session['size'])}分辨率。\n"
                    "回复「/确认」确认生成\n"
                    "回复「/重做 [数量]」重新生成\n"
                    "回复「/手动 <提示词>」手动指定\n"
                    "也可加入 1k/2k/4k 修改分辨率\n"
                    "回复「/取消」取消操作"
                )
            else:
                lines = [f"表情「{session['name']}」共生成 {len(descriptions)} 个描述：", ""]
                for i, d in enumerate(descriptions, 1):
                    lines.append(f"{i}. {d}")
                lines.append("")
                lines.append(f"接下来将使用{resolution_label(session['size'])}分辨率。")
                lines.append("回复「/确认 [序号]」选择描述")
                lines.append("回复「/重做 [数量]」重新生成")
                lines.append("回复「/手动 <提示词>」手动指定")
                lines.append("也可加入 1k/2k/4k 修改分辨率")
                lines.append("回复「/取消」取消操作")
                msg = "\n".join(lines)
            self._send_reply(group_id, msg)
            return True

        if cmd == "手动":
            name = session["name"]
            avatar_id = session.get("avatar_id")
            self._sessions.pop(sender_id, None)
            self._send_reply(group_id, f"已使用手动描述，正在生成表情「{name}」... 接下来将使用{resolution_label(session['size'])}分辨率。")
            threading.Thread(
                target=self._do_generate, args=(group_id, name, manual_prompt, avatar_id, session["size"]),
                daemon=True,
            ).start()
            return True

        return False

    # --- Command handlers ---

    def _handle_list_cmd(self, group_id):
        self._ensure_db()
        stickers = self._db.get_all_sticker_names()
        if not stickers:
            self._send_reply(group_id, "当前还没有任何表情")
            return

        names = [s["name"] for s in stickers]
        self._send_reply(group_id, "=== 表情列表 ===\n\n" + "\n".join(f"「{n}」" for n in names))

    def _handle_view_cmd(self, group_id, name):
        self._ensure_db()
        stickers = self._db.get_stickers_by_name(name)
        if not stickers:
            self._send_reply(group_id, f"没有找到表情「{name}」")
            return

        self._send_reply(group_id, f"表情「{name}」共 {len(stickers)} 张：")
        for s in stickers:
            file_path = s.get("file_path", "")
            generated_at = s.get("generated_at")
            time_label = generated_at.strftime("%m-%d %H:%M") if generated_at else "未知"
            info = f"ID: {s['id']} | 头像ID: {s['avatar_id']} | {time_label}"

            if file_path and os.path.exists(file_path):
                self._send_image_with_text(group_id, file_path, info)
            else:
                self._send_reply(group_id, info + "\n(图片文件不存在)")

    def _handle_generate_cmd(self, group_id, sender_id, name, avatar_id=None, size=None):
        size = size or self.config.CCODE_DEFAULT_IMAGE_SIZE
        self._send_reply(group_id, f"正在为「{name}」生成表情描述，请稍候... 接下来将使用{resolution_label(size)}分辨率。")
        try:
            description = self._llm_generate_description(name)
        except Exception as e:
            logger.error(f"LLM description generation failed: {e}")
            self._send_reply(group_id, f"表情描述生成失败：{e}")
            return

        if not description:
            self._send_reply(group_id, "表情描述生成失败，请重试")
            return

        self._sessions[sender_id] = {
            "state": "awaiting_confirm",
            "group_id": group_id,
            "name": name,
            "descriptions": [description],
            "avatar_id": avatar_id,
            "size": size,
            "created_at": time.time(),
        }
        self._send_reply(
            group_id,
            f"表情「{name}」描述：\n{description}\n\n"
            f"接下来将使用{resolution_label(size)}分辨率。\n"
            "回复「/确认」确认生成\n"
            "回复「/重做 [数量]」重新生成描述\n"
            "回复「/手动 <提示词>」手动指定描述\n"
            "也可加入 1k/2k/4k 修改分辨率\n"
            "回复「/取消」取消操作",
        )

    def _handle_delete_cmd(self, group_id, sticker_id):
        self._ensure_db()
        sticker = self._db.get_sticker_by_id(sticker_id)
        if sticker is None:
            self._send_reply(group_id, f"表情 #{sticker_id} 不存在")
            return

        self._db.delete_sticker(sticker_id)
        self._send_reply(group_id, f"表情「{sticker['name']}」#{sticker_id} 已删除")
        logger.info(f"Sticker #{sticker_id} [{sticker['name']}] deleted")

    # --- OneBot message helpers ---

    def _send_reply(self, group_id, text):
        try:
            headers = {
                "Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}",
                "Content-Type": "application/json",
            }
            r = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/send_group_msg",
                json={"group_id": group_id, "message": text},
                headers=headers,
                timeout=10,
            )
            r.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send reply: {e}")

    def _send_image_with_text(self, group_id, image_path, text):
        try:
            headers = {
                "Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}",
                "Content-Type": "application/json",
            }
            payload = {
                "group_id": group_id,
                "message": [
                    {"type": "image", "data": {"file": f"file://{image_path}"}},
                    {"type": "text", "data": {"text": text}},
                ],
            }
            r = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/send_group_msg",
                json=payload,
                headers=headers,
                timeout=10,
            )
            r.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send image reply: {e}")
            self._send_reply(group_id, text)

    def _send_private_msg(self, user_id, image_path, text):
        try:
            headers = {
                "Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}",
                "Content-Type": "application/json",
            }
            payload = {
                "user_id": user_id,
                "message": [
                    {"type": "image", "data": {"file": f"file://{image_path}"}},
                    {"type": "text", "data": {"text": text}},
                ],
            }
            r = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/send_private_msg",
                json=payload,
                headers=headers,
                timeout=10,
            )
            r.raise_for_status()
            logger.info(f"Sticker notification sent to admin {user_id}")
        except Exception as e:
            logger.error(f"Failed to send private msg: {e}")
