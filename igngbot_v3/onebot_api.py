import base64
import io
import json
import logging
import os

from PIL import Image

import aiohttp

logger = logging.getLogger(__name__)

OUTBOUND_SOURCE_DEFAULT = "outbound"
OUTBOUND_SOURCE_AI = "ai"
OUTBOUND_SOURCE_COMMAND = "command"
OUTBOUND_SOURCE_AUTO_PLUS_ONE = "auto_plus_one"
OUTBOUND_SOURCE_NOTIFICATION = "notification"
OUTBOUND_SOURCE_MEDIA = "media"
OUTBOUND_SOURCE_ONEBOT_EVENT = "onebot_event"
OUTBOUND_SOURCE_INBOUND = "inbound"


def _message_target(group_id):
    value = int(group_id)
    if value < 0:
        return "/send_private_msg", {"user_id": -value}
    return "/send_group_msg", {"group_id": value}


async def _onebot_response_json(response, operation: str, *, allow_missing_status=False):
    body = await response.text()
    if response.status != 200:
        logger.warning(
            "OneBot %s failed: HTTP %s %s",
            operation,
            response.status,
            body[:500],
        )
        return None
    try:
        result = json.loads(body) if body else {}
    except json.JSONDecodeError:
        logger.warning("OneBot %s returned invalid JSON: %s", operation, body[:500])
        return None
    if allow_missing_status:
        rejected = result.get("retcode") not in (None, 0) or result.get("status") not in (None, "ok")
    else:
        rejected = result.get("status") != "ok" or result.get("retcode") != 0
    if rejected:
        logger.warning("OneBot %s rejected request: %s", operation, body[:1000])
        return None
    return result


async def _onebot_response_succeeded(response, operation: str) -> bool:
    return (await _onebot_response_json(response, operation)) is not None


