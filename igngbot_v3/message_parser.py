import ast
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
    obj = _coerce_payload(text)
    if obj is None:
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


def _coerce_payload(raw):
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    try:
        return _json.loads(value)
    except (_json.JSONDecodeError, TypeError):
        pass
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError):
        return None


def _extract_payload_dicts(raw):
    if not isinstance(raw, str):
        return []

    payloads = []
    start = None
    depth = 0
    quote = None
    escaped = False
    for index, char in enumerate(raw):
        if start is None:
            if char == "{":
                start = index
                depth = 1
                quote = None
                escaped = False
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth != 0:
                continue
            candidate = raw[start:index + 1]
            try:
                payload = _json.loads(candidate)
            except (_json.JSONDecodeError, TypeError):
                try:
                    payload = ast.literal_eval(candidate)
                except (ValueError, SyntaxError, TypeError):
                    payload = None
            if isinstance(payload, dict):
                payloads.append(payload)
            start = None
            quote = None
            escaped = False
    return payloads


def _card_text(value, limit=500):
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _card_url(value):
    value = _card_text(value, 1000)
    return value if value.startswith(("http://", "https://")) else ""


def _card_value(payloads, *keys):
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in keys:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _normalize_card(raw, card_type):
    payload = _coerce_payload(raw)
    if not isinstance(payload, dict):
        payloads = _extract_payload_dicts(raw)
        payload = payloads[0] if payloads else None
    if not isinstance(payload, dict):
        return None

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    share_data = payload.get("shareTemplateData") if isinstance(payload.get("shareTemplateData"), dict) else {}
    payloads = (payload, meta, share_data)
    title = _card_text(_card_value(payloads, "title", "prompt", "app", "app_name"), 200)
    description = _card_text(
        _card_value(payloads, "desc", "description", "summary", "detail_1"),
        1000,
    )
    preview_url = _card_url(
        _card_value(payloads, "preview", "preview_url", "cover", "image", "pic", "thumb")
    )
    icon_url = _card_url(_card_value(payloads, "icon", "icon_url", "appicon", "app_icon"))
    target_url = _card_url(
        _card_value(payloads, "qqdocurl", "qdocurl", "url", "jump_url", "link")
    )
    summary = title or description or target_url or _try_parse_json(raw) or ""
    card = {
        "type": "card",
        "card_type": card_type,
        "title": title,
        "description": description,
        "summary": summary[:1000],
        "preview_url": preview_url,
        "icon_url": icon_url,
        "url": target_url,
        "media": [],
    }
    if preview_url:
        card["media"].append({"type": "image", "role": "preview", "url": preview_url})
    if icon_url and icon_url != preview_url:
        card["media"].append({"type": "image", "role": "icon", "url": icon_url})
    return card


def normalize_message_structure(structure):
    """Normalize legacy json/miniapp parts into persistable card parts."""
    if not isinstance(structure, list):
        return False

    changed = False

    def walk(parts):
        nonlocal changed
        for part in parts:
            if not isinstance(part, dict):
                continue
            part_type = part.get("type")
            if part_type in ("json", "miniapp"):
                card_type = part_type
                raw = next(
                    (
                        part.get(key)
                        for key in ("data", "raw", "content", "summary")
                        if part.get(key) not in (None, "")
                    ),
                )
                card = _normalize_card(raw, card_type)
                if card:
                    for key in ("attachments", "children"):
                        if part.get(key):
                            card[key] = part[key]
                    part.clear()
                    part.update(card)
                else:
                    part["type"] = "card"
                    part.setdefault("card_type", card_type)
                changed = True
            elif part_type == "card" and not part.get("media"):
                raw = next(
                    (
                        part.get(key)
                        for key in ("data", "raw", "content", "summary")
                        if part.get(key) not in (None, "")
                    ),
                )
                card = _normalize_card(raw, part.get("card_type", "json"))
                if card:
                    for key in ("attachments", "children"):
                        if part.get(key):
                            card[key] = part[key]
                    part.clear()
                    part.update(card)
                    changed = True
            walk(part.get("children") or [])

    walk(structure)
    return changed


