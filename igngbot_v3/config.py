import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _load_astrbot_deepseek_config() -> dict:
    """Optional legacy fallback that reads a DeepSeek provider out of an
    AstrBot config file. It is disabled unless ASTRBOT_CONFIG_PATH is set
    explicitly, so a container never depends on a path from another host."""
    config_path = os.getenv("ASTRBOT_CONFIG_PATH", "")
    if not config_path:
        return {}
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
    # OneBot endpoints. Inside the NAS compose network the QQ client is a
    # sibling service named "napcat"; on the old VM the same URLs were reached
    # through the host loopback. Both are overridable from .env.
    ONEBOT_WS_URL = os.getenv("ONEBOT_WS_URL", "ws://napcat:3001")
    ONEBOT_ACCESS_TOKEN = os.getenv("ONEBOT_ACCESS_TOKEN", "")
    ONEBOT_HTTP_URL = os.getenv("ONEBOT_HTTP_URL", "http://napcat:3000")
    ONEBOT_HTTP_TOKEN = os.getenv("ONEBOT_HTTP_TOKEN", "")
    ONEBOT_NAME = os.getenv("ONEBOT_NAME", "SnowLuma OneBot")

    # The RDS endpoint and account are deployment secrets: they must come from
    # .env (deploy/docker/.env on the NAS). Nothing here may default to a real
    # host or username, because this repository is public.
    DB_HOST = os.getenv("DB_HOST", "")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_USER = os.getenv("DB_USER", "")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_NAME = os.getenv("DB_NAME", "igng_bot")

    # Aliyun RDS enforces require_secure_transport. DB_SSL turns on TLS for every
    # MySQL client (pymysql, aiomysql and the Node mysql2 pool). Verification is
    # off by default because the RDS certificate is not in the container trust
    # store; point DB_SSL_CA at Aliyun's CA to verify the server instead.
    DB_SSL = os.getenv("DB_SSL", "").strip().lower() in ("1", "true", "yes", "on")
    DB_SSL_VERIFY = os.getenv("DB_SSL_VERIFY", "").strip().lower() in ("1", "true", "yes", "on")
    DB_SSL_CA = os.getenv("DB_SSL_CA", "").strip()

    @staticmethod
    def db_ssl_context():
        """One SSLContext shared by pymysql and aiomysql, or None when TLS is off."""
        if not Config.DB_SSL:
            return None
        import ssl as _ssl
        context = _ssl.create_default_context(cafile=Config.DB_SSL_CA or None)
        if Config.DB_SSL_CA or Config.DB_SSL_VERIFY:
            context.check_hostname = True
            context.verify_mode = _ssl.CERT_REQUIRED
        else:
            context.check_hostname = False
            context.verify_mode = _ssl.CERT_NONE
        return context

    # IGNG site AI records database (ai_jobs / ai_job_attempts). It lives on the
    # same RDS instance as the bot database but in the dedicated site schema.
    # Every bot LLM call is mirrored there in addition to call_logs.
    SITE_AI_RECORDS_ENABLED = os.getenv("SITE_AI_RECORDS_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "on"
    )
    SITE_AI_DB_HOST = os.getenv("SITE_AI_DB_HOST", DB_HOST)
    SITE_AI_DB_PORT = int(os.getenv("SITE_AI_DB_PORT", str(DB_PORT)))
    SITE_AI_DB_USER = os.getenv("SITE_AI_DB_USER", DB_USER)
    SITE_AI_DB_PASSWORD = os.getenv("SITE_AI_DB_PASSWORD", DB_PASSWORD)
    SITE_AI_DB_NAME = os.getenv("SITE_AI_DB_NAME", "igng_sites")

    # new-api real-bill authority. The bot's DSH profile calls this gateway, so
    # its per-request `quota` is the actual bill. Empty base URL disables the
    # adapter: cost stays NULL (unknown) instead of being fabricated from a
    # third-party price list. The sk- key reads /api/log/token; an access token
    # (with New-Api-User id) reads /api/log/self and the authenticated pricing.
    NEWAPI_BASE_URL = os.getenv("NEWAPI_BASE_URL", "").rstrip("/")
    NEWAPI_API_KEY = os.getenv("NEWAPI_API_KEY", "")
    NEWAPI_ACCESS_TOKEN = os.getenv("NEWAPI_ACCESS_TOKEN", "")
    NEWAPI_USER_ID = os.getenv("NEWAPI_USER_ID", "")
    NEWAPI_GROUP = os.getenv("NEWAPI_GROUP", "")
    NEWAPI_QUOTA_PER_UNIT = int(os.getenv("NEWAPI_QUOTA_PER_UNIT", "500000") or "500000")
    NEWAPI_TIMEOUT_SECONDS = float(os.getenv("NEWAPI_TIMEOUT_SECONDS", "4"))

    # Shared MC database. The bot application database above remains separate.
    MC_DB_HOST = os.getenv("MC_DB_HOST", DB_HOST)
    MC_DB_PORT = int(os.getenv("MC_DB_PORT", "3306"))
    MC_DB_USER = os.getenv("MC_DB_USER", DB_USER)
    MC_DB_PASSWORD = os.getenv("MC_DB_PASSWORD", DB_PASSWORD)
    MC_DB_NAME = os.getenv("MC_DB_NAME", "mc")

    # Attachment storage root. Inside the container this is a bind mount onto
    # the NAS data directory, so no CIFS/SMB client is involved any more.
    # MESSAGE_ROOT is the authoritative name; NAS_MOUNT_* are kept working as
    # deployment-time compatibility aliases for existing .env files and scripts.
    MESSAGE_ROOT = os.getenv(
        "MESSAGE_ROOT",
        os.getenv("NAS_MOUNT_PATH", "/data/message_logs"),
    )
    NAS_MOUNT_PATH = MESSAGE_ROOT
    # Legacy prefixes that may still appear in message_logs rows or in
    # attachments_json written before the container migration. They are only
    # used to normalise old values back to storage-root-relative paths.
    LEGACY_PATH_PREFIXES = tuple(
        item.strip()
        for item in os.getenv(
            "LEGACY_PATH_PREFIXES",
            "/mnt/media/message_logs,/vol1/1000/IGNGbot/message_logs,/data/message_logs",
        ).split(",")
        if item.strip()
    )
    # Fail fast instead of silently falling back to local storage when the
    # attachment root is missing: a silent fallback hides a misconfigured
    # mount and makes every attachment invisible to the site afterwards.
    STORAGE_REQUIRE_MOUNT = os.getenv(
        "STORAGE_REQUIRE_MOUNT", "1"
    ).strip().lower() in ("1", "true", "yes", "on")

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
    LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low").strip().lower()
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

    ANIME_API_BASE_URL = os.getenv("ANIME_API_BASE_URL", "http://192.0.2.34:23335").rstrip("/")
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

    BOT_USER_ID = int(os.getenv("BOT_USER_ID", "0"))
    LOCAL_STORAGE = os.getenv("LOCAL_STORAGE", str(_PROJECT_DIR / "runtime"))
    PROMPT_DIR = os.getenv("PROMPT_DIR", str(_PROJECT_DIR / "prompts"))
    SYSTEM_PROMPT_CACHE_DIR = os.getenv(
        "SYSTEM_PROMPT_CACHE_DIR",
        str(_PROJECT_DIR / "runtime" / "system_prompts"),
    )
    SYSTEM_PROMPT_SYNC_INTERVAL_SECONDS = float(
        os.getenv("SYSTEM_PROMPT_SYNC_INTERVAL_SECONDS", "60")
    )
    # Legacy image-generation output directory. The feature was removed, but the
    # setting is kept so older deployments keep loading; it follows the current
    # attachment root rather than a hardcoded host mount.
    IMAGE_STORAGE_PATH = os.getenv(
        "IMAGE_STORAGE_PATH", os.path.join(os.path.dirname(MESSAGE_ROOT), "igngbot", "images")
    )

    MAX_FILE_SIZE = 50 * 1024 * 1024

    # Central IGNG site database.  The old MC_REPORT_IDENTITY_DB_* names are
    # accepted as deployment-time compatibility aliases because this database
    # now stores mc_tickets as well as users and permission assignments.
    IGNG_SITE_DB_HOST = os.getenv(
        "IGNG_SITE_DB_HOST",
        os.getenv("MC_REPORT_IDENTITY_DB_HOST", ""),
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
    NTFY_REPORT_URL = os.getenv("NTFY_REPORT_URL", "")
    NTFY_REPORT_TOKEN = os.getenv(
        "NTFY_REPORT_TOKEN",
        "",
    )