async def get_forward_msg(config, forward_id):
    normalized_id = str(forward_id or "").strip()
    if not normalized_id:
        return []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.ONEBOT_HTTP_URL}/get_forward_msg",
                json={"message_id": normalized_id},
                headers={"Authorization": f"Bearer {config.ONEBOT_HTTP_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                result = await _onebot_response_json(response, "/get_forward_msg")
        if not result:
            return []
        data = result.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("messages", "message", "content"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []
    except Exception as exc:
        logger.warning("Failed to fetch forwarded message %s: %s", normalized_id, exc)
        return []


def _response_message_id(result):
    if not isinstance(result, dict):
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    message_id = data.get("message_id")
    if message_id in (None, ""):
        return None
    return str(message_id)


def _text_message_structure(text):
    return json.dumps([{"type": "text", "text": str(text)}], ensure_ascii=False)


def _image_message_structure(image_ref, summary, sub_type):
    return json.dumps(
        [
            {
                "type": "image",
                "data": {
                    "file": str(image_ref),
                    "summary": str(summary or ""),
                    "sub_type": sub_type,
                },
            }
        ],
        ensure_ascii=False,
    )


def _record_outbound_message(
    config,
    db,
    *,
    conversation_id,
    message_id,
    message_content,
    message_source=OUTBOUND_SOURCE_DEFAULT,
    plain_text_content=None,
    message_structure=None,
    attachments_json=None,
    file_url=None,
    file_type=None,
):
    """Persist a successfully sent OneBot message without changing send status.

    The HTTP send already succeeded at this point. A database failure is logged
    but deliberately not raised: retrying the message would create a duplicate
    QQ message. The unique (group_id, msg_id) key and insert-side lookup make a
    later ``message_sent`` event idempotent.
    """
    if db is None:
        return False
    normalized_message_id = _response_message_id({"data": {"message_id": message_id}})
    if normalized_message_id is None:
        logger.warning(
            "OneBot send succeeded without message_id; outbound message was not persisted: conversation=%s source=%s",
            conversation_id,
            message_source,
        )
        return False
    try:
        sender_id = int(config.BOT_USER_ID)
        normalized_conversation_id = int(conversation_id)
    except (AttributeError, TypeError, ValueError):
        logger.error(
            "Cannot persist outbound message: invalid bot/conversation identity conversation=%r bot=%r",
            conversation_id,
            getattr(config, "BOT_USER_ID", None),
        )
        return False

    try:
        db.insert_message(
            group_id=normalized_conversation_id,
            sender_id=sender_id,
            message_content=message_content,
            plain_text_content=plain_text_content,
            message_structure=message_structure,
            attachments_json=attachments_json,
            reply_to_msg_id=None,
            msg_id=normalized_message_id,
            file_url=file_url,
            file_type=file_type,
            is_self=True,
            message_source=message_source,
        )
        logger.debug(
            "Persisted outbound message: conversation=%s msg=%s source=%s",
            normalized_conversation_id,
            normalized_message_id,
            message_source,
        )
        return True
    except Exception:
        logger.exception(
            "Failed to persist outbound message: conversation=%s msg=%s source=%s",
            normalized_conversation_id,
            normalized_message_id,
            message_source,
        )
        return False


async def send_group_text(
    config,
    group_id: int | str,
    text: str,
    *,
    db=None,
    message_source=OUTBOUND_SOURCE_DEFAULT,
) -> bool:
    try:
        endpoint, target = _message_target(group_id)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.ONEBOT_HTTP_URL}{endpoint}",
                json={**target, "message": text},
                headers={"Authorization": f"Bearer {config.ONEBOT_HTTP_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                result = await _onebot_response_json(response, endpoint)
                if result is None:
                    return False
                _record_outbound_message(
                    config,
                    db,
                    conversation_id=group_id,
                    message_id=_response_message_id(result),
                    message_content=str(text),
                    plain_text_content=str(text),
                    message_structure=_text_message_structure(text),
                    message_source=message_source,
                )
                return True
    except Exception as exc:
        logger.warning("Failed to send group text: %s", exc)
        return False


async def send_private_text(
    config,
    user_id: int | str,
    text: str,
    *,
    db=None,
    message_source=OUTBOUND_SOURCE_DEFAULT,
) -> bool:
    """Send a text message to one QQ account through the OneBot HTTP API."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.ONEBOT_HTTP_URL}/send_private_msg",
                json={"user_id": int(user_id), "message": text},
                headers={"Authorization": f"Bearer {config.ONEBOT_HTTP_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                result = await _onebot_response_json(response, "/send_private_msg")
                if result is None:
                    return False
                _record_outbound_message(
                    config,
                    db,
                    conversation_id=-int(user_id),
                    message_id=_response_message_id(result),
                    message_content=str(text),
                    plain_text_content=str(text),
                    message_structure=_text_message_structure(text),
                    message_source=message_source,
                )
                return True
    except Exception as exc:
        logger.warning("Failed to send private text to QQ %s: %s", user_id, exc)
        return False


async def send_group_image(
    config,
    group_id: int | str,
    image_ref: str,
    summary: str = "",
    sub_type: int = 1,
    return_result: bool = False,
    *,
    db=None,
    message_source=OUTBOUND_SOURCE_MEDIA,
) -> bool:
    async def send_once(ref):
        endpoint, target = _message_target(group_id)
        payload = {
            **target,
            "message": [
                {
                    "type": "image",
                    "data": {
                        "file": ref,
                        "summary": summary,
                        "sub_type": sub_type,
                    },
                }
            ],
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{config.ONEBOT_HTTP_URL}{endpoint}",
                    json=payload,
                    headers={"Authorization": f"Bearer {config.ONEBOT_HTTP_TOKEN}"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    body = await response.text()
                    if response.status != 200:
                        logger.warning("Failed to send group image: HTTP %s %s", response.status, body[:500])
                        return {"ok": False, "message_id": None}
                    try:
                        result = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        result = {}
                    if result.get("retcode") not in (None, 0) or result.get("status") not in (None, "ok"):
                        logger.warning("OneBot rejected group image: %s", body[:1000])
                        return {"ok": False, "message_id": None}
                    message_id = _response_message_id(result)
                    _record_outbound_message(
                        config,
                        db,
                        conversation_id=group_id,
                        message_id=message_id,
                        message_content="[图片]",
                        plain_text_content=summary or "[图片]",
                        message_structure=_image_message_structure(ref, summary, sub_type),
                        attachments_json=json.dumps(
                            [{"type": "image", "file": str(ref), "summary": str(summary or "")}],
                            ensure_ascii=False,
                        ),
                        file_url=str(image_ref),
                        file_type="image",
                        message_source=message_source,
                    )
                    return {
                        "ok": True,
                        "message_id": message_id,
                    }
        except Exception as exc:
            logger.warning("Failed to send group image: %s", exc)
            return {"ok": False, "message_id": None}

    result = await send_once(image_ref)
    if result["ok"]:
        return result if return_result else True

    if str(image_ref).startswith("file://"):
        path = str(image_ref)[7:]
        if os.path.isfile(path):
            try:
                with Image.open(path) as image:
                    image = image.convert("RGB")
                    image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=88, optimize=True)
                    fallback_ref = "base64://" + base64.b64encode(buffer.getvalue()).decode("ascii")
                result = await send_once(fallback_ref)
                if result["ok"]:
                    return result if return_result else True
            except Exception as exc:
                logger.warning("Failed to prepare fallback image: %s", exc)
    return {"ok": False, "message_id": None} if return_result else False
