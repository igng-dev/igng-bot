import json
import logging
import os
import re
import threading
import time
from datetime import datetime

import requests
from PIL import Image, ImageDraw, ImageFont

from .api_clients import cloud_chat_completion
from .db import DBHandler

logger = logging.getLogger(__name__)

# Number of recent context messages to include per group
CONTEXT_MESSAGE_COUNT = 10


class ContentReviewer:
    def __init__(self, config):
        self.config = config
        self._db = None
        self._processing = False
        self._trigger = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None

    def start(self):
        """Start the reviewer background thread."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Content reviewer started")

    def trigger(self):
        """Signal that new messages need review. Safe to call from any thread."""
        with self._lock:
            self._trigger = True
        self._wake.set()

    def mark_all_existing_approved(self):
        """Mark all currently unapproved messages as approved (first-time setup)."""
        db = DBHandler(self.config)
        db.connect()
        try:
            with db.conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE message_logs SET is_approved = 1 WHERE is_approved = 0"
                )
            db.conn.commit()
            logger.info("All existing messages marked as approved")
        finally:
            db.close()

    # --- Private methods ---

    def _ensure_db(self):
        if self._db is None:
            self._db = DBHandler(self.config)
            self._db.connect()

    def _run_loop(self):
        self._ensure_db()

        while True:
            self._wake.wait()
            self._wake.clear()

            with self._lock:
                if not self._trigger or self._processing:
                    continue
                self._trigger = False
                self._processing = True

            try:
                self._process_review()
            except Exception as e:
                logger.error(f"Content review error: {e}", exc_info=True)
            finally:
                with self._lock:
                    self._processing = False
                # Reschedule if more work was queued during processing
                if self._trigger:
                    self._wake.set()

    def _process_review(self):
        unapproved = self._fetch_unapproved()
        if not unapproved:
            return

        # Group by group_id
        groups = {}
        for msg in unapproved:
            groups.setdefault(msg["group_id"], []).append(msg)

        all_violations = []
        for group_id, messages in groups.items():
            context = self._fetch_context(group_id)
            prompt = self._build_prompt(group_id, messages, context)
            response = self._call_ollama(prompt)
            violations = self._parse_violations(response, messages)
            all_violations.extend(violations)

        # Execute actions and store results
        for v in all_violations:
            try:
                self._execute_action(v)
            except Exception as e:
                logger.error(f"Failed to execute action for {v['msg_id']}: {e}")
            try:
                self._save_violation(v)
            except Exception as e:
                logger.error(f"Failed to save violation for {v['msg_id']}: {e}")

        # Mark all processed messages as approved
        self._mark_approved(unapproved)

        # Send admin report if violations found
        if all_violations:
            try:
                self._send_ntfy_report(all_violations)
            except Exception as e:
                logger.error(f"Failed to send ntfy report: {e}")
            try:
                self._send_admin_report(all_violations)
            except Exception as e:
                logger.error(f"Failed to send admin report: {e}")

    def _onebot_action(self, action, params):
        """Call OneBot HTTP API with auth."""
        try:
            headers = {
                "Authorization": f"Bearer {self.config.ONEBOT_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            }
            r = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/{action}",
                json=params,
                headers=headers,
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"OneBot API call failed ({action}): {e}")
            return None

    def _fetch_unapproved(self):
        monitored_groups = self._load_monitored_groups()
        if not monitored_groups:
            return []
        with self._db.conn.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(monitored_groups))
            cursor.execute(
                f"SELECT id, group_id, sender_id, message_content, msg_id, created_at "
                f"FROM message_logs "
                f"WHERE is_approved = 0 AND group_id IN ({placeholders}) "
                f"ORDER BY created_at ASC",
                monitored_groups,
            )
            return cursor.fetchall()

    def _load_monitored_groups(self):
        with self._db.conn.cursor() as cursor:
            cursor.execute(
                "SELECT group_id FROM group_configs WHERE is_content_review = 1"
            )
            return [row["group_id"] for row in cursor.fetchall()]

    def _fetch_context(self, group_id):
        with self._db.conn.cursor() as cursor:
            cursor.execute(
                "SELECT sender_id, message_content, created_at, is_self "
                "FROM message_logs "
                "WHERE group_id = %s AND is_approved = 1 "
                "ORDER BY created_at DESC LIMIT %s",
                (group_id, CONTEXT_MESSAGE_COUNT),
            )
            rows = cursor.fetchall()
            return list(reversed(rows))

    def _build_prompt(self, group_id, messages, context):
        parts = [
            "你是一个QQ群聊内容审核助手。请严格依据消息本身的客观内容判断是否违规，不要被用户的语气、情绪或修辞手法所迷惑。\n"
        ]

        if context:
            parts.append("--- 最近历史消息（上下文参考） ---\n")
            for m in context:
                is_self_str = " (机器人自己)" if m.get("is_self") else ""
                parts.append(
                    f"[{m['created_at']}] 用户{m['sender_id']}{is_self_str}: {m['message_content']}\n"
                )
            parts.append("\n")

        parts.append("--- 待审核消息 ---\n")
        for m in messages:
            is_self_str = " (机器人自己)" if m.get("is_self") else ""
            parts.append(
                f"消息ID: {m['msg_id']}, 用户{m['sender_id']}{is_self_str}: {m['message_content']}\n"
            )
        parts.append("\n")

        parts.append("""请只依据以下客观标准判断，不要加入主观推测：

