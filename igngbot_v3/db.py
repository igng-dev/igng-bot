import pymysql
from pymysql.cursors import DictCursor
import logging
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import Config
from .timeutil import MYSQL_UTC_INIT_COMMAND, ensure_utc_naive, utc_now

logger = logging.getLogger(__name__)


class DBHandler:
    def __init__(self, config, *, legacy_compat=True):
        self.config = config
        # V3 rollback opts into its old schema. V4 never creates, reads or writes it.
        self.legacy_compat = legacy_compat
        self._conn = None
        self._identity_conn = None
        self._identity_lock = threading.Lock()
        # Short-lived cache of the YunYing feature tier so private sends do not
        # hit the identity database on every message.
        self._tier_cache = {}
        self._tier_lock = threading.Lock()

    def connect(self):
        self._conn = pymysql.connect(
            host=self.config.DB_HOST,
            port=getattr(self.config, "DB_PORT", 3306),
            user=self.config.DB_USER,
            password=self.config.DB_PASSWORD,
            database=self.config.DB_NAME,
            charset="utf8mb4",
            cursorclass=DictCursor,
            ssl=Config.db_ssl_context(),
            init_command=MYSQL_UTC_INIT_COMMAND,
        )
        logger.info("Connected to MySQL database with session time_zone=+00:00")

    @property
    def conn(self):
        if self._conn is None:
            self.connect()
        else:
            try:
                self._conn.ping(reconnect=True)
            except Exception as e:
                logger.warning(f"Database connection ping/reconnect failed: {e}. Attempting manual reconnect.")
                try:
                    self.connect()
                except Exception as e2:
                    logger.error(f"Manual database reconnect failed: {e2}")
                    raise
        return self._conn

    @property
    def _media_columns(self):
        return "attachments_json, file_url, file_type" if self.legacy_compat else "attachments_json"

    def init_message_tables(self):
        legacy_columns = """
                    file_url VARCHAR(500) COMMENT '文件地址',
                    file_type VARCHAR(50) COMMENT '文件类型(image/video/file/audio)',
                    audio_file_path VARCHAR(500) COMMENT '语音文件路径',""" if self.legacy_compat else ""
        with self.conn.cursor() as cursor:
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS message_logs (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    group_id BIGINT NOT NULL COMMENT '群号',
                    sender_id BIGINT NOT NULL COMMENT '发送者QQ号',
                    message_content TEXT COMMENT '消息文本内容',
                    plain_text_content TEXT COMMENT '纯文本内容',
                    message_structure MEDIUMTEXT COMMENT '结构化消息JSON',
                    attachments_json MEDIUMTEXT COMMENT '附件JSON',
                    reply_to_msg_id VARCHAR(50) COMMENT '回复的消息ID',
                    msg_id VARCHAR(50) NOT NULL COMMENT '消息ID',
                    {legacy_columns}
                    is_self TINYINT(1) DEFAULT 0 COMMENT '是否是自己发送的消息',
                    message_source VARCHAR(32) NOT NULL DEFAULT 'inbound' COMMENT '消息来源',
                    is_recalled TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已撤回',
                    recalled_at DATETIME NULL COMMENT '检测到撤回的UTC时间',
                    recall_operator_id BIGINT NULL COMMENT '执行撤回的QQ号',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'UTC时间(+00:00)',
                    INDEX idx_group_id (group_id),
                    INDEX idx_created_at (created_at),
                    INDEX idx_msg_id (msg_id),
                    INDEX idx_recalled (group_id, is_recalled),
                    UNIQUE KEY uq_message_group_msg (group_id, msg_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='聊天记录表'
            """)
            try:
                cursor.execute(
                    "ALTER TABLE message_logs "
                    "MODIFY COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP "
                    "COMMENT 'UTC时间(+00:00)'"
                )
            except Exception as exc:
                logger.debug("Skip message_logs.created_at comment refresh: %s", exc)

            # Ensure is_self column exists on message_logs
            try:
                cursor.execute(
                    "ALTER TABLE message_logs ADD COLUMN is_self TINYINT(1) DEFAULT 0 "
                    "COMMENT '是否是自己发送的消息'"
                )
            except Exception:
                pass
            for sql in (
                "ALTER TABLE message_logs ADD COLUMN plain_text_content TEXT COMMENT '纯文本内容'",
                "ALTER TABLE message_logs ADD COLUMN message_structure MEDIUMTEXT COMMENT '结构化消息JSON'",
                "ALTER TABLE message_logs ADD COLUMN attachments_json MEDIUMTEXT COMMENT '附件JSON'",
                "ALTER TABLE message_logs ADD COLUMN audio_file_path VARCHAR(500) COMMENT '语音文件路径'",
                "ALTER TABLE message_logs ADD COLUMN is_recalled TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已撤回'",
                "ALTER TABLE message_logs ADD COLUMN recalled_at DATETIME NULL COMMENT '检测到撤回的UTC时间'",
                "ALTER TABLE message_logs ADD COLUMN recall_operator_id BIGINT NULL COMMENT '执行撤回的QQ号'",
                "ALTER TABLE message_logs ADD COLUMN message_source VARCHAR(32) NOT NULL DEFAULT 'inbound' COMMENT '消息来源'",
            ):
                if not self.legacy_compat and "audio_file_path" in sql:
                    continue
                try:
                    cursor.execute(sql)
                except Exception as exc:
                    logger.debug("Skip message_logs column migration %s: %s", sql, exc)
            try:
                cursor.execute(
                    "UPDATE message_logs SET message_source = "
                    "CASE WHEN is_self = 1 THEN 'onebot_event' ELSE 'inbound' END "
                    "WHERE message_source IS NULL OR message_source = '' "
                    "OR (is_self = 1 AND message_source = 'inbound')"
                )
            except Exception as exc:
                logger.warning("Failed to backfill message_logs.message_source: %s", exc)
            try:
                cursor.execute(
                    "ALTER TABLE message_logs ADD INDEX idx_recalled (group_id, is_recalled)"
                )
            except Exception:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE message_logs ADD UNIQUE KEY uq_message_group_msg (group_id, msg_id)"
                )
            except Exception as exc:
                logger.warning(
                    "Could not add message_logs unique key (group_id,msg_id); "
                    "duplicate outbound protection may be unavailable: %s",
                    exc,
                )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS message_recall_events (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    group_id BIGINT NOT NULL COMMENT '群号',
                    msg_id VARCHAR(50) NOT NULL COMMENT '被撤回消息ID',
                    recall_operator_id BIGINT NULL COMMENT '执行撤回的QQ号',
                    recalled_at DATETIME NOT NULL COMMENT '检测到撤回的UTC时间',
                    processed_at DATETIME NULL COMMENT '已应用到消息记录的UTC时间',
                    UNIQUE KEY uq_recall_group_msg (group_id, msg_id),
                    INDEX idx_recall_pending (processed_at, recalled_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='待应用的消息撤回事件'
            """)

        self.conn.commit()
        logger.info("Message and recall tables initialized")

    def init_table(self):
        """V3 rollback initializer; V4 uses only init_message_tables."""
        self.init_message_tables()
        with self.conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS context_summaries (
                    group_id BIGINT NOT NULL PRIMARY KEY COMMENT '群号',
                    summary_text MEDIUMTEXT NOT NULL COMMENT '群聊上下文摘要',
                    summarized_through_id BIGINT NOT NULL DEFAULT 0 COMMENT '已总结到的message_logs.id',
                    status VARCHAR(20) NOT NULL DEFAULT 'complete' COMMENT 'complete/summarizing/failed/stale',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_summary_boundary (summarized_through_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='群聊上下文摘要'
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_prompts (
                    prompt_key VARCHAR(32) NOT NULL PRIMARY KEY COMMENT '提示词类型: chat',
                    prompt_text MEDIUMTEXT NOT NULL COMMENT '完整聊天系统提示词',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='完整聊天系统提示词配置'
            """)
            chat_seed_path = Path(self.config.PROMPT_DIR) / "system.txt"
            try:
                # This file is only a first-install bootstrap.  The database is
                # authoritative after the row exists, so startup must never
                # overwrite an operator-edited prompt.
                chat_seed = chat_seed_path.read_text(encoding="utf-8").strip()
            except OSError:
                chat_seed = ""
            if chat_seed:
                cursor.execute(
                    "SELECT 1 FROM system_prompts WHERE prompt_key = %s LIMIT 1",
                    ("chat",),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        "INSERT INTO system_prompts (prompt_key, prompt_text) VALUES (%s, %s)",
                        ("chat", chat_seed),
                    )
                    logger.info("Seeded missing chat system prompt from %s", chat_seed_path)


        self.conn.commit()
        logger.info("Database tables initialized")

    def get_system_prompt_record(self, prompt_key):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT prompt_key, prompt_text, created_at, updated_at "
                "FROM system_prompts WHERE prompt_key = %s LIMIT 1",
                (str(prompt_key),),
            )
            return cursor.fetchone() or {}

    def get_system_prompt(self, prompt_key):
        return self.get_system_prompt_record(prompt_key).get("prompt_text", "")

    def set_system_prompt(self, prompt_key, prompt_text):
        normalized = (prompt_text or "").strip()
        if not normalized:
            raise ValueError("system prompt cannot be empty")
        with self.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO system_prompts (prompt_key, prompt_text) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE prompt_text = VALUES(prompt_text)",
                (str(prompt_key), normalized),
            )
        self.conn.commit()

    def _permission_user_id(self, user_id):
        """Resolve QQ input to the canonical IGNG user ID for permission checks."""
        return self.resolve_bound_igng_account_id(user_id) or str(user_id)

    def _get_unified_permission_snapshot(self, user_id):
        """Read the central permission center from the shared identity database.

        None means the new tables are not available yet. An empty snapshot is a
        valid account with no elevated group and is deliberately different from
        a database/schema failure so administrator checks can fail closed.
        """
        igng_user_id = self._permission_user_id(user_id)
        try:
            with self._identity_lock:
                conn = self._identity_connection()
                with conn.cursor() as cursor:
                    cursor.execute("SELECT id, status FROM users WHERE id = %s LIMIT 1", (str(igng_user_id),))
                    user = cursor.fetchone()
                    if not user:
                        return {"user_id": str(igng_user_id), "status": None, "groups": set(), "permissions": set(), "denied_permissions": set(), "is_superadmin": False}

                    cursor.execute(
                        """
                        SELECT pg.id, pg.code
                        FROM user_permission_groups upg
                        INNER JOIN permission_groups pg ON pg.id = upg.group_id
                        WHERE upg.user_id = %s
                          AND upg.status = 'ACTIVE'
                          AND pg.enabled = 1
                          AND (upg.expires_at IS NULL OR upg.expires_at > UTC_TIMESTAMP())
                        """,
                        (str(igng_user_id),),
                    )
                    memberships = cursor.fetchall()

                    cursor.execute(
                        "SELECT id, code FROM permission_groups WHERE enabled = 1"
                    )
                    groups = cursor.fetchall()
                    cursor.execute(
                        "SELECT child_group_id, parent_group_id FROM permission_group_inherits"
                    )
                    inherits = cursor.fetchall()
                    cursor.execute(
                        """
                        SELECT pgp.group_id, pd.code
                        FROM permission_group_permissions pgp
                        INNER JOIN permission_definitions pd ON pd.id = pgp.permission_id
                        WHERE pd.enabled = 1
                        """
                    )
                    group_permissions = cursor.fetchall()
                    cursor.execute(
                        """
                        SELECT pd.code, upo.effect
                        FROM user_permission_overrides upo
                        INNER JOIN permission_definitions pd ON pd.id = upo.permission_id
                        WHERE upo.user_id = %s
                          AND (upo.expires_at IS NULL OR upo.expires_at > UTC_TIMESTAMP())
                          AND pd.enabled = 1
                        """,
                        (str(igng_user_id),),
                    )
                    overrides = cursor.fetchall()

                    # Compatibility reads only; new assignments are written to
                    # user_permission_groups by the shared permission center.
                    # global_admins is retained as a compatibility source by
                    # the central permission center.  The former module-specific
                    # administrator cache was removed with the old report system
                    # and must not make
                    # this whole snapshot fail.
                    cursor.execute("SELECT 1 FROM global_admins WHERE user_id = %s LIMIT 1", (str(igng_user_id),))
                    legacy_global_admin = cursor.fetchone() is not None
        except Exception as exc:
            logger.warning("Unified permission center unavailable for user %s: %s", igng_user_id, exc)
            return None

        group_by_id = {int(row["id"]): row["code"] for row in groups}
        effective_group_ids = {int(row["id"]) for row in memberships}
        if legacy_global_admin and "platform.superadmin" in group_by_id.values():
            effective_group_ids.add(next(group_id for group_id, code in group_by_id.items() if code == "platform.superadmin"))
        parents_by_child = {}
        for row in inherits:
            parents_by_child.setdefault(int(row["child_group_id"]), set()).add(int(row["parent_group_id"]))
        pending = list(effective_group_ids)
        while pending:
            group_id = pending.pop()
            for parent_id in parents_by_child.get(group_id, set()):
                if parent_id not in effective_group_ids:
                    effective_group_ids.add(parent_id)
                    pending.append(parent_id)

        permissions_by_group = {}
        for row in group_permissions:
            permissions_by_group.setdefault(int(row["group_id"]), set()).add(row["code"])
        permission_codes = set()
        for group_id in effective_group_ids:
            permission_codes.update(permissions_by_group.get(group_id, set()))

        denied_permissions = {row["code"] for row in overrides if row["effect"] == "DENY"}
        allowed_permissions = {row["code"] for row in overrides if row["effect"] == "ALLOW"}
        group_codes = {group_by_id[group_id] for group_id in effective_group_ids if group_id in group_by_id}
        is_superadmin = "platform.superadmin" in group_codes

        return {
            "user_id": str(igng_user_id),
            "status": user["status"],
            "groups": group_codes,
            "permissions": permission_codes,
            "denied_permissions": denied_permissions,
            "allowed_permissions": allowed_permissions,
            "is_superadmin": is_superadmin,
        }

    def _snapshot_has_permission(self, snapshot, permission_code):
        if not snapshot or snapshot.get("status") == "BANNED":
            return False
        if snapshot.get("is_superadmin"):
            return True
        if permission_code in snapshot.get("denied_permissions", set()):
            return False
        return permission_code in snapshot.get("permissions", set()) or permission_code in snapshot.get("allowed_permissions", set())

    def get_user_group(self, user_id):
        """Return the Yunying feature tier from the central permission center."""
        snapshot = self._get_unified_permission_snapshot(user_id)
        if snapshot is not None:
            if snapshot.get("is_superadmin") or self._snapshot_has_permission(snapshot, "yunying.manage"):
                return "admin"
            if self._snapshot_has_permission(snapshot, "yunying.image.pro_models"):
                return "pro"
            if self._snapshot_has_permission(snapshot, "yunying.task.system_prompt"):
                return "plus"
        return "plus"

    def is_bot_admin(self, user_id):
        snapshot = self._get_unified_permission_snapshot(user_id)
        if snapshot is None:
            return False
        return self._snapshot_has_permission(snapshot, "yunying.manage")

    def yunying_tier(self, user_id):
        """Resolve the YunYing feature tier from the central permission center.

        Returns (plus|pro|admin) only for an IGNG account that actually holds the
        feature. A QQ with no bound IGNG account, or an account without the
        permission, returns None so private access fails closed.
        """
        cache_key = str(user_id)
        now = time.monotonic()
        with self._tier_lock:
            cached = self._tier_cache.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]
        snapshot = self._get_unified_permission_snapshot(user_id)
        tier = None
        if snapshot is not None:
            if snapshot.get("is_superadmin") or self._snapshot_has_permission(snapshot, "yunying.manage"):
                tier = "admin"
            else:
                groups = snapshot.get("groups") or set()
                if "yunying.pro" in groups or self._snapshot_has_permission(snapshot, "yunying.image.pro_models"):
                    tier = "pro"
                elif "yunying.plus" in groups or self._snapshot_has_permission(snapshot, "yunying.task.system_prompt"):
                    tier = "plus"
        with self._tier_lock:
            if len(self._tier_cache) > 4096:
                self._tier_cache.clear()
            self._tier_cache[cache_key] = (now + 60, tier)
        return tier

    def private_allowed(self, user_id):
        """Only plus and above may talk to YunYing in private; unbound QQs are denied."""
        return self.yunying_tier(user_id) in ("plus", "pro", "admin")

    def get_account_qqs(self, account_id):
        """Every QQ bound to one IGNG account, for cross-group person memory reads."""
        try:
            with self._identity_lock:
                with self._identity_connection().cursor() as cursor:
                    cursor.execute(
                        "SELECT qq_number FROM user_qqs WHERE user_id = %s",
                        (str(account_id),),
                    )
                    return [
                        str(row["qq_number"])
                        for row in cursor.fetchall()
                        if row.get("qq_number") not in (None, "")
                    ]
        except Exception as exc:
            logger.warning("Failed to read QQs for account %s: %s", account_id, exc)
            return []

    def get_bot_admin_qqs(self):
        try:
            with self._identity_lock:
                with self._identity_connection().cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT DISTINCT upg.user_id
                        FROM user_permission_groups upg
                        INNER JOIN permission_groups pg ON pg.id = upg.group_id
                        INNER JOIN users u ON u.id = upg.user_id
                        WHERE upg.status = 'ACTIVE'
                          AND u.status <> 'BANNED'
                          AND pg.enabled = 1
                          AND pg.code IN ('platform.superadmin', 'yunying.admin')
                          AND (upg.expires_at IS NULL OR upg.expires_at > UTC_TIMESTAMP())
                        UNION
                        SELECT ga.user_id
                        FROM global_admins ga
                        INNER JOIN users u ON u.id = ga.user_id
                        WHERE u.status <> 'BANNED'
                        """
                    )
                    user_ids = [str(row["user_id"]) for row in cursor.fetchall()]
                    if not user_ids:
                        return []
                    placeholders = ",".join(["%s"] * len(user_ids))
                    cursor.execute(
                        f"SELECT qq_number FROM user_qqs WHERE user_id IN ({placeholders})",
                        tuple(user_ids),
                    )
                    return [str(row["qq_number"]) for row in cursor.fetchall()]
        except Exception as exc:
            logger.warning("Failed to read unified bot administrators: %s", exc)
            return []

    def set_user_group(self, igng_user_id, group_name):
        """Assign a Yunying tier in the central permission center."""
        group_name = str(group_name or "").strip().lower()
        if group_name not in ("pro", "plus"):
            return False
        target_id = str(igng_user_id)
        try:
            with self._identity_lock:
                identity_conn = self._identity_connection()
                with identity_conn.cursor() as identity_cursor:
                    identity_cursor.execute("SELECT id FROM users WHERE id = %s LIMIT 1", (target_id,))
                    if not identity_cursor.fetchone():
                        return False
                    identity_cursor.execute(
                        "SELECT id, code FROM permission_groups WHERE code IN ('yunying.plus', 'yunying.pro')"
                    )
                    group_ids = {row["code"]: int(row["id"]) for row in identity_cursor.fetchall()}
                    target_group_id = group_ids.get(f"yunying.{group_name}")
                    if not target_group_id:
                        return False
                    if group_name == "plus" and group_ids.get("yunying.pro"):
                        identity_cursor.execute(
                            "DELETE FROM user_permission_groups WHERE user_id = %s AND group_id = %s",
                            (target_id, group_ids["yunying.pro"]),
                        )
                    identity_cursor.execute(
                        """
                        INSERT INTO user_permission_groups (user_id, group_id, status, source)
                        VALUES (%s, %s, 'ACTIVE', 'yunying:command')
                        ON DUPLICATE KEY UPDATE status = 'ACTIVE', expires_at = NULL, updated_at = CURRENT_TIMESTAMP
                        """,
                        (target_id, target_group_id),
                    )
                identity_conn.commit()
            return True
        except Exception as exc:
            logger.warning("Failed to assign Yunying group %s to IGNG user %s: %s", group_name, target_id, exc)
            return False

    def get_image_models_for_user(self, user_id):
        models = list(self.config.IMAGE_STANDARD_MODELS)
        if self.get_user_group(user_id) in ("admin", "pro"):
            for model in self.config.IMAGE_PRO_MODELS:
                if model not in models:
                    models.append(model)
        return models

    def init_group_configs_table(self):
        """Create group_configs table if not exists."""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS group_configs (
                    group_id BIGINT NOT NULL PRIMARY KEY COMMENT '群号',
                    group_name VARCHAR(255) DEFAULT '' COMMENT '群名称',
                    is_chat_mode TINYINT(1) DEFAULT 0 COMMENT '聊天模式（跳过filter分析）',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='群聊配置表'
            """)
        self.conn.commit()
        logger.info("group_configs table initialized")

    def ensure_group_exists(self, group_id, group_name=""):
        """Auto-register a group if it doesn't exist yet."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT group_id FROM group_configs WHERE group_id = %s", (group_id,)
            )
            if not cursor.fetchone():
                cursor.execute(
                    "INSERT INTO group_configs (group_id, group_name) VALUES (%s, %s)",
                    (group_id, group_name),
                )
                self.conn.commit()

    def get_group_config(self, group_id):
        """Get a single group's config, or None."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM group_configs WHERE group_id = %s", (group_id,)
            )
            return cursor.fetchone()

    def toggle_chat_mode(self, group_id):
        """Toggle chat mode for a group. Returns the new state (True=on, False=off)."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT is_chat_mode FROM group_configs WHERE group_id = %s", (group_id,)
            )
            row = cursor.fetchone()
            if row:
                new_state = 0 if row["is_chat_mode"] else 1
                cursor.execute(
                    "UPDATE group_configs SET is_chat_mode = %s WHERE group_id = %s",
                    (new_state, group_id),
                )
            else:
                new_state = 1
                cursor.execute(
                    "INSERT INTO group_configs (group_id, is_chat_mode) VALUES (%s, 1)",
                    (group_id,),
                )
        self.conn.commit()
        return bool(new_state)



    def _identity_connection(self):
        if self._identity_conn is None:
            self._identity_conn = pymysql.connect(
                host=self.config.IGNG_SITE_DB_HOST,
                port=self.config.IGNG_SITE_DB_PORT,
                user=self.config.IGNG_SITE_DB_USER,
                password=self.config.IGNG_SITE_DB_PASSWORD,
                database=self.config.IGNG_SITE_DB_NAME,
                charset="utf8mb4",
                cursorclass=DictCursor,
                ssl=Config.db_ssl_context(),
            )
        else:
            self._identity_conn.ping(reconnect=True)
        return self._identity_conn

    def resolve_bound_igng_account_id(self, user_id):
        """Resolve a QQ number to its canonical IGNG account ID."""
        qq_number = str(user_id)
        try:
            with self._identity_lock:
                conn = self._identity_connection()
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT user_id FROM user_qqs WHERE qq_number = %s LIMIT 1",
                        (qq_number,),
                    )
                    row = cursor.fetchone()
                    if row and row.get("user_id") is not None:
                        return str(row["user_id"])
        except Exception as exc:
            logger.warning("Failed to resolve QQ %s to IGNG account: %s", qq_number, exc)
        return None

    def resolve_igng_account_id(self, user_id):
        """Resolve a QQ number for legacy user settings, falling back to QQ."""
        return self.resolve_bound_igng_account_id(user_id) or str(user_id)



    def insert_message(self, group_id, sender_id, message_content,
                       reply_to_msg_id, msg_id, file_url=None,
                       file_type=None, created_at=None, is_self=False,
                       plain_text_content=None, message_structure=None,
                       attachments_json=None, audio_file_path=None,
                       is_recalled=False,
                       recalled_at=None, recall_operator_id=None,
                       message_source=None):
        # message_logs.created_at is always stored as naive UTC (+00:00).
        stored_created_at = ensure_utc_naive(created_at) or utc_now()
        stored_recalled_at = ensure_utc_naive(recalled_at)
        if is_recalled and stored_recalled_at is None:
            stored_recalled_at = utc_now()
        normalized_group_id = int(group_id)
        normalized_msg_id = str(msg_id).strip()
        if not normalized_msg_id:
            raise ValueError("Message ID must not be empty")
        normalized_source = str(
            message_source or ("onebot_event" if is_self else "inbound")
        ).strip()[:32] or ("onebot_event" if is_self else "inbound")
        columns = ["group_id", "sender_id", "message_content", "plain_text_content", "message_structure",
                   "attachments_json", "reply_to_msg_id", "msg_id"]
        values = (normalized_group_id, sender_id, message_content, plain_text_content, message_structure,
                  attachments_json, reply_to_msg_id, normalized_msg_id)
        if self.legacy_compat:
            columns += ["file_url", "file_type", "audio_file_path"]
            values += (file_url, file_type, audio_file_path)
        columns += ["created_at", "is_self", "message_source", "is_recalled", "recalled_at", "recall_operator_id"]
        values += (stored_created_at, is_self, normalized_source,
                   1 if is_recalled else 0, stored_recalled_at, recall_operator_id)
        sql = "INSERT INTO message_logs (" + ", ".join(columns) + ") VALUES (" + ", ".join(["%s"] * len(columns)) + ")"
        for attempt in range(2):
            try:
                with self.conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id
                        FROM message_logs
                        WHERE group_id = %s AND msg_id = %s
                        ORDER BY id ASC
                        LIMIT 1
                        FOR UPDATE
                        """,
                        (normalized_group_id, normalized_msg_id),
                    )
                    existing_message = cursor.fetchone()
                    cursor.execute(
                        """
                        SELECT recall_operator_id, recalled_at
                        FROM message_recall_events
                        WHERE group_id = %s AND msg_id = %s AND processed_at IS NULL
                        FOR UPDATE
                        """,
                        (normalized_group_id, normalized_msg_id),
                    )
                    pending_recall = cursor.fetchone()
                    if existing_message:
                        if pending_recall:
                            cursor.execute(
                                """
                                UPDATE message_logs
                                SET is_recalled = 1,
                                    recalled_at = COALESCE(recalled_at, %s),
                                    recall_operator_id = COALESCE(recall_operator_id, %s)
                                WHERE group_id = %s AND msg_id = %s
                                """,
                                (
                                    ensure_utc_naive(pending_recall.get("recalled_at")) or utc_now(),
                                    pending_recall.get("recall_operator_id"),
                                    normalized_group_id,
                                    normalized_msg_id,
                                ),
                            )
                            cursor.execute(
                                """
                                UPDATE message_recall_events
                                SET processed_at = COALESCE(processed_at, %s)
                                WHERE group_id = %s AND msg_id = %s
                                """,
                                (utc_now(), normalized_group_id, normalized_msg_id),
                            )
                        self.conn.commit()
                        return False
                    if pending_recall:
                        is_recalled = True
                        stored_recalled_at = ensure_utc_naive(
                            pending_recall.get("recalled_at")
                        ) or stored_recalled_at or utc_now()
                        recall_operator_id = (
                            pending_recall.get("recall_operator_id")
                            if pending_recall.get("recall_operator_id") is not None
                            else recall_operator_id
                        )
                        values = values[:-3] + (1, stored_recalled_at, recall_operator_id)
                    cursor.execute(sql, values)
                    if pending_recall:
                        cursor.execute(
                            """
                            UPDATE message_recall_events
                            SET processed_at = COALESCE(processed_at, %s)
                            WHERE group_id = %s AND msg_id = %s
                            """,
                            (utc_now(), normalized_group_id, normalized_msg_id),
                        )
                self.conn.commit()
                return True
            except pymysql.err.IntegrityError:
                # Another event/API call inserted the same logical message while
                # this transaction was running. The unique key makes this safe.
                try:
                    if self._conn is not None:
                        self._conn.rollback()
                except Exception:
                    logger.debug("Rollback after duplicate message insert failed", exc_info=True)
                return False
            except Exception:
                if attempt == 1:
                    raise
                try:
                    if self._conn is not None:
                        self._conn.rollback()
                except Exception:
                    logger.debug("Rollback after message insert failure also failed", exc_info=True)
                logger.warning("Message insert failed; reconnecting and retrying once")
                self.connect()

    def mark_message_recalled(
        self, group_id, msg_id, recall_operator_id=None, recalled_at=None
    ):
        """Record a group recall event and apply it to message_logs when possible.

        The durable recall-event row is written before updating message_logs so
        that a recall received before the original message is inserted cannot be
        lost. The operation is idempotent for repeated notices.
        """
        try:
            normalized_group_id = int(group_id)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid recall group_id: {group_id!r}")
        normalized_msg_id = str(msg_id).strip()
        if not normalized_msg_id:
            raise ValueError("Recall message_id must not be empty")
        if recall_operator_id in (None, ""):
            normalized_operator_id = None
        else:
            try:
                normalized_operator_id = int(recall_operator_id)
            except (TypeError, ValueError):
                raise ValueError(f"Invalid recall operator_id: {recall_operator_id!r}")
        normalized_recalled_at = ensure_utc_naive(recalled_at) or utc_now()

        for attempt in range(2):
            try:
                with self.conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO message_recall_events
                            (group_id, msg_id, recall_operator_id, recalled_at, processed_at)
                        VALUES (%s, %s, %s, %s, NULL)
                        ON DUPLICATE KEY UPDATE
                            recall_operator_id = COALESCE(recall_operator_id, VALUES(recall_operator_id)),
                            recalled_at = COALESCE(recalled_at, VALUES(recalled_at)),
                            processed_at = NULL
                        """,
                        (
                            normalized_group_id,
                            normalized_msg_id,
                            normalized_operator_id,
                            normalized_recalled_at,
                        ),
                    )
                    cursor.execute(
                        """
                        SELECT id, is_recalled
                        FROM message_logs
                        WHERE group_id = %s AND msg_id = %s
                        ORDER BY id ASC
                        FOR UPDATE
                        """,
                        (normalized_group_id, normalized_msg_id),
                    )
                    matched_messages = list(cursor.fetchall())
                    message_count = len(matched_messages)
                    already_recalled = any(
                        bool(row.get("is_recalled")) for row in matched_messages
                    )
                    if message_count:
                        cursor.execute(
                            """
                            UPDATE message_logs
                            SET is_recalled = 1,
                                recalled_at = COALESCE(recalled_at, %s),
                                recall_operator_id = COALESCE(recall_operator_id, %s)
                            WHERE group_id = %s AND msg_id = %s
                            """,
                            (
                                normalized_recalled_at,
                                normalized_operator_id,
                                normalized_group_id,
                                normalized_msg_id,
                            ),
                        )
                        updated_rows = cursor.rowcount
                        cursor.execute(
                            """
                            UPDATE message_recall_events
                            SET processed_at = COALESCE(processed_at, %s)
                            WHERE group_id = %s AND msg_id = %s
                            """,
                            (utc_now(), normalized_group_id, normalized_msg_id),
                        )
                        # A summary may already contain the message body. Reset it
                        # when the recalled row was summarized so the next chat turn
                        # rebuilds context from the redacted message row.
                        message_ids = [
                            int(row["id"])
                            for row in matched_messages
                            if row.get("id") is not None
                        ]
                        if self.legacy_compat and message_ids:
                            cursor.execute(
                                """
                                UPDATE context_summaries
                                SET summary_text = '', summarized_through_id = 0, status = 'stale'
                                WHERE group_id = %s AND summarized_through_id >= %s
                                """,
                                (normalized_group_id, min(message_ids)),
                            )
                        status = "already_recalled" if already_recalled else "marked"
                    else:
                        updated_rows = 0
                        status = "pending"
                self.conn.commit()
                result = {
                    "status": status,
                    "group_id": normalized_group_id,
                    "msg_id": normalized_msg_id,
                    "message_found": bool(message_count),
                    "updated_rows": int(updated_rows or 0),
                }
                logger.info(
                    "Processed group recall: group=%s msg=%s status=%s updated_rows=%s operator=%s",
                    normalized_group_id,
                    normalized_msg_id,
                    status,
                    updated_rows,
                    normalized_operator_id,
                )
                return result
            except Exception:
                if attempt == 1:
                    raise
                try:
                    if self._conn is not None:
                        self._conn.rollback()
                except Exception:
                    logger.debug("Rollback after recall update failure also failed", exc_info=True)
                logger.warning("Recall update failed; reconnecting and retrying once")
                self.connect()

    def get_previous_non_self_message(self, group_id, exclude_msg_id=None):
        with self.conn.cursor() as cursor:
            if exclude_msg_id is None:
                cursor.execute(
                    "SELECT id, msg_id, message_content, plain_text_content, is_recalled "
                    "FROM message_logs WHERE group_id = %s AND is_self = 0 "
                    "ORDER BY id DESC LIMIT 1",
                    (group_id,),
                )
            else:
                cursor.execute(
                    "SELECT id, msg_id, message_content, plain_text_content, is_recalled "
                    "FROM message_logs WHERE group_id = %s AND is_self = 0 "
                    "AND msg_id <> %s ORDER BY id DESC LIMIT 1",
                    (group_id, str(exclude_msg_id)),
                )
            return cursor.fetchone()

    def get_recent_messages(self, group_id, limit=10):
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id AS _db_id, msg_id, sender_id, message_content, plain_text_content,
                       {self._media_columns}, created_at, is_self, is_recalled
                FROM message_logs
                WHERE group_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (group_id, int(limit)),
            )
            rows = list(cursor.fetchall())
        rows.reverse()
        return rows

    def get_message_by_msg_id(self, group_id, msg_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM message_logs WHERE group_id = %s AND msg_id = %s "
                "ORDER BY id DESC LIMIT 1",
                (group_id, str(msg_id)),
            )
            return cursor.fetchone()

    def get_messages_from_msg_id(self, group_id, from_msg_id, limit=10):
        """Return messages from an inclusive message boundary in chronological order."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM message_logs WHERE group_id = %s AND msg_id = %s "
                "ORDER BY id ASC LIMIT 1",
                (group_id, str(from_msg_id)),
            )
            anchor = cursor.fetchone()
            if not anchor:
                return self.get_recent_messages(group_id, limit)
            cursor.execute(
                f"""
                SELECT id AS _db_id, msg_id, sender_id, message_content, plain_text_content,
                       {self._media_columns}, created_at, is_self, is_recalled
                FROM message_logs
                WHERE group_id = %s AND id >= %s
                ORDER BY id ASC
                LIMIT %s
                """,
                (group_id, anchor["id"], int(limit)),
            )
            return list(cursor.fetchall())

    def get_messages_after_id(self, group_id, after_id=0, limit=5000):
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT id AS _db_id, msg_id, sender_id, message_content, plain_text_content,
                       {self._media_columns}, created_at, is_self, is_recalled
                FROM message_logs
                WHERE group_id = %s AND id > %s
                ORDER BY id ASC
                LIMIT %s
                """,
                (group_id, int(after_id or 0), int(limit)),
            )
            return list(cursor.fetchall())

    def get_context_summary(self, group_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT group_id, summary_text, summarized_through_id, status, updated_at "
                "FROM context_summaries WHERE group_id = %s",
                (group_id,),
            )
            return cursor.fetchone()

    def mark_context_summary_status(self, group_id, status):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO context_summaries (group_id, summary_text, status) VALUES (%s, '', %s) "
                "ON DUPLICATE KEY UPDATE status = VALUES(status)",
                (group_id, status),
            )
        self.conn.commit()

    def save_context_summary(self, group_id, summary_text, summarized_through_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO context_summaries (group_id, summary_text, summarized_through_id, status) "
                "VALUES (%s, %s, %s, 'complete') "
                "ON DUPLICATE KEY UPDATE summary_text = VALUES(summary_text), "
                "summarized_through_id = VALUES(summarized_through_id), status = 'complete'",
                (group_id, summary_text, int(summarized_through_id)),
            )
        self.conn.commit()
        return self.get_context_summary(group_id)

    def get_messages_before(self, group_id, before_msg_id, limit=20):
        if before_msg_id:
            with self.conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM message_logs WHERE group_id = %s AND msg_id = %s ORDER BY id DESC LIMIT 1",
                    (group_id, str(before_msg_id)),
                )
                anchor = cursor.fetchone()
            if not anchor:
                return []
            anchor_id = anchor["id"]
            sql = f"""
                SELECT msg_id, sender_id, message_content, plain_text_content,
                       {self._media_columns}, created_at, is_self, is_recalled
                FROM message_logs
                WHERE group_id = %s AND id < %s
                ORDER BY id DESC
                LIMIT %s
            """
            params = (group_id, anchor_id, int(limit))
        else:
            sql = f"""
                SELECT msg_id, sender_id, message_content, plain_text_content,
                       {self._media_columns}, created_at, is_self, is_recalled
                FROM message_logs
                WHERE group_id = %s
                ORDER BY id DESC
                LIMIT %s
            """
            params = (group_id, int(limit))

        with self.conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = list(cursor.fetchall())
        rows.reverse()
        return rows

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            logger.info("Database connection closed")
        if self._identity_conn:
            try:
                self._identity_conn.close()
            except Exception:
                pass
            self._identity_conn = None
