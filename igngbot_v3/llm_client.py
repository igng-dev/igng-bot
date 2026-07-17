import json

import aiohttp


class LLMClient:
    def __init__(self, config):
        self.config = config

    async def generate_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 500,
        model: str | None = None,
    ) -> str:
        payload = {
            "model": model or self.config.OPENAI_CHAT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "extra_body": {"reasoning_effort": "none"},
        }
        headers = {
            "Authorization": f"Bearer {self.config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        base_url = self.config.OPENAI_BASE_URL.rstrip("/")
        url = f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.config.CLOUD_LLM_TIMEOUT),
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"LLM request failed: HTTP {response.status} {text}")
                data = json.loads(text)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return (message.get("content") or "").strip()
