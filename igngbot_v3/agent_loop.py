import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[str]]

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class OpenAIToolAgent:
    def __init__(self, config):
        self.config = config

    async def run(
        self,
        *,
        system_prompt: str,
        user_prompt: str | None = None,
        conversation: list[dict[str, Any]] | None = None,
        tools: list[ToolSpec],
        max_steps: int | None = None,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        on_tool_progress: Callable[[str], Awaitable[None]] | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if conversation:
            messages.extend(conversation)
        else:
            messages.append({"role": "user", "content": user_prompt or ""})
        tool_map = {tool.name: tool for tool in tools}
        steps = max_steps or self.config.CLOUD_LLM_MAX_STEPS
        token_usage = {}

        for _ in range(steps):
            response = None
            last_error = None
            for attempt in range(2):
                try:
                    response = await self._chat_completion(
                        messages=messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens or self.config.AGENT_MAX_TOKENS,
                        model=model,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt == 0:
                        logger.warning("LLM completion failed; retrying once: %s", exc)
                        await asyncio.sleep(2)
            if response is None:
                raise last_error or RuntimeError("LLM request failed")
            self._merge_usage(token_usage, response.get("usage"))
            choice = (response.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []

            assistant_message = {
                "role": "assistant",
                "content": message.get("content") or "",
            }
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            messages.append(assistant_message)

            if not tool_calls:
                return {
                    "text": message.get("content") or "",
                    "raw": response,
                    "messages": messages,
                    "token_usage": token_usage or None,
                }

            progress_text = (message.get("content") or "").strip()
            if progress_text and on_tool_progress:
                try:
                    await on_tool_progress(progress_text)
                except Exception:
                    logger.exception("Tool progress message failed")

            for tool_call in tool_calls:
                function_call = tool_call.get("function") or {}
                tool_name = function_call.get("name", "")
                arguments_text = function_call.get("arguments") or "{}"
                try:
                    arguments = json.loads(arguments_text) if arguments_text else {}
                except json.JSONDecodeError:
                    arguments = {}

                tool = tool_map.get(tool_name)
                if tool is None:
                    result_text = f"error: Tool {tool_name} not found."
                else:
                    try:
                        result_text = await tool.handler(**arguments)
                    except Exception as exc:
                        logger.exception("Tool %s failed", tool_name)
                        result_text = f"error: {exc}"

                tool_content = result_text
                if isinstance(result_text, dict):
                    tool_content = result_text.get("content")
                    if tool_content is None:
                        tool_content = []
                        result_text_text = str(result_text.get("text") or "")
                        if result_text_text:
                            tool_content.append({"type": "text", "text": result_text_text})
                        for image_url in result_text.get("image_urls") or []:
                            tool_content.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": str(image_url)},
                                }
                            )
                elif isinstance(result_text, list):
                    tool_content = result_text

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "name": tool_name,
                        "content": tool_content,
                    }
                )

        return {
            "text": "",
            "raw": {"error": "max_steps_reached"},
            "messages": messages,
            "token_usage": token_usage or None,
        }

    async def summarize(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        model: str | None = None,
    ) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        response = None
        last_error = None
        for attempt in range(2):
            try:
                response = await self._chat_completion(
                    messages=messages,
                    tools=[],
                    temperature=0.2,
                    max_tokens=max_tokens,
                    model=model,
                )
                break
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    logger.warning("LLM summary failed; retrying once: %s", exc)
                    await asyncio.sleep(2)
        if response is None:
            raise last_error or RuntimeError("LLM request failed")
        self.last_summary_usage = response.get("usage")
        choice = (response.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return str(message.get("content") or "").strip()

    @staticmethod
    def _merge_usage(total: dict, usage: dict | None) -> None:
        if not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if isinstance(value, (int, float)):
                total[key] = total.get(key, 0) + value

        # Different OpenAI-compatible providers expose prompt-cache usage under
        # different names. Keep one normalized field for reporting and retain
        # the provider-specific top-level fields handled above.
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

    async def _chat_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolSpec],
        temperature: float,
        max_tokens: int,
        model: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": model or self.config.OPENAI_CHAT_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "extra_body": {"reasoning_effort": "none"},
        }
        if tools:
            payload["tools"] = [tool.to_openai_tool() for tool in tools]
            payload["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {self.config.OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }
        base_url = self.config.OPENAI_BASE_URL.rstrip("/")
        url = f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"
        retry_statuses = {429, 500, 502, 503, 504}
        last_error = None
        for attempt in range(1, 4):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=self.config.CLOUD_LLM_TIMEOUT),
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
