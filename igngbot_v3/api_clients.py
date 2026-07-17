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


def resolution_label(size):
    return SIZE_RESOLUTION_MAP.get(size, size)


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


def call_ccode_image(config, prompt, size, images=None, timeout=300, model=None):
    headers = {
        "Authorization": f"Bearer {config.OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "prompt": prompt,
        "model": model or config.OPENAI_IMAGE_MODEL,
        "size": size,
    }
    if images:
        payload["image"] = images

    endpoint = "edits" if images else "generations"
    url = f"{config.OPENAI_BASE_URL.rstrip('/')}/v1/images/{endpoint}"
    last_error = None
    for attempt in range(1, 4):
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
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
            f"{r.status_code} Client Error: {r.text[:300]} for url: {url}",
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