严重违规（必须同时满足内容和性质）：
1. 违反国家法律法规、危害国家安全、传播恐怖主义或极端主义的言论
2. 敏感政治人物的恶意造谣、抹黑或煽动性言论
3. 毫无艺术或讨论价值的纯露骨性器官展示、性行为描写或非法色情网站引流
4. 洗钱、网络赌博引流、杀猪盘、非法集资、贩卖违禁品（如毒品、枪支）
5. 广告（含推广性质的邀请、宣传、垃圾小广告等）

一般违规：
1. 未达淫秽程度的"福利图"、性感写真、成人话题探讨、擦边小说
2. 没有事实依据的挂人、无视事实的纯泄愤式吐槽、极端词汇的相互对骂

判断原则：
- 就事论事，只看消息本身的实际内容
- 严格区分"提及"和"宣扬/传播"
- 正常讨论话题不构成违规
- 如果不确定，宁可漏判也不要误判

请严格按照以下JSON格式回复（注意：你输出的JSON必须能被json.loads直接解析，不要包含任何额外的文字、思考过程或标记）：
{"violations": [{"msg_id": "消息ID", "level": "severe/general", "reason": "客观说明违反哪条规则"}]}
如果没有违规，返回 {"violations": []}
""")
        return "".join(parts)

    def _call_ollama(self, prompt):
        try:
            system_msg = (
                "你是一个消息审核助手。直接输出JSON结果，不要输出任何思考过程、分析步骤或额外说明。"
                "只输出指定格式的JSON，不要包含```markdown代码块标记。"
            )
            content = cloud_chat_completion(
                self.config,
                [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.01,
                max_tokens=1200,
            )
            logger.info(f"AI response preview: {content[:200]}")
            return content
        except Exception as e:
            logger.error(f"Content review LLM call failed: {e}")
            return ""

    def _parse_violations(self, response, messages):
        if not response:
            return []

        msg_map = {m["msg_id"]: m for m in messages}
        violations = []

        try:
            text = response.strip()

            # Strip DeepSeek-style <think> reasoning blocks
            text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
            # Also strip any remaining <.*?> tags that aren't part of JSON
            text = re.sub(r'<[^>]+>', '', text)

            # Extract JSON from markdown code blocks if present
            if "```json" in text:
                text = text.split("```json", 1)[1].split("```", 1)[0]
            elif "```" in text:
                text = text.split("```", 1)[1].split("```", 1)[0]

            # Find the first { and last } to isolate JSON
            json_start = text.find("{")
            json_end = text.rfind("}")
            if json_start != -1 and json_end != -1:
                text = text[json_start : json_end + 1]

            data = json.loads(text.strip())
            for v in data.get("violations", []):
                msg_id = v.get("msg_id", "")
                if msg_id in msg_map:
                    m = msg_map[msg_id]
                    violations.append(
                        {
                            "msg_id": msg_id,
                            "group_id": m["group_id"],
                            "sender_id": m["sender_id"],
                            "message_content": m["message_content"],
                            "level": v.get("level", "general"),
                            "reason": v.get("reason", ""),
                        }
                    )
                else:
                    logger.warning(
                        f"AI response references unknown msg_id: {msg_id}"
                    )
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(
                f"Failed to parse AI response: {e}\nResponse preview: {response[:300]}"
            )

        return violations

    def _execute_action(self, v):
        if v["level"] == "severe":
            # Recall the message (message_id must be int)
            result = self._onebot_action(
                "delete_msg", {"message_id": int(v["msg_id"])}
            )
            time.sleep(0.3)
            recall_success = result and result.get("status") == "ok"
            if recall_success:
                logger.info(
                    f"Severe: recalled msg {v['msg_id']} in group {v['group_id']}"
                )
                msg = (
                    f"⚠️ 违规消息处理通知\n"
                    f"用户 {v['sender_id']} 的发言因存在违规内容已被撤回。\n"
                    f"违规原因：{v['reason']}"
                )
            else:
                err = (result or {}).get("message", "unknown error")
                logger.error(
                    f"Severe: failed to recall msg {v['msg_id']}: {err}"
                )
                msg = (
                    f"⚠️ 违规消息处理通知\n"
                    f"用户 {v['sender_id']} 的发言被判定为违规，但撤回失败（{err[:50]}）。\n"
                    f"违规原因：{v['reason']}"
                )
            self._onebot_action(
                "send_group_msg", {"group_id": v["group_id"], "message": msg}
            )
            v["action_taken"] = "recalled" if recall_success else "recall_failed"
        else:
            v["action_taken"] = "admin_report_only"
            logger.info(
                f"General violation: report-only for {v['msg_id']} in group {v['group_id']}"
            )

    def _save_violation(self, v):
        with self._db.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO violation_logs "
                "(msg_id, group_id, sender_id, message_content, violation_reason, violation_level) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    v["msg_id"],
                    v["group_id"],
                    v["sender_id"],
                    v["message_content"],
                    v["reason"],
                    v["level"],
                ),
            )
        self._db.conn.commit()

    def _mark_approved(self, messages):
        ids = [m["msg_id"] for m in messages]
        if not ids:
            return
        with self._db.conn.cursor() as cursor:
            placeholders = ",".join(["%s"] * len(ids))
            cursor.execute(
                f"UPDATE message_logs SET is_approved = 1 WHERE msg_id IN ({placeholders})",
                ids,
            )
        self._db.conn.commit()
        logger.info(f"Marked {len(ids)} messages as approved")

    def _send_admin_report(self, violations):
        self._ensure_db()
        admin_qqs = self._db.get_bot_admin_qqs()
        if not admin_qqs:
            logger.warning("No bot administrator is configured in user_groups; skipping content review report")
            return
        admin_user_id = int(admin_qqs[0])
        lines = [
            "=== 内容审核处理报告 ===",
            f"处理时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"共处理 {len(violations)} 条违规",
            "",
        ]
        for i, v in enumerate(violations, 1):
            label = "严重违规" if v["level"] == "severe" else "一般违规"
            action_map = {
                "recalled": "已撤回并群内通知",
                "recall_failed": "撤回失败，已群内通知",
                "admin_report_only": "仅管理员报告",
            }
            action_text = action_map.get(v.get("action_taken"), "未记录")
            lines.append(f"--- 违规 #{i} ({label}) ---")
            lines.append(f"消息ID: {v['msg_id']}")
            lines.append(f"发送者QQ: {v['sender_id']}")
            lines.append(f"群号: {v['group_id']}")
            lines.append(f"消息内容: {v['message_content'][:200]}")
            lines.append(f"处理理由: {v['reason']}")
            lines.append(f"处理动作: {action_text}")
            lines.append("")

        report_text = "\n".join(lines)

        # Send as image (text-to-image)
        try:
            img_path = self._text_to_image(report_text)
            if img_path:
                self._onebot_action(
                    "send_private_msg",
                    {
                        "user_id": admin_user_id,
                        "message": [
                            {
                                "type": "image",
                                "data": {"file": f"file://{img_path}"},
                            }
                        ],
                    },
                )
                # Clean up temp image
                try:
                    os.unlink(img_path)
                except OSError:
                    pass
                logger.info("Admin report sent as image")
                return
        except Exception as e:
            logger.warning(f"Image report failed, falling back to text: {e}")

        # Fallback: send as text (chunked)
        max_len = 1000
        for i in range(0, len(report_text), max_len):
            chunk = report_text[i : i + max_len]
            self._onebot_action(
                "send_private_msg",
                {"user_id": admin_user_id, "message": chunk},
            )
        logger.info("Admin report sent as text (fallback)")

    def _send_ntfy_report(self, violations):
        severe_count = sum(1 for v in violations if v["level"] == "severe")
        general_count = len(violations) - severe_count
        first = violations[0]
        message = (
            "IGNGBot 检测到违规记录\n"
            f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"数量: {len(violations)} 条（严重 {severe_count}，一般 {general_count}）\n"
            f"首条群号: {first['group_id']}\n"
            f"首条发送者: {first['sender_id']}\n"
            f"首条原因: {first['reason']}\n"
            f"首条内容: {first['message_content'][:120]}"
        )
        tags = []
        if severe_count:
            tags.append("QQ严重违规")
        if general_count:
            tags.append("QQ一般违规")
        headers = {
            "Authorization": f"Bearer {self.config.NTFY_REPORT_TOKEN}",
            "Title": "QQ消息违规通知",
            "Tags": ",".join(tags),
        }
        response = requests.post(
            self.config.NTFY_REPORT_URL,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        logger.info("ntfy violation report sent")

    def _text_to_image(self, text):
        """Render multiline text as a PNG image using Pillow with auto-wrapping."""
        font_paths = [
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "C:/Windows/Fonts/msyh.ttc",
            "msyh.ttc",
        ]
        font = None
        for fp in font_paths:
            try:
                font = ImageFont.truetype(fp, 22)
                break
            except (IOError, OSError):
                continue
        if font is None:
            font = ImageFont.load_default()
            logger.warning("No CJK font found, report image may show garbled text")

        max_img_w = 800
        line_h = 32
        padding = 15
        margin = 20

        raw_lines = text.split("\n")
        wrapped_lines = []
        for line in raw_lines:
            if not line:
                wrapped_lines.append("")
                continue
            while line:
                # Binary search for the longest substring that fits within max_img_w
                lo, hi = 1, len(line)
                while lo <= hi:
                    mid = (lo + hi) // 2
                    w = font.getbbox(line[:mid])[2]
                    if w <= max_img_w - margin:
                        lo = mid + 1
                    else:
                        hi = mid - 1
                if hi == 0:
                    hi = 1
                wrapped_lines.append(line[:hi])
                line = line[hi:]

        img_w = max_img_w
        img_h = len(wrapped_lines) * line_h + padding * 2
        img = Image.new("RGB", (img_w, img_h), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        y = padding
        for line in wrapped_lines:
            draw.text((margin, y), line, fill=(0, 0, 0), font=font)
            y += line_h

        tmp_dir = f"{self.config.LOCAL_STORAGE}/tmp"
        os.makedirs(tmp_dir, exist_ok=True)
        path = os.path.join(tmp_dir, f"review_{int(time.time())}.png")
        img.save(path)
        return path