def parse_message(data):
    if not isinstance(data, dict):
        return None
    message_type = data.get("message_type")
    if message_type not in ("group", "private"):
        return None

    is_self = (
        data.get("post_type") == "message_sent"
        or data.get("message_sent_type") == "self"
    )
    self_id = data.get("self_id")
    sender_id = self_id if is_self and self_id not in (None, "") else data.get("user_id")
    sender = data.get("sender", {})

    if message_type == "group":
        conversation_id = data.get("group_id")
        if conversation_id in (None, ""):
            return None
        try:
            storage_group_id = int(conversation_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring message with invalid group_id=%r", conversation_id)
            return None
    else:
        # OneBot private messages do not have a group_id. Use a negative
        # conversation key so private history cannot collide with a group ID.
        # For message_sent events target_id is the recipient; older clients may
        # only expose user_id, which is the best available fallback.
        conversation_id = data.get("target_id")
        if conversation_id in (None, ""):
            conversation_id = data.get("user_id")
        if conversation_id in (None, "") and isinstance(sender, dict):
            conversation_id = sender.get("user_id")
        if conversation_id in (None, ""):
            return None
        try:
            storage_group_id = -int(conversation_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring private message with invalid conversation_id=%r", conversation_id)
            return None

    message_source = "onebot_event" if is_self else "inbound"

    # Detect self-sent messages
    message_array = data.get("message", [])
    if isinstance(message_array, str):
        # Plain text fallback — try to extract CQ code info
        content = message_array
        reply_match = re.search(r"\[CQ:reply,[^\]]*id=([^,\]]+)", content)
        reply_to_msg_id = reply_match.group(1) if reply_match else None
        content = re.sub(r"\[CQ:reply,[^\]]*\]", "", content).strip()
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
            "group_id": storage_group_id,
            "sender_id": sender_id,
            "conversation_type": message_type,
            "conversation_id": conversation_id,
            "sender_name": (sender.get("card") or sender.get("nickname") or str(data.get("user_id"))) if isinstance(sender, dict) else str(data.get("user_id")),
            "sender_role": sender.get("role", "member") if isinstance(sender, dict) else "member",
            "msg_id": str(data.get("message_id", "")),
            "message_content": content,
            "reply_to_msg_id": reply_to_msg_id,
            "files": [],
            "created_at": data.get("time"),
            "is_self": is_self,
            "message_source": message_source,
            "message_structure": [],
        }

    result = {
        "group_id": storage_group_id,
        "sender_id": sender_id,
        "conversation_type": message_type,
        "conversation_id": conversation_id,
        "sender_name": (sender.get("card") or sender.get("nickname") or str(data.get("user_id"))) if isinstance(sender, dict) else str(data.get("user_id")),
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
        "message_source": message_source,
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
            face_id = str(seg_data.get("id", "")).strip()
            face_url = seg_data.get("url", "")
            result["files"].append({
                "type": "face",
                "id": face_id,
                "file": f"face_{face_id}",
                "url": face_url,
            })
            text_parts.append(f"[表情:{face_id}]" if face_id else "[表情]")
            structure.append({"type": "face", "id": face_id})
        elif seg_type == "mface":
            summary = seg_data.get("summary", "")
            emoji_id = str(seg_data.get("emoji_id", "")).strip()
            mface_url = seg_data.get("url", "")
            mface_file = seg_data.get("file", "") or (f"mface_{emoji_id}" if emoji_id else "mface")
            result["files"].append({
                "type": "mface",
                "file": mface_file,
                "url": mface_url,
                "emoji_id": emoji_id,
                "summary": summary,
            })
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
            raw = seg_data.get("data", "") if isinstance(seg_data, dict) else seg_data
            card = _normalize_card(raw, "json")
            if card:
                text_parts.append(f"[卡片] {card['summary']}" if card["summary"] else "[卡片]")
                _append_with_sep(plain_text_parts, card["summary"])
                structure.append(card)
            else:
                text_parts.append("[卡片]")
                structure.append({"type": "card", "card_type": "json", "summary": ""})
        elif seg_type == "miniapp":
            raw = seg_data.get("data", "") if isinstance(seg_data, dict) else seg_data
            card = _normalize_card(raw, "miniapp")
            if card:
                text_parts.append(f"[小程序] {card['summary']}" if card["summary"] else "[小程序]")
                _append_with_sep(plain_text_parts, card["summary"])
                structure.append(card)
            else:
                text_parts.append("[小程序]")
                structure.append({"type": "card", "card_type": "miniapp", "summary": ""})
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


def apply_forward_content(parsed, messages, forward_id=None, force=False):
    if not isinstance(parsed, dict) or not isinstance(messages, list) or not messages:
        return False

    nested_lines, nested_structure = _parse_forward_content(messages, depth=1)
    if not nested_structure:
        return False

    replaced = False
    structure = []
    for part in parsed.get("message_structure", []):
        if (
            isinstance(part, dict)
            and part.get("type") == "forward"
            and (force or not part.get("children"))
            and (forward_id is None or str(part.get("id")) == str(forward_id))
        ):
            expanded = dict(part)
            expanded["children"] = nested_structure
            structure.append(expanded)
            replaced = True
        else:
            structure.append(part)
    if not replaced:
        return False

    rendered = "[聊天记录]\n" + "\n".join(nested_lines)
    content = str(parsed.get("message_content") or "")
    if force and content.lstrip().startswith("[聊天记录]"):
        parsed["message_content"] = rendered
    elif "[转发消息]" in content:
        parsed["message_content"] = content.replace("[转发消息]", rendered, 1)
    elif "[聊天记录]" not in content:
        parsed["message_content"] = f"{content}\n{rendered}".strip()
    parsed["plain_text_content"] = re.sub(r"\s+", " ", parsed["message_content"]).strip()
    parsed["message_structure"] = structure
    parsed["_forward_content"] = messages
    return True


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
            nested_parsed = None
            if isinstance(msg_content, list):
                nested_message = {
                    "message_type": "group",
                    "message": msg_content,
                    "sender": sender,
                    "group_id": 0,
                    "user_id": sender.get("user_id") if isinstance(sender, dict) else None,
                    "message_id": "",
                }
                nested_parsed = parse_message(nested_message) or {}
                msg_content = nested_parsed.get("message_content", "")
                child_structure = nested_parsed.get("message_structure", [])
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
                "user_id": sender.get("user_id") if isinstance(sender, dict) else None,
                "content": msg_content,
                "plain_text_content": (
                    nested_parsed.get("plain_text_content", "")
                    if nested_parsed is not None
                    else msg_content
                ),
                "children": child_structure,
                "files": nested_parsed.get("files", []) if nested_parsed is not None else [],
            })
    return lines, structure
