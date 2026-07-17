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
    DB_USER = os.getenv("DB_USER", "igng_bot")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    DB_NAME = os.getenv("DB_NAME", "igng_bot")

    SMB_HOST = os.getenv("SMB_HOST", "192.0.2.17")
    SMB_USER = os.getenv("SMB_USER", "serviceuser")
    SMB_PASSWORD = os.getenv("SMB_PASSWORD", "")
    SMB_SHARE = os.getenv("SMB_SHARE", "IGNGbot")
    NAS_MOUNT_BASE = os.getenv("NAS_MOUNT_BASE", "/mnt/media")
    NAS_MOUNT_PATH = os.getenv("NAS_MOUNT_PATH", "/mnt/media/message_logs")

    OPENAI_BASE_URL = os.getenv(
        "OPENAI_BASE_URL",
        os.getenv("CLOUD_LLM_BASE_URL", "https://api.ccode.vip/v1"),
    ).rstrip("/")
    OPENAI_API_KEY = os.getenv(
        "OPENAI_API_KEY",
        os.getenv("CLOUD_LLM_API_KEY", ""),
    )
    OPENAI_CHAT_MODEL = os.getenv(
        "OPENAI_CHAT_MODEL",
        os.getenv("CLOUD_LLM_MODEL", "grok-4.5"),
    )
    OPENAI_IMAGE_MODEL = os.getenv(
        "OPENAI_IMAGE_MODEL",
        os.getenv("CCODE_IMAGE_MODEL", "grok-imagine-image-quality"),
    )
    OPENAI_IMAGE_FALLBACK_MODEL = os.getenv(
        "OPENAI_IMAGE_FALLBACK_MODEL",
        os.getenv("CCODE_IMAGE_FALLBACK_MODEL", "grok-imagine-image-quality"),
    )
    OPENAI_DEFAULT_IMAGE_SIZE = os.getenv(
        "OPENAI_DEFAULT_IMAGE_SIZE",
        os.getenv("CCODE_DEFAULT_IMAGE_SIZE", "1024x1024"),
    )

    CLOUD_LLM_BASE_URL = OPENAI_BASE_URL
    CLOUD_LLM_API_KEY = OPENAI_API_KEY
    CLOUD_LLM_MODEL = OPENAI_CHAT_MODEL
    CLOUD_LLM_TIMEOUT = int(os.getenv("CLOUD_LLM_TIMEOUT", "120"))
    CLOUD_LLM_MAX_STEPS = int(os.getenv("CLOUD_LLM_MAX_STEPS", "8"))
    CONTENT_REVIEW_ENABLED = os.getenv("CONTENT_REVIEW_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on"
    )
    FILTER_MODEL = os.getenv("FILTER_MODEL", CLOUD_LLM_MODEL)
    FILTER_MAX_TOKENS = int(os.getenv("FILTER_MAX_TOKENS", "500"))
    AGENT_MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "1800"))
    CONTEXT_MAX_TOKENS = int(os.getenv("CONTEXT_MAX_TOKENS", "4096"))
    CONTEXT_SUMMARY_TRIGGER_RATIO = float(os.getenv("CONTEXT_SUMMARY_TRIGGER_RATIO", "0.82"))
    CONTEXT_SUMMARY_KEEP_RECENT_RATIO = float(os.getenv("CONTEXT_SUMMARY_KEEP_RECENT_RATIO", "0.15"))
    CONTEXT_SUMMARY_MAX_TOKENS = int(os.getenv("CONTEXT_SUMMARY_MAX_TOKENS", "900"))
    CONTEXT_SUMMARY_PROMPT = os.getenv(
        "CONTEXT_SUMMARY_PROMPT",
        """# 群聊上下文总结\n你负责维护云萤的群聊长期上下文。请把已有摘要和新增记录合并成一份紧凑、可继续使用的摘要。\n\n## 总结要求\n- 使用中文，保留当前正在讨论的主要话题、事实、结论、未解决的问题和下一步。\n- 区分不同群友的发言，保留必要的 QQ 号、玩家名、服务器名、工具查询结果和云萤已经说过的内容。\n- 记录群聊中的梗、图片或表情只有在记录明确提供了含义时才记录，不要猜测图片内容。\n- 明确标记已经完成的事情与仍然待处理的事情。\n- 不要把普通闲聊扩写成正式报告，不要编造记录中没有的事实。\n- 摘要要服务于下一次群聊回复：帮助云萤判断最新消息是否需要回应，以及避免重复自己已经说过的话。\n- 只输出摘要正文，不要输出分析过程、Markdown 代码块或额外说明。""",
    )
    CCODE_BASE_URL = OPENAI_BASE_URL
    CCODE_API_KEY = OPENAI_API_KEY
    CCODE_IMAGE_MODEL = OPENAI_IMAGE_MODEL
    CCODE_IMAGE_FALLBACK_MODEL = OPENAI_IMAGE_FALLBACK_MODEL
    CCODE_DEFAULT_IMAGE_SIZE = OPENAI_DEFAULT_IMAGE_SIZE

    BOT_USER_ID = int(os.getenv("BOT_USER_ID", "1000000001"))
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "1000000002"))
    _ADMIN_USER_IDS_RAW = os.getenv(
        "ADMIN_USER_IDS",
        "1000000002,1000000003",
    )
    ADMIN_USER_IDS = set(
        int(item.strip())
        for item in _ADMIN_USER_IDS_RAW.split(",")
        if item.strip()
    )

    @classmethod
    def is_admin_user(cls, user_id):
        try:
            return int(user_id) in cls.ADMIN_USER_IDS
        except (TypeError, ValueError):
            return False

    LOCAL_STORAGE = os.getenv("LOCAL_STORAGE", str(_PROJECT_DIR / "runtime"))
    PROMPT_DIR = os.getenv("PROMPT_DIR", str(_PROJECT_DIR / "prompts"))
    AVATAR_STORAGE_PATH = os.getenv("AVATAR_STORAGE_PATH", "/mnt/media/avatar_storage")
    STICKER_STORAGE_PATH = os.getenv("STICKER_STORAGE_PATH", "/mnt/media/sticker_storage")
    MAX_FILE_SIZE = 50 * 1024 * 1024

    REPORT_WS_URL = os.getenv("REPORT_WS_URL", "ws://192.0.2.17:8001")
    REPORT_WS_KEY = os.getenv("REPORT_WS_KEY", "")
    REPORT_USER_ID = os.getenv("REPORT_USER_ID", "1000000002")
    MC_REPORTS_DB_HOST = os.getenv("MC_REPORTS_DB_HOST", os.getenv("MC_STATUS_DB_HOST", DB_HOST))
    MC_REPORTS_DB_PORT = int(os.getenv("MC_REPORTS_DB_PORT", "3306"))
    MC_REPORTS_DB_USER = os.getenv("MC_REPORTS_DB_USER", os.getenv("MC_STATUS_DB_USER", "igng_sites"))
    MC_REPORTS_DB_PASSWORD = os.getenv(
        "MC_REPORTS_DB_PASSWORD",
        os.getenv("MC_STATUS_DB_PASS", ""),
    )
    MC_REPORTS_DB_NAME = os.getenv("MC_REPORTS_DB_NAME", "mc_reports")
    MC_REPORT_NOTIFICATION_GROUP = int(os.getenv("MC_REPORT_NOTIFICATION_GROUP", "1000000007"))
    MC_REPORT_POLL_INTERVAL = float(os.getenv("MC_REPORT_POLL_INTERVAL", "10"))
    MC_REPORTS_URL = os.getenv("MC_REPORTS_URL", "https://mc.igng.net/reports")
    MC_REPORT_TIMEZONE = os.getenv("MC_REPORT_TIMEZONE", "Asia/Shanghai")
    MC_REPORT_DAILY_REMINDER_HOUR = int(os.getenv("MC_REPORT_DAILY_REMINDER_HOUR", "10"))
    OLLAMA_BASE_URL = OPENAI_BASE_URL
    OLLAMA_MODEL = OPENAI_CHAT_MODEL
    NTFY_REPORT_URL = os.getenv("NTFY_REPORT_URL", "https://ntfy.example.org/reports")
    NTFY_REPORT_TOKEN = os.getenv(
        "NTFY_REPORT_TOKEN",
        "",
    )
