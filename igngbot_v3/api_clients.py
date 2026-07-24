import base64
import mimetypes
import os
import re

import requests


RESOLUTION_RE = re.compile(r"(?i)(?:^|\s)([124])k(?:\s|$)")
RESOLUTION_SIZE_MAP = {
    "1k": "1024x1024",
    "2k": "2048x2048",
    "4k": "3840x2160",
}
SIZE_RESOLUTION_MAP = {v: k for k, v in RESOLUTION_SIZE_MAP.items()}
IMAGE_ASPECT_RATIOS = (
    "1:1", "5:4", "9:16", "21:9", "16:9",
    "4:3", "3:2", "4:5", "3:4", "2:3",
)
IMAGE_QUALITIES = ("low", "medium", "high")
IMAGE_QUALITY_LABELS = {
    "low": "低质量",
    "medium": "中等质量",
    "high": "高质量",
}


def extract_resolution(text, default_size):
    """Return (clean_text, size, changed) after removing a trailing/in-message 1k/2k/4k marker."""
    if not text:
        return text, default_size, False
    matches = list(RESOLUTION_RE.finditer(text))
    if not matches:
        return text, default_size, False
    marker = f"{matches[-1].group(1).lower()}k"
    size = RESOLUTION_SIZE_MAP[marker]
    clean = (text[: matches[-1].start()] + " " + text[matches[-1].end() :]).strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean, size, True


def extract_leading_resolution(text, default_size):
    """Parse only a leading 1k/2k/4k token as the command resolution."""
    if not text:
        return text, default_size, False
    match = re.match(r"^\s*([124])k(?:\s+|$)", text, flags=re.IGNORECASE)
    if not match:
        return text.strip(), default_size, False
    marker = f"{match.group(1).lower()}k"
    size = RESOLUTION_SIZE_MAP[marker]
    return text[match.end() :].strip(), size, True


def resolution_label(size):
    return SIZE_RESOLUTION_MAP.get(size, size)


def normalize_aspect_ratio(value):
    value = str(value or "1:1").strip().replace("：", ":")
    return value if value in IMAGE_ASPECT_RATIOS else "1:1"


def normalize_image_quality(value):
    value = str(value or "high").strip().lower()
    return value if value in IMAGE_QUALITIES else "high"


def cloud_chat_completion(config, messages, temperature=0.7, max_tokens=800):
    headers = {
        "Authorization": f"Bearer {config.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.OPENAI_CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
        "extra_body": {"reasoning_effort": "none"},
    }
    base_url = config.OPENAI_BASE_URL.rstrip("/")
    if base_url.endswith("/v1"):
        endpoint = f"{base_url}/chat/completions"
    else:
        endpoint = f"{base_url}/v1/chat/completions"
    r = requests.post(
        endpoint,
        json=payload,
        headers=headers,
        timeout=300,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""


def call_ccode_image(
    config,
    prompt,
    size,
    images=None,
    timeout=None,
    model=None,
    aspect_ratio=None,
    quality="high",
):
    headers = {
        "Authorization": f"Bearer {config.CCODE_API_KEY}",
        "Content-Type": "application/json",
    }
    aspect_ratio = normalize_aspect_ratio(aspect_ratio)
    quality = normalize_image_quality(quality)
    prompt = (
        f"{prompt}\n输出分辨率等级：{resolution_label(size)}。"
        f"输出质量要求：{IMAGE_QUALITY_LABELS[quality]}。"
        "请尽可能按照该质量档位完成细节、清晰度和整体一致性。"
    )
    if aspect_ratio != "1:1":
        prompt += f"\n输出图像比例必须为 {aspect_ratio}。请保持主体构图适配该比例，不要输出其他比例。"
    payload = {
        "prompt": prompt,
        "model": model or config.OPENAI_IMAGE_MODEL,
    }
    # Non-square ratios are controlled by the prompt. Sending the legacy
    # square size alongside them causes the relay to force a 1:1 result.
    if size and payload["model"] != "gpt-image-2-fast" and aspect_ratio == "1:1":
        payload["size"] = size
    if images:
        payload["image"] = images
    base_url = config.CCODE_BASE_URL.rstrip("/")
    endpoint = "edits" if images else "generations"
    url = f"{base_url}/images/{endpoint}" if base_url.endswith("/v1") else f"{base_url}/v1/images/{endpoint}"
    timeout = timeout or getattr(config, "CCODE_IMAGE_TIMEOUT", 900)
    last_error = None
    for attempt in range(1, 4):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.Timeout as exc:
            raise requests.Timeout(
                f"图像接口请求超过 {timeout} 秒仍未返回。",
            ) from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"图像接口网络请求失败：{exc}") from exc
        if r.status_code < 400:
            data = r.json()
            item = data.get("data", [{}])[0] if isinstance(data.get("data"), list) else {}
            return {
                "url": item.get("url"),
                "b64_json": item.get("b64_json"),
                "revised_prompt": item.get("revised_prompt") or prompt,
                "usage": data.get("usage"),
            }

        last_error = requests.HTTPError(
            f"图像接口返回 HTTP {r.status_code}：{r.text[:300]}",
            response=r,
        )
        if r.status_code not in (429, 500, 502, 503, 504):
            raise last_error

        retry_after = r.headers.get("Retry-After")
        sleep_s = int(retry_after) if retry_after and retry_after.isdigit() else 2 * attempt
        if attempt < 3:
            import time

            time.sleep(sleep_s)

    if last_error:
        raise last_error
    raise RuntimeError("OpenAI-compatible image request failed without response")


def image_file_to_data_url(path):
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"
