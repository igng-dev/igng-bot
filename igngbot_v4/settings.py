from dataclasses import dataclass
import os
import re
from urllib.parse import urlsplit


def conversation_key(kind, identity):
    if kind not in {"group", "private"} or not re.fullmatch(r"[0-9]{1,20}", str(identity)):
        raise ValueError("invalid conversation")
    if int(identity) <= 0:
        raise ValueError("invalid conversation")
    return f"{kind}:{int(identity)}"


def signed_conversation(key):
    kind, identity = key.split(":", 1)
    canonical = conversation_key(kind, identity)
    if canonical != key:
        raise ValueError("noncanonical conversation")
    return int(identity) * (1 if kind == "group" else -1)


def _ids(value):
    return frozenset(str(int(s)) for s in re.split(r"[,\s]+", value) if s and s.isdecimal() and int(s) > 0)


@dataclass(frozen=True)
class Settings:
    internal_secret: str
    dsh_url: str = "http://127.0.0.1:8787"
    host: str = "127.0.0.1"
    port: int = 8788
    groups: frozenset = frozenset()
    private: frozenset = frozenset()

    @classmethod
    def from_env(cls):
        secret = os.getenv("YUNYING_INTERNAL_SECRET", "")
        if len(secret) < 32:
            raise ValueError("YUNYING_INTERNAL_SECRET must contain at least 32 characters")
        url = os.getenv("YUNYING_DSH_URL", "http://127.0.0.1:8787").rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("invalid YUNYING_DSH_URL")
        return cls(secret, url, os.getenv("YUNYING_INFRA_HOST", "127.0.0.1"),
                   int(os.getenv("YUNYING_INFRA_PORT", "8788")),
                   _ids(os.getenv("YUNYING_ALLOW_GROUPS", "")), _ids(os.getenv("YUNYING_ALLOW_PRIVATE", "")))

    def allowed(self, key):
        kind, value = key.split(":", 1)
        signed_conversation(key)
        return value in (self.groups if kind == "group" else self.private)
