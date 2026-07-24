"""Consumes Account-created image repository jobs on the bot/NAS host."""
import asyncio
import base64
import json
import logging
import os
import shutil
import threading
import time

import aiohttp
import pymysql
from pymysql.cursors import DictCursor

logger = logging.getLogger(__name__)


class MediaRepositoryWorker:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.conn = None
        self._thread = None
        self._stopping = threading.Event()

    def start(self):
        self.conn = pymysql.connect(
            host=self.config.DB_HOST, user=self.config.DB_USER,
            password=self.config.DB_PASSWORD, database=self.config.DB_NAME,
            charset="utf8mb4", cursorclass=DictCursor, autocommit=False,
        )
        self._thread = threading.Thread(target=self._run, name="media-repository", daemon=True)
        self._thread.start()
        logger.info("Image repository worker started")

    def stop(self):
        self._stopping.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self.conn:
            self.conn.close()
            self.conn = None

    def _run(self):
        while not self._stopping.is_set():
            try:
                if not self._consume_one():
                    self._stopping.wait(self.config.IMAGE_REPOSITORY_POLL_INTERVAL)
            except Exception:
                logger.exception("Image repository worker cycle failed")
                self._stopping.wait(self.config.IMAGE_REPOSITORY_POLL_INTERVAL)

    def _consume_one(self):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT p.id, p.image_id, p.task_type, i.file_path FROM image_processing_tasks p "
                "INNER JOIN images i ON i.id = p.image_id WHERE p.status = 'pending' "
                "ORDER BY p.created_at, p.id LIMIT 1"
            )
            job = cursor.fetchone()
            if not job:
                return False
            cursor.execute(
                "UPDATE image_processing_tasks SET status = 'processing', started_at = NOW() "
                "WHERE id = %s AND status = 'pending'", (job["id"],)
            )
            if cursor.rowcount != 1:
                self.conn.commit()
                return True
        self.conn.commit()
        try:
            category = asyncio.run(self._classify(job["file_path"], job["task_type"]))
            destination = self._copy_image(job["file_path"], job["task_type"])
            self._save_catalog(job["image_id"], job["task_type"], category)
            with self.conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE image_processing_tasks SET status = 'completed', completed_at = NOW(), error_message = NULL "
                    "WHERE id = %s", (job["id"],)
                )
            self.conn.commit()
            logger.info("Repository job %s complete: %s -> %s (%s)", job["id"], job["task_type"], destination, category)
        except Exception as exc:
            logger.exception("Repository job %s failed", job["id"])
            with self.conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE image_processing_tasks SET status = 'failed', error_message = %s WHERE id = %s",
                    (str(exc)[:2000], job["id"]),
                )
            self.conn.commit()
        return True

    def _copy_image(self, source, task_type):
        source = os.path.realpath(source)
        allowed_root = os.path.realpath(self.config.NAS_MOUNT_BASE)
        if not source.startswith(allowed_root + os.sep) or not os.path.isfile(source):
            raise ValueError("图片文件不在可访问的 NAS 目录内")
        target_dir = self.config.AVATAR_STORAGE_PATH if task_type == "avatar" else self.config.STICKER_STORAGE_PATH
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, os.path.basename(source))
        if not os.path.exists(target):
            shutil.copy2(source, target)
        return target

    def _categories(self, task_type):
        table = "avatars" if task_type == "avatar" else "stickers"
        with self.conn.cursor() as cursor:
            cursor.execute(f"SELECT DISTINCT category FROM {table} WHERE category IS NOT NULL AND category <> '' ORDER BY category")
            return [row["category"] for row in cursor.fetchall()]

    async def _classify(self, file_path, task_type):
        with open(file_path, "rb") as image:
            encoded = base64.b64encode(image.read()).decode("ascii")
        extension = os.path.splitext(file_path)[1].lower().lstrip(".") or "png"
        mime = "image/webp" if extension == "webp" else f"image/{extension}"
        categories = self._categories(task_type)
        kind = "头像" if task_type == "avatar" else "表情"
        system = "你是图片仓库分类器。只输出 JSON，不要代码块。"
        instruction = (
            f"请分析这张{kind}图片。已有类别：{json.dumps(categories, ensure_ascii=False)}。"
            "选择最合适的已有类别，或创建一个简短的新中文类别。"
            '返回 {"category":"类别名"}。'
        )
        payload = {
            "model": self.config.IMAGE_ANALYSIS_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ]}],
            "temperature": 0.1,
            "max_tokens": 120,
        }
        url = self.config.OPENAI_BASE_URL.rstrip("/")
        url = f"{url}/chat/completions" if url.endswith("/v1") else f"{url}/v1/chat/completions"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers={"Authorization": f"Bearer {self.config.OPENAI_API_KEY}"}, timeout=aiohttp.ClientTimeout(total=self.config.CLOUD_LLM_TIMEOUT)) as response:
                raw = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"分类模型返回 HTTP {response.status}: {raw[:300]}")
        content = ((json.loads(raw).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        category = str(json.loads(content).get("category") or "未分类").strip()[:255]
        return category or "未分类"

    def _save_catalog(self, image_id, task_type, category):
        with self.conn.cursor() as cursor:
            if task_type == "avatar":
                cursor.execute("INSERT INTO avatars (image_id, category) VALUES (%s, %s) ON DUPLICATE KEY UPDATE category = VALUES(category)", (image_id, category))
            else:
                cursor.execute("SELECT id FROM stickers WHERE image_id = %s LIMIT 1", (image_id,))
                existing = cursor.fetchone()
                if existing:
                    cursor.execute("UPDATE stickers SET category = %s WHERE id = %s", (category, existing["id"]))
                else:
                    cursor.execute("INSERT INTO stickers (image_id, category) VALUES (%s, %s)", (image_id, category))
        self.conn.commit()
