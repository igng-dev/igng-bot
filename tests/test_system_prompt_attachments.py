from igngbot_v3.system_prompt_commands import SystemPromptCommandHandler
from igngbot_v3.chat_service import ChatService
from pathlib import Path

db = Path("igngbot_v3/db.py").read_text(encoding="utf-8")
assert "chat_system_prompt_attachments" in db
assert "list_chat_system_prompt_attachments" in db
assert "add_chat_system_prompt_attachment" in db
assert "disable_chat_system_prompt_attachment" in db
print("db methods ok")

h = SystemPromptCommandHandler.__new__(SystemPromptCommandHandler)
assert h._parse_review_response('{"approved": true, "reason": "ok"}')["approved"] is True
assert h._parse_review_response('{"approved": false, "reason": "no"}')["approved"] is False
assert h._parse_review_response("not json")["approved"] is False
assert h._parse_review_response('```json\n{"approved": true, "reason": "x"}\n```')["approved"] is True
print("parser ok")

class FakeDB:
    def get_enabled_chat_system_prompt_attachments(self):
        return [
            {"id": 1, "owner_qq": 123, "prompt_text": "偶尔说一句神了", "status": "enabled"},
            {"id": 2, "owner_qq": 456, "prompt_text": "少用 emoji", "status": "enabled"},
        ]

svc = ChatService.__new__(ChatService)
svc.db = FakeDB()
section = svc._build_user_attachment_section()
assert "## 用户附加系统提示词" in section
assert "ID:1" in section and "ID:2" in section
assert "偶尔说一句神了" in section
print(section)
print("all unit checks passed")
