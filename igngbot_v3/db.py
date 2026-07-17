import pymysql
from pymysql.cursors import DictCursor
import logging
import re

logger = logging.getLogger(__name__)


class DBHandler:
    def __init__(self, config):
        self.config = config
        self._conn = None

    def connect(self):
        self._conn = pymysql.connect(
            host=self.config.DB_HOST,
            user=self.config.DB_USER,
            password=self.config.DB_PASSWORD,
            database=self.config.DB_NAME,
            charset="utf8mb4",
            cursorclass=DictCursor,
        )
        logger.info("Connected to MySQL database")

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

    def init_table(self):
        with self.conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS message_logs (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    group_id BIGINT NOT NULL COMMENT '群号',
                    sender_id BIGINT NOT NULL COMMENT '发送者QQ号',
                    message_content TEXT COMMENT '消息文本内容',
                    plain_text_content TEXT COMMENT '纯文本/语音转写文本',
                    message_structure MEDIUMTEXT COMMENT '结构化消息JSON',
                    attachments_json MEDIUMTEXT COMMENT '附件JSON',
                    reply_to_msg_id VARCHAR(50) COMMENT '回复的消息ID',
                    msg_id VARCHAR(50) NOT NULL COMMENT '消息ID',
                    is_approved TINYINT(1) DEFAULT 0 COMMENT '是否已审核',
                    file_url VARCHAR(500) COMMENT '文件地址',
                    file_type VARCHAR(50) COMMENT '文件类型(image/video/file/audio)',
                    audio_file_path VARCHAR(500) COMMENT '语音文件路径',
                    audio_transcript TEXT COMMENT '语音转写文本',
                    is_self TINYINT(1) DEFAULT 0 COMMENT '是否是自己发送的消息',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '时间',
                    INDEX idx_group_id (group_id),
                    INDEX idx_created_at (created_at),
                    INDEX idx_msg_id (msg_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='聊天记录表'
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS violation_logs (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    msg_id VARCHAR(50) NOT NULL COMMENT '违规消息ID',
                    group_id BIGINT NOT NULL COMMENT '群号',
                    sender_id BIGINT NOT NULL COMMENT '发送者QQ号',
                    message_content TEXT COMMENT '违规消息内容',
                    violation_reason TEXT COMMENT '违规理由',
                    violation_level VARCHAR(20) NOT NULL COMMENT '违规等级: severe/general',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '记录时间',
                    INDEX idx_group_id (group_id),
                    INDEX idx_violation_level (violation_level),
                    INDEX idx_created_at (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='违规记录表'
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS avatar_records (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    prompt_original TEXT COMMENT '主题描述',
                    prompt_final TEXT COMMENT 'Ollama微调后的提示词',
                    revised_prompt TEXT COMMENT 'API返回的改写提示词',
                    image_url VARCHAR(500) COMMENT 'API返回的图片下载URL',
                    file_path VARCHAR(500) COMMENT '本地/NAS存储路径',
                    status VARCHAR(20) DEFAULT 'generated' COMMENT 'generated/applied/failed',
                    is_current TINYINT(1) DEFAULT 0 COMMENT '是否为当前正在使用的头像',
                    generated_at DATETIME COMMENT '头像生成时间',
                    applied_at DATETIME COMMENT '应用为QQ头像的时间',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_status (status),
                    INDEX idx_is_current (is_current),
                    INDEX idx_generated_at (generated_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每日头像记录表'
            """)
            # Ensure is_current column exists (for tables created before the field was added)
            try:
                cursor.execute(
                    "ALTER TABLE avatar_records ADD COLUMN is_current TINYINT(1) DEFAULT 0 "
                    "COMMENT '是否为当前正在使用的头像'"
                )
            except Exception:
                pass  # column already exists
            # Ensure is_self column exists on message_logs
            try:
                cursor.execute(
                    "ALTER TABLE message_logs ADD COLUMN is_self TINYINT(1) DEFAULT 0 "
                    "COMMENT '是否是自己发送的消息'"
                )
            except Exception:
                pass
            for sql in (
                "ALTER TABLE message_logs ADD COLUMN plain_text_content TEXT COMMENT '纯文本/语音转写文本'",
                "ALTER TABLE message_logs ADD COLUMN message_structure MEDIUMTEXT COMMENT '结构化消息JSON'",
                "ALTER TABLE message_logs ADD COLUMN attachments_json MEDIUMTEXT COMMENT '附件JSON'",
                "ALTER TABLE message_logs ADD COLUMN audio_file_path VARCHAR(500) COMMENT '语音文件路径'",
                "ALTER TABLE message_logs ADD COLUMN audio_transcript TEXT COMMENT '语音转写文本'",
            ):
                try:
                    cursor.execute(sql)
                except Exception:
                    pass
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stickers (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL COMMENT '表情名称',
                    description TEXT COMMENT '表情描述/具体动作',
                    avatar_id BIGINT COMMENT '关联的头像记录ID',
                    file_path VARCHAR(500) COMMENT 'NAS存储路径',
                    image_url VARCHAR(500) COMMENT 'API返回的图片URL',
                    generated_at DATETIME COMMENT '生成时间',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_name (name),
                    INDEX idx_avatar_id (avatar_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='表情包表'
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS context_summaries (
                    group_id BIGINT NOT NULL PRIMARY KEY COMMENT '群号',
                    summary_text MEDIUMTEXT NOT NULL COMMENT '群聊上下文摘要',
                    summarized_through_id BIGINT NOT NULL DEFAULT 0 COMMENT '已总结到的message_logs.id',
                    status VARCHAR(20) NOT NULL DEFAULT 'complete' COMMENT 'complete/summarizing/failed',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_summary_boundary (summarized_through_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='群聊上下文摘要'
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS personality_profiles (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE COMMENT '性格名称',
                    prompt_text MEDIUMTEXT NOT NULL COMMENT '性格提示词内容',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='云萤性格配置'
            """)
            cursor.execute(
                "SELECT COUNT(*) AS column_count FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'personality_profiles' "
                "AND COLUMN_NAME = 'is_active'"
            )
            if cursor.fetchone()["column_count"]:
                cursor.execute("ALTER TABLE personality_profiles DROP COLUMN is_active")
            cursor.execute(
                "INSERT IGNORE INTO personality_profiles (name, prompt_text) VALUES (%s, %s)",
                (
                    "亲和",
                    "性格：活泼、话不多但不代表没精神，该冒泡就冒泡，不该说话时也不硬凑话题。"
                    "懒得打长句，看到长篇大论容易不耐烦。爱追番、打游戏，很容易被群里正在聊的梗带跑注意力。"
                    "看到好玩的事第一反应是笑和起哄，而不是分析。",
                ),
            )
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS group_personality_configs (
                    group_id BIGINT NOT NULL PRIMARY KEY COMMENT '群号',
                    personality_id BIGINT NOT NULL COMMENT '性格ID',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_group_personality (personality_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='群聊性格选择'
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_config (
                    user_id VARCHAR(32) NOT NULL PRIMARY KEY,
                    affinity_value INT DEFAULT 50,
                    affinity_enabled TINYINT(1) DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户配置表'
            """)
        self.conn.commit()
        logger.info("Database tables initialized")

    def get_active_personality(self, group_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT p.id, p.name, p.prompt_text FROM group_personality_configs g "
                "JOIN personality_profiles p ON p.id = g.personality_id "
                "WHERE g.group_id = %s LIMIT 1",
                (group_id,),
            )
            selected = cursor.fetchone()
            if selected:
                return selected
            cursor.execute(
                "SELECT id, name, prompt_text FROM personality_profiles "
                "WHERE name = %s LIMIT 1",
                ("亲和",),
            )
            return cursor.fetchone()

    def list_personalities(self):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM personality_profiles ORDER BY id ASC"
            )
            return list(cursor.fetchall())

    def activate_personality(self, group_id, name):
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT id FROM personality_profiles WHERE name = %s", (name,))
            target = cursor.fetchone()
            if not target:
                return False
            cursor.execute(
                "INSERT INTO group_personality_configs (group_id, personality_id) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE personality_id = VALUES(personality_id)",
                (group_id, target["id"]),
            )
        self.conn.commit()
        return True

    def init_group_configs_table(self):
        """Create group_configs table if not exists."""
        with self.conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS group_configs (
                    group_id BIGINT NOT NULL PRIMARY KEY COMMENT '群号',
                    group_name VARCHAR(255) DEFAULT '' COMMENT '群名称',
                    is_content_review TINYINT(1) DEFAULT 0 COMMENT '内容审核标记',
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

    def set_content_review(self, group_id, enabled):
        """Set content review flag for a group."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO group_configs (group_id, is_content_review) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE is_content_review = %s",
                (group_id, 1 if enabled else 0, 1 if enabled else 0),
            )
        self.conn.commit()

    def get_content_review_groups(self):
        """Get all group IDs with content review enabled."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT group_id FROM group_configs WHERE is_content_review = 1"
            )
            return [row["group_id"] for row in cursor.fetchall()]

    def ensure_user_config(self, user_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT IGNORE INTO user_config
                (user_id, affinity_value, affinity_enabled)
                VALUES (%s, 50, 1)
                """,
                (str(user_id),),
            )
        self.conn.commit()

    def get_user_configs_batch(self, user_ids):
        if not user_ids:
            return {}
        user_ids = [str(uid) for uid in user_ids]
        placeholders = ",".join(["%s"] * len(user_ids))
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT user_id, affinity_value, affinity_enabled
                FROM user_config
                WHERE user_id IN ({placeholders})
                """,
                user_ids,
            )
            rows = cursor.fetchall()
        result = {}
        for row in rows:
            result[str(row["user_id"])] = row
        for uid in user_ids:
            if uid not in result:
                self.ensure_user_config(uid)
                result[uid] = {
                    "user_id": uid,
                    "affinity_value": 50,
                    "affinity_enabled": 1,
                }
        return result

    def update_affinity(self, user_id, affinity_value):
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO user_config (user_id, affinity_value, affinity_enabled)
                VALUES (%s, %s, 1)
                ON DUPLICATE KEY UPDATE affinity_value = VALUES(affinity_value)
                """,
                (str(user_id), int(affinity_value)),
            )
        self.conn.commit()

    def insert_message(self, group_id, sender_id, message_content,
                       reply_to_msg_id, msg_id, file_url=None,
                       file_type=None, created_at=None, is_self=False,
                       plain_text_content=None, message_structure=None,
                       attachments_json=None, audio_file_path=None,
                       audio_transcript=None):
        with self.conn.cursor() as cursor:
            sql = """
                INSERT INTO message_logs
                (group_id, sender_id, message_content, plain_text_content, message_structure,
                 attachments_json, reply_to_msg_id, msg_id, file_url, file_type,
                 audio_file_path, audio_transcript, created_at, is_self)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(sql, (
                group_id, sender_id, message_content, plain_text_content, message_structure,
                attachments_json, reply_to_msg_id, msg_id, file_url, file_type,
                audio_file_path, audio_transcript, created_at, is_self,
            ))
        self.conn.commit()

    def get_previous_non_self_message(self, group_id, exclude_msg_id=None):
        with self.conn.cursor() as cursor:
            if exclude_msg_id is None:
                cursor.execute(
                    "SELECT id, msg_id, message_content, plain_text_content "
                    "FROM message_logs WHERE group_id = %s AND is_self = 0 "
                    "ORDER BY id DESC LIMIT 1",
                    (group_id,),
                )
            else:
                cursor.execute(
                    "SELECT id, msg_id, message_content, plain_text_content "
                    "FROM message_logs WHERE group_id = %s AND is_self = 0 "
                    "AND msg_id <> %s ORDER BY id DESC LIMIT 1",
                    (group_id, str(exclude_msg_id)),
                )
            return cursor.fetchone()

    def get_recent_messages(self, group_id, limit=10):
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id AS _db_id, msg_id, sender_id, message_content, plain_text_content,
                       attachments_json, file_url, file_type, created_at, is_self
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
                """
                SELECT id AS _db_id, msg_id, sender_id, message_content, plain_text_content,
                       attachments_json, file_url, file_type, created_at, is_self
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
                """
                SELECT id AS _db_id, msg_id, sender_id, message_content, plain_text_content,
                       attachments_json, file_url, file_type, created_at, is_self
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
            sql = """
                SELECT msg_id, sender_id, message_content, plain_text_content,
                       attachments_json, file_url, file_type, created_at, is_self
                FROM message_logs
                WHERE group_id = %s AND id < %s
                ORDER BY id DESC
                LIMIT %s
            """
            params = (group_id, anchor_id, int(limit))
        else:
            sql = """
                SELECT msg_id, sender_id, message_content, plain_text_content,
                       attachments_json, file_url, file_type, created_at, is_self
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

    def extract_referenced_user_ids(self, text):
        if not text:
            return []
        return re.findall(r"\[(\d{5,15})\]", str(text))

    def insert_avatar_record(self, prompt_original, prompt_final,
                             revised_prompt, image_url, file_path,
                             status="generated", generated_at=None):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO avatar_records "
                "(prompt_original, prompt_final, revised_prompt, "
                "image_url, file_path, status, generated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (prompt_original, prompt_final, revised_prompt,
                 image_url, file_path, status, generated_at),
            )
        self.conn.commit()
        return cursor.lastrowid

    def get_latest_generated_avatar(self):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM avatar_records "
                "WHERE status = 'generated' "
                "ORDER BY generated_at DESC LIMIT 1"
            )
            return cursor.fetchone()

    def mark_avatar_applied(self, avatar_id, applied_at=None):
        with self.conn.cursor() as cursor:
            # Clear previous is_current flag on all records
            cursor.execute("UPDATE avatar_records SET is_current = 0")
            # Mark this avatar as applied and current
            cursor.execute(
                "UPDATE avatar_records SET status = 'applied', "
                "is_current = 1, applied_at = %s WHERE id = %s",
                (applied_at, avatar_id),
            )
        self.conn.commit()

    def mark_avatar_failed(self, avatar_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "UPDATE avatar_records SET status = 'failed' WHERE id = %s",
                (avatar_id,),
            )
        self.conn.commit()

    def get_avatar_by_id(self, avatar_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM avatar_records WHERE id = %s", (avatar_id,)
            )
            return cursor.fetchone()

    def get_avatars_since(self, since_time):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM avatar_records "
                "WHERE generated_at >= %s AND status != 'failed' "
                "ORDER BY generated_at DESC",
                (since_time,),
            )
            return cursor.fetchall()

    # --- Sticker methods ---

    def insert_sticker(self, name, description, avatar_id, file_path,
                       image_url, generated_at):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO stickers "
                "(name, description, avatar_id, file_path, image_url, generated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (name, description, avatar_id, file_path, image_url, generated_at),
            )
        self.conn.commit()
        return cursor.lastrowid

    def get_all_sticker_names(self):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT name, description FROM stickers "
                "GROUP BY name ORDER BY name"
            )
            return cursor.fetchall()

    def get_stickers_by_name(self, name):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM stickers WHERE name = %s ORDER BY created_at DESC",
                (name,),
            )
            return cursor.fetchall()

    def get_sticker_by_id(self, sticker_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM stickers WHERE id = %s", (sticker_id,)
            )
            return cursor.fetchone()

    def delete_sticker(self, sticker_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM stickers WHERE id = %s", (sticker_id,)
            )
        self.conn.commit()

    def get_current_avatar(self):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM avatar_records "
                "WHERE is_current = 1 AND status = 'applied' "
                "ORDER BY applied_at DESC LIMIT 1"
            )
            return cursor.fetchone()

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            logger.info("Database connection closed")
