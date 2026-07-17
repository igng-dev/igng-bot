import logging
import os
import random
import re
import threading
import time
import base64
import json
from datetime import datetime, timedelta

import requests

from .db import DBHandler
from .api_clients import (
    call_ccode_image,
    cloud_chat_completion,
    extract_resolution,
    resolution_label,
)

logger = logging.getLogger(__name__)

AVATAR_THEME = "二次元可爱美少女"

DEFAULT_PROMPTS = [
    "二次元可爱美少女头像，可爱美少女穿着宽松卫衣，深夜蹲在便利店门口喂一只三花流浪猫，霓虹灯招牌映在水洼里，氛围安静温柔",
    "二次元可爱美少女头像，可爱美少女穿着洛丽塔洋装，在水族馆的玻璃隧道里仰头看蝠鲼游过，蓝光透过水波洒在脸上",
    "二次元可爱美少女头像，可爱美少女身穿牛仔背带裤，坐在天台边缘用粉笔在地上涂鸦，夕阳把她的影子拉得很长",
    "二次元可爱美少女头像，可爱美少女穿着针织开衫，在老旧唱片店里踮脚够高处的黑胶碟，阳光里有浮尘飞舞",
    "二次元可爱美少女头像，可爱美少女穿着汉服元素改良裙，在河边芦苇丛中放风筝，风筝线缠住了，她正笑着解线",
    "二次元可爱美少女头像，可爱美少女穿连帽外套戴着头灯，深夜在天文台透过望远镜看星星，银河在头顶横贯",
    "二次元可爱美少女头像，可爱美少女穿着雨衣踩进路边的水坑，水花溅起映着彩虹光，身后是刚下过雨的小巷",
    "二次元可爱美少女头像，可爱美少女穿着毛衣围着围巾，在植物园温室里俯身闻一朵盛开的兰花，玻璃上有凝结的水珠",
    "二次元可爱美少女头像，可爱美少女穿着运动背心和短裤，掰手腕比赛后累得趴在桌边，额头有汗珠，晨光从窗户照进来",
    "二次元可爱美少女头像，可爱美少女穿着风衣站在屋顶露台，逆光看着城市天际线，黄昏金色光线勾勒出轮廓",
    "二次元可爱美少女头像，可爱美少女裹着羽绒服蹲在雪地里，用树枝在地上画了一个笑脸，口中呼出白气",
    "二次元可爱美少女头像，可爱美少女穿着旗袍改良款，在游戏厅里全神贯注地玩街机，手指飞速按着按钮",
    "二次元可爱美少女头像，可爱美少女穿着睡衣抱着枕头，在温泉旅馆的走廊里追一只偷走她拖鞋的小狗",
    "二次元可爱美少女头像，可爱美少女穿着连衣裙在花店里整理鲜花，一束满天星挡住了半张脸，透过花枝看向镜头",
    "二次元可爱美少女头像，可爱美少女穿着浴衣坐在河边，用团扇拨弄萤火虫，月光在水面碎成一片银光",
    "二次元可爱美少女头像，可爱美少女戴着草帽在向日葵田里给自行车链条上油，手上沾了油污在脸上擦出一道痕",
    "二次元可爱美少女头像，可爱美少女穿着连帽衫站在雷雨中的公交站台下，闪电照亮了她手中正在翻页的小说",
    "二次元可爱美少女头像，可爱美少女在深夜厨房里踮脚够橱柜顶上的饼干盒，冰箱的暖光勾勒出她的轮廓",
    "二次元可爱美少女头像，可爱美少女穿着背带裤在旧书店里踩着小梯子翻顶层书架，午后阳光斜照，空气中有旧纸张的味道",
    "二次元可爱美少女头像，可爱美少女穿着日式校服在教室窗边喂窗台上的文鸟，窗外是午后的蓝天白云，风吹起她的刘海",
    "二次元可爱美少女头像，可爱美少女穿着宽松针织衫坐在咖啡馆靠窗位置画速写，手边一杯冒热气的拿铁，窗外是秋天的银杏",
    "二次元可爱美少女头像，可爱美少女披着毛毯窝在被炉里剥橘子看电视，猫咪趴在脚边打盹，窗外飘着雪花",
    "二次元可爱美少女头像，可爱美少女穿着运动服在清晨的操场拉伸，朝霞把天空染成粉橙色，脸上带着刚睡醒的迷糊",
    "二次元可爱美少女头像，可爱美少女穿着制服在电车上靠着车窗睡着了，手中还握着一本翻到一半的文库本",
    "二次元可爱美少女头像，可爱美少女穿着围裙在开放式厨房里烤焦了饼干，正对着冒烟的烤盘哭笑不得",
    "二次元可爱美少女头像，可爱美少女举着透明雨伞站在傍晚的樱花树下，花瓣落在伞面上，路灯刚亮起暖光",
    "二次元可爱美少女头像，可爱美少女在夏日祭捞金鱼的摊位前蹲着，纸网破了溅了一脸水，烟花在头顶绽放",
    "二次元可爱美少女头像，可爱美少女穿着登山装在秋天的枫叶林中徒步，踩过落叶发出脆响，阳光从红叶间漏下",
    "二次元可爱美少女头像，可爱美少女坐在画室的落地窗前对着一盆向日葵写生，颜料弄脏了脸颊，调色盘上色彩斑斓",
    "二次元可爱美少女头像，可爱美少女穿着雨靴在雨后的操场上踩水坑，低角度阳光把水花照得闪闪发光",
    "二次元可爱美少女头像，可爱美少女裹着大浴巾坐在海边沙滩椅上喝椰子水，夕阳在海平面沉没，天空呈紫橙色渐变",
    "二次元可爱美少女头像，可爱美少女戴着耳罩在溜冰场上小心翼翼地滑冰，双手平伸像企鹅，旁边的小伙伴在偷偷笑",
    "二次元可爱美少女头像，可爱美少女在满是星光的夜晚爬上屋顶，用毯子裹着自己，手边放着保温杯看流星划过",
    "二次元可爱美少女头像，可爱美少女穿着和服参加新年参拜，双手合十闭眼许愿，雪花落在头发和睫毛上",
    "二次元可爱美少女头像，可爱美少女躺在草地上举着一朵蒲公英轻轻吹散，逆光中绒毛像金色的小精灵飞舞",
    "二次元可爱美少女头像，可爱美少女穿着oversize衬衫光脚在木地板上给盆栽浇水，阳光穿过纱帘在墙上投下斑驳光影",
]


