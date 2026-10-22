from types import SimpleNamespace
from unittest.mock import patch

from igngbot_v3.onebot_client import OneBotClient


def test_connection_status_reflects_open_close_and_stop():
    config = SimpleNamespace(
        ONEBOT_NAME="fixture",
        ONEBOT_WS_URL="ws://fixture",
        ONEBOT_ACCESS_TOKEN="",
    )
    client = OneBotClient(config, lambda _message: None)

    assert client.status()["connected"] is False
    assert "self_id" not in client.status()
    client._on_open(None)
    assert client.status()["connected"] is True

    client._on_close(None, 1000, "bye")
    assert client.status()["connected"] is False

    client._on_open(None)
    client.stop()
    assert client.status()["connected"] is False


def test_websocket_client_sends_authorization_as_header_line():
    config = SimpleNamespace(
        ONEBOT_NAME="fixture",
        ONEBOT_WS_URL="ws://fixture",
        ONEBOT_ACCESS_TOKEN="secret-token",
    )
    client = OneBotClient(config, lambda _message: None)

    with patch("igngbot_v3.onebot_client.websocket.WebSocketApp") as websocket_app:
        client._connect()

    websocket_app.assert_called_once()
    assert websocket_app.call_args.kwargs["header"] == ["Authorization: Bearer secret-token"]
    websocket_app.return_value.run_forever.assert_called_once_with()
