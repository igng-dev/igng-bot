import base64
import io
import json
import logging
import os

from PIL import Image

import aiohttp

logger = logging.getLogger(__name__)


def _message_target(group_id):
    value = int(group_id)
    if value < 0:
        return "/send_private_msg", {"user_id": -value}
    return "/send_group_msg", {"group_id": value}


async def send_group_text(config, group_id: int | str, text: str) -> bool:
    try:
        endpoint, target = _message_target(group_id)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.ONEBOT_HTTP_URL}{endpoint}",
                json={**target, "message": text},
                headers={"Authorization": f"Bearer {config.ONEBOT_HTTP_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                return response.status == 200
    except Exception as exc:
        logger.warning("Failed to send group text: %s", exc)
        return False


async def send_group_image(
    config,
    group_id: int | str,
    image_ref: str,
    summary: str = "",
    sub_type: int = 1,
    return_result: bool = False,
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
                        return False
                    try:
                        result = json.loads(body) if body else {}
                    except json.JSONDecodeError:
                        result = {}
                    if result.get("retcode") not in (None, 0) or result.get("status") not in (None, "ok"):
                        logger.warning("OneBot rejected group image: %s", body[:1000])
                        return {"ok": False, "message_id": None}
                    return {
                        "ok": True,
                        "message_id": (result.get("data") or {}).get("message_id"),
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
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                result = await send_once(f"base64://{encoded}")
                if result["ok"]:
                    logger.info(
                        "Sent group image through compressed base64 fallback: %s (%s bytes)",
                        path,
                        len(buffer.getvalue()),
                    )
                    return result if return_result else True
            except Exception as exc:
                logger.warning("Failed to prepare base64 image fallback: %s", exc)
    return {"ok": False, "message_id": None} if return_result else False


async def send_report(config, content: str) -> bool:
    headers = {"Authorization": f"Bearer {config.REPORT_WS_KEY}"}
    payload = {
        "action": "send_private_msg",
        "params": {
            "user_id": int(config.REPORT_USER_ID),
            "message": content,
        },
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                config.REPORT_WS_URL,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as ws:
                await ws.send_str(json.dumps(payload, ensure_ascii=False))
        return True
    except Exception as exc:
        logger.warning("Failed to send report: %s", exc)
        return False
