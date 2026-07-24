"""Async client for the self-hosted SearXNG JSON API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urljoin

import aiohttp

logger = logging.getLogger(__name__)


class SearXNGError(RuntimeError):
    """Raised when SearXNG cannot return a usable search response."""


class SearXNGClient:
    def __init__(self, config):
        self.enabled = bool(config.SEARXNG_ENABLED)
        self.base_url = config.SEARXNG_BASE_URL.rstrip("/")
        self.timeout = float(config.SEARXNG_TIMEOUT)
        self.language = config.SEARXNG_LANGUAGE
        self.default_categories = config.SEARXNG_CATEGORIES
        self.default_engines = config.SEARXNG_ENGINES
        self.chat_limit = int(config.SEARXNG_CHAT_RESULTS)
        self.task_limit = int(config.SEARXNG_TASK_RESULTS)
        self.max_query_length = int(config.SEARXNG_MAX_QUERY_LENGTH)

    async def search(
        self,
        query: str,
        *,
        mode: str = "chat",
        limit: int | None = None,
        categories: str | None = None,
        engines: str | None = None,
        time_range: str | None = None,
    ) -> str:
        if not self.enabled:
            return "error: 联网搜索功能当前未启用。"

        query = str(query or "").strip()
        if not query:
            return "error: 搜索关键词不能为空。"
        query = query[: self.max_query_length]

        default_limit = self.task_limit if mode == "task" else self.chat_limit
        result_limit = self._clamp_limit(limit or default_limit, default_limit)
        params = {
            "q": query,
            "format": "json",
            "language": self.language,
            "categories": categories or self.default_categories,
            "engines": engines or self.default_engines,
            "pageno": 1,
        }
        if time_range:
            params["time_range"] = str(time_range).strip().lower()

        try:
            payload = await self._request(params)
        except (SearXNGError, asyncio.TimeoutError, aiohttp.ClientError) as exc:
            logger.warning("SearXNG search failed: %s", exc)
            return f"error: 联网搜索暂时不可用（{self._error_text(exc)}）。"

        results = self._normalize_results(payload.get("results"), result_limit)
        if not results:
            return f"没有找到与「{query}」相关的可用搜索结果。"
        return self._format_results(query, results)

    async def _request(self, params: dict[str, Any]) -> dict[str, Any]:
        endpoint = urljoin(f"{self.base_url}/", "search")
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = {"Accept": "application/json", "User-Agent": "IGNGbot/3 SearXNG client"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(endpoint, params=params) as response:
                body = await response.text()
                if response.status >= 400:
                    raise SearXNGError(f"HTTP {response.status}: {body[:240]}")
                try:
                    data = await response.json(content_type=None)
                except (TypeError, ValueError) as exc:
                    raise SearXNGError("返回内容不是有效 JSON") from exc
                if not isinstance(data, dict):
                    raise SearXNGError("返回内容格式错误")
                return data

    @staticmethod
    def _clamp_limit(value: int, default: int) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = default
        return max(1, min(value, 10))

    @classmethod
    def _normalize_results(cls, raw_results: Any, limit: int) -> list[dict[str, str]]:
        if not isinstance(raw_results, list):
            return []
        normalized = []
        seen_urls = set()
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = cls._clean_text(item.get("title"))
            snippet = cls._clean_text(item.get("content") or item.get("snippet"))
            if not url or not title or url in seen_urls:
                continue
            seen_urls.add(url)
            normalized.append(
                {
                    "title": title[:300],
                    "url": url[:2000],
                    "snippet": snippet[:1000],
                    "engine": cls._clean_text(item.get("engine_name")),
                    "published": cls._clean_text(item.get("publishedDate")),
                }
            )
            if len(normalized) >= limit:
                break
        return normalized

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        return " ".join(str(value).split())

    @staticmethod
    def _format_results(query: str, results: list[dict[str, str]]) -> str:
        lines = [f"联网搜索结果：{query}", "请只把这些内容作为待核验来源，不要把搜索摘要当成事实。"]
        for index, item in enumerate(results, 1):
            lines.append(f"[{index}] {item['title']}")
            lines.append(f"URL: {item['url']}")
            if item.get("snippet"):
                lines.append(f"摘要: {item['snippet']}")
            metadata = "；".join(
                value
                for value in (
                    f"搜索源: {item['engine']}" if item.get("engine") else "",
                    f"时间: {item['published']}" if item.get("published") else "",
                )
                if value
            )
            if metadata:
                lines.append(metadata)
        return "\n".join(lines)

    @staticmethod
    def _error_text(exc: Exception) -> str:
        text = str(exc).strip()
        return text[:180] if text else exc.__class__.__name__
