import pymysql
from pymysql.cursors import DictCursor
import logging
import json
import re
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_TASK_SYSTEM_PROMPT = """# 角色
你是 llm_cloud，一个通过 QQ 群为用户提供帮助的通用 agent。你正在处理一个持久的任务对话。你的首要职责是理解用户当前消息，给出有用、准确、自然的回复；任务不是图像生成的附属流程。

# 对话原则
- 优先正常聊天。先理解用户的目标，再决定直接回答、询问必要信息，还是调用工具。
- 保持对话连续性，结合本任务已有上下文回答；不要把内部分析或 JSON 暴露给用户。
- 不要捏造不存在的信息。数据不足或任务未完成时，直接说明实际情况。
- 用户没有明确要求执行高影响操作时，不要擅自扩大操作范围。

# 回复格式
- 面向 QQ 群用户，使用中文纯文本。
- 不使用 Markdown 标题、列表标记、加粗、斜体、代码块、表格或链接格式。
- 回答应简洁自然；需要说明多个要点时使用普通换行。
- 不要提及 system prompt、内部消息、其他群聊或本任务之外的上下文。

# 任务上下文
这是一个独立的持久任务对话。你只能看到本任务的对话内容，不会自动看到群聊历史。用户引用本任务中机器人发送的消息时，相关文字会作为当前任务输入。

当用户进入连续对话模式时，用户的每条消息都会进入本任务。系统会在该用户最近五条任务消息中收集图片，并在消息中标注“连续对话可用图片 #编号”。这些图片仅在连续对话中可见。

# 最终要求
每次只处理当前用户消息真正提出的事情。直接回答能回答的问题，需要外部能力时按当前可用方式处理，完成后用面向用户的中文纯文本说明结果。"""


