import asyncio
import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, config):
        self.config = config

    @staticmethod
    def _merge_usage(total: dict, usage: dict | None) -> None:
        if not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                total[key] = total.get(key, 0) + value

        cache_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
        if isinstance(cache_details, dict):
            cached_tokens = cache_details.get("cached_tokens")
            if isinstance(cached_tokens, (int, float)):
                total["cached_tokens"] = total.get("cached_tokens", 0) + cached_tokens

        cache_hit_tokens = usage.get("prompt_cache_hit_tokens")
        if isinstance(cache_hit_tokens, (int, float)):
            total["cached_tokens"] = total.get("cached_tokens", 0) + cache_hit_tokens

        cache_miss_tokens = usage.get("prompt_cache_miss_tokens")
        if isinstance(cache_miss_tokens, (int, float)):
            total["cache_miss_tokens"] = total.get("cache_miss_tokens", 0) + cache_miss_tokens

    async def chat_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": model or self.config.OPENAI_CHAT_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "extra_body": {"reasoning_effort": "none"},
        }
        headers = {
            "Authorization": f"Bearer {api_key if api_key is not None else self.config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": getattr(self.config, "LLM_USER_AGENT", "IGNGbot/3.0 (aiohttp)"),
        }
        endpoint_base_url = (base_url or self.config.OPENAI_BASE_URL).rstrip("/")
        url = (
            f"{endpoint_base_url}/chat/completions"
            if endpoint_base_url.endswith("/v1")
            else f"{endpoint_base_url}/v1/chat/completions"
        )
        retry_statuses = {429, 500, 502, 503, 504}
        last_error = None
        for attempt in range(1, 4):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(
                            total=timeout or self.config.CLOUD_LLM_TIMEOUT
                        ),
                    ) as response:
                        text = await response.text()
                        if response.status < 400:
                            return json.loads(text)
                        last_error = RuntimeError(
                            f"LLM request failed: HTTP {response.status} {text[:1000]}"
                        )
                        if response.status not in retry_statuses:
                            raise last_error
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt >= 3:
                    raise
            if attempt < 3:
                await asyncio.sleep(2 * attempt)
        if last_error:
            raise last_error
        raise RuntimeError("LLM request failed without response")

    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        data = await self.chat_completion(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model=model,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return (message.get("content") or "").strip()
