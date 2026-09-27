from types import SimpleNamespace
from unittest.mock import patch

from igngbot_v3.onebot_client import OneBotClient


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