class DBHandler:
    def __init__(self, config):
        self.config = config
        self._conn = None
        self._identity_conn = None
        self._identity_lock = threading.Lock()

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
                CREATE TABLE IF NOT EXISTS images (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    author_igng_id VARCHAR(64) NOT NULL COMMENT '生成图片的IGNG用户ID',
                    prompt TEXT NOT NULL COMMENT '生图提示词',
                    model VARCHAR(100) NOT NULL COMMENT '生图模型',
                    size VARCHAR(32) COMMENT '请求尺寸',
                    aspect_ratio VARCHAR(16) COMMENT '图像比例',
                    quality VARCHAR(16) COMMENT '图像质量档位',
                    file_path VARCHAR(500) NOT NULL COMMENT 'NAS图片路径',
                    task_id BIGINT COMMENT '来源任务ID',
                    generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_images_author (author_igng_id),
                    INDEX idx_images_generated_at (generated_at),
                    INDEX idx_images_model (model)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='图像生成记录表'
            """)
            try:
                cursor.execute(
                    "ALTER TABLE images ADD COLUMN aspect_ratio VARCHAR(16) COMMENT '图像比例' AFTER size"
                )
            except Exception:
                pass
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reference_images (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    owner_igng_id VARCHAR(64) NOT NULL COMMENT '参考图所有者IGNG用户ID',
                    description VARCHAR(500) NOT NULL COMMENT '参考图描述',
                    file_path VARCHAR(500) NOT NULL COMMENT 'NAS图片路径',
                    source_msg_id VARCHAR(50) COMMENT '来源消息ID',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_reference_owner (owner_igng_id),
                    INDEX idx_reference_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='任务参考图表'
            """)
            try:
                cursor.execute("ALTER TABLE reference_images DROP COLUMN source_group_id")
            except Exception:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE images ADD COLUMN quality VARCHAR(16) COMMENT '图像质量档位' AFTER aspect_ratio"
                )
            except Exception:
                pass
            for sql in (
                "ALTER TABLE images ADD COLUMN archived_at DATETIME NULL AFTER generated_at",
                "ALTER TABLE images ADD COLUMN is_public TINYINT(1) NOT NULL DEFAULT 0 AFTER archived_at",
                "ALTER TABLE images ADD COLUMN avatar_submitted TINYINT(1) NOT NULL DEFAULT 0 AFTER is_public",
                "ALTER TABLE images ADD COLUMN sticker_submitted TINYINT(1) NOT NULL DEFAULT 0 AFTER avatar_submitted",
            ):
                try:
                    cursor.execute(sql)
                except Exception:
                    pass
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_user_permissions (
                    user_id VARCHAR(64) PRIMARY KEY,
                    permission_name VARCHAR(32) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS image_processing_tasks (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    image_id BIGINT NOT NULL,
                    task_type ENUM('avatar','sticker') NOT NULL,
                    status ENUM('pending','processing','completed','failed') NOT NULL DEFAULT 'pending',
                    error_message TEXT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at DATETIME NULL,
                    completed_at DATETIME NULL,
                    UNIQUE KEY uq_image_processing (image_id, task_type),
                    KEY idx_image_processing_pending (status, created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cursor.execute(
                "SELECT COUNT(*) AS table_count FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'avatars'"
            )
            has_avatars = bool(cursor.fetchone()["table_count"])
            cursor.execute(
                "SELECT COUNT(*) AS table_count FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'avatar_records'"
            )
            has_avatar_records = bool(cursor.fetchone()["table_count"])
            if not has_avatars and has_avatar_records:
                cursor.execute("RENAME TABLE avatar_records TO avatars")
                has_avatars = True
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS avatars (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    image_id BIGINT NOT NULL UNIQUE COMMENT '关联图像ID',
                    category VARCHAR(255) NOT NULL DEFAULT '未分类' COMMENT '头像分类',
                    INDEX idx_avatar_image (image_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='头像目录'
            """)
            for sql in (
                "ALTER TABLE avatars ADD COLUMN category VARCHAR(255) NOT NULL DEFAULT '未分类' COMMENT '头像分类' AFTER image_id",
                "ALTER TABLE avatars ADD INDEX idx_avatars_category (category)",
            ):
                try:
                    cursor.execute(sql)
                except Exception:
                    pass
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
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'stickers'"
            )
            sticker_columns = {row["COLUMN_NAME"] for row in cursor.fetchall()}
            cursor.execute(
                "SELECT COUNT(*) AS table_count FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sticker_images'"
            )
            has_sticker_images = bool(cursor.fetchone()["table_count"])
            if has_sticker_images and "description" in sticker_columns:
                cursor.execute("DROP TABLE IF EXISTS stickers_flat")
                cursor.execute("""
                    CREATE TABLE stickers_flat (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        category VARCHAR(255) NOT NULL COMMENT '表情分类',
                        image_id BIGINT NOT NULL COMMENT '关联图像ID',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_stickers_category (category),
                        INDEX idx_stickers_image (image_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='表情描述与图像目录'
                """)
                cursor.execute(
                    "INSERT INTO stickers_flat (category, image_id, created_at) "
                    "SELECT s.description, si.image_id, COALESCE(si.created_at, s.created_at) "
                    "FROM stickers AS s INNER JOIN sticker_images AS si ON si.sticker_id = s.id"
                )
                cursor.execute("DROP TABLE sticker_images")
                cursor.execute("DROP TABLE stickers")
                cursor.execute("RENAME TABLE stickers_flat TO stickers")
                sticker_columns = {"id", "category", "image_id", "created_at"}
            elif sticker_columns and (
                "name" in sticker_columns
                or "file_path" in sticker_columns
                or "image_url" in sticker_columns
            ):
                cursor.execute("DROP TABLE stickers")
                sticker_columns = set()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stickers (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    category VARCHAR(255) NOT NULL DEFAULT '未分类' COMMENT '表情分类',
                    image_id BIGINT NOT NULL COMMENT '关联图像ID',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='表情分类与图像目录'
            """)
            for sql in (
                "ALTER TABLE stickers ADD COLUMN image_id BIGINT NULL COMMENT '关联图像ID' AFTER description",
                "ALTER TABLE stickers ADD COLUMN category VARCHAR(255) NULL COMMENT '表情分类' AFTER id",
                "UPDATE stickers SET category = description WHERE (category IS NULL OR category = '') AND description IS NOT NULL",
                "ALTER TABLE stickers DROP INDEX description",
                "ALTER TABLE stickers DROP COLUMN description",
                "ALTER TABLE stickers ADD INDEX idx_stickers_category (category)",
            ):
                try:
                    cursor.execute(sql)
                except Exception:
                    pass
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    owner_igng_id VARCHAR(64) NOT NULL COMMENT '任务所属IGNG用户ID',
                    task_name VARCHAR(200) COMMENT '用户为任务设置的名称',
                    system_prompt MEDIUMTEXT COMMENT '任务专属系统提示词',
                    context_summary MEDIUMTEXT COMMENT '已压缩的任务上下文摘要',
                    context_summary_through BIGINT NOT NULL DEFAULT 0 COMMENT '摘要覆盖的任务消息数量',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_used_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    named_at DATETIME NULL,
                    INDEX idx_tasks_owner_last_used (owner_igng_id, last_used_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='任务表'
            """)
            try:
                cursor.execute(
                    "ALTER TABLE tasks ADD COLUMN owner_igng_id VARCHAR(64) NULL "
                    "COMMENT '任务所属IGNG用户ID' AFTER id"
                )
            except Exception:
                pass
            try:
                cursor.execute(
                    "ALTER TABLE tasks ADD INDEX idx_tasks_owner_last_used "
                    "(owner_igng_id, last_used_at)"
                )
            except Exception:
                pass
            try:
                with self._identity_lock:
                    identity_cursor = self._identity_connection().cursor()
                    identity_cursor.execute(
                        "SELECT id FROM users WHERE nickname = %s OR username = %s LIMIT 1",
                        ("IGNG", "IGNG"),
                    )
                    igng_user = identity_cursor.fetchone()
                    if igng_user:
                        cursor.execute(
                            "UPDATE tasks SET owner_igng_id = %s WHERE owner_igng_id IS NULL",
                            (str(igng_user["id"]),),
                        )
            except Exception as exc:
                logger.warning("Failed to assign existing tasks to IGNG user: %s", exc)
            try:
                cursor.execute(
                    "ALTER TABLE tasks MODIFY COLUMN owner_igng_id VARCHAR(64) NOT NULL "
                    "COMMENT '任务所属IGNG用户ID'"
                )
            except Exception:
                pass
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS task_messages (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    task_id BIGINT NOT NULL COMMENT '任务ID',
                    is_bot TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否为bot消息',
                    user_id VARCHAR(64) NOT NULL COMMENT '发送者IGNG用户ID',
                    message_role VARCHAR(16) NOT NULL COMMENT 'user/assistant/tool',
                    message_type VARCHAR(32) NOT NULL DEFAULT 'chat' COMMENT 'chat/system',
                    source_msg_id VARCHAR(50) DEFAULT NULL COMMENT 'OneBot来源消息ID',
                    image_refs MEDIUMTEXT COMMENT '任务消息中的图片引用列表',
                    image_id BIGINT DEFAULT NULL COMMENT '关联图像ID',
                    system_code VARCHAR(64) DEFAULT NULL COMMENT '系统消息标记',
                    task_status TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否为不传给LLM的状态消息',
                    message_content MEDIUMTEXT COMMENT '消息正文',
                    enable TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否传输给bot',
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_task_messages_task (task_id, id),
                    INDEX idx_task_messages_enabled (task_id, enable, id),
                    INDEX idx_task_messages_user (user_id),
                    INDEX idx_task_messages_source (source_msg_id),
                    CONSTRAINT fk_task_messages_task FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='任务消息记录'
            """)
            for sql in (
                "ALTER TABLE task_messages ADD COLUMN source_msg_id VARCHAR(50) DEFAULT NULL COMMENT 'OneBot来源消息ID' AFTER message_type",
                "ALTER TABLE task_messages ADD COLUMN image_refs MEDIUMTEXT COMMENT '任务消息中的图片引用列表' AFTER source_msg_id",
                "ALTER TABLE task_messages ADD COLUMN image_id BIGINT DEFAULT NULL COMMENT '关联图像ID' AFTER image_refs",
                "ALTER TABLE task_messages ADD COLUMN system_code VARCHAR(64) DEFAULT NULL COMMENT '系统消息标记' AFTER image_id",
                "ALTER TABLE task_messages ADD COLUMN task_status TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否为不传给LLM的状态消息' AFTER system_code",
                "ALTER TABLE task_messages ADD INDEX idx_task_messages_source (source_msg_id)",
            ):
                try:
                    cursor.execute(sql)
                except Exception:
                    pass
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'task_messages' "
                "AND COLUMN_NAME = 'message_json'"
            )
            if cursor.fetchone():
                cursor.execute(
                    "SELECT id, message_json FROM task_messages "
                    "WHERE message_json IS NOT NULL AND message_json <> ''"
                )
                for stored in cursor.fetchall():
                    try:
                        message = json.loads(stored["message_json"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        message = {}
                    if not isinstance(message, dict):
                        message = {}
                    image_refs = message.get("image_refs")
                    cursor.execute(
                        "UPDATE task_messages SET image_refs = %s, image_id = %s, "
                        "system_code = %s, task_status = %s WHERE id = %s",
                        (
                            json.dumps(image_refs, ensure_ascii=False) if image_refs else None,
                            message.get("image_id"),
                            message.get("system_message"),
                            1 if message.get("task_status") else 0,
                            stored["id"],
                        ),
                    )
                cursor.execute("ALTER TABLE task_messages DROP COLUMN message_json")
            # Backfill task image messages whose NAS path and message log are
            # still available. This makes old generated images quotable too.
            cursor.execute(
                "SELECT i.id AS image_id, i.task_id, t.owner_igng_id, "
                "m.msg_id FROM images AS i INNER JOIN tasks AS t ON t.id = i.task_id "
                "INNER JOIN message_logs AS m ON m.file_url = i.file_path "
                "WHERE i.task_id IS NOT NULL AND m.is_self = 1 "
                "AND NOT EXISTS (SELECT 1 FROM task_messages tm "
                "WHERE tm.source_msg_id = m.msg_id)"
            )
            for image_message in cursor.fetchall():
                message = {
                    "role": "assistant",
                    "content": "[图片]",
                    "image_id": image_message["image_id"],
                    "message_type": "system",
                    "system_message": "image_generation_image",
                }
                cursor.execute(
                    "INSERT INTO task_messages "
                    "(task_id, is_bot, user_id, message_role, message_type, source_msg_id, "
                    "image_id, system_code, message_content, enable, created_at) "
                    "VALUES (%s,1,%s,'assistant','system',%s,%s,%s,%s,1,NOW())",
                    (
                        image_message["task_id"],
                        str(image_message["owner_igng_id"]),
                        str(image_message["msg_id"]),
                        image_message["image_id"],
                        "image_generation_image",
                        "[图片]",
                    ),
                )
            # Migrate the legacy JSON transcript once. The old format had no
            # per-message timestamps, so migrated rows use the migration time.
            cursor.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'tasks' "
                "AND COLUMN_NAME = 'conversation_json'"
            )
            if cursor.fetchone():
                cursor.execute(
                    "SELECT t.id, t.owner_igng_id, t.conversation_json "
                    "FROM tasks AS t "
                    "WHERE t.conversation_json IS NOT NULL AND t.conversation_json <> '' "
                    "AND NOT EXISTS (SELECT 1 FROM task_messages tm WHERE tm.task_id = t.id LIMIT 1)"
                )
                legacy_tasks = list(cursor.fetchall())
                migration_time = datetime.now()
                for legacy in legacy_tasks:
                    try:
                        messages = json.loads(legacy["conversation_json"] or "[]")
                    except (TypeError, json.JSONDecodeError):
                        messages = []
                    if not isinstance(messages, list):
                        messages = []
                    for message in messages:
                        if not isinstance(message, dict):
                            continue
                        role = str(message.get("role") or "assistant")
                        is_bot = 0 if role == "user" else 1
                        message_type = (
                            "system"
                            if message.get("task_status") or message.get("system_message")
                            else "chat"
                        )
                        cursor.execute(
                            "INSERT INTO task_messages "
                            "(task_id, is_bot, user_id, message_role, message_type, "
                            "image_refs, image_id, system_code, task_status, "
                            "message_content, enable, created_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                legacy["id"],
                                is_bot,
                                str(legacy["owner_igng_id"]),
                                role,
                                message_type,
                                json.dumps(message.get("image_refs"), ensure_ascii=False)
                                if message.get("image_refs") else None,
                                message.get("image_id"),
                                message.get("system_message"),
                                1 if message.get("task_status") else 0,
                                str(message.get("content") or ""),
                                1 if message.get("enable", True) else 0,
                                migration_time,
                            ),
                        )
                cursor.execute("ALTER TABLE tasks DROP COLUMN conversation_json")
            for sql in (
                "ALTER TABLE tasks ADD COLUMN system_prompt MEDIUMTEXT COMMENT '任务专属系统提示词' AFTER task_name",
                "ALTER TABLE tasks ADD COLUMN context_summary MEDIUMTEXT COMMENT '已压缩的任务上下文摘要'",
                "ALTER TABLE tasks ADD COLUMN context_summary_through BIGINT NOT NULL DEFAULT 0 COMMENT '摘要覆盖的任务消息数量'",
            ):
                try:
                    cursor.execute(sql)
                except Exception:
                    pass
            for index_name in ("idx_tasks_owner_group", "idx_tasks_name"):
                try:
                    cursor.execute(f"ALTER TABLE tasks DROP INDEX {index_name}")
                except Exception:
                    pass
            for column_name in (
                "owner_id",
                "group_id",
                "task_type",
                "subject_name",
                "resolution",
                "status",
                "image_count",
            ):
                try:
                    cursor.execute(f"ALTER TABLE tasks DROP COLUMN {column_name}")
                except Exception:
                    pass
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
                CREATE TABLE IF NOT EXISTS system_prompts (
                    prompt_key VARCHAR(32) NOT NULL PRIMARY KEY COMMENT '提示词类型: chat/task',
                    prompt_text MEDIUMTEXT NOT NULL COMMENT '基础系统提示词',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统提示词配置'
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_system_prompt_attachments (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    prompt_text MEDIUMTEXT NOT NULL COMMENT '附加提示词正文',
                    owner_qq BIGINT NOT NULL COMMENT '提交者QQ',
                    status VARCHAR(16) NOT NULL DEFAULT 'enabled' COMMENT 'enabled/disabled',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_chat_spa_status (status),
                    INDEX idx_chat_spa_owner (owner_qq)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='聊天模式用户附加系统提示词'
            """)
            chat_seed_path = Path(self.config.PROMPT_DIR) / "system.txt"
            try:
                chat_seed = chat_seed_path.read_text(encoding="utf-8").split("\n# 工具使用", 1)[0].strip()
            except OSError:
                chat_seed = ""
            for prompt_key, prompt_text in (("chat", chat_seed), ("task", DEFAULT_TASK_SYSTEM_PROMPT)):
                if prompt_text:
                    cursor.execute(
                        "INSERT IGNORE INTO system_prompts (prompt_key, prompt_text) VALUES (%s, %s)",
                        (prompt_key, prompt_text),
                    )
            cursor.execute(
                "UPDATE tasks AS t INNER JOIN system_prompts AS p "
                "ON p.prompt_key = 'task' SET t.system_prompt = p.prompt_text "
                "WHERE t.system_prompt IS NULL OR t.system_prompt = ''"
            )
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
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_groups (
                    igng_user_id VARCHAR(64) NOT NULL PRIMARY KEY COMMENT 'IGNG用户ID',
                    group_name VARCHAR(16) NOT NULL DEFAULT 'plus' COMMENT '用户组: admin/pro/plus',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_user_groups_name (group_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='IGNG用户组'
            """)
            try:
                cursor.execute(
                    "UPDATE user_groups SET group_name = 'admin' WHERE is_bot_admin = 1"
                )
            except Exception:
                pass
            try:
                cursor.execute("ALTER TABLE user_groups DROP COLUMN is_bot_admin")
            except Exception:
                pass
            self._seed_default_user_groups(cursor)
        self.conn.commit()
        logger.info("Database tables initialized")

    def get_system_prompt(self, prompt_key):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT prompt_text FROM system_prompts WHERE prompt_key = %s LIMIT 1",
                (str(prompt_key),),
            )
            row = cursor.fetchone()
        return (row or {}).get("prompt_text", "")

    def list_chat_system_prompt_attachments(self, status=None):
        """List chat-mode system prompt attachments, optionally filtered by status."""
        with self.conn.cursor() as cursor:
            if status:
                cursor.execute(
                    "SELECT id, prompt_text, owner_qq, status, created_at, updated_at "
                    "FROM chat_system_prompt_attachments WHERE status = %s ORDER BY id ASC",
                    (status,),
                )
            else:
                cursor.execute(
                    "SELECT id, prompt_text, owner_qq, status, created_at, updated_at "
                    "FROM chat_system_prompt_attachments ORDER BY id ASC"
                )
            return list(cursor.fetchall())

    def get_enabled_chat_system_prompt_attachments(self):
        return self.list_chat_system_prompt_attachments(status="enabled")

    def get_chat_system_prompt_attachment(self, attachment_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, prompt_text, owner_qq, status, created_at, updated_at "
                "FROM chat_system_prompt_attachments WHERE id = %s LIMIT 1",
                (int(attachment_id),),
            )
            return cursor.fetchone()

    def add_chat_system_prompt_attachment(self, prompt_text, owner_qq):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO chat_system_prompt_attachments (prompt_text, owner_qq, status) "
                "VALUES (%s, %s, 'enabled')",
                (prompt_text, int(owner_qq)),
            )
            attachment_id = cursor.lastrowid
        self.conn.commit()
        return attachment_id

    def disable_chat_system_prompt_attachment(self, attachment_id):
        """Soft-disable an attachment by id. Returns the row if found, else None."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, prompt_text, owner_qq, status FROM chat_system_prompt_attachments "
                "WHERE id = %s LIMIT 1",
                (int(attachment_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            if row.get("status") == "disabled":
                return row
            cursor.execute(
                "UPDATE chat_system_prompt_attachments SET status = 'disabled' WHERE id = %s",
                (int(attachment_id),),
            )
        self.conn.commit()
        row["status"] = "disabled"
        return row

    def update_chat_system_prompt_attachment(self, attachment_id, prompt_text):
        """Update attachment text. Returns updated row if found, else None."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, prompt_text, owner_qq, status FROM chat_system_prompt_attachments "
                "WHERE id = %s LIMIT 1",
                (int(attachment_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            cursor.execute(
                "UPDATE chat_system_prompt_attachments SET prompt_text = %s WHERE id = %s",
                (prompt_text, int(attachment_id)),
            )
        self.conn.commit()
        row["prompt_text"] = prompt_text
        return row

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
                    cursor.execute("SELECT 1 FROM global_admins WHERE user_id = %s LIMIT 1", (str(igng_user_id),))
                    legacy_global_admin = cursor.fetchone() is not None
                    cursor.execute("SELECT role FROM mc_report_admins WHERE user_id = %s LIMIT 1", (str(igng_user_id),))
                    legacy_mc_admin = cursor.fetchone()
        except Exception as exc:
            logger.warning("Unified permission center unavailable for user %s: %s", igng_user_id, exc)
            return None

        group_by_id = {int(row["id"]): row["code"] for row in groups}
        effective_group_ids = {int(row["id"]) for row in memberships}
        if legacy_global_admin and "platform.superadmin" in group_by_id.values():
            effective_group_ids.add(next(group_id for group_id, code in group_by_id.items() if code == "platform.superadmin"))
        if legacy_mc_admin and legacy_mc_admin.get("role") == "ADMIN" and "mc.report.admin" in group_by_id.values():
            effective_group_ids.add(next(group_id for group_id, code in group_by_id.items() if code == "mc.report.admin"))
        if legacy_mc_admin and legacy_mc_admin.get("role") == "SUPERADMIN" and "platform.superadmin" in group_by_id.values():
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

        # Temporary compatibility fallback for an installation before the
        # central migration has been applied. Elevated admin checks never use
        # this fallback.
        igng_user_id = self.resolve_bound_igng_account_id(user_id)
        if igng_user_id is None:
            return "plus"
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT group_name FROM user_groups WHERE igng_user_id = %s LIMIT 1",
                (str(igng_user_id),),
            )
            row = cursor.fetchone()
        return row.get("group_name") if row and row.get("group_name") in ("admin", "pro", "plus") else "plus"

    def is_bot_admin(self, user_id):
        snapshot = self._get_unified_permission_snapshot(user_id)
        if snapshot is None:
            return False
        return self._snapshot_has_permission(snapshot, "yunying.manage")

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

    def _seed_default_user_groups(self, bot_cursor):
        """Do not assign permissions by username; the central table is authoritative."""
        return None

    def set_user_group(self, igng_user_id, group_name):
        """Assign a Yunying tier in the central permission center.

        The old igng_bot.user_groups row is updated as a compatibility cache,
        but it is not consulted when the central tables are available.
        """
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

            with self.conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO user_groups (igng_user_id, group_name) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE group_name = VALUES(group_name)",
                    (target_id, group_name),
                )
            self.conn.commit()
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
        config_key = self.resolve_igng_account_id(user_id)
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT IGNORE INTO user_config
                (user_id, affinity_value, affinity_enabled)
                VALUES (%s, 50, 1)
                """,
                (config_key,),
            )
        self.conn.commit()

    def _identity_connection(self):
        if self._identity_conn is None:
            self._identity_conn = pymysql.connect(
                host=self.config.MC_REPORT_IDENTITY_DB_HOST,
                port=self.config.MC_REPORT_IDENTITY_DB_PORT,
                user=self.config.MC_REPORT_IDENTITY_DB_USER,
                password=self.config.MC_REPORT_IDENTITY_DB_PASSWORD,
                database=self.config.MC_REPORT_IDENTITY_DB_NAME,
                charset="utf8mb4",
                cursorclass=DictCursor,
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

    def get_user_configs_batch(self, user_ids):
        if not user_ids:
            return {}
        original_ids = [str(uid) for uid in user_ids]
        account_ids = {uid: self.resolve_igng_account_id(uid) for uid in original_ids}
        lookup_ids = list(dict.fromkeys([*original_ids, *account_ids.values()]))
        placeholders = ",".join(["%s"] * len(lookup_ids))
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT user_id, affinity_value, affinity_enabled
                FROM user_config
                WHERE user_id IN ({placeholders})
                """,
                lookup_ids,
            )
            rows = {str(row["user_id"]): row for row in cursor.fetchall()}
        result = {}
        for uid in original_ids:
            account_id = account_ids[uid]
            row = rows.get(account_id) or rows.get(uid)
            if row is None:
                self.ensure_user_config(account_id)
                row = {"user_id": account_id, "affinity_value": 50, "affinity_enabled": 1}
            elif account_id not in rows:
                with self.conn.cursor() as cursor:
                    cursor.execute(
                        "INSERT IGNORE INTO user_config "
                        "(user_id, affinity_value, affinity_enabled) VALUES (%s, %s, %s)",
                        (account_id, row["affinity_value"], row["affinity_enabled"]),
                    )
                self.conn.commit()
                row = dict(row)
                row["user_id"] = account_id
            result[uid] = row
        return result

    def update_affinity(self, user_id, affinity_value):
        config_key = self.resolve_igng_account_id(user_id)
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO user_config (user_id, affinity_value, affinity_enabled)
                VALUES (%s, %s, 1)
                ON DUPLICATE KEY UPDATE affinity_value = VALUES(affinity_value)
                """,
                (config_key, int(affinity_value)),
            )
        self.conn.commit()

    def insert_message(self, group_id, sender_id, message_content,
                       reply_to_msg_id, msg_id, file_url=None,
                       file_type=None, created_at=None, is_self=False,
                       plain_text_content=None, message_structure=None,
                       attachments_json=None, audio_file_path=None,
                       audio_transcript=None):
        sql = """
            INSERT INTO message_logs
            (group_id, sender_id, message_content, plain_text_content, message_structure,
             attachments_json, reply_to_msg_id, msg_id, file_url, file_type,
             audio_file_path, audio_transcript, created_at, is_self)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        values = (
            group_id, sender_id, message_content, plain_text_content, message_structure,
            attachments_json, reply_to_msg_id, msg_id, file_url, file_type,
            audio_file_path, audio_transcript, created_at, is_self,
        )
        for attempt in range(2):
            try:
                with self.conn.cursor() as cursor:
                    cursor.execute(sql, values)
                self.conn.commit()
                return
            except Exception:
                if attempt == 1:
                    raise
                logger.warning("Message insert failed; reconnecting and retrying once")
                self.connect()

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

    # --- Image task methods ---

    def create_task(self, owner_igng_id, task_name, conversation_json=None):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT prompt_text FROM system_prompts WHERE prompt_key = 'task' LIMIT 1"
            )
            system_prompt_row = cursor.fetchone()
            cursor.execute(
                "INSERT INTO tasks (owner_igng_id, task_name, system_prompt) VALUES (%s, %s, %s)",
                (
                    str(owner_igng_id),
                    task_name,
                    (system_prompt_row or {}).get("prompt_text") or DEFAULT_TASK_SYSTEM_PROMPT,
                ),
            )
            task_id = cursor.lastrowid
            if task_name:
                cursor.execute(
                    "UPDATE tasks SET named_at = NOW() WHERE id = %s",
                    (task_id,),
                )
        self.conn.commit()
        if conversation_json:
            try:
                messages = json.loads(conversation_json)
            except (TypeError, json.JSONDecodeError):
                messages = []
            for message in messages if isinstance(messages, list) else []:
                if isinstance(message, dict):
                    self.append_task_message(task_id, owner_igng_id, message)
        return task_id

    def _load_task_conversation(self, task_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT message_role, message_type, image_refs, image_id, system_code, "
                "task_status, message_content FROM task_messages "
                "WHERE task_id = %s AND enable = 1 ORDER BY id",
                (int(task_id),),
            )
            rows = cursor.fetchall()
        conversation = []
        for row in rows:
            message = {
                "role": row["message_role"],
                "content": row.get("message_content") or "",
            }
            if row.get("message_type") == "system":
                message["message_type"] = "system"
            if row.get("image_refs"):
                try:
                    refs = json.loads(row["image_refs"])
                except (TypeError, json.JSONDecodeError):
                    refs = []
                if isinstance(refs, list):
                    message["image_refs"] = refs
            if row.get("image_id") is not None:
                message["image_id"] = row["image_id"]
            if row.get("system_code"):
                message["system_message"] = row["system_code"]
            if row.get("task_status"):
                message["task_status"] = True
            conversation.append(message)
        return conversation

    def append_task_message(
        self, task_id, owner_igng_id, message, created_at=None, source_msg_id=None
    ):
        if not isinstance(message, dict):
            return None
        role = str(message.get("role") or "assistant")
        is_bot = 0 if role == "user" else 1
        message_type = (
            "system"
            if message.get("task_status")
            or message.get("system_message")
            or message.get("message_type") == "system"
            else "chat"
        )
        user_id = str(message.get("user_id") or owner_igng_id)
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM tasks WHERE id = %s AND owner_igng_id = %s LIMIT 1",
                (int(task_id), str(owner_igng_id)),
            )
            if not cursor.fetchone():
                return None
            cursor.execute(
                "INSERT INTO task_messages "
                "(task_id, is_bot, user_id, message_role, message_type, source_msg_id, "
                "image_refs, image_id, system_code, task_status, message_content, enable, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    int(task_id),
                    is_bot,
                    user_id,
                    role,
                    message_type,
                    str(source_msg_id) if source_msg_id is not None else None,
                    json.dumps(message.get("image_refs"), ensure_ascii=False)
                    if message.get("image_refs") else None,
                    message.get("image_id"),
                    message.get("system_message"),
                    1 if message.get("task_status") else 0,
                    str(message.get("content") or ""),
                    1 if message.get("enable", True) else 0,
                    created_at or datetime.now(),
                ),
            )
            message_id = cursor.lastrowid
            cursor.execute(
                "UPDATE tasks SET last_used_at = NOW() WHERE id = %s",
                (int(task_id),),
            )
        self.conn.commit()
        return message_id

    def set_task_messages_enabled(self, task_id, owner_igng_id, enabled):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "UPDATE task_messages tm INNER JOIN tasks t ON t.id = tm.task_id "
                "SET tm.enable = %s WHERE tm.task_id = %s AND t.owner_igng_id = %s",
                (1 if enabled else 0, int(task_id), str(owner_igng_id)),
            )
        self.conn.commit()

    def get_task(self, task_id, owner_igng_id=None):
        with self.conn.cursor() as cursor:
            sql = "SELECT * FROM tasks WHERE id = %s"
            params = [int(task_id)]
            if owner_igng_id is not None:
                sql += " AND owner_igng_id = %s"
                params.append(str(owner_igng_id))
            cursor.execute(sql, tuple(params))
            row = cursor.fetchone()
        if not row:
            return None
        row = dict(row)
        row["conversation_json"] = json.dumps(
            self._load_task_conversation(row["id"]), ensure_ascii=False
        )
        return row

    def get_latest_task(self, owner_igng_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM tasks WHERE owner_igng_id = %s "
                "ORDER BY last_used_at DESC, id DESC LIMIT 1",
                (str(owner_igng_id),),
            )
            row = cursor.fetchone()
        if not row:
            return None
        row = dict(row)
        row["conversation_json"] = json.dumps(
            self._load_task_conversation(row["id"]), ensure_ascii=False
        )
        return row

    def find_task_by_assistant_text_global(
        self, owner_igng_id, message_text, source_msg_id=None
    ):
        if source_msg_id:
            with self.conn.cursor() as cursor:
                cursor.execute(
                    "SELECT t.* FROM tasks AS t INNER JOIN task_messages AS tm "
                    "ON tm.task_id = t.id WHERE t.owner_igng_id = %s "
                    "AND tm.source_msg_id = %s ORDER BY tm.id DESC LIMIT 1",
                    (str(owner_igng_id), str(source_msg_id)),
                )
                row = cursor.fetchone()
            if row:
                row = dict(row)
                row["conversation_json"] = json.dumps(
                    self._load_task_conversation(row["id"]), ensure_ascii=False
                )
                return row
        if not message_text:
            return None
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM tasks WHERE owner_igng_id = %s "
                "ORDER BY last_used_at DESC, id DESC LIMIT 100",
                (str(owner_igng_id),),
            )
            task_ids = [row["id"] for row in cursor.fetchall()]
        rows = [self.get_task(task_id, owner_igng_id) for task_id in task_ids]
        for row in rows:
            try:
                conversation = json.loads(row.get("conversation_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            for item in conversation:
                content = str(item.get("content") or "")
                if item.get("role") == "assistant" and content:
                    if (
                        content == message_text
                        or (len(content) >= 8 and content in message_text)
                        or (len(message_text) >= 8 and message_text in content)
                    ):
                        return row
        return None

    def update_task(self, task_id, owner_igng_id, conversation_json):
        try:
            desired = json.loads(conversation_json or "[]")
        except (TypeError, json.JSONDecodeError):
            desired = []
        current = self._load_task_conversation(task_id)
        common = 0
        while common < len(current) and common < len(desired) and current[common] == desired[common]:
            common += 1
        for message in desired[common:]:
            self.append_task_message(task_id, owner_igng_id, message)
        with self.conn.cursor() as cursor:
            cursor.execute(
                "UPDATE tasks SET last_used_at = NOW() WHERE id = %s AND owner_igng_id = %s",
                (int(task_id), str(owner_igng_id)),
            )
        self.conn.commit()

    def update_task_context(
        self,
        task_id,
        owner_igng_id,
        conversation_json,
        context_summary,
        context_summary_through,
    ):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "UPDATE task_messages tm INNER JOIN tasks t ON t.id = tm.task_id "
                "SET tm.enable = 0 WHERE tm.task_id = %s AND t.owner_igng_id = %s",
                (int(task_id), str(owner_igng_id)),
            )
            cursor.execute(
                "UPDATE tasks SET context_summary = %s, "
                "context_summary_through = %s, last_used_at = NOW() "
                "WHERE id = %s AND owner_igng_id = %s",
                (
                    context_summary,
                    int(context_summary_through),
                    int(task_id),
                    str(owner_igng_id),
                ),
            )
        self.conn.commit()
        try:
            recent_messages = json.loads(conversation_json or "[]")
        except (TypeError, json.JSONDecodeError):
            recent_messages = []
        for message in recent_messages if isinstance(recent_messages, list) else []:
            if isinstance(message, dict):
                self.append_task_message(task_id, owner_igng_id, message)

    def rename_task_global(self, owner_igng_id, task_id, task_name):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "UPDATE tasks SET task_name = %s, named_at = NOW(), last_used_at = NOW() "
                "WHERE id = %s AND owner_igng_id = %s",
                (task_name, int(task_id), str(owner_igng_id)),
            )
            changed = cursor.rowcount > 0
        self.conn.commit()
        return changed

    def list_named_tasks_global(self, owner_igng_id, keyword=None, limit=50):
        sql = (
            "SELECT id, task_name, created_at, last_used_at FROM tasks "
            "WHERE owner_igng_id = %s AND task_name IS NOT NULL"
        )
        params = [str(owner_igng_id)]
        if keyword:
            sql += " AND task_name LIKE %s"
            params.append(f"%{keyword}%")
        sql += " ORDER BY last_used_at DESC, id DESC LIMIT %s"
        params.append(int(limit))
        with self.conn.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            return list(cursor.fetchall())

    def extract_referenced_user_ids(self, text):
        if not text:
            return []
        return re.findall(r"\[(\d{5,15})\]", str(text))

    # --- Image catalog methods ---

    def insert_image(self, author_igng_id, prompt, model, size, file_path,
                     generated_at=None, task_id=None, aspect_ratio=None, quality=None):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO images "
                "(author_igng_id, prompt, model, size, aspect_ratio, quality, file_path, task_id, generated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    str(author_igng_id), prompt, model, size, aspect_ratio, quality,
                    file_path, task_id, generated_at,
                ),
            )
            image_id = cursor.lastrowid
        self.conn.commit()
        return image_id

    def get_image_by_id(self, image_id):
        with self.conn.cursor() as cursor:
            cursor.execute("SELECT * FROM images WHERE id = %s", (int(image_id),))
        return cursor.fetchone()

    def insert_reference_image(
        self, owner_igng_id, description, file_path, source_msg_id=None
    ):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO reference_images "
                "(owner_igng_id, description, file_path, source_msg_id) "
                "VALUES (%s, %s, %s, %s)",
                (
                    str(owner_igng_id), description, file_path,
                    source_msg_id,
                ),
            )
            reference_id = cursor.lastrowid
        self.conn.commit()
        return reference_id

    def get_reference_images(self, owner_igng_id, reference_ids):
        ids = []
        for value in reference_ids or []:
            try:
                reference_id = int(value)
            except (TypeError, ValueError):
                continue
            if reference_id > 0 and reference_id not in ids:
                ids.append(reference_id)
        if not ids:
            return []
        placeholders = ",".join(["%s"] * len(ids))
        with self.conn.cursor() as cursor:
            cursor.execute(
                f"SELECT id, owner_igng_id, description, file_path, "
                f"source_msg_id, created_at FROM reference_images "
                f"WHERE owner_igng_id = %s AND id IN ({placeholders}) "
                "ORDER BY id",
                (str(owner_igng_id), *ids),
            )
            return list(cursor.fetchall())

    def add_avatar(self, image_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO avatars (image_id, category) VALUES (%s, '未分类')",
                (int(image_id),),
            )
            avatar_id = cursor.lastrowid
        self.conn.commit()
        return avatar_id

    def get_avatar_by_id(self, avatar_id, author_igng_id=None):
        with self.conn.cursor() as cursor:
            owner_clause = " AND i.author_igng_id = %s" if author_igng_id is not None else ""
            params = [int(avatar_id)]
            if author_igng_id is not None:
                params.append(str(author_igng_id))
            cursor.execute(
                "SELECT a.id, a.image_id, a.category, i.author_igng_id, i.prompt, i.model, i.size, i.aspect_ratio, i.quality, "
                "i.file_path, i.task_id, t.task_name, i.generated_at "
                "FROM avatars AS a INNER JOIN images AS i ON i.id = a.image_id "
                "LEFT JOIN tasks AS t ON t.id = i.task_id "
                f"WHERE a.id = %s{owner_clause}",
                tuple(params),
            )
            row = cursor.fetchone()
        return self._with_author_name(row)

    def get_avatars(self, author_igng_id=None, limit=50):
        with self.conn.cursor() as cursor:
            owner_clause = " WHERE i.author_igng_id = %s" if author_igng_id is not None else ""
            params = []
            if author_igng_id is not None:
                params.append(str(author_igng_id))
            params.append(int(limit))
            cursor.execute(
                "SELECT a.id, a.image_id, a.category, i.author_igng_id, i.prompt, i.model, i.size, i.aspect_ratio, i.quality, "
                "i.file_path, i.task_id, t.task_name, i.generated_at "
                "FROM avatars AS a INNER JOIN images AS i ON i.id = a.image_id "
                "LEFT JOIN tasks AS t ON t.id = i.task_id "
                f"{owner_clause} ORDER BY i.generated_at DESC, a.id DESC LIMIT %s",
                tuple(params),
            )
            rows = list(cursor.fetchall())
        return [self._with_author_name(row) for row in rows]

    def _with_author_name(self, row):
        if not row:
            return row
        row = dict(row)
        row["author"] = row.get("author_igng_id") or "未知"
        try:
            with self._identity_lock:
                with self._identity_connection().cursor() as cursor:
                    cursor.execute(
                        "SELECT nickname, username FROM users WHERE id = %s LIMIT 1",
                        (row.get("author_igng_id"),),
                    )
                    user = cursor.fetchone()
            if user:
                row["author"] = user.get("nickname") or user.get("username") or row["author"]
        except Exception as exc:
            logger.debug("Failed to resolve image author %s: %s", row.get("author_igng_id"), exc)
        return row

    def get_avatars_since(self, since_time):
        rows = self.get_avatars(limit=500)
        return [row for row in rows if row.get("generated_at") and row["generated_at"] >= since_time]

    def get_all_sticker_names(self):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT MIN(id) AS id, category AS name, category "
                "FROM stickers GROUP BY category ORDER BY category"
            )
            return list(cursor.fetchall())

    def get_stickers_by_category(self, category, author_igng_id=None):
        with self.conn.cursor() as cursor:
            owner_clause = " AND i.author_igng_id = %s" if author_igng_id is not None else ""
            params = [category]
            if author_igng_id is not None:
                params.append(str(author_igng_id))
            cursor.execute(
                "SELECT s.image_id AS id, s.id AS sticker_id, s.category AS name, "
                "s.category, i.author_igng_id, i.prompt, i.model, i.size, i.aspect_ratio, i.quality, i.file_path, "
                "i.task_id, t.task_name, i.generated_at "
                "FROM stickers AS s INNER JOIN images AS i ON i.id = s.image_id "
                "LEFT JOIN tasks AS t ON t.id = i.task_id "
                f"WHERE s.category = %s{owner_clause} "
                "ORDER BY i.generated_at DESC, s.image_id DESC",
                tuple(params),
            )
            rows = list(cursor.fetchall())
        return [self._with_author_name(row) for row in rows]

    def get_sticker_by_id(self, image_id):
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT s.image_id AS id, s.id AS sticker_id, s.category AS name, "
                "s.category, i.author_igng_id, i.prompt, i.model, i.size, i.aspect_ratio, i.quality, i.file_path, "
                "i.task_id, t.task_name, i.generated_at "
                "FROM stickers AS s INNER JOIN images AS i ON i.id = s.image_id "
                "LEFT JOIN tasks AS t ON t.id = i.task_id "
                "WHERE s.image_id = %s",
                (int(image_id),),
            )
            row = cursor.fetchone()
        return self._with_author_name(row)

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
