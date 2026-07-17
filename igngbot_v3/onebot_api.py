import json
import logging

import aiohttp

logger = logging.getLogger(__name__)


async def send_group_text(config, group_id: int | str, text: str) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.ONEBOT_HTTP_URL}/send_group_msg",
                json={"group_id": int(group_id), "message": text},
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
) -> bool:
    payload = {
        "group_id": int(group_id),
        "message": [
            {
                "type": "image",
                "data": {
                    "file": image_ref,
                    "summary": summary,
                    "sub_type": sub_type,
                },
            }
        ],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.ONEBOT_HTTP_URL}/send_group_msg",
                json=payload,
                headers={"Authorization": f"Bearer {config.ONEBOT_HTTP_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as response:
                return response.status == 200
    except Exception as exc:
        logger.warning("Failed to send group image: %s", exc)
        return False


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
