import asyncio
import logging
import re
import threading

from .onebot_api import send_group_image

logger = logging.getLogger(__name__)


class AvatarCatalog:
    """Read-only avatar catalog."""

    def __init__(self, config, db):
        self.config = config
        self.db = db

    def start(self):
        logger.info("Avatar catalog started (read-only)")

    def handle_command(self, parsed):
        content = re.sub(r"^@[^\s/]+\s*", "", (parsed.get("message_content") or "").strip())
        match = re.fullmatch(r"/头像查询(?:\s+#?(\d+))?", content)
        if not match:
            return False
        avatar_id = int(match.group(1)) if match.group(1) else None
        owner_igng_id = self.db.resolve_bound_igng_account_id(parsed.get("sender_id"))
        if owner_igng_id is None:
            self._send_text(parsed["group_id"], "你的 QQ 尚未绑定 IGNG 账号，请先在 IGNG 网站绑定 QQ。")
            return True
        threading.Thread(
            target=self._query,
            args=(int(parsed["group_id"]), avatar_id, owner_igng_id),
            daemon=True,
        ).start()
        return True

    def _query(self, group_id, avatar_id=None, owner_igng_id=None):
        if avatar_id is not None:
            rows = [self.db.get_avatar_by_id(avatar_id, owner_igng_id)]
            rows = [row for row in rows if row]
        else:
            rows = self.db.get_avatars(owner_igng_id=owner_igng_id, limit=20)
        if not rows:
            self._send_text(group_id, "当前没有可查询的头像。")
            return

        if avatar_id is None:
            lines = ["头像列表："]
            for row in rows:
                task_id = row.get("task_id")
                task_name = row.get("task_name") or "未命名"
                task_text = f"任务 #{task_id}（{task_name}）" if task_id else "无关联任务"
                lines.append(
                    f"头像 #{row['id']} | 分类：{row.get('category') or '未分类'} | "
                    f"模型：{row.get('model') or '未知'} | {task_text}"
                )
            self._send_text(group_id, "\n".join(lines))
            return

        self._send_text(group_id, f"找到头像 #{avatar_id}：")
        for row in rows:
            task_line = (
                f"任务：#{row['task_id']}（{row.get('task_name') or '未命名'}）"
                if row.get("task_id")
                else "任务：无关联任务"
            )
            info = "\n".join((
                f"头像 #{row['id']}，图像 #{row['image_id']}",
                f"分类：{row.get('category') or '未分类'}",
                f"模型：{row.get('model') or '未知'}",
                f"作者：{row.get('author') or row.get('author_igng_id') or '未知'}",
                task_line,
            ))
            path = row.get("file_path")
            if path:
                sent = asyncio.run(send_group_image(self.config, group_id, f"file://{path}"))
                self._send_text(group_id, info if sent else info + "\n图片发送失败。")
            else:
                self._send_text(group_id, info + "\n图片文件不存在。")

    def _send_text(self, group_id, text):
        import requests

        try:
            group_id = int(group_id)
            endpoint = "send_private_msg" if group_id < 0 else "send_group_msg"
            target = {"user_id": -group_id} if group_id < 0 else {"group_id": group_id}
            response = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/{endpoint}",
                json={**target, "message": text},
                headers={"Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}"},
                timeout=15,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Failed to send avatar catalog text: %s", exc)


class StickerCatalog:
    """Read-only sticker category and image catalog."""

    def __init__(self, config, db):
        self.config = config
        self.db = db

    def start(self):
        logger.info("Sticker catalog started (read-only)")

    def trigger(self, *args, **kwargs):
        return None

    def handle_command(self, parsed):
        content = re.sub(r"^@[^\s/]+\s*", "", (parsed.get("message_content") or "").strip())
        list_match = re.fullmatch(r"/表情列表", content)
        view_match = re.fullmatch(r"/表情查看\s+(.+)", content)
        if not list_match and not view_match:
            return False
        owner_igng_id = None
        if view_match:
            owner_igng_id = self.db.resolve_bound_igng_account_id(parsed.get("sender_id"))
            if owner_igng_id is None:
                self._send_text(parsed["group_id"], "你的 QQ 尚未绑定 IGNG 账号，请先在 IGNG 网站绑定 QQ。")
                return True
        threading.Thread(
            target=self._list if list_match else self._view,
            args=(
                int(parsed["group_id"]),
                None if list_match else view_match.group(1).strip(),
                owner_igng_id,
            ),
            daemon=True,
        ).start()
        return True

    def _list(self, group_id, _unused=None, _owner_igng_id=None):
        rows = self.db.get_all_sticker_names()
        if not rows:
            self._send_text(group_id, "当前没有任何表情分类。")
            return
        self._send_text(
            group_id,
            "表情分类列表：\n" + "\n".join(f"{row['id']}：{row['name']}" for row in rows),
        )

    def _view(self, group_id, description, owner_igng_id=None):
        rows = self.db.get_stickers_by_category(description, owner_igng_id)
        if not rows:
            self._send_text(group_id, f"没有找到表情分类「{description}」。")
            return
        self._send_text(group_id, f"表情分类「{description}」共 {len(rows)} 张图像：")
        for row in rows:
            info = (
                f"图像 #{row['id']}\n"
                f"模型：{row.get('model') or '未知'}\n"
                f"作者：{row.get('author') or row.get('author_igng_id') or '未知'}"
            )
            path = row.get("file_path")
            if path:
                sent = asyncio.run(send_group_image(self.config, group_id, f"file://{path}", summary=description))
                self._send_text(group_id, info if sent else info + "\n图片发送失败。")
            else:
                self._send_text(group_id, info + "\n图片文件不存在。")

    def _send_text(self, group_id, text):
        import requests

        try:
            group_id = int(group_id)
            endpoint = "send_private_msg" if group_id < 0 else "send_group_msg"
            target = {"user_id": -group_id} if group_id < 0 else {"group_id": group_id}
            response = requests.post(
                f"{self.config.ONEBOT_HTTP_URL}/{endpoint}",
                json={**target, "message": text},
                headers={"Authorization": f"Bearer {self.config.ONEBOT_HTTP_TOKEN}"},
                timeout=15,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.warning("Failed to send sticker catalog text: %s", exc)
