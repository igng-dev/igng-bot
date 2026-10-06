"""new-api 真实账单适配器。

计费权威是 bot 实际调用的 new-api 网关：它日志里的 `quota` 就是真实账单。
模块做三件事：

1. 从 `/api/status` 与 `/api/pricing` 读取 `quota_per_unit` 和模型倍率
   （带 access token 时是账号可用分组的完整视图，匿名只有 default 分组）；
2. 用 new-api v1.0.0-rc.31 的结算公式（`service/text_quota.go`
   `calculateTextQuotaSummary`）把五桶换算成 quota，倍率优先取日志里该模型
   实际使用的值；
3. 从 `/api/log/token`（sk- key）或 `/api/log/self`（access token）读取真实
   日志：命中同一请求时直接采用日志 quota，并持续用日志倍率校准目录。

任何一步不可用都返回 None —— 未知账单不伪造为零。
"""

import json
import logging
import time
from decimal import Decimal, ROUND_HALF_UP

import aiohttp

from igngbot_v3.config import Config

logger = logging.getLogger("yunying_chat")

INT32_MAX = 2**31 - 1
_CATALOG_TTL_SECONDS = 600.0
_LOG_TTL_SECONDS = 60.0
_FAILURE_BACKOFF_SECONDS = 60.0
_LOG_MATCH_WINDOW_SECONDS = 300

_catalog = {"fetched": 0.0, "quota_per_unit": None, "group_ratio": {}, "models": {}, "error": ""}
_logs = {"fetched": 0.0, "items": [], "error": ""}
_observed = {}
_retry_after = 0.0


def _now() -> float:
    return time.monotonic()


def _enabled() -> bool:
    return bool(Config.NEWAPI_BASE_URL)


def enabled() -> bool:
    """True when a new-api base URL is configured (cost may still be unknown)."""
    return _enabled()


def _headers(auth: str) -> dict:
    if auth == "access" and Config.NEWAPI_ACCESS_TOKEN:
        headers = {"Authorization": "Bearer " + Config.NEWAPI_ACCESS_TOKEN}
        if Config.NEWAPI_USER_ID:
            headers["New-Api-User"] = Config.NEWAPI_USER_ID
        return headers
    if auth == "token" and Config.NEWAPI_API_KEY:
        return {"Authorization": "Bearer " + Config.NEWAPI_API_KEY}
    return {}


async def _get_json(path: str, auth: str = "none"):
    timeout = aiohttp.ClientTimeout(total=max(1.0, Config.NEWAPI_TIMEOUT_SECONDS))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(Config.NEWAPI_BASE_URL + path, headers=_headers(auth)) as response:
            if response.status != 200:
                raise RuntimeError(f"new-api {path}: HTTP {response.status}")
            payload = await response.json(content_type=None)
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(f"new-api {path}: {payload.get('message') or 'request failed'}")
    return payload


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _int(value) -> int:
    parsed = _number(value)
    return int(parsed) if parsed is not None else 0


