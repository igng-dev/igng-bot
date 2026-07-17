import json as _json
import logging
import re

logger = logging.getLogger(__name__)

SEGMENT_LABELS = {
    "image": "[图片]",
    "video": "[视频]",
    "audio": "[语音]",
    "record": "[语音]",
}

AUDIO_SEGMENT_TYPES = {"audio", "record"}


def _extract_audio_text(data):
    if not isinstance(data, dict):
        return ""
    for key in (
        "text",
        "transcript",
        "transcription",
        "recognized_text",
        "recognition",
        "summary",
        "content",
        "note",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _append_with_sep(parts, text, sep=" "):
    text = (text or "").strip()
    if not text:
        return
    if parts and not str(parts[-1]).endswith(("\n", " ", ":", "：", "|")):
        parts.append(sep)
    parts.append(text)


def _indent(text, prefix="  "):
    return "\n".join(prefix + line if line else prefix for line in str(text).splitlines())


def _try_parse_json(text):
    """Attempt to parse JSON and extract a human-readable summary."""
    if not text:
        return None
    try:
        obj = _json.loads(text) if isinstance(text, str) else text
    except (_json.JSONDecodeError, TypeError):
        return None
    if isinstance(obj, dict):
        meta = obj.get("meta", obj)
        title = meta.get("title") or meta.get("prompt") or meta.get("detail_1") or obj.get("title") or obj.get("prompt") or obj.get("app") or obj.get("desc")
        desc = meta.get("detail_1") or meta.get("desc") or obj.get("desc") or obj.get("summary") or ""
        url = meta.get("url") or obj.get("url") or ""
        if title:
            return f"{title}: {desc}" if desc else title
        if url:
            return url
        return _json.dumps(obj, ensure_ascii=False)[:200]
    return str(obj)[:200]


def parse_message(data):
    message_type = data.get("message_type")
    if message_type != "group":
        return None

    # Detect self-sent messages
    is_self = (
        data.get("post_type") == "message_sent"
        or data.get("message_sent_type") == "self"
    )

    message_array = data.get("message", [])
    if isinstance(message_array, str):
        # Plain text fallback — try to extract CQ code info
        content = message_array
        # Try to extract title from CQ:json / CQ:share patterns
        for pattern in [r'\[CQ:json,data=(.*?)\]', r'\[CQ:share,[^\]]*title=([^,\]]+)']:
            m = re.search(pattern, content, re.DOTALL)
            if m:
                inner = _try_parse_json(m.group(1))
                if inner:
                    content = content + " " + inner
                    break
        sender = data.get("sender", {})
        return {
            "group_id": data.get("group_id"),
            "sender_id": data.get("user_id"),
            "sender_role": sender.get("role", "member") if isinstance(sender, dict) else "member",
            "msg_id": str(data.get("message_id", "")),
            "message_content": content,
            "reply_to_msg_id": None,
            "files": [],
            "created_at": data.get("time"),
            "is_self": is_self,
        }

    sender = data.get("sender", {})
    result = {
        "group_id": data.get("group_id"),
        "sender_id": data.get("user_id"),
        "sender_role": sender.get("role", "member") if isinstance(sender, dict) else "member",
        "msg_id": str(data.get("message_id", "")),
        "message_content": "",
        "plain_text_content": "",
        "audio_transcript": "",
        "message_structure": [],
        "reply_to_msg_id": None,
        "files": [],
        "created_at": data.get("time"),
        "is_self": is_self,
    }

    text_parts = []
    plain_text_parts = []
    audio_transcripts = []
    structure = []

    for segment in message_array:
        seg_type = segment.get("type")
        seg_data = segment.get("data", {})

        if seg_type == "text":
            text = seg_data.get("text", "")
            text_parts.append(text)
            _append_with_sep(plain_text_parts, text, sep="")
            structure.append({"type": "text", "text": text})
        elif seg_type == "reply":
            result["reply_to_msg_id"] = str(seg_data.get("id", ""))
            structure.append({"type": "reply", "id": result["reply_to_msg_id"]})
        elif seg_type in ("image", "video", "audio", "record"):
            transcript = _extract_audio_text(seg_data) if seg_type in AUDIO_SEGMENT_TYPES else ""
            result["files"].append({
                "type": seg_type,
                "file": seg_data.get("file", ""),
                "url": seg_data.get("url", ""),
                "transcript": transcript,
            })
            if seg_type in AUDIO_SEGMENT_TYPES and transcript:
                audio_transcripts.append(transcript)
                text_parts.append(f"{SEGMENT_LABELS.get(seg_type, f'[{seg_type}]')}（转写: {transcript}）")
                _append_with_sep(plain_text_parts, transcript)
            else:
                text_parts.append(SEGMENT_LABELS.get(seg_type, f"[{seg_type}]"))
            structure.append({
                "type": seg_type,
                "file": seg_data.get("file", ""),
                "url": seg_data.get("url", ""),
                "transcript": transcript,
            })
        elif seg_type == "file":
            name = seg_data.get("name", "未知")
            result["files"].append({
                "type": seg_type,
                "file": seg_data.get("file", ""),
                "url": seg_data.get("url", ""),
                "name": name,
                "size": seg_data.get("size", 0),
            })
            text_parts.append(f"[文件: {name}]")
            structure.append({"type": "file", "name": name})
        elif seg_type == "at":
            qq = seg_data.get("qq", "")
            label = seg_data.get("name", "")
            at_text = f"@{label or qq}"
            text_parts.append(at_text)
            _append_with_sep(plain_text_parts, at_text)
            structure.append({"type": "at", "qq": qq, "name": label})
        elif seg_type == "face":
            face_id = seg_data.get("id", "")
            text_parts.append(f"[表情:{face_id}]" if face_id else "[表情]")
            structure.append({"type": "face", "id": face_id})
        elif seg_type == "mface":
            summary = seg_data.get("summary", "")
            emoji_id = seg_data.get("emoji_id", "")
            if summary:
                text_parts.append(f"[贴纸: {summary}]")
            elif emoji_id:
                text_parts.append(f"[贴纸:{emoji_id}]")
            else:
                text_parts.append("[贴纸]")
            structure.append({"type": "mface", "summary": summary, "emoji_id": emoji_id})
        elif seg_type == "forward":
            forward_id = seg_data.get("id", "")
            if forward_id:
                result["_forward_id"] = forward_id
            # content may already be expanded by the OneBot implementation
            pre_content = seg_data.get("content")
            if pre_content and isinstance(pre_content, list):
                result["_forward_content"] = pre_content
                nested_lines, nested_structure = _parse_forward_content(pre_content, depth=1)
                text_parts.append("[聊天记录]\n" + "\n".join(nested_lines))
                structure.append({"type": "forward", "id": forward_id, "children": nested_structure})
            else:
                text_parts.append("[转发消息]")
                structure.append({"type": "forward", "id": forward_id, "children": []})
        elif seg_type == "node":
            nickname = seg_data.get("nickname", "") or seg_data.get("name", "")
            summary = seg_data.get("summary", "")
            node_content = seg_data.get("content")
            header = f"{nickname}: " if nickname else ""
            if node_content and isinstance(node_content, list):
                nested_lines, nested_structure = _parse_forward_content(node_content, depth=1)
                rendered = "\n".join(nested_lines)
                if nickname:
                    text_parts.append(f"[聊天记录节点] {nickname}\n{rendered}")
                else:
                    text_parts.append(f"[聊天记录节点]\n{rendered}")
                structure.append({"type": "node", "nickname": nickname, "children": nested_structure})
            elif summary:
                text_parts.append(f"{header}{summary}")
                structure.append({"type": "node", "nickname": nickname, "summary": summary})
            elif nickname:
                text_parts.append(f"[转发: {nickname}]")
                structure.append({"type": "node", "nickname": nickname})
            else:
                text_parts.append("[转发消息]")
                structure.append({"type": "node"})
        elif seg_type == "json":
            raw = seg_data.get("data", "")
            if isinstance(raw, dict):
                raw = _json.dumps(raw, ensure_ascii=False)
            parsed = _try_parse_json(raw)
            if parsed:
                text_parts.append(f"[卡片: {parsed}]")
            else:
                text_parts.append(f"[卡片: {str(raw)[:200]}]" if raw else "[卡片]")
            structure.append({"type": "json", "summary": parsed or str(raw)[:200]})
        elif seg_type == "miniapp":
            raw = seg_data.get("data", "")
            parsed = _try_parse_json(raw)
            if parsed:
                text_parts.append(f"[小程序: {parsed}]")
            else:
                text_parts.append(f"[小程序: {str(raw)[:200]}]" if raw else "[小程序]")
            structure.append({"type": "miniapp", "summary": parsed or str(raw)[:200]})
        elif seg_type == "markdown":
            content = seg_data.get("content", "")
            text_parts.append(content if content else "[Markdown]")
            if content:
                _append_with_sep(plain_text_parts, content)
            structure.append({"type": "markdown", "content": content})
        elif seg_type == "xml":
            raw = seg_data.get("data", "")
            text_parts.append(f"[XML: {str(raw)[:200]}]" if raw else "[XML]")
            structure.append({"type": "xml", "content": str(raw)[:200] if raw else ""})
        elif seg_type == "poke":
            text_parts.append("[戳一戳]")
            structure.append({"type": "poke"})
        elif seg_type == "dice":
            text_parts.append("[骰子]")
            structure.append({"type": "dice"})
        elif seg_type == "rps":
            text_parts.append("[猜拳]")
            structure.append({"type": "rps"})
        elif seg_type == "contact":
            text_parts.append("[联系人]")
            structure.append({"type": "contact"})
        elif seg_type == "location":
            text_parts.append("[位置]")
            structure.append({"type": "location"})
        elif seg_type == "onlinefile":
            fname = seg_data.get("fileName", "")
            text_parts.append(f"[在线文件: {fname}]" if fname else "[在线文件]")
            structure.append({"type": "onlinefile", "name": fname})
        elif seg_type == "flashtransfer":
            text_parts.append("[闪传]")
            structure.append({"type": "flashtransfer"})
        elif seg_type == "music":
            text_parts.append("[音乐]")
            structure.append({"type": "music"})
        else:
            logger.debug(f"Unknown segment type: {seg_type}")
            text_parts.append(f"[{seg_type}]" if seg_type else "[未知]")
            structure.append({"type": seg_type or "unknown"})

    result["message_content"] = "".join(text_parts)
    result["plain_text_content"] = re.sub(r"\s+", " ", "".join(plain_text_parts)).strip()
    result["audio_transcript"] = "\n".join(audio_transcripts).strip()
    result["message_structure"] = structure
    return result


def _parse_forward_content(messages, depth=0):
    """Recursively parse nested forwarded messages into readable lines and structure."""
    lines = []
    structure = []
    for msg in messages:
        if isinstance(msg, dict):
            sender = msg.get("sender", {})
            nickname = ""
            if isinstance(sender, dict):
                nickname = sender.get("nickname", "") or sender.get("card", "") or str(sender.get("user_id", ""))
            msg_content = msg.get("content") or msg.get("message", "")
            if isinstance(msg_content, list):
                nested_parsed = {
                    "message_type": "group",
                    "message": msg_content,
                    "sender": sender,
                    "group_id": None,
                    "user_id": sender.get("user_id") if isinstance(sender, dict) else None,
                    "message_id": "",
                }
                parsed = parse_message(nested_parsed)
                msg_content = parsed["message_content"]
                child_structure = parsed.get("message_structure", [])
            elif isinstance(msg_content, str):
                child_structure = [{"type": "text", "text": msg_content}]
            else:
                msg_content = str(msg_content)
                child_structure = [{"type": "text", "text": msg_content}]
            prefix = f"{nickname}: " if nickname else ""
            rendered = f"{prefix}{msg_content}".strip()
            lines.append(_indent(rendered, "  " * depth) if depth else rendered)
            structure.append({
                "nickname": nickname,
                "content": msg_content,
                "children": child_structure,
            })
    return lines, structure
