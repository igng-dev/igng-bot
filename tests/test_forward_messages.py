import json

from igngbot_v3.message_parser import (
    apply_forward_content,
    normalize_message_structure,
    parse_message,
)


def test_apply_forward_content_expands_placeholder():
    parsed = parse_message(
        {
            "message_type": "group",
            "group_id": 1000000006,
            "user_id": 1000000009,
            "message_id": 44245,
            "sender": {"nickname": "sender"},
            "message": [
                {
                    "type": "forward",
                    "data": {"id": "forward-44245"},
                }
            ],
        }
    )

    assert parsed["message_content"] == "[转发消息]"
    assert apply_forward_content(
        parsed,
        [
            {
                "sender": {"nickname": "甲", "user_id": 1},
                "content": [
                    {"type": "text", "data": {"text": "第一条"}},
                ],
            },
            {
                "sender": {"nickname": "乙", "user_id": 2},
                "content": "第二条",
            },
        ],
    )

    assert parsed["message_content"].startswith("[聊天记录]\n")
    assert "甲: 第一条" in parsed["message_content"]
    assert "乙: 第二条" in parsed["message_content"]
    structure = json.loads(json.dumps(parsed["message_structure"], ensure_ascii=False))
    assert structure[0]["type"] == "forward"
    assert len(structure[0]["children"]) == 2


def test_apply_forward_content_does_not_replace_existing_expansion():
    parsed = parse_message(
        {
            "message_type": "group",
            "group_id": 1,
            "user_id": 2,
            "message_id": 3,
            "message": [
                {
                    "type": "forward",
                    "data": {
                        "id": "forward-3",
                        "content": [
                            {"sender": {"nickname": "甲"}, "content": "已有内容"},
                        ],
                    },
                }
            ],
        }
    )

    assert not apply_forward_content(parsed, [{"content": "不应覆盖"}])
    assert "已有内容" in parsed["message_content"]


def test_forward_children_keep_media_files_for_later_storage():
    parsed = parse_message(
        {
            "message_type": "group",
            "group_id": 1000000006,
            "user_id": 2,
            "message_id": 4,
            "message": [
                {
                    "type": "forward",
                    "data": {
                        "id": "forward-4",
                        "content": [
                            {
                                "sender": {"nickname": "甲", "user_id": 1},
                                "content": [
                                    {
                                        "type": "image",
                                        "data": {
                                            "file": "child.jpg",
                                            "url": "https://example.test/child.jpg",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                }
            ],
        }
    )

    child = parsed["message_structure"][0]["children"][0]
    assert child["files"][0]["type"] == "image"
    assert child["files"][0]["url"] == "https://example.test/child.jpg"
    assert child["children"][0]["type"] == "image"


def test_json_card_is_normalized_without_raw_payload_in_message_body():
    parsed = parse_message(
        {
            "message_type": "group",
            "group_id": 1000000006,
            "user_id": 2,
            "message_id": 5,
            "message": [
                {
                    "type": "json",
                    "data": "{'appid': '1000000010', 'preview': 'https://example.test/preview.jpg', 'desc': '发布会摘要', 'title': 'IT之家', 'qqdocurl': 'https://example.test/article'}",
                }
            ],
        }
    )

    card = parsed["message_structure"][0]
    assert card["type"] == "card"
    assert card["title"] == "IT之家"
    assert card["preview_url"] == "https://example.test/preview.jpg"
    assert card["url"] == "https://example.test/article"
    assert "appid" not in parsed["message_content"]
    assert "IT之家" in parsed["plain_text_content"]


def test_legacy_json_structure_is_normalized_from_python_payload():
    structure = [
        {
            "type": "json",
            "summary": (
                "{'appid': '1000000008', 'preview': 'https://example.test/preview.jpg', "
                "'title': '哔哩哔哩', 'icon': 'https://example.test/icon.jpg'}: "
                "{'appid': '1000000008', 'preview': 'https://example.test/preview.jpg'}"
            ),
        }
    ]

    assert normalize_message_structure(structure)
    card = structure[0]
    assert card["type"] == "card"
    assert card["title"] == "哔哩哔哩"
    assert card["preview_url"] == "https://example.test/preview.jpg"
    assert card["icon_url"] == "https://example.test/icon.jpg"
