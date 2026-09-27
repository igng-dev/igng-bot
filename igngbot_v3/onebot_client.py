import json
import logging
import time

import websocket

logger = logging.getLogger(__name__)


class OneBotClient:
    def __init__(self, config, message_callback, recall_callback=None):
        self.config = config
        self.message_callback = message_callback
        self.recall_callback = recall_callback
        self.ws = None
        self._running = False
        self.self_id = None
        self._retry_delay = 1
        self._max_retry_delay = 300
        self._stable_connection_seconds = 30
        self._last_error_log = 0.0
        self._last_error_message = None
        self._last_close_log = 0.0
        self._consecutive_failures = 0

    def start(self):
        self._running = True
        while self._running:
            started_at = time.monotonic()
            try:
                self._connect()
            except Exception as e:
                reason = f"exception: {e}"
            else:
                reason = "connection closed"
            finally:
                self.ws = None

            if not self._running:
                break

            connected_for = time.monotonic() - started_at
            if connected_for >= self._stable_connection_seconds:
                self._retry_delay = 1
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1

            self._log_retry(reason, connected_for)
            self._sleep_with_stop(self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, self._max_retry_delay)

    def stop(self):
        self._running = False
        if self.ws:
            self.ws.close()

    def _connect(self):
        access_token = str(getattr(self.config, "ONEBOT_ACCESS_TOKEN", "") or "").strip()
        headers = [f"Authorization: Bearer {access_token}"] if access_token else []
        self.ws = websocket.WebSocketApp(
            self.config.ONEBOT_WS_URL,
            header=headers,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        logger.info(
            "Connecting to %s at %s",
            self.config.ONEBOT_NAME,
            self.config.ONEBOT_WS_URL,
        )
        self.ws.run_forever()

    def _log_retry(self, reason, connected_for):
        should_log = (
            self._consecutive_failures <= 3
            or self._retry_delay >= 60
            or self._consecutive_failures % 10 == 0
        )
        if should_log:
            logger.warning(
                "%s disconnected (%s). connected_for=%.1fs, retry #%s in %ss",
                self.config.ONEBOT_NAME,
                reason,
                connected_for,
                self._consecutive_failures,
                self._retry_delay,
            )

    def _sleep_with_stop(self, seconds):
        end_time = time.monotonic() + seconds
        while self._running and time.monotonic() < end_time:
            time.sleep(min(1, end_time - time.monotonic()))

    def _on_message(self, ws, raw):
        try:
            data = json.loads(raw)
            post_type = data.get("post_type")

            if post_type == "meta_event":
                meta_type = data.get("meta_event_type", "")
                if meta_type == "lifecycle" and not self.self_id:
                    self.self_id = data.get("self_id")
                    logger.info(f"Bot self_id detected: {self.self_id}")
                return

            if post_type in ("message", "message_sent"):
                msg_type = data.get("message_type", "?")
                group_id = data.get("group_id", "?")
                is_self = post_type == "message_sent" or data.get("message_sent_type") == "self"
                if is_self and not self.self_id:
                    self.self_id = data.get("self_id") or data.get("user_id")
                # Some OneBot implementations omit self_id from message_sent
                # events even though it was provided by the lifecycle event.
                # Pass the known identity downstream so persistence can use the
                # bot QQ rather than a private-message recipient.
                if self.self_id not in (None, ""):
                    data.setdefault("self_id", self.self_id)
                logger.info(
                    f"WS recv: post_type={post_type} message_type={msg_type} "
                    f"group_id={group_id} self={is_self}"
                )
                self.message_callback(data)
            elif post_type == "notice" and data.get("notice_type") == "group_recall":
                group_id = data.get("group_id")
                message_id = data.get("message_id")
                if group_id in (None, "") or message_id in (None, ""):
                    logger.warning(
                        "Ignoring malformed group_recall notice: group_id=%r message_id=%r",
                        group_id,
                        message_id,
                    )
                    return
                logger.info(
                    "WS recv: post_type=notice notice_type=group_recall "
                    "group_id=%s message_id=%s operator_id=%s user_id=%s",
                    group_id,
                    message_id,
                    data.get("operator_id"),
                    data.get("user_id"),
                )
                if self.recall_callback is not None:
                    self.recall_callback(data)
                else:
                    logger.debug("No group recall callback configured; notice ignored")
            else:
                logger.debug(f"WS recv: post_type={post_type} (ignored)")

        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)

    def _on_open(self, ws):
        self._retry_delay = 1
        self._consecutive_failures = 0
        logger.info("Connected to %s WebSocket", self.config.ONEBOT_NAME)

    def _on_error(self, ws, error):
        error_text = str(error)
        now = time.monotonic()
        if error_text != self._last_error_message or now - self._last_error_log >= 60:
            logger.warning("WebSocket error: %s", error_text)
            self._last_error_message = error_text
            self._last_error_log = now

    def _on_close(self, ws, close_status_code, close_msg):
        now = time.monotonic()
        if now - self._last_close_log >= 30:
            logger.info("WebSocket closed (%s): %s", close_status_code, close_msg)
            self._last_close_log = now
