import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _load_astrbot_deepseek_config() -> dict:
    config_path = os.getenv(
        "ASTRBOT_CONFIG_PATH",
        "/home/serviceuser/astrbot/data/config/abconf_fbb8c746-e762-4090-a93b-9e1b0882788c.json",
    )
    try:
        with open(config_path, "r", encoding="utf-8-sig") as file:
            data = json.load(file)
    except Exception:
        return {}

    provider_sources = {
        item.get("id"): item
        for item in data.get("provider_sources", [])
        if isinstance(item, dict) and item.get("id")
    }
    providers = data.get("provider", [])

    target = None
    for item in providers:
        if not isinstance(item, dict):
            continue
        if (
            item.get("provider_source_id") == "deepseek"
            and item.get("model") == "deepseek-v4-flash"
        ):
            target = item
            break

    if target is None:
        for item in providers:
            if isinstance(item, dict) and item.get("provider_source_id") == "deepseek":
                target = item
                break

    if target is None:
        return {}

    source = provider_sources.get(target.get("provider_source_id"), {})
    keys = source.get("key") or []
    api_key = keys[0] if keys else ""
    base_url = source.get("api_base") or ""
    model = target.get("model") or ""

    if not (api_key and base_url and model):
        return {}

    return {
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "model": model,
    }


_ASTRBOT_DEEPSEEK = _load_astrbot_deepseek_config()
_BASE_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _BASE_DIR.parent