class AvatarGenerator:
    def __init__(self, config):
        self.config = config
        self._db = None
        self._thread = None
        self._running = False
        self._sessions = {}  # sender_id -> {state, group_id, prompt, original_prompt, created_at}

    def start(self):
        self._running = True
        self._ensure_db()
        logger.info("Avatar generator started (Daily auto-generation disabled)")

    def _ensure_db(self):
        if self._db is None:
            self._db = DBHandler(self.config)
            self._db.connect()

    # --- Generation ---

    def _generate_daily(self):
        logger.info("Starting daily avatar generation...")

        # Step 1: Refine prompt via cloud LLM
        prompt_final = self._refine_prompt()
        logger.info(f"Final prompt: {prompt_final[:120]}...")

        # Step 2: Call image generation API
        generated_at = datetime.now()
        result = self._call_image_api(prompt_final, self.config.CCODE_DEFAULT_IMAGE_SIZE)
        if result is None:
            self._db.insert_avatar_record(
                prompt_original=AVATAR_THEME,
                prompt_final=prompt_final,
                revised_prompt=None,
                image_url=None,
                file_path=None,
                status="failed",
                generated_at=generated_at,
            )
            logger.error("Image API call failed, record saved as 'failed'")
            return

        # Step 3: Save image
        image_url = result.get("url")
        revised_prompt = result.get("revised_prompt")

        if result.get("url"):
            file_path = self._download_image(result["url"], generated_at)
        elif result.get("b64_json"):
            file_path = self._save_image_from_b64(result["b64_json"], generated_at)
        else:
            file_path = None
        if file_path is None:
            self._db.insert_avatar_record(
                prompt_original=AVATAR_THEME,
                prompt_final=prompt_final,
                revised_prompt=revised_prompt,
                image_url=image_url,
                file_path=None,
                status="failed",
                generated_at=generated_at,
            )
            logger.error("Image save failed, record saved as 'failed'")
            return

        # Step 4: Save record
        avatar_id = self._db.insert_avatar_record(
            prompt_original=AVATAR_THEME,
            prompt_final=prompt_final,
            revised_prompt=revised_prompt,
            image_url=image_url,
            file_path=file_path,
            status="generated",
            generated_at=generated_at,
        )
        logger.info(
            f"Avatar #{avatar_id} generated and stored at {file_path}"
        )

    def _apply_latest(self):
        logger.info("Applying latest avatar...")
        avatar = self._db.get_latest_generated_avatar()
        if avatar is None:
            logger.warning("No unapplied avatar found")
            return

        file_path = avatar.get("file_path")
        if not file_path or not os.path.exists(file_path):
            logger.error(f"Avatar file not found: {file_path}")
            self._db.mark_avatar_failed(avatar["id"])
            return

        success = self._apply_qq_avatar(file_path)
        if success:
            self._db.mark_avatar_applied(avatar["id"], datetime.now())
            logger.info(f"Avatar #{avatar['id']} applied as QQ avatar")
        else:
            logger.error(f"Failed to apply avatar #{avatar['id']}")

    # --- Cloud prompt refinement ---

    def _extract_prompt(self, raw):
        """Safety net: strip any think blocks from model response."""
        clean = re.sub(r'<think>.*?(?:</think>|$)', '', raw, flags=re.DOTALL).strip()
        return clean

    def _normalize_prompt(self, content):
        content = self._extract_prompt(content or "")
        content = re.sub(r"^```(?:\w+)?|```$", "", content.strip()).strip()
        content = re.sub(r"^\s*(?:\d+[.、)]|[-*])\s*", "", content).strip()
        if not content:
            return ""
        if not content.startswith("二次元可爱美少女头像"):
            content = "二次元可爱美少女头像，" + content
        return content

    def _split_prompt_versions(self, content):
        content = self._extract_prompt(content or "")
        versions = []
        buffer = []
        for raw_line in content.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                if buffer:
                    merged = self._normalize_prompt("，".join(buffer))
                    if merged:
                        versions.append(merged)
                    buffer = []
                continue
            if re.match(r"^\s*(?:\d+[.、)]|[-*])\s*", raw_line) and buffer:
                merged = self._normalize_prompt("，".join(buffer))
                if merged:
                    versions.append(merged)
                buffer = []
            line = re.sub(r"^\s*(?:\d+[.、)]|[-*])\s*", "", raw_line).strip()
            if not line:
                continue
            if re.match(r"^\s*(?:提示词|版本)\s*\d*[:：]", line):
                if buffer:
                    merged = self._normalize_prompt("，".join(buffer))
                    if merged:
                        versions.append(merged)
                    buffer = []
                line = re.sub(r"^\s*(?:提示词|版本)\s*\d*[:：]\s*", "", line).strip()
            if line:
                buffer.append(line)
        if buffer:
            merged = self._normalize_prompt("，".join(buffer))
            if merged:
                versions.append(merged)
        deduped = []
        seen = set()
        for prompt in versions:
            if prompt and prompt not in seen:
                deduped.append(prompt)
                seen.add(prompt)
        versions = deduped
        if not versions:
            one_line = self._normalize_prompt(content)
            if one_line:
                versions.append(one_line)
        return [p for p in versions if p]

    def _parse_prompt_response(self, content, count):
        content = self._extract_prompt(content or "").strip()
        if not content:
            return []

        text = content
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0].strip()

        try:
            data = json.loads(text)
            if isinstance(data, list):
                prompts = [self._normalize_prompt(item) for item in data if isinstance(item, str)]
                return [p for p in prompts if p][:count]
            if isinstance(data, dict):
                items = data.get("prompts")
                if isinstance(items, list):
                    prompts = [self._normalize_prompt(item) for item in items if isinstance(item, str)]
                    return [p for p in prompts if p][:count]
        except Exception:
            pass

        return self._split_prompt_versions(content)[:count]

    def _refine_prompt(self):
        try:
            system_msg = (
                "你是一个二次元插画提示词创作者。用户会给你一个主题，你要创作一个具体的生动画面描述。"
                "规则：1) 用中文自然语言写成一个完整句子，不要关键词堆砌；"
                "2) 100字以内，细节丰富；"
                "3) 天马行空发挥创意；"
                "4) 只输出提示词本身，严禁输出分析、解释、多个版本或编号。"
            )
            user_msg = (
                f"请为\"{AVATAR_THEME}\"创作一个具体的画面描述。"
                "自由发挥，场景服饰动作氛围任选，越独特越好。"
            )
            content = cloud_chat_completion(
                self.config,
                [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                temperature=1.3,
                max_tokens=300,
            )
            content = content.strip()
            if content:
                content = self._normalize_prompt(content)
                if content:
                    return content
        except Exception as e:
            logger.warning(f"Cloud prompt refinement failed: {e}")
        prompt = random.choice(DEFAULT_PROMPTS)
        logger.info("Using random default prompt (cloud LLM unavailable)")
        return prompt

    def _optimize_user_prompt(self, user_prompt, count=1):
        """Refine a user-provided prompt via cloud LLM."""
        count = max(1, min(count, 5))
        system_msg = (
            "你是一个二次元插画提示词优化器。用户提供原始提示词，你用自己语言完全重写为更生动的画面描述。"
            "规则：1) 完全重写，严禁照抄原文；"
            "2) 中文自然语言一个完整句子，100字以内；"
            "3) 必须保留用户所有核心元素，再扩展光线、氛围、表情等细节；"
            "4) 不得引入与原提示词冲突的新主体；"
            "5) 优先输出 JSON 数组字符串，例如 [\"提示词1\", \"提示词2\"]；"
            "6) 不要解释，不要输出除 JSON 之外的其他内容。"
        )
        user_msg = (
            f"请将此描述完全重写为 {count} 条不同的二次元可爱美少女头像提示词。"
            f"每条都必须保留原提示词全部核心元素，不得跑题。原提示词：\n{user_prompt}"
        )
        content = cloud_chat_completion(
            self.config,
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.9,
            max_tokens=300,
        )
        logger.info(f"Avatar prompt optimization raw response: {content[:500]}")
        prompts = self._parse_prompt_response(content, count)
        if not prompts:
            retry_system_msg = (
                "你是提示词重写器。请严格只返回 JSON 数组，每个元素是一条中文提示词完整句子。"
                "必须保留原提示词核心元素，不得跑题，不得解释。"
            )
            retry_user_msg = (
                f"把这段提示词重写成 {count} 条不同版本，返回 JSON 数组。"
                f"原提示词：{user_prompt}"
            )
            retry_content = cloud_chat_completion(
                self.config,
                [
                    {"role": "system", "content": retry_system_msg},
                    {"role": "user", "content": retry_user_msg},
                ],
                temperature=0.4,
                max_tokens=300,
            )
            logger.info(f"Avatar prompt optimization retry response: {retry_content[:500]}")
            prompts = self._parse_prompt_response(retry_content, count)
        if not prompts:
            raise RuntimeError("云端 DeepSeek 未返回可用提示词")

        original_normalized = self._normalize_prompt(user_prompt)
        prompts = [p for p in prompts if p != user_prompt and p != original_normalized]
        if not prompts:
            raise RuntimeError("云端 DeepSeek 返回内容与原提示词相同")
        return prompts[0] if count == 1 else prompts

    # --- OpenAI-compatible image generation ---

    def _call_image_api(self, prompt, size):
        """Call the unified OpenAI-compatible image generation API."""
        primary_model = self.config.OPENAI_IMAGE_MODEL
        fallback_model = getattr(self.config, "OPENAI_IMAGE_FALLBACK_MODEL", primary_model)
        try:
            result = call_ccode_image(self.config, prompt, size, model=primary_model)
            if result.get("url") or result.get("b64_json"):
                image_ref = result.get("url") or "[base64 image]"
                logger.info(
                    f"Image generated via {primary_model} ({resolution_label(size)}): {image_ref[:80]}..."
                )
                return result
            logger.error(f"No image data in image API response: {result}")
            return None
        except requests.exceptions.HTTPError as e:
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            if status_code in (429, 500, 502, 503, 504) and fallback_model and fallback_model != primary_model:
                logger.warning(
                    f"Primary image model {primary_model} failed with HTTP {status_code}, retrying with {fallback_model}"
                )
                try:
                    result = call_ccode_image(self.config, prompt, size, model=fallback_model)
                    if result.get("url") or result.get("b64_json"):
                        image_ref = result.get("url") or "[base64 image]"
                        logger.info(
                            f"Image generated via fallback {fallback_model} ({resolution_label(size)}): {image_ref[:80]}..."
                        )
                        return result
                    logger.error(f"No image data in fallback image API response: {result}")
                    return None
                except Exception as fallback_error:
                    logger.error(f"Fallback image model {fallback_model} failed: {fallback_error}")
                    return None
            if status_code == 429:
                logger.error(f"Image API rate limited: {e}")
            else:
                logger.error(f"Image API HTTP error: {e}")
            return None
        except requests.exceptions.Timeout:
            logger.error("Image API request timed out")
            return None
        except Exception as e:
            logger.error(f"Image API call failed: {e}")
            return None

    def _parse_image_result(self, image_result):
        """Normalize provider image result into url or b64_json."""
        if not image_result:
            return None

        if isinstance(image_result, str):
            if image_result.startswith(("http://", "https://")):
                return {"url": image_result}
            return {"b64_json": image_result}

        if isinstance(image_result, dict):
            url = (
                image_result.get("url")
                or image_result.get("image_url")
                or image_result.get("image")
                or image_result.get("src")
            )
            b64_data = (
                image_result.get("b64_json")
                or image_result.get("base64")
                or image_result.get("data")
            )
            if url:
                return {"url": url}
            if b64_data:
                return {"b64_json": b64_data}
            return None

        if isinstance(image_result, list):
            for item in image_result:
                parsed = self._parse_image_result(item)
                if parsed:
                    return parsed

        return None

    # --- Download image from URL ---

    def _download_image(self, url, generated_at):
        try:
            # Organize by year-month
            date_dir = generated_at.strftime("%Y-%m")
            dir_path = os.path.join(self.config.AVATAR_STORAGE_PATH, date_dir)
            os.makedirs(dir_path, exist_ok=True)

            file_name = f"avatar_{generated_at.strftime('%Y%m%d_%H%M%S')}.png"
            file_path = os.path.join(dir_path, file_name)

            logger.info(f"Downloading avatar from: {url[:80]}...")
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
                    logger.warning(f"Avatar download attempt {attempt}/3 failed: {e}")
                    time.sleep(3 * attempt)

            if last_error is not None:
                raise last_error

            file_size = os.path.getsize(file_path)
            logger.info(f"Avatar saved: {file_path} ({file_size / 1024:.0f}KB)")
            return file_path
        except Exception as e:
            logger.error(f"Failed to download image: {e}")
            return None

    def _save_image_from_b64(self, b64_data, generated_at):
        try:
            date_dir = generated_at.strftime("%Y-%m")
            dir_path = os.path.join(self.config.AVATAR_STORAGE_PATH, date_dir)
            os.makedirs(dir_path, exist_ok=True)

            file_name = f"avatar_{generated_at.strftime('%Y%m%d_%H%M%S')}.png"
            file_path = os.path.join(dir_path, file_name)

            if "," in b64_data and b64_data.lstrip().startswith("data:"):
                b64_data = b64_data.split(",", 1)[1]

            with open(file_path, "wb") as f:
                f.write(base64.b64decode(b64_data))

            file_size = os.path.getsize(file_path)
            logger.info(f"Avatar saved from base64: {file_path} ({file_size / 1024:.0f}KB)")
            return file_path
        except Exception as e:
            logger.error(f"Failed to save base64 avatar image: {e}")
            return None

    # --- Command handler ---

    def _cleanup_sessions(self):
        """Remove expired sessions (older than 10 minutes)."""
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
        """Route bot commands from group messages.
        Returns True if the message was handled as a command.
        """
        content = parsed.get("message_content", "").strip()
        normalized = re.sub(r'^@[^\s/]+\s*', '', content).strip()
        sender_id = parsed.get("sender_id", 0)
        group_id = parsed.get("group_id", 0)
        is_admin = self.config.is_admin_user(sender_id)
        sender_role = parsed.get("sender_role", "member")
        can_manage_chat = is_admin or sender_role in ("admin", "owner")

        self._cleanup_sessions()

        # Check /help or /帮助 (always available)
        if re.match(r'^/(help|帮助)\b', normalized):
            self._cancel_session(sender_id)
            self._send_help(group_id, is_admin, can_manage_chat)
            return True

        # Check session response commands: /使用, /重做, /优化, /直接生成
        session = self._sessions.get(sender_id)
        if session and session["group_id"] == group_id:
            clean_normalized, new_size, has_size = extract_resolution(
                normalized, session.get("size", self.config.CCODE_DEFAULT_IMAGE_SIZE)
            )
            if has_size:
                session["size"] = new_size
                session["created_at"] = time.time()
                normalized = clean_normalized
            m = re.match(r'^/(使用|重做|优化|直接生成)\s*(\d*)', normalized)
            if m:
                cmd = m.group(1)
                num = int(m.group(2)) if m.group(2) else None
                return self._handle_session_cmd(sender_id, group_id, cmd, num)

        # Find /头像 command anywhere in message
        m = re.match(r'^/头像\S*', normalized)
        if m:
            self._cancel_session(sender_id)
            cmd = m.group()
            rest = normalized[m.end():].strip()

            if cmd == "/头像生成":
                if not is_admin:
                    self._send_reply(group_id, "只有bot管理员可以使用此命令")
                    return True
                rest, size, _ = extract_resolution(rest, self.config.CCODE_DEFAULT_IMAGE_SIZE)
                user_prompt = rest if rest else None
                threading.Thread(target=self._handle_generate_cmd, args=(group_id, sender_id, user_prompt, size), daemon=True).start()
                return True

            if cmd == "/头像更新":
                if not is_admin:
                    self._send_reply(group_id, "只有bot管理员可以使用此命令")
                    return True
                m2 = re.match(r'(\d+)', rest)
                avatar_id = int(m2.group(1)) if m2 else None
                threading.Thread(target=self._handle_update_cmd, args=(group_id, avatar_id), daemon=True).start()
                return True

            if cmd == "/头像查询":
                m2 = re.match(r'(\d+[dhw])', rest)
                time_str = m2.group(1) if m2 else "1d"
                threading.Thread(target=self._handle_query_cmd, args=(group_id, time_str), daemon=True).start()
                return True

            return False

        return False

    def _handle_session_cmd(self, sender_id, group_id, cmd, num):
        session = self._sessions.get(sender_id)
        if not session:
            return False

        if cmd == "优化" and session["state"] == "awaiting_mode":
            self._send_reply(group_id, f"正在优化提示词，请稍候... 接下来将使用{resolution_label(session['size'])}分辨率。")
            try:
                optimized = self._optimize_user_prompt(session["original_prompt"])
            except Exception as e:
                logger.error(f"Prompt optimization failed: {e}")
                self._send_reply(group_id, f"提示词优化失败：{e}\n请稍后重试，或回复「/直接生成」使用原始提示词。")
                return True
            session["state"] = "awaiting_confirm"
            session["prompts"] = [optimized]
            session["created_at"] = time.time()
            self._send_reply(
                group_id,
                f"优化后提示词：\n{optimized}\n\n接下来将使用{resolution_label(session['size'])}分辨率。"
                f"\n是否使用此提示词生成？\n回复「/使用」或「/重做 [数量]」；也可在回复中加入 1k/2k/4k 修改分辨率。",
            )
            return True

        if cmd == "直接生成" and session["state"] == "awaiting_mode":
            self._sessions.pop(sender_id, None)
            threading.Thread(
                target=self._do_generate, args=(group_id, session["original_prompt"], session["size"]),
                daemon=True,
            ).start()
            return True

        if cmd == "使用" and session["state"] == "awaiting_confirm":
            prompts = session.get("prompts", [])
            idx = max(0, min((num or 1) - 1, len(prompts) - 1))
            if idx >= len(prompts):
                self._send_reply(group_id, f"序号超出范围，共有 {len(prompts)} 个提示词")
                return True
            prompt = prompts[idx]
            self._sessions.pop(sender_id, None)
            self._send_reply(group_id, f"已选择第 {idx + 1} 个提示词，接下来将使用{resolution_label(session['size'])}分辨率。")
            threading.Thread(
                target=self._do_generate, args=(group_id, prompt, session["size"]),
                daemon=True,
            ).start()
            return True

        if cmd == "重做" and session["state"] == "awaiting_confirm":
            count = max(1, min(num or 1, 5))
            self._send_reply(group_id, f"正在生成 {count} 个提示词，请稍候... 接下来将使用{resolution_label(session['size'])}分辨率。")
            try:
                if session.get("original_prompt"):
                    result = self._optimize_user_prompt(session["original_prompt"], count=count)
                    prompts = result if isinstance(result, list) else [result]
                else:
                    prompts = []
                    for _ in range(count):
                        p = self._refine_prompt()
                        prompts.append(p)
            except Exception as e:
                logger.error(f"Re-generate prompts failed: {e}")
                self._send_reply(group_id, f"重新生成提示词失败：{e}")
                return True

            session["prompts"] = prompts
            session["created_at"] = time.time()

            if count == 1:
                msg = (
                    f"新提示词：\n{prompts[0]}\n\n接下来将使用{resolution_label(session['size'])}分辨率。"
                    f"\n是否使用此提示词生成？\n回复「/使用」或「/重做 [数量]」；也可加入 1k/2k/4k 修改分辨率。"
                )
            else:
                lines = [f"共生成 {count} 个提示词：", ""]
                for i, p in enumerate(prompts, 1):
                    lines.append(f"{i}. {p}")
                lines.append("")
                lines.append(f"接下来将使用{resolution_label(session['size'])}分辨率。")
                lines.append("回复「/使用 [序号]」选择提示词，或「/重做 [数量]」重新生成；也可加入 1k/2k/4k 修改分辨率。")
                msg = "\n".join(lines)
            self._send_reply(group_id, msg)
            return True

        return False

    def _do_generate(self, group_id, prompt_final, size):
        """Execute image generation from a confirmed prompt."""
        self._send_reply(group_id, f"正在调用图像生成模型，请稍候（最长5分钟）... 当前分辨率：{resolution_label(size)}")

        generated_at = datetime.now()
        result = self._call_image_api(prompt_final, size)
        if result is None:
            self._ensure_db()
            self._db.insert_avatar_record(
                prompt_original=AVATAR_THEME,
                prompt_final=prompt_final,
                revised_prompt=None,
                image_url=None,
                file_path=None,
                status="failed",
                generated_at=generated_at,
            )
            self._send_reply(group_id, "图像生成失败，已记录。若刚刚频繁生成过头像，可能是图像接口限流，请稍后再试。")
            return

        image_url = result.get("url")
        b64_data = result.get("b64_json")
        revised_prompt = result.get("revised_prompt")

        # Try URL first, fallback to base64
        if image_url:
            file_path = self._download_image(image_url, generated_at)
        elif b64_data:
            file_path = self._save_image_from_b64(b64_data, generated_at)
        else:
            file_path = None

        if file_path is None:
            self._ensure_db()
            self._db.insert_avatar_record(
                prompt_original=AVATAR_THEME,
                prompt_final=prompt_final,
                revised_prompt=revised_prompt,
                image_url=image_url,
                file_path=None,
                status="failed",
                generated_at=generated_at,
            )
            self._send_reply(group_id, "图像保存失败，已记录")
            return

        self._ensure_db()
        avatar_id = self._db.insert_avatar_record(
            prompt_original=AVATAR_THEME,
            prompt_final=prompt_final,
            revised_prompt=revised_prompt,
            image_url=image_url,
            file_path=file_path,
            status="generated",
            generated_at=generated_at,
        )

        info = f"头像生成完成 ID: {avatar_id}\n分辨率: {resolution_label(size)}\n可使用 /头像更新 立即应用"
        if os.path.exists(file_path):
            self._send_image_with_text(group_id, file_path, info)
        else:
            self._send_reply(group_id, info)

    def _send_help(self, group_id, is_admin, can_manage_chat=False):
        lines = ["=== 可用指令 ===", ""]
        if is_admin:
            lines.append("【管理员指令 — 头像】")
            lines.append("/头像生成 [提示词] [1k/2k/4k] — 生成头像，默认1k")
            lines.append("/头像更新 [id] — 应用最新头像，或指定ID")
            lines.append("")
            lines.append("【管理员指令 — 表情】")
            lines.append("/表情生成 <名称> [头像id] [1k/2k/4k] — 生成表情，默认1k")
            lines.append("/表情删除 <id> — 删除指定表情记录")
            lines.append("")
            lines.append("【管理员指令 — IGNG管理】")
            lines.append("/通知 <内容> [群号] — 向指定群发送通知，不写群号默认审核群")
            lines.append("/总结 <时间> [群号] — 总结本群聊天（如1d/12h/30m）")
            lines.append("")
        if can_manage_chat:
            lines.append("【管理指令 — 聊天设置】")
            lines.append("/聊天模式 — 切换高频/低频聊天模式")
            lines.append("")
        lines.append("【所有人可用 — 头像】")
        lines.append("/头像查询 [范围] — 查询近期头像，如 /头像查询 3d")
        lines.append("")
        lines.append("【所有人可用 — 表情】")
        lines.append("/表情列表 — 查看所有表情")
        lines.append("/表情查看 <名称> — 查看某个表情的全部图片")
        lines.append("")
        lines.append("【所有人可用 — IGNG查询】")
        lines.append("/服务器列表 — 列出全部服务器")
        lines.append("/服务器状态 [服务器] — 查看服务器延迟和性能")
        lines.append("/假人列表 [服务器] — 查看服务器假人")
        lines.append("/我的假人 [服务器] — 查看自己的假人")
        lines.append("/BBS队列 — 查看审核队列")
        lines.append("/领地列表 <玩家名> — 查看玩家公开领地")
        lines.append("")
        lines.append("【所有人可用 — 个人设定】")
        lines.append("")
        lines.append("【会话指令（生成流程中响应）】")
        lines.append("/确认 — 确认使用当前描述生成")
        lines.append("/重做 — 重新生成描述")
        lines.append("/取消 — 取消当前操作")
        lines.append("/优化 — 优化用户提供的头像提示词")
        lines.append("/直接生成 — 直接使用原始提示词生成头像")
        lines.append("")
        lines.append("/help 或 /帮助 — 显示此帮助")
        self._send_reply(group_id, "\n".join(lines))

    def _handle_generate_cmd(self, group_id, sender_id, user_prompt=None, size=None):
        size = size or self.config.CCODE_DEFAULT_IMAGE_SIZE
        if user_prompt:
            # Custom prompt: ask whether to optimize or generate directly
            self._sessions[sender_id] = {
                "state": "awaiting_mode",
                "group_id": group_id,
                "original_prompt": user_prompt,
                "prompts": None,
                "size": size,
                "created_at": time.time(),
            }
            self._send_reply(
                group_id,
                f"收到提示词：{user_prompt}\n\n接下来将使用{resolution_label(size)}分辨率。"
                f"\n请选择：\n「/优化」— 让AI润色优化提示词\n「/直接生成」— 使用原始提示词生成"
                f"\n后续回复也可加入 1k/2k/4k 修改分辨率。",
            )
        else:
            # Auto generate: generate prompt then ask for confirmation
            self._send_reply(group_id, f"正在生成提示词，请稍候... 接下来将使用{resolution_label(size)}分辨率。")
            try:
                prompt_final = self._refine_prompt()
            except Exception as e:
                logger.error(f"Prompt generation failed: {e}", exc_info=True)
                self._send_reply(group_id, f"提示词生成失败：{e}")
                return

            self._sessions[sender_id] = {
                "state": "awaiting_confirm",
                "group_id": group_id,
                "original_prompt": None,
                "prompts": [prompt_final],
                "size": size,
                "created_at": time.time(),
            }
            self._send_reply(
                group_id,
                f"生成的提示词：\n{prompt_final}\n\n接下来将使用{resolution_label(size)}分辨率。"
                f"\n是否使用此提示词生成？\n回复「/使用」或「/重做 [数量]」；也可加入 1k/2k/4k 修改分辨率。",
            )

    def _handle_update_cmd(self, group_id, avatar_id):
        if avatar_id is not None:
            self._send_reply(group_id, f"正在将头像更新为 #{avatar_id}...")
            try:
                success = self._apply_by_id(avatar_id)
                if success:
                    self._send_reply(group_id, f"头像已更新为 #{avatar_id}")
                else:
                    self._send_reply(group_id, f"头像 #{avatar_id} 更新失败，请检查ID是否正确或文件是否存在")
            except Exception as e:
                logger.error(f"Command update #{avatar_id} failed: {e}", exc_info=True)
                self._send_reply(group_id, f"头像更新失败：{e}")
        else:
            self._send_reply(group_id, "正在应用最新头像...")
            try:
                self._apply_latest()
                self._send_reply(group_id, "头像已更新为最新生成的头像")
            except Exception as e:
                logger.error(f"Command update latest failed: {e}", exc_info=True)
                self._send_reply(group_id, f"头像更新失败：{e}")

    def _handle_query_cmd(self, group_id, time_str):
        since = self._parse_time_range(time_str)
        self._ensure_db()
        avatars = self._db.get_avatars_since(since)

        if not avatars:
            self._send_reply(group_id, f"最近 {time_str} 内没有生成的头像记录")
            return

        self._send_reply(group_id, f"最近 {time_str} 内共生成 {len(avatars)} 张头像：")
        for av in avatars:
            file_path = av.get("file_path", "")
            prompt_final = av.get("prompt_final", "") or ""
            revised = av.get("revised_prompt", "") or ""
            generated_at = av.get("generated_at")
            time_label = generated_at.strftime("%m-%d %H:%M") if generated_at else "未知"
            status_label = {"generated": "待应用", "applied": "已应用", "failed": "失败"}.get(
                av.get("status", ""), av.get("status", "")
            )

            info = (
                f"ID: {av['id']} | {time_label} | {status_label}\n"
                f"提示词: {prompt_final[:120]}{'...' if len(prompt_final) > 120 else ''}"
            )
            if revised and revised != prompt_final:
                info += f"\n改写: {revised[:120]}{'...' if len(revised) > 120 else ''}"

            if file_path and os.path.exists(file_path):
                # Send image + info together
                self._send_image_with_text(group_id, file_path, info)
            else:
                self._send_reply(group_id, info)

    def _apply_by_id(self, avatar_id):
        self._ensure_db()
        avatar = self._db.get_avatar_by_id(avatar_id)
        if avatar is None:
            logger.error(f"Avatar #{avatar_id} not found")
            return False

        file_path = avatar.get("file_path", "")
        if not file_path or not os.path.exists(file_path):
            logger.error(f"Avatar #{avatar_id} file not found: {file_path}")
            self._db.mark_avatar_failed(avatar_id)
            return False

        success = self._apply_qq_avatar(file_path)
        if success:
            self._db.mark_avatar_applied(avatar_id, datetime.now())
            logger.info(f"Avatar #{avatar_id} applied as QQ avatar")
        return success

    def _parse_time_range(self, text):
        m = re.match(r"(\d+)\s*([dhw])", text.strip())
        if not m:
            return datetime.now() - timedelta(days=1)
        num = int(m.group(1))
        unit = m.group(2)
        if unit == "h":
            return datetime.now() - timedelta(hours=num)
        elif unit == "d":
            return datetime.now() - timedelta(days=num)
        elif unit == "w":
            return datetime.now() - timedelta(weeks=num)
        return datetime.now() - timedelta(days=1)

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
            # Fallback: text only
            self._send_reply(group_id, text)

    # --- Apply QQ avatar via OneBot ---

    def _apply_qq_avatar(self, file_path):
        try:
            headers = {
                "Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}",
                "Content-Type": "application/json",
            }
            payload = {
                "file": f"file://{file_path}",
            }
            r = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/set_qq_avatar",
                json=payload,
                headers=headers,
                timeout=30,
            )
            r.raise_for_status()
            result = r.json()
            if result.get("status") == "ok":
                logger.info("QQ avatar updated successfully")
                return True
            else:
                logger.warning(f"set_qq_avatar returned non-ok: {result}")
                return False
        except Exception as e:
            logger.error(f"Failed to set QQ avatar: {e}")
            return False