async def refresh_catalog(force: bool = False) -> dict:
    """Load quota_per_unit and the model ratio table (TTL cached)."""
    global _retry_after
    if not _enabled():
        return _catalog
    fresh = _catalog["fetched"] and _now() - _catalog["fetched"] <= _CATALOG_TTL_SECONDS
    if fresh and not force:
        return _catalog
    if _now() < _retry_after and not force:
        return _catalog
    try:
        status = await _get_json("/api/status")
        # /api/pricing is optional-auth: the access token sees the account's
        # usable groups, anonymous sees only the public default group.
        pricing = await _get_json("/api/pricing", auth="access" if Config.NEWAPI_ACCESS_TOKEN else "none")
    except Exception as error:
        _retry_after = _now() + _FAILURE_BACKOFF_SECONDS
        _catalog["error"] = str(error)
        logger.warning("[云萤] new-api 价格目录不可用: %s", error)
        return _catalog
    data = status.get("data") if isinstance(status, dict) else None
    data = data if isinstance(data, dict) else {}
    models = {}
    for entry in (pricing.get("data") if isinstance(pricing, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("model_name") or "").strip()
        if not name:
            continue
        models[name] = {
            "quota_type": _int(entry.get("quota_type")),
            "model_ratio": _number(entry.get("model_ratio")),
            "model_price": _number(entry.get("model_price")),
            "completion_ratio": _number(entry.get("completion_ratio")),
            "cache_ratio": _number(entry.get("cache_ratio")),
            "create_cache_ratio": _number(entry.get("create_cache_ratio")),
        }
    group_ratio = {}
    for group, ratio in ((pricing.get("group_ratio") if isinstance(pricing, dict) else None) or {}).items():
        value = _number(ratio)
        if value is not None:
            group_ratio[str(group)] = value
    _catalog.update({
        "fetched": _now(),
        "quota_per_unit": _int(data.get("quota_per_unit")) or Config.NEWAPI_QUOTA_PER_UNIT,
        "group_ratio": group_ratio,
        "models": models,
        "error": "",
    })
    _retry_after = 0.0
    return _catalog


def normalize_log(item: dict) -> dict:
    other = item.get("other")
    if isinstance(other, str):
        try:
            other = json.loads(other)
        except ValueError:
            other = {}
    return {
        "id": _int(item.get("id")),
        "model": str(item.get("model_name") or "").strip(),
        "quota": _int(item.get("quota")),
        "prompt_tokens": _int(item.get("prompt_tokens")),
        "completion_tokens": _int(item.get("completion_tokens")),
        "created_at": _int(item.get("created_at")),
        "group": str(item.get("group") or ""),
        "request_id": str(item.get("request_id") or ""),
        "other": other if isinstance(other, dict) else {},
    }


def remember_ratios(log: dict) -> None:
    """Learn the ratios new-api actually charged with, keyed by model name."""
    model = log.get("model")
    if not model:
        return
    other = log.get("other") or {}
    observed = _observed.setdefault(model, {})
    for key, target in (("model_ratio", "model_ratio"), ("group_ratio", "group_ratio"),
                        ("completion_ratio", "completion_ratio"), ("cache_ratio", "cache_ratio"),
                        ("cache_creation_ratio", "create_cache_ratio"), ("create_cache_ratio", "create_cache_ratio")):
        value = _number(other.get(key))
        if value is not None:
            observed[target] = value


async def refresh_logs(force: bool = False) -> dict:
    """Load recent real-bill logs (TTL cached, newest first)."""
    global _retry_after
    if not _enabled():
        return _logs
    fresh = _logs["fetched"] and _now() - _logs["fetched"] <= _LOG_TTL_SECONDS
    if fresh and not force:
        return _logs
    if _now() < _retry_after and not force:
        return _logs
    try:
        if Config.NEWAPI_API_KEY:
            payload = await _get_json("/api/log/token", auth="token")
            raw = payload.get("data") if isinstance(payload, dict) else payload
        elif Config.NEWAPI_ACCESS_TOKEN:
            payload = await _get_json("/api/log/self?p=1&page_size=100", auth="access")
            data = payload.get("data") if isinstance(payload, dict) else None
            raw = data.get("items") if isinstance(data, dict) else data
        else:
            raw = None
    except Exception as error:
        _retry_after = _now() + _FAILURE_BACKOFF_SECONDS
        _logs["error"] = str(error)
        logger.warning("[云萤] new-api 账单日志不可用: %s", error)
        return _logs
    items = [normalize_log(entry) for entry in (raw or []) if isinstance(entry, dict)]
    for item in items:
        remember_ratios(item)
    _logs.update({"fetched": _now(), "items": items, "error": ""})
    _retry_after = 0.0
    return _logs


def _round_half_away(value: float) -> int:
    if value <= 0:
        return 0
    rounded = int(Decimal(repr(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return min(rounded, INT32_MAX)


def compute_quota(breakdown: dict | None, model: str) -> dict | None:
    """Reproduce new-api rc.31 settlement for one attempt, or None when unpriced.

    `breakdown` carries tokscale-style additive buckets; new-api bills
    `input + cache_read*cache_ratio + cache_write*create_ratio` at the model
    ratio and `completion` (output + reasoning) at completion_ratio.
    """
    if not breakdown or not model:
        return None
    observed = _observed.get(model) or {}
    entry = (_catalog.get("models") or {}).get(model) or {}
    group_ratio = observed.get("group_ratio")
    if group_ratio is None:
        ratios = _catalog.get("group_ratio") or {}
        group_ratio = ratios.get(Config.NEWAPI_GROUP) if Config.NEWAPI_GROUP else None
        if group_ratio is None:
            group_ratio = ratios.get("default")
    if group_ratio is None:
        group_ratio = 1.0
    quota_per_unit = int(_catalog.get("quota_per_unit") or Config.NEWAPI_QUOTA_PER_UNIT)
    total_tokens = int(breakdown.get("total_tokens") or 0)
    if total_tokens <= 0:
        return {"quota": 0, "pricing_model": model, "source": "newapi-pricing",
                "model_ratio": observed.get("model_ratio") or entry.get("model_ratio"),
                "completion_ratio": observed.get("completion_ratio") or entry.get("completion_ratio"),
                "cache_ratio": observed.get("cache_ratio") or entry.get("cache_ratio"),
                "group_ratio": group_ratio, "quota_per_unit": quota_per_unit}
    model_price = entry.get("model_price")
    if entry.get("quota_type") == 1 and model_price:
        quota = _round_half_away(model_price * quota_per_unit * group_ratio)
        return {"quota": quota, "pricing_model": model, "source": "newapi-pricing",
                "model_ratio": None, "completion_ratio": None, "cache_ratio": None,
                "group_ratio": group_ratio, "quota_per_unit": quota_per_unit}
    model_ratio = observed.get("model_ratio")
    if model_ratio is None:
        model_ratio = entry.get("model_ratio")
    completion_ratio = observed.get("completion_ratio")
    if completion_ratio is None:
        completion_ratio = entry.get("completion_ratio")
    if model_ratio is None or completion_ratio is None:
        return None
    cache_ratio = observed.get("cache_ratio")
    if cache_ratio is None:
        cache_ratio = entry.get("cache_ratio")
    if cache_ratio is None:
        cache_ratio = 1.0
    create_ratio = observed.get("create_cache_ratio")
    if create_ratio is None:
        create_ratio = entry.get("create_cache_ratio")
    if create_ratio is None:
        create_ratio = 1.25
    ratio = model_ratio * group_ratio
    prompt_quota = (int(breakdown.get("input_tokens") or 0)
                    + int(breakdown.get("cache_read_tokens") or 0) * cache_ratio
                    + int(breakdown.get("cache_write_tokens") or 0) * create_ratio)
    completion_quota = int(breakdown.get("completion_tokens") or 0) * completion_ratio
    quota = _round_half_away((prompt_quota + completion_quota) * ratio)
    if ratio != 0 and quota <= 0 and total_tokens > 0:
        quota = 1
    return {"quota": quota, "pricing_model": model, "source": "newapi-pricing",
            "model_ratio": model_ratio, "completion_ratio": completion_ratio,
            "cache_ratio": cache_ratio, "group_ratio": group_ratio, "quota_per_unit": quota_per_unit}


def _match_log(breakdown: dict, model: str, ended_at_ms: int | None) -> dict | None:
    """Find the real log for this attempt by model, token counts and time."""
    items = _logs.get("items") or []
    prompt_tokens = int(breakdown.get("prompt_tokens") or 0)
    completion_tokens = int(breakdown.get("completion_tokens") or 0)
    ended_seconds = (ended_at_ms or 0) / 1000
    best = None
    best_delta = None
    for item in items:
        if item["model"] != model or item["prompt_tokens"] != prompt_tokens or item["completion_tokens"] != completion_tokens:
            continue
        delta = abs(item["created_at"] - ended_seconds) if ended_seconds else 0
        if ended_seconds and delta > _LOG_MATCH_WINDOW_SECONDS:
            continue
        if best is None or delta < best_delta or (delta == best_delta and item["id"] > best["id"]):
            best, best_delta = item, delta
    return best


async def cost_for(breakdown: dict | None, model: str, ended_at_ms: int | None = None) -> dict | None:
    """Return {quota, usd, source, pricing_model} or None when the bill is unknown."""
    if not _enabled() or not breakdown or not model:
        return None
    await refresh_catalog()
    await refresh_logs()
    quota_per_unit = int(_catalog.get("quota_per_unit") or Config.NEWAPI_QUOTA_PER_UNIT)
    matched = _match_log(breakdown, model, ended_at_ms)
    if matched is not None:
        quota = matched["quota"]
        return {"quota": quota, "usd": float(Decimal(quota) / Decimal(quota_per_unit or 1)),
                "source": "newapi-log", "pricing_model": matched["model"]}
    computed = compute_quota(breakdown, model)
    if computed is None:
        return None
    computed["usd"] = float(Decimal(computed["quota"]) / Decimal(quota_per_unit or 1))
    return computed


def reset() -> None:
    """Drop all cached state (tests and explicit reconciliation runs)."""
    global _retry_after
    _catalog.update({"fetched": 0.0, "quota_per_unit": None, "group_ratio": {}, "models": {}, "error": ""})
    _logs.update({"fetched": 0.0, "items": [], "error": ""})
    _observed.clear()
    _retry_after = 0.0