class Config:
    ONEBOT_WS_URL = os.getenv("ONEBOT_WS_URL", "ws://192.0.2.28:3001")
    ONEBOT_ACCESS_TOKEN = os.getenv("ONEBOT_ACCESS_TOKEN", "")
    ONEBOT_HTTP_URL = os.getenv("ONEBOT_HTTP_URL", "http://192.0.2.28:3200")
    ONEBOT_HTTP_TOKEN = os.getenv("ONEBOT_HTTP_TOKEN", "")
    ONEBOT_NAME = os.getenv("ONEBOT_NAME", "SnowLuma OneBot")

    DB_HOST = os.getenv("DB_HOST", "rm-2ze7v7v0evurv0953po.mysql.rds.aliyuncs.com")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_USER = os.getenv("DB_USER", "igng_bot")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_NAME = os.getenv("DB_NAME", "igng_bot")

    # Shared MC database. The bot application database above remains separate.
    MC_DB_HOST = os.getenv("MC_DB_HOST", DB_HOST)
    MC_DB_PORT = int(os.getenv("MC_DB_PORT", "3306"))
    MC_DB_USER = os.getenv("MC_DB_USER", DB_USER)
    MC_DB_PASSWORD = os.getenv("MC_DB_PASSWORD", DB_PASSWORD)
    MC_DB_NAME = os.getenv("MC_DB_NAME", "mc")

    SMB_HOST = os.getenv("SMB_HOST", "192.0.2.17")
    SMB_USER = os.getenv("SMB_USER", "serviceuser")
    SMB_PASSWORD = os.getenv("SMB_PASSWORD", "")
    SMB_SHARE = os.getenv("SMB_SHARE", "IGNGbot")
    NAS_MOUNT_BASE = os.getenv("NAS_MOUNT_BASE", "/mnt/media")
    NAS_MOUNT_PATH = os.getenv("NAS_MOUNT_PATH", "/mnt/media/message_logs")

    LLM_CLOUD_BASE_URL = os.getenv(
        "LLM_CLOUD_BASE_URL",
        os.getenv("OPENAI_BASE_URL", os.getenv("CLOUD_LLM_BASE_URL", "https://api.ccode.vip/v1")),
    ).rstrip("/")
    LLM_CLOUD_API_KEY = os.getenv(
        "LLM_CLOUD_API_KEY",
        os.getenv("OPENAI_API_KEY", os.getenv("CLOUD_LLM_API_KEY", "")),
    )
    LLM_CLOUD_MODEL = os.getenv(
        "LLM_CLOUD_MODEL",
        os.getenv("OPENAI_CHAT_MODEL", os.getenv("CLOUD_LLM_MODEL", "grok-4.5")),
    )
    LLM_LOCAL_BASE_URL = os.getenv("LLM_LOCAL_BASE_URL", "http://192.0.2.34:23333").rstrip("/")
    LLM_LOCAL_API_KEY = os.getenv("LLM_LOCAL_API_KEY", "")
    LLM_LOCAL_MODEL = os.getenv("LLM_LOCAL_MODEL", "Qwen3-4B-Q4_K_M.gguf")
    LLM_LOCAL_MULTIMODAL = os.getenv("LLM_LOCAL_MULTIMODAL", "0").strip().lower() in (
        "1", "true", "yes", "on"
    )
    # Compatibility names for modules that still use the OpenAI naming.
    OPENAI_BASE_URL = LLM_CLOUD_BASE_URL
    OPENAI_API_KEY = LLM_CLOUD_API_KEY
    OPENAI_CHAT_MODEL = LLM_CLOUD_MODEL
    OPENAI_IMAGE_MODEL = os.getenv(
        "OPENAI_IMAGE_MODEL",
        os.getenv("CCODE_IMAGE_MODEL", "gpt-image-2-plus"),
    )
    OPENAI_IMAGE_FALLBACK_MODEL = os.getenv(
        "OPENAI_IMAGE_FALLBACK_MODEL",
        os.getenv("CCODE_IMAGE_FALLBACK_MODEL", "gpt-image-2-plus"),
    )
    IMAGE_PRO_MODELS = ("gpt-image-2-plus", "gpt-image-2-fast")
    IMAGE_STANDARD_MODELS = tuple(
        item.strip()
        for item in os.getenv("IMAGE_STANDARD_MODELS", "").split(",")
        if item.strip()
    )
    OPENAI_DEFAULT_IMAGE_SIZE = os.getenv(
        "OPENAI_DEFAULT_IMAGE_SIZE",
        os.getenv("CCODE_DEFAULT_IMAGE_SIZE", "1024x1024"),
    )

    CLOUD_LLM_BASE_URL = LLM_CLOUD_BASE_URL
    CLOUD_LLM_API_KEY = LLM_CLOUD_API_KEY
    CLOUD_LLM_MODEL = LLM_CLOUD_MODEL
    CLOUD_LLM_TIMEOUT = int(os.getenv("CLOUD_LLM_TIMEOUT", "120"))

    CONTENT_REVIEW_ENABLED = os.getenv("CONTENT_REVIEW_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on"
    )
    CONTENT_REVIEW_BASE_URL = os.getenv(
        "CONTENT_REVIEW_BASE_URL", "http://192.0.2.34:23334"
    ).rstrip("/")
    CONTENT_REVIEW_API_KEY = os.getenv("CONTENT_REVIEW_API_KEY", "")
    CONTENT_REVIEW_MODEL = os.getenv("CONTENT_REVIEW_MODEL", "Qwen3Guard-0.6B")
    CONTENT_REVIEW_TIMEOUT = float(os.getenv("CONTENT_REVIEW_TIMEOUT", "90"))
    ANIME_API_BASE_URL = os.getenv("ANIME_API_BASE_URL", "http://192.0.2.34:23335").rstrip("/")
    IMAGE_REVIEW_ENABLED = os.getenv("IMAGE_REVIEW_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on"
    )
    FILTER_MODEL = os.getenv("FILTER_MODEL", CLOUD_LLM_MODEL)
    FILTER_MAX_TOKENS = int(os.getenv("FILTER_MAX_TOKENS", "500"))
    CONTEXT_MAX_TOKENS = int(os.getenv("CONTEXT_MAX_TOKENS", "8192"))
    CONTEXT_SUMMARY_TRIGGER_RATIO = float(os.getenv("CONTEXT_SUMMARY_TRIGGER_RATIO", "0.82"))
    CONTEXT_SUMMARY_KEEP_RECENT_RATIO = float(os.getenv("CONTEXT_SUMMARY_KEEP_RECENT_RATIO", "0.15"))
    CONTEXT_SUMMARY_MAX_TOKENS = int(os.getenv("CONTEXT_SUMMARY_MAX_TOKENS", "900"))
    CHAT_DEBOUNCE_SECONDS = float(os.getenv("CHAT_DEBOUNCE_SECONDS", "1.5"))
    CHAT_NON_DIRECT_COOLDOWN_SECONDS = float(
        os.getenv("CHAT_NON_DIRECT_COOLDOWN_SECONDS", "15")
    )
    CHAT_HISTORY_MAX_MESSAGES = int(os.getenv("CHAT_HISTORY_MAX_MESSAGES", "12"))
    CHAT_HISTORY_MAX_TOKENS = int(os.getenv("CHAT_HISTORY_MAX_TOKENS", "1600"))
    CHAT_DIRECT_ALIASES = tuple(
        item.strip()
        for item in os.getenv("CHAT_DIRECT_ALIASES", "云萤,莹宝").split(",")
        if item.strip()
    )
    CHAT_INCLUDE_SUMMARY = os.getenv("CHAT_INCLUDE_SUMMARY", "0").strip().lower() in (
        "1", "true", "yes", "on"
    )
    CONTEXT_SUMMARY_PROMPT = os.getenv(
        "CONTEXT_SUMMARY_PROMPT",
        """# 群聊上下文总结\n你负责维护云萤的群聊长期上下文。请把已有摘要和新增记录合并成一份紧凑、可继续使用的摘要。\n\n## 总结要求\n- 使用中文，保留当前正在讨论的主要话题、事实、结论、未解决的问题和下一步。\n- 区分不同群友的发言，保留必要的 QQ 号、玩家名、服务器名、工具查询结果和云萤已经说过的内容。\n- 记录群聊中的梗、图片或表情只有在记录明确提供了含义时才记录，不要猜测图片内容。\n- 明确标记已经完成的事情与仍然待处理的事情。\n- 不要把普通闲聊扩写成正式报告，不要编造记录中没有的事实。\n- 摘要要服务于下一次群聊回复：帮助云萤判断最新消息是否需要回应，以及避免重复自己已经说过的话。\n- 只输出摘要正文，不要输出分析过程、Markdown 代码块或额外说明。""",
    )
    CCODE_BASE_URL = os.getenv("CCODE_BASE_URL", OPENAI_BASE_URL).rstrip("/")
    CCODE_API_KEY = os.getenv("CCODE_API_KEY", "")
    CCODE_IMAGE_MODEL = OPENAI_IMAGE_MODEL
    CCODE_IMAGE_FALLBACK_MODEL = OPENAI_IMAGE_FALLBACK_MODEL
    CCODE_DEFAULT_IMAGE_SIZE = OPENAI_DEFAULT_IMAGE_SIZE
    CCODE_IMAGE_TIMEOUT = int(os.getenv("CCODE_IMAGE_TIMEOUT", "900"))

    BOT_USER_ID = int(os.getenv("BOT_USER_ID", "1000000001"))
    LOCAL_STORAGE = os.getenv("LOCAL_STORAGE", str(_PROJECT_DIR / "runtime"))
    PROMPT_DIR = os.getenv("PROMPT_DIR", str(_PROJECT_DIR / "prompts"))
    AVATAR_STORAGE_PATH = os.getenv("AVATAR_STORAGE_PATH", "/mnt/media/avatar_storage")
    STICKER_STORAGE_PATH = os.getenv("STICKER_STORAGE_PATH", "/mnt/media/sticker_storage")
    IMAGE_STORAGE_PATH = os.getenv("IMAGE_STORAGE_PATH", "/mnt/media/igngbot/images")
    IMAGE_REPOSITORY_POLL_INTERVAL = float(os.getenv("IMAGE_REPOSITORY_POLL_INTERVAL", "5"))
    IMAGE_ANALYSIS_MODEL = os.getenv("IMAGE_ANALYSIS_MODEL", OPENAI_CHAT_MODEL)

    # Incoming media text extraction.  The local providers are lazy-loaded so
    # a missing optional model package does not prevent ordinary bot startup.
    MEDIA_OCR_ENABLED = os.getenv("MEDIA_OCR_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "on"
    )
    MEDIA_OCR_PROVIDER = os.getenv("MEDIA_OCR_PROVIDER", "rapidocr")
    MEDIA_OCR_BASE_URL = os.getenv("MEDIA_OCR_BASE_URL", "").rstrip("/")
    MEDIA_OCR_API_KEY = os.getenv("MEDIA_OCR_API_KEY", "")
    MEDIA_OCR_MODEL = os.getenv("MEDIA_OCR_MODEL", LLM_LOCAL_MODEL)
    MEDIA_OCR_TIMEOUT = float(os.getenv("MEDIA_OCR_TIMEOUT", "90"))
    MEDIA_OCR_MAX_TOKENS = int(os.getenv("MEDIA_OCR_MAX_TOKENS", "1024"))
    MEDIA_OCR_MIN_SCORE = float(os.getenv("MEDIA_OCR_MIN_SCORE", "0.25"))
    MEDIA_OCR_MAX_CHARS = int(os.getenv("MEDIA_OCR_MAX_CHARS", "4000"))

    MEDIA_ASR_ENABLED = os.getenv("MEDIA_ASR_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "on"
    )
    MEDIA_ASR_PROVIDER = os.getenv("MEDIA_ASR_PROVIDER", "faster-whisper")
    MEDIA_ASR_BASE_URL = os.getenv("MEDIA_ASR_BASE_URL", "").rstrip("/")
    MEDIA_ASR_API_KEY = os.getenv("MEDIA_ASR_API_KEY", "")
    MEDIA_ASR_MODEL = os.getenv("MEDIA_ASR_MODEL", "small")
    MEDIA_ASR_MODEL_DIR = os.getenv(
        "MEDIA_ASR_MODEL_DIR", os.path.join(LOCAL_STORAGE, "media-models")
    )
    MEDIA_ASR_DEVICE = os.getenv("MEDIA_ASR_DEVICE", "cpu")
    MEDIA_ASR_COMPUTE_TYPE = os.getenv("MEDIA_ASR_COMPUTE_TYPE", "int8")
    MEDIA_ASR_LANGUAGE = os.getenv("MEDIA_ASR_LANGUAGE", "zh")
    MEDIA_ASR_BEAM_SIZE = int(os.getenv("MEDIA_ASR_BEAM_SIZE", "5"))
    MEDIA_ASR_CONVERT_AUDIO = os.getenv("MEDIA_ASR_CONVERT_AUDIO", "1").strip().lower() in (
        "1", "true", "yes", "on"
    )
    MEDIA_ASR_FFMPEG_BIN = os.getenv("MEDIA_ASR_FFMPEG_BIN", "ffmpeg")
    MEDIA_ASR_TEMP_DIR = os.getenv("MEDIA_ASR_TEMP_DIR", "")
    MEDIA_ASR_CONVERT_TIMEOUT = float(os.getenv("MEDIA_ASR_CONVERT_TIMEOUT", "60"))
    MEDIA_ASR_VAD_FILTER = os.getenv("MEDIA_ASR_VAD_FILTER", "1").strip().lower() in (
        "1", "true", "yes", "on"
    )
    MEDIA_ASR_TIMEOUT = float(os.getenv("MEDIA_ASR_TIMEOUT", "180"))
    MEDIA_ASR_MAX_CHARS = int(os.getenv("MEDIA_ASR_MAX_CHARS", "6000"))
    MAX_FILE_SIZE = 50 * 1024 * 1024

    # Central IGNG site database.  The old MC_REPORT_IDENTITY_DB_* names are
    # accepted as deployment-time compatibility aliases because this database
    # now stores mc_tickets as well as users and permission assignments.
    IGNG_SITE_DB_HOST = os.getenv(
        "IGNG_SITE_DB_HOST",
        os.getenv(
            "MC_REPORT_IDENTITY_DB_HOST",
            "rm-rj94w0fari8g50ztf4o.mysql.rds-aliyun-america.rds.aliyuncs.com",
        ),
    )
    IGNG_SITE_DB_PORT = int(
        os.getenv("IGNG_SITE_DB_PORT", os.getenv("MC_REPORT_IDENTITY_DB_PORT", "3306"))
    )
    IGNG_SITE_DB_USER = os.getenv(
        "IGNG_SITE_DB_USER",
        os.getenv("MC_REPORT_IDENTITY_DB_USER", DB_USER),
    )
    IGNG_SITE_DB_PASSWORD = os.getenv(
        "IGNG_SITE_DB_PASSWORD",
        os.getenv("MC_REPORT_IDENTITY_DB_PASSWORD", DB_PASSWORD),
    )
    IGNG_SITE_DB_NAME = os.getenv(
        "IGNG_SITE_DB_NAME",
        os.getenv("MC_REPORT_IDENTITY_DB_NAME", "igng_sites"),
    )

    # Compatibility attributes for modules or installations that still use
    # the old identity naming.  New ticket code uses IGNG_SITE_DB_* directly.
    MC_REPORT_IDENTITY_DB_HOST = IGNG_SITE_DB_HOST
    MC_REPORT_IDENTITY_DB_PORT = IGNG_SITE_DB_PORT
    MC_REPORT_IDENTITY_DB_USER = IGNG_SITE_DB_USER
    MC_REPORT_IDENTITY_DB_PASSWORD = IGNG_SITE_DB_PASSWORD
    MC_REPORT_IDENTITY_DB_NAME = IGNG_SITE_DB_NAME

    MC_TICKET_NOTIFICATION_GROUP = int(
        os.getenv(
            "MC_TICKET_NOTIFICATION_GROUP",
            os.getenv("MC_REPORT_NOTIFICATION_GROUP", "1000000007"),
        )
    )
    MC_TICKET_TECH_NOTIFICATION_GROUP = int(
        os.getenv("MC_TICKET_TECH_NOTIFICATION_GROUP", "1000000006")
    )
    MC_TICKET_POLL_INTERVAL = float(
        os.getenv("MC_TICKET_POLL_INTERVAL", os.getenv("MC_REPORT_POLL_INTERVAL", "10"))
    )
    MC_TICKET_ADMIN_URL = os.getenv(
        "MC_TICKET_ADMIN_URL",
        "https://mc.igng.net/admin",
    ).rstrip("/")
    MC_TICKET_TIMEZONE = os.getenv(
        "MC_TICKET_TIMEZONE",
        os.getenv("MC_REPORT_TIMEZONE", "Asia/Shanghai"),
    )
    MC_TICKET_DAILY_REMINDER_HOUR = int(
        os.getenv(
            "MC_TICKET_DAILY_REMINDER_HOUR",
            os.getenv("MC_REPORT_DAILY_REMINDER_HOUR", "10"),
        )
    )
    NTFY_REPORT_URL = os.getenv("NTFY_REPORT_URL", "https://ntfy.example.org/reports")
    NTFY_REPORT_TOKEN = os.getenv(
        "NTFY_REPORT_TOKEN",
        "",
    )
