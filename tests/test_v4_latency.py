from datetime import datetime, timezone

from igngbot_v4.latency import _span


def test_latency_span_contract_normalizes_dates_and_drops_content_fields():
    span = _span({
        "span_id": "model:fixture:1",
        "trace_id": "tr_0123456789abcdef0123456789abcdef",
        "task_key": "dsh:session:turn:1",
        "span_type": "model_attempt",
        "started_at": "2026-10-13T00:00:00.123456Z",
        "ended_at": datetime(2026, 10, 13, 0, 0, 1, tzinfo=timezone.utc),
        "attributes": {
            "provider": "fixture",
            "ttftMs": 120,
            "prompt": "must not cross the boundary",
            "response": "must not cross the boundary",
        },
    })
    assert span["started_at"] == datetime(2026, 10, 13, 0, 0, 0, 123456)
    assert span["ended_at"] == datetime(2026, 10, 13, 0, 0, 1)
    assert span["attributes"] == {"provider": "fixture", "ttftMs": 120}
