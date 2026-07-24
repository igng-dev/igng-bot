import json
import logging
import re
import asyncio
from pathlib import Path

from .api_clients import cloud_chat_completion
from .onebot_api import send_group_text

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_CHARS = 1000

STATUS_LABELS = {
    "enabled": "启用",
    "disabled": "关闭",
}


class SystemPromptCommandHandler:
    """Handle /系统提示词 commands for chat-mode user attachment prompts."""

    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.review_prompt = self._read_review_prompt()

    def _read_review_prompt(self) -> str:
        path = Path(self.config.PROMPT_DIR) / "system_prompt_attachment_review.txt"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return (
            "你是系统提示词附加审核助手。检查用户附加内容是否合法且有意义。"
            '只输出 JSON：{"approved": true或false, "reason": "原因"}'
        )

    async def handle(self, parsed: dict, normalized_content: str) -> bool:
        match = re.fullmatch(r"/系统提示词(?:\s+(.+))?", normalized_content)
        if not match:
            return False

        args = (match.group(1) or "").strip()
        group_id = parsed["group_id"]
        sender_id = parsed["sender_id"]

        if not args:
            await send_group_text(self.config, group_id, self._usage_text())
            return True

        if args == "列表" or args.startswith("列表 "):
            await self._handle_list(group_id)
            return True

        close_match = re.fullmatch(r"关闭\s+#?(\d+)", args)
        if close_match:
            await self._handle_close(group_id, sender_id, int(close_match.group(1)))
            return True

        modify_match = re.fullmatch(r"修改\s+#?(\d+)\s+([\s\S]+)", args)
        if modify_match:
            await self._handle_modify(
                group_id,
                sender_id,
                int(modify_match.group(1)),
                modify_match.group(2).strip(),
            )
            return True

        attach_match = re.fullmatch(r"附加\s+([\s\S]+)", args)
        if attach_match:
            await self._handle_attach(group_id, sender_id, attach_match.group(1).strip())
            return True

        await send_group_text(self.config, group_id, self._usage_text())
        return True

    def _usage_text(self) -> str:
        return (
            "系统提示词附加（仅影响聊天模式）\n"
            "/系统提示词 附加 <内容>\n提交附加提示词，先经审核通过后才会生效。\n"
            "/系统提示词 列表\n查看当前生效的全部附加提示词。\n"
            "/系统提示词 修改 #id <内容>\n修改自己的附加提示词（管理员可改所有人），需重新审核。\n"
            "/系统提示词 关闭 #id\n关闭自己的附加提示词（管理员可关所有人）。"
        )

    def _can_manage(self, sender_id, owner_qq) -> bool:
        if self.db.is_bot_admin(sender_id):
            return True
        try:
            return int(sender_id) == int(owner_qq)
        except (TypeError, ValueError):
            return str(sender_id) == str(owner_qq)

    async def _handle_list(self, group_id: int):
        rows = self.db.get_enabled_chat_system_prompt_attachments()
        if not rows:
            await send_group_text(self.config, group_id, "当前没有生效的附加系统提示词。")
            return
        lines = ["当前生效的附加系统提示词："]
        for row in rows:
            text = (row.get("prompt_text") or "").strip()
            preview = text if len(text) <= 200 else text[:200] + "…"
            lines.append(
                f"ID:#{row.get('id')} | 用户QQ:{row.get('owner_qq')} | "
                f"{STATUS_LABELS.get(row.get('status'), row.get('status'))}\n{preview}"
            )
        await send_group_text(self.config, group_id, "\n\n".join(lines))

    async def _handle_close(self, group_id: int, sender_id: int, attachment_id: int):
        row = self.db.get_chat_system_prompt_attachment(attachment_id)
        if not row:
            await send_group_text(self.config, group_id, f"未找到提示词 ID:#{attachment_id}。")
            return
        if not self._can_manage(sender_id, row.get("owner_qq")):
            await send_group_text(
                self.config,
                group_id,
                "只能关闭自己提交的附加系统提示词；bot 管理员可关闭任意条目。",
            )
            return
        if row.get("status") == "disabled":
            await send_group_text(
                self.config,
                group_id,
                f"附加系统提示词 ID:#{attachment_id} 已经是关闭状态。",
            )
            return
        self.db.disable_chat_system_prompt_attachment(attachment_id)
        await send_group_text(
            self.config,
            group_id,
            f"已关闭附加系统提示词 ID:#{attachment_id}（提交者 QQ:{row.get('owner_qq')}）。",
        )

    async def _handle_attach(self, group_id: int, sender_id: int, prompt_text: str):
        prompt_text = (prompt_text or "").strip()
        if not prompt_text:
            await send_group_text(self.config, group_id, "附加内容不能为空。用法：/系统提示词 附加 <内容>")
            return
        if len(prompt_text) > MAX_ATTACHMENT_CHARS:
            await send_group_text(
                self.config,
                group_id,
                f"附加内容过长，最多 {MAX_ATTACHMENT_CHARS} 字，当前 {len(prompt_text)} 字。",
            )
            return

        await send_group_text(self.config, group_id, "正在审核附加系统提示词，请稍候…")
        try:
            decision = await self._review_attachment(prompt_text)
        except Exception:
            logger.exception("system prompt attachment review failed")
            await send_group_text(self.config, group_id, "审核服务暂时不可用，请稍后再试。")
            return

        if not decision.get("approved"):
            reason = (decision.get("reason") or "未通过审核").strip()
            await send_group_text(self.config, group_id, f"附加系统提示词未通过审核：{reason}")
            return

        attachment_id = self.db.add_chat_system_prompt_attachment(prompt_text, sender_id)
        await send_group_text(
            self.config,
            group_id,
            f"附加系统提示词已生效，ID:#{attachment_id}。可用 /系统提示词 列表 查看。",
        )

    async def _handle_modify(
        self,
        group_id: int,
        sender_id: int,
        attachment_id: int,
        prompt_text: str,
    ):
        prompt_text = (prompt_text or "").strip()
        if not prompt_text:
            await send_group_text(
                self.config,
                group_id,
                "修改内容不能为空。用法：/系统提示词 修改 #id <内容>",
            )
            return
        if len(prompt_text) > MAX_ATTACHMENT_CHARS:
            await send_group_text(
                self.config,
                group_id,
                f"修改内容过长，最多 {MAX_ATTACHMENT_CHARS} 字，当前 {len(prompt_text)} 字。",
            )
            return

        row = self.db.get_chat_system_prompt_attachment(attachment_id)
        if not row:
            await send_group_text(self.config, group_id, f"未找到提示词 ID:#{attachment_id}。")
            return
        if not self._can_manage(sender_id, row.get("owner_qq")):
            await send_group_text(
                self.config,
                group_id,
                "只能修改自己提交的附加系统提示词；bot 管理员可修改任意条目。",
            )
            return
        if row.get("status") == "disabled":
            await send_group_text(
                self.config,
                group_id,
                f"附加系统提示词 ID:#{attachment_id} 已关闭，无法修改。请重新附加。",
            )
            return

        old_text = (row.get("prompt_text") or "").strip()
        if prompt_text == old_text:
            await send_group_text(self.config, group_id, "内容和当前版本相同，无需修改。")
            return

        await send_group_text(self.config, group_id, f"正在审核修改后的系统提示词 ID:#{attachment_id}，请稍候…")
        try:
            decision = await self._review_attachment(prompt_text)
        except Exception:
            logger.exception("system prompt attachment modify review failed")
            await send_group_text(
                self.config,
                group_id,
                f"审核服务暂时不可用，已保留原版本。ID:#{attachment_id}",
            )
            return

        if not decision.get("approved"):
            reason = (decision.get("reason") or "未通过审核").strip()
            await send_group_text(
                self.config,
                group_id,
                f"修改未通过审核，已保留原版本。原因：{reason}",
            )
            return

        updated = self.db.update_chat_system_prompt_attachment(attachment_id, prompt_text)
        if not updated:
            await send_group_text(self.config, group_id, f"修改失败：未找到提示词 ID:#{attachment_id}。")
            return
        await send_group_text(
            self.config,
            group_id,
            f"附加系统提示词 ID:#{attachment_id} 已更新并生效。",
        )

    async def _review_attachment(self, prompt_text: str) -> dict:
        user_prompt = (
            "请审核以下用户提交的附加系统提示词：\n"
            f"---\n{prompt_text}\n---"
        )
        content = await asyncio.to_thread(
            cloud_chat_completion,
            self.config,
            [
                {"role": "system", "content": self.review_prompt},
                {"role": "user", "content": user_prompt},
            ],
            0.1,
            400,
        )
        return self._parse_review_response(content)

    def _parse_review_response(self, response: str) -> dict:
        if not response:
            return {"approved": False, "reason": "审核无返回结果"}

        text = response.strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0]
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0]
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            logger.warning("attachment review unparsable response: %s", response[:300])
            return {"approved": False, "reason": "审核结果无法解析，已拒绝"}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            logger.warning("attachment review JSON error: %s", response[:300])
            return {"approved": False, "reason": "审核结果无法解析，已拒绝"}

        approved = data.get("approved")
        if isinstance(approved, str):
            approved = approved.strip().lower() in ("true", "1", "yes", "通过", "approved")
        else:
            approved = bool(approved)
        reason = str(data.get("reason") or ("通过审核" if approved else "未通过审核")).strip()
        return {"approved": approved, "reason": reason}
