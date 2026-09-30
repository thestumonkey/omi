"""Text-timestamp repair script for data left by the Rust desktop backend. [fork-only]"""

from datetime import datetime, timezone

from scripts.selfhosted_fix_text_dates import fix_document, parse_text_date


def test_naive_text_is_read_as_utc():
    assert parse_text_date("2026-06-25T10:17:51.049000") == datetime(
        2026, 6, 25, 10, 17, 51, 49000, tzinfo=timezone.utc
    )
    assert parse_text_date("2026-06-25T10:17:51Z").tzinfo is not None
    assert parse_text_date("not a date") is None
    assert parse_text_date(datetime.now()) is None


def test_nested_list_paths_are_fixed_and_real_dates_kept():
    real = datetime(2026, 1, 1, tzinfo=timezone.utc)
    doc = {
        "created_at": "2026-06-25T10:17:51",
        "started_at": real,
        "structured": {"title": "x", "action_items": [{"due_at": "2026-07-01T09:00:00"}, {"due_at": None}]},
    }
    update, count = fix_document(doc, ["created_at", "started_at", "structured.action_items[].due_at"])
    assert count == 2
    assert set(update) == {"created_at", "structured"}
    assert update["structured"]["action_items"][0]["due_at"].year == 2026
    assert update["structured"]["title"] == "x"
