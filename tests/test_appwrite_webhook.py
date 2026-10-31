from __future__ import annotations

import importlib.util
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "appwrite" / "functions" / "webhook" / "src" / "main.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("appwrite_user_sync_main", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRes:
    def __init__(self) -> None:
        self.payload = None
        self.status = None

    def json(self, payload, status=200):  # noqa: ANN001
        self.payload = payload
        self.status = status
        return payload


class FakeSentMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "API_ID",
        "API_HASH",
        "SESSION_STRING",
        "SOURCE_CHANNEL_ID",
        "SOURCE_CHANNEL_IDS",
        "SOURCE_CHANNEL_USERNAMES",
        "DESTINATION_CHANNEL_ID",
        "LIMIT_PER_SOURCE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_load_settings_requires_session_string(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    clear_env(monkeypatch)
    monkeypatch.setenv("API_ID", "123456")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("SOURCE_CHANNEL_ID", "-1001")
    monkeypatch.setenv("DESTINATION_CHANNEL_ID", "-1002")

    with pytest.raises(ValueError, match="SESSION_STRING is required"):
        module._load_settings_from_env()


def test_load_settings_with_multiple_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    clear_env(monkeypatch)
    monkeypatch.setenv("API_ID", "123456")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("SESSION_STRING", "session")
    monkeypatch.setenv("SOURCE_CHANNEL_IDS", "-1001,-1002,-1003")
    monkeypatch.setenv("DESTINATION_CHANNEL_ID", "-2001")
    monkeypatch.setenv("LIMIT_PER_SOURCE", "25")

    settings = module._load_settings_from_env()

    assert settings.api_id == 123456
    assert settings.source_channel_ids == (-1001, -1002, -1003)
    assert settings.destination_channel_id == -2001
    assert settings.limit_per_source == 25


def test_group_message_ids_keeps_album_boundaries() -> None:
    module = load_module()
    messages = [
        SimpleNamespace(id=1, grouped_id=None),
        SimpleNamespace(id=2, grouped_id=10),
        SimpleNamespace(id=3, grouped_id=10),
        SimpleNamespace(id=4, grouped_id=None),
        SimpleNamespace(id=5, grouped_id=11),
    ]

    grouped = module._group_message_ids(messages)

    assert grouped == [(False, [1]), (True, [2, 3]), (False, [4]), (True, [5])]


def test_sanitize_message_text_preserves_links_by_default() -> None:
    module = load_module()
    text = "visit https://t.me/some/path and https://t.me/+qKumi0QiYOsyMDY9 now"
    assert module._sanitize_message_text(text) == text


def test_sanitize_message_text_removes_telegram_link_variants_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    monkeypatch.setenv("STRIP_T_LINKS", "1")
    text = (
        "one t.me/somechannel two telegram.me/joinchat/abc "
        "three www.t.me/+invitecode four https://www.telegram.me/c/123/456"
    )
    assert module._sanitize_message_text(text) == "one two three four"


def test_sanitize_message_text_removes_bad_words_and_youtube_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    monkeypatch.setenv("STRIP_YOUTUBE_LINKS", "1")
    monkeypatch.setenv("FILTER_BAD_WORDS", "mc,bc")
    text = "hello MC world bc see https://youtube.com/watch?v=1 and youtube.com/test"
    assert module._sanitize_message_text(text) == "hello world see and"


def test_should_skip_message_text_blocks_ad_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    assert module._should_skip_message_text("#ad InsideAds (https://t.me/InsideAds_bot/open?startapp=abc)")
    assert module._should_skip_message_text("insideAds special")
    assert not module._should_skip_message_text("this is an ad")
    monkeypatch.setenv("BLOCK_GENERIC_AD_WORD", "1")
    assert module._should_skip_message_text("this is an ad")
    assert not module._should_skip_message_text("this is a normal update")


def test_is_gif_message_detects_flag() -> None:
    module = load_module()
    assert module._is_gif_message(SimpleNamespace(gif=True))
    assert not module._is_gif_message(SimpleNamespace(gif=False, media=None))


def test_is_gif_message_detects_mp4_animation_variant() -> None:
    module = load_module()

    class DocumentAttributeVideo:
        def __init__(self) -> None:
            self.supports_streaming = False

    media = SimpleNamespace(
        document=SimpleNamespace(
            mime_type="video/mp4",
            attributes=[DocumentAttributeVideo()],
        )
    )
    assert module._is_gif_message(SimpleNamespace(gif=False, media=media))


def test_main_returns_error_payload_on_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    clear_env(monkeypatch)
    res = FakeRes()
    context = SimpleNamespace(req=SimpleNamespace(), res=res)

    module.main(context)

    assert res.status == 500
    assert res.payload["ok"] is False


def test_load_state_does_not_use_telegram_fallback_by_default() -> None:
    module = load_module()

    class FakeClient:
        async def iter_messages(self, *args, **kwargs):  # noqa: ANN001, ANN003
            raise AssertionError("telegram fallback should be disabled by default")

        async def get_messages(self, *args, **kwargs):  # noqa: ANN001, ANN003
            raise AssertionError("telegram fallback should be disabled by default")

    state, state_id = asyncio.run(module._load_state(client=FakeClient()))
    assert state == {}
    assert state_id is None


def test_fetch_recent_messages_respects_cutoff_not_last_seen() -> None:
    module = load_module()
    now = datetime.now(timezone.utc)

    class FakeClient:
        async def iter_messages(self, source_id):  # noqa: ANN001
            assert source_id == -1001
            yield SimpleNamespace(id=110, date=now - timedelta(minutes=5))
            yield SimpleNamespace(id=109, date=now - timedelta(minutes=50))
            yield SimpleNamespace(id=100, date=now - timedelta(minutes=1))
            yield SimpleNamespace(id=108, date=now - timedelta(minutes=70))

    result = asyncio.run(
        module._fetch_recent_messages(
            client=FakeClient(),
            source_id=-1001,
            last_seen_id=100,
            cutoff_dt=now - timedelta(minutes=60),
        )
    )

    assert [msg.id for msg in result] == [100, 109, 110]


def test_copy_source_messages_forwards_each_group(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return [SimpleNamespace(id=1, message="", grouped_id=10), SimpleNamespace(id=2, message="", grouped_id=11)]

    forward_mock = AsyncMock(return_value=(1, 0, (1,)))
    run_snapshot_mock = AsyncMock(return_value=[99])

    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_fetch_recent_messages", fake_fetch_recent_messages)
    monkeypatch.setattr(module, "_load_recent_run_snapshot_ids", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "_forward_with_fallback", forward_mock)
    monkeypatch.setattr(module, "_save_run_window_ids", run_snapshot_mock)

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
    )

    result = asyncio.run(module._copy_source_messages(client=SimpleNamespace(), settings=settings))

    assert forward_mock.await_count == 2
    assert result["copied"] == 2


def test_copy_source_messages_skips_ad_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    now = datetime.now(timezone.utc)

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return [
            SimpleNamespace(id=1, message="#ad InsideAds link", grouped_id=None, date=now),
            SimpleNamespace(id=2, message="normal message", grouped_id=None, date=now),
        ]

    forward_mock = AsyncMock(return_value=(1, 0, (2,)))
    run_snapshot_mock = AsyncMock(return_value=[99])

    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_fetch_recent_messages", fake_fetch_recent_messages)
    monkeypatch.setattr(module, "_load_recent_run_snapshot_ids", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "_forward_with_fallback", forward_mock)
    monkeypatch.setattr(module, "_save_run_window_ids", run_snapshot_mock)

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
    )

    result = asyncio.run(module._copy_source_messages(client=SimpleNamespace(), settings=settings))

    assert forward_mock.await_count == 1
    assert result["copied"] == 1
    assert result["skipped_invalid"] == 1
    assert result["skipped_blocked_ad"] == 1
    assert result["skipped_blocked_gif"] == 0


def test_copy_source_messages_skips_gif_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    now = datetime.now(timezone.utc)

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return [
            SimpleNamespace(id=1, message="gif here", grouped_id=None, date=now, gif=True),
            SimpleNamespace(id=2, message="normal message", grouped_id=None, date=now, gif=False),
        ]

    forward_mock = AsyncMock(return_value=(1, 0, (2,)))
    run_snapshot_mock = AsyncMock(return_value=[99])

    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_fetch_recent_messages", fake_fetch_recent_messages)
    monkeypatch.setattr(module, "_load_recent_run_snapshot_ids", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "_forward_with_fallback", forward_mock)
    monkeypatch.setattr(module, "_save_run_window_ids", run_snapshot_mock)

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
    )

    result = asyncio.run(module._copy_source_messages(client=SimpleNamespace(), settings=settings))

    assert forward_mock.await_count == 1
    assert result["copied"] == 1
    assert result["skipped_invalid"] == 1
    assert result["skipped_blocked_gif"] == 1


def test_build_state_payload_compacts_when_too_large() -> None:
    module = load_module()
    huge_errors = {f"-100{i}": "x" * 1200 for i in range(20)}
    state = {
        "sources": {
            "-1001": {"last_id": 123, "recent": list(range(1, 100))},
            "-1002": {"last_id": 456, "recent": list(range(1, 120))},
        },
        "meta": {
            "status": "completed_with_errors",
            "last_run_ts_ms": 1234567890,
            "source_errors": huge_errors,
        },
    }

    payload = module._build_state_payload(state)

    assert payload.startswith(module.STATE_MARKER)
    assert len(payload) <= module.MAX_STATE_PAYLOAD_CHARS
    parsed = module._extract_state(payload)
    assert parsed["sources"]["-1001"]["last_id"] == 123
    assert len(parsed["sources"]["-1001"]["recent"]) <= module.MAX_RECENT_IDS
    assert "source_errors" in parsed["meta"]
    assert len(parsed["meta"]["source_errors"]) <= 8


def test_save_run_window_ids_logs_to_backend_and_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()

    class FakeClient:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send_message(self, entity, message):  # noqa: ANN001
            assert entity == "me"
            self.messages.append(message)
            return FakeSentMessage(len(self.messages))

    client = FakeClient()
    ids_by_source = {"-1001": list(range(1, 3000))}
    logged_payloads: list[dict] = []

    def fake_log(log_data):  # noqa: ANN001
        logged_payloads.append(log_data)
        return True

    monkeypatch.setattr(module, "log_copier_execution", fake_log)

    sent_ids = asyncio.run(
        module._save_run_window_ids(
            client=client,
            run_ts_ms=123,
            lookback_minutes=60,
            run_status="completed",
            ids_by_source=ids_by_source,
        )
    )

    assert len(sent_ids) >= 2
    assert len(logged_payloads) == len(sent_ids)
    assert len(client.messages) == 0
    assert all(payload["function_name"] == module.DEFAULT_FUNCTION_NAME for payload in logged_payloads)
    assert all(payload["source_id"] == -1001 for payload in logged_payloads)


def test_log_copier_execution_retries_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_FUNCTION_API_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_FUNCTION_PROJECT_ID", "proj")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_LOGS_COLLECTION_ID", "logs")

    calls = {"count": 0}

    def flaky_create_document(database_id, collection_id, data):  # noqa: ANN001
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return {"$id": "x"}

    monkeypatch.setattr(module, "_appwrite_create_document", flaky_create_document)

    ok = module.log_copier_execution({"function_name": "x"})

    assert ok is True
    assert calls["count"] == 2


def test_appwrite_api_key_prefers_function_env(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.delenv("APPWRITE_API_KEY", raising=False)
    monkeypatch.setenv("APPWRITE_FUNCTION_API_KEY", "fn_key")

    assert module._appwrite_api_key() == "fn_key"


def test_appwrite_create_document_falls_back_to_tablesdb(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    calls: list[str] = []

    def fake_request_json(method, path, payload=None, queries=None):  # noqa: ANN001
        calls.append(path)
        if path.startswith("/databases/"):
            raise RuntimeError("legacy endpoint not available")
        return {"$id": "row_1"}

    monkeypatch.setattr(module, "_appwrite_request_json", fake_request_json)

    created = module._appwrite_create_document(database_id="db1", collection_id="logs", data={"x": 1})

    assert created["$id"] == "row_1"
    assert calls[0] == "/databases/db1/collections/logs/documents"
    assert calls[1] == "/tablesdb/db1/tables/logs/rows"


def test_appwrite_create_document_retries_on_row_already_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    calls = {"tables": 0}

    def fake_request_json(method, path, payload=None, queries=None):  # noqa: ANN001
        if path.startswith("/databases/"):
            raise RuntimeError("legacy endpoint not available")
        calls["tables"] += 1
        if calls["tables"] == 1:
            raise RuntimeError("row_already_exists")
        return {"$id": "row_ok"}

    monkeypatch.setattr(module, "_appwrite_request_json", fake_request_json)

    created = module._appwrite_create_document(database_id="db1", collection_id="logs", data={"x": 1})

    assert created["$id"] == "row_ok"
    assert calls["tables"] == 2


def test_appwrite_create_document_prefers_tables_auto_id(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()

    def fake_request_json(method, path, payload=None, queries=None):  # noqa: ANN001
        if path.startswith("/databases/"):
            raise RuntimeError("legacy endpoint not available")
        if path.startswith("/tablesdb/") and payload == {"data": {"x": 1}}:
            return {"$id": "auto_row"}
        raise RuntimeError("unexpected payload")

    monkeypatch.setattr(module, "_appwrite_request_json", fake_request_json)

    created = module._appwrite_create_document(database_id="db1", collection_id="logs", data={"x": 1})
    assert created["$id"] == "auto_row"


def test_appwrite_list_documents_falls_back_to_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    calls: list[str] = []

    def fake_request_json(method, path, payload=None, queries=None):  # noqa: ANN001
        calls.append(path)
        if path.startswith("/databases/"):
            raise RuntimeError("legacy endpoint not available")
        return {"rows": [{"$id": "r1"}]}

    monkeypatch.setattr(module, "_appwrite_request_json", fake_request_json)

    rows = module._appwrite_list_documents(database_id="db1", collection_id="logs", queries=["limit(1)"])

    assert rows == [{"$id": "r1"}]
    assert calls[0] == "/databases/db1/collections/logs/documents"
    assert calls[1] == "/tablesdb/db1/tables/logs/rows"


def test_appwrite_build_url_handles_endpoint_without_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://fra.cloud.appwrite.io")

    url = module._appwrite_build_url("/health")

    assert url == "https://fra.cloud.appwrite.io/v1/health"


def test_appwrite_build_url_handles_endpoint_with_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://fra.cloud.appwrite.io/v1")

    url = module._appwrite_build_url("/health")

    assert url == "https://fra.cloud.appwrite.io/v1/health"


def test_strip_unknown_attribute_from_error() -> None:
    module = load_module()
    payload = {"a": 1, "created_at": "x"}
    changed = module._strip_unknown_attribute_from_error(
        payload, 'RuntimeError: HTTP 400 Bad Request: {"message":"Unknown attribute: \\"created_at\\""}'
    )
    assert changed is True
    assert "created_at" not in payload


def test_log_copier_execution_retries_after_unknown_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_PROJECT_ID", "proj")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_LOGS_COLLECTION_ID", "logs")

    calls = {"count": 0}
    seen_payloads: list[dict] = []

    def fake_create_document(database_id, collection_id, data):  # noqa: ANN001
        calls["count"] += 1
        seen_payloads.append(dict(data))
        if calls["count"] == 1:
            raise RuntimeError('HTTP 400 Bad Request: {"message":"Unknown attribute: \\"created_at\\""}')
        return {"$id": "ok"}

    monkeypatch.setattr(module, "_appwrite_create_document", fake_create_document)

    ok = module.log_copier_execution(
        {
            "function_name": "telegram_copier_run_v1",
            "run_ts_ms": 1,
            "status": "completed",
            "lookback_minutes": 60,
            "source_id": -1001,
            "part": "1/1",
            "message_ids": "1,2",
            "created_at": "2026-03-07T00:00:00+00:00",
        }
    )

    assert ok is True
    assert calls["count"] == 2
    assert "created_at" in seen_payloads[0]
    assert "created_at" not in seen_payloads[1]


def test_adjust_json_field_by_error_array_type() -> None:
    module = load_module()
    payload = {"message_ids_json": "[1,2,3]"}
    changed = module._adjust_json_field_by_error(
        payload,
        "message_ids_json",
        'HTTP 400 Bad Request: {"message":"Attribute \\"message_ids_json\\" must be an array"}',
    )
    assert changed is True
    assert payload["message_ids_json"] == [1, 2, 3]


def test_adjust_json_field_by_error_string_type() -> None:
    module = load_module()
    payload = {"payload_json": {"sources": {}}}
    changed = module._adjust_json_field_by_error(
        payload,
        "payload_json",
        'HTTP 400 Bad Request: {"message":"Attribute \\"payload_json\\" must be a string"}',
    )
    assert changed is True
    assert isinstance(payload["payload_json"], str)


def test_adjust_json_field_by_error_array_item_must_be_string() -> None:
    module = load_module()
    payload = {"message_ids_json": [1, 2, 3]}
    changed = module._adjust_json_field_by_error(
        payload,
        "message_ids_json",
        'HTTP 400 Bad Request: {"message":"Attribute \\"message_ids_json[\'0\']\\" has invalid type. Value must be a valid string"}',
    )
    assert changed is True
    assert payload["message_ids_json"] == ["1", "2", "3"]


def test_adjust_json_field_by_error_valid_string_phrase() -> None:
    module = load_module()
    payload = {"payload_json": {"sources": {}}}
    changed = module._adjust_json_field_by_error(
        payload,
        "payload_json",
        'HTTP 400 Bad Request: {"message":"Attribute \\"payload_json\\" has invalid type. Value must be a valid string"}',
    )
    assert changed is True
    assert isinstance(payload["payload_json"], str)


def test_parse_run_snapshot_message() -> None:
    module = load_module()
    text = (
        "telegram_copier_run_v1:"
        "run_ts_ms=123;status=completed;lookback_minutes=60;source_id=-1001;part=1/1;ids=11,12,13"
    )
    parsed = module._parse_run_snapshot_message(text)
    assert parsed == (123, "-1001", [11, 12, 13])


def test_load_recent_run_snapshot_ids_filters_by_time() -> None:
    module = load_module()
    now_ms = 1_700_000_000_000

    class FakeClient:
        async def iter_messages(self, entity, search=None, limit=None):  # noqa: ANN001
            assert entity == "me"
            assert search == module.RUN_MARKER
            yield SimpleNamespace(
                message=(
                    f"{module.RUN_MARKER}"
                    f"run_ts_ms={now_ms};status=completed;lookback_minutes=60;"
                    "source_id=-1001;part=1/1;ids=1,2"
                )
            )
            yield SimpleNamespace(
                message=(
                    f"{module.RUN_MARKER}"
                    f"run_ts_ms={now_ms - (70 * 60 * 1000)};status=completed;lookback_minutes=60;"
                    "source_id=-1001;part=1/1;ids=3,4"
                )
            )

    original_time = module.time.time
    module.time.time = lambda: now_ms / 1000
    try:
        result = asyncio.run(module._load_recent_run_snapshot_ids(client=FakeClient(), lookback_minutes=60))
    finally:
        module.time.time = original_time

    assert result == {"-1001": {1, 2}}


def test_load_recent_run_snapshot_ids_uses_message_ids_json(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_PROJECT_ID", "proj")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_LOGS_COLLECTION_ID", "logs")

    now_ms = 1_700_000_000_000
    monkeypatch.setattr(module.time, "time", lambda: now_ms / 1000)

    def fake_list_documents(database_id, collection_id, queries):  # noqa: ANN001
        return [
            {
                "run_ts_ms": now_ms,
                "source_id": -1001,
                "message_ids_json": [7, 8, 9],
            }
        ]

    monkeypatch.setattr(module, "_appwrite_list_documents", fake_list_documents)

    result = asyncio.run(module._load_recent_run_snapshot_ids(client=SimpleNamespace(), lookback_minutes=60))

    assert result == {"-1001": {7, 8, 9}}


def test_load_recent_run_snapshot_ids_uses_message_ids_json_string(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_PROJECT_ID", "proj")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_LOGS_COLLECTION_ID", "logs")

    now_ms = 1_700_000_000_000
    monkeypatch.setattr(module.time, "time", lambda: now_ms / 1000)

    def fake_list_documents(database_id, collection_id, queries):  # noqa: ANN001
        return [
            {
                "run_ts_ms": now_ms,
                "source_id": -1001,
                "message_ids_json": "[10,11,12]",
            }
        ]

    monkeypatch.setattr(module, "_appwrite_list_documents", fake_list_documents)

    result = asyncio.run(module._load_recent_run_snapshot_ids(client=SimpleNamespace(), lookback_minutes=60))

    assert result == {"-1001": {10, 11, 12}}


def test_copy_source_messages_includes_run_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    now = datetime.now(timezone.utc)

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return [SimpleNamespace(id=101, message="hello", grouped_id=None, date=now)]

    forward_mock = AsyncMock(return_value=(1, 0, (101,)))
    run_snapshot_mock = AsyncMock(return_value=[11, 12])

    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_fetch_recent_messages", fake_fetch_recent_messages)
    monkeypatch.setattr(module, "_load_recent_run_snapshot_ids", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "_forward_with_fallback", forward_mock)
    monkeypatch.setattr(module, "_save_run_window_ids", run_snapshot_mock)

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
    )

    result = asyncio.run(module._copy_source_messages(client=SimpleNamespace(), settings=settings))

    assert run_snapshot_mock.await_count == 1
    assert result["run_snapshot_count"] == 2
    assert result["run_snapshot_message_ids"] == [11, 12]


def test_load_state_prefers_payload_json(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_PROJECT_ID", "proj")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_STATE_COLLECTION_ID", "state")

    state_doc = {"$id": "doc_1", "payload_json": {"sources": {"-1001": {"last_id": 123, "recent": []}}}}

    def fake_list_documents(database_id, collection_id, queries):  # noqa: ANN001
        return [state_doc]

    monkeypatch.setattr(module, "_appwrite_list_documents", fake_list_documents)

    state, doc_id = asyncio.run(module._load_state(client=SimpleNamespace()))

    assert doc_id == "doc_1"
    assert state["sources"]["-1001"]["last_id"] == 123


def test_load_state_accepts_payload_json_string(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_PROJECT_ID", "proj")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_STATE_COLLECTION_ID", "state")

    state_doc = {"$id": "doc_2", "payload_json": '{"sources":{"-1002":{"last_id":456,"recent":[]}}}'}

    def fake_list_documents(database_id, collection_id, queries):  # noqa: ANN001
        return [state_doc]

    monkeypatch.setattr(module, "_appwrite_list_documents", fake_list_documents)

    state, doc_id = asyncio.run(module._load_state(client=SimpleNamespace()))

    assert doc_id == "doc_2"
    assert state["sources"]["-1002"]["last_id"] == 456


def test_save_state_clears_stale_error_on_successful_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_STATE_COLLECTION_ID", "state")

    module.DB_STATE_LAST_ERROR = "stale_error"

    def fake_request_json(method, path, payload=None, queries=None):  # noqa: ANN001
        assert method == "PATCH"
        assert path == "/tablesdb/db/tables/state/rows/state_global_v1"
        return {"$id": "state_global_v1"}

    monkeypatch.setattr(module, "_appwrite_request_json", fake_request_json)

    state_id, did_update = asyncio.run(
        module._save_state(
            client=SimpleNamespace(),
            state={"sources": {"-1001": {"last_id": 1, "recent": []}}},
            state_message_id="state_global_v1",
        )
    )

    assert did_update is True
    assert state_id == "state_global_v1"
    assert module.DB_STATE_LAST_ERROR == ""


def test_save_state_reports_real_db_error_after_retry_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_STATE_COLLECTION_ID", "state")
    monkeypatch.setenv("ENABLE_TELEGRAM_STATE_FALLBACK", "0")

    def fake_request_json(method, path, payload=None, queries=None):  # noqa: ANN001
        raise RuntimeError("HTTP 409 Conflict: row_already_exists")

    monkeypatch.setattr(module, "_appwrite_request_json", fake_request_json)

    state_id, did_update = asyncio.run(
        module._save_state(
            client=SimpleNamespace(),
            state={"sources": {"-1001": {"last_id": 1, "recent": []}}},
            state_message_id="state_global_v1",
        )
    )

    assert did_update is False
    assert state_id == "state_global_v1"
    assert "row_already_exists" in module.DB_STATE_LAST_ERROR
    assert "db_state_unavailable_and_telegram_fallback_disabled" not in module.DB_STATE_LAST_ERROR


def test_save_state_handles_legacy_document_already_exists_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_STATE_COLLECTION_ID", "state")

    calls = {"count": 0}

    def fake_request_json(method, path, payload=None, queries=None):  # noqa: ANN001
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("HTTP 404 Not Found")
        if calls["count"] == 2:
            raise RuntimeError("HTTP 404 Not Found")
        if calls["count"] == 3:
            raise RuntimeError("HTTP 409 Conflict: document_already_exists")
        return {"$id": "state_global_v1"}

    monkeypatch.setattr(module, "_appwrite_request_json", fake_request_json)

    state_id, did_update = asyncio.run(
        module._save_state(
            client=SimpleNamespace(),
            state={"sources": {"-1001": {"last_id": 1, "recent": []}}},
            state_message_id="state_global_v1",
        )
    )

    assert did_update is True
    assert state_id == "state_global_v1"
    assert module.DB_STATE_LAST_ERROR == ""


def test_save_state_recovers_from_unique_state_key_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_STATE_COLLECTION_ID", "state")
    calls: list[tuple[str, str]] = []

    def fake_request_json(method, path, payload=None, queries=None):  # noqa: ANN001
        calls.append((method, path))
        if len(calls) == 1:
            raise RuntimeError("HTTP 404 Not Found")
        if len(calls) == 2:
            raise RuntimeError("HTTP 404 Not Found")
        if len(calls) == 3:
            raise RuntimeError(
                'HTTP 409 Conflict: {"type":"document_unique_constraint_violation",'
                '"message":"Document violates a unique attribute constraint."}'
            )
        assert method == "PATCH"
        assert path.endswith("/rows/existing_state")
        return {"$id": "existing_state"}

    monkeypatch.setattr(module, "_appwrite_request_json", fake_request_json)
    monkeypatch.setattr(
        module,
        "_appwrite_list_documents",
        lambda database_id, collection_id, queries: [{"$id": "existing_state", "state_key": "global"}],
    )

    state_id, did_update = asyncio.run(
        module._save_state(
            client=SimpleNamespace(),
            state={"sources": {"-1001": {"last_id": 1, "recent": []}}},
            state_message_id="state_global_v1",
        )
    )

    assert state_id == "existing_state"
    assert did_update is True
    assert module.DB_STATE_LAST_ERROR == ""


def test_prime_source_entities_loads_dialogs_for_fresh_session() -> None:
    module = load_module()
    calls = {"input": 0, "dialogs": 0}

    async def get_input_entity(source_id):  # noqa: ANN001
        calls["input"] += 1
        if calls["input"] == 1:
            raise ValueError("Could not find input entity")
        return f"input-peer:{source_id}"

    async def get_dialogs(limit=None):  # noqa: ANN001
        calls["dialogs"] += 1
        assert limit is None
        return []

    resolved, errors = asyncio.run(
        module._prime_source_entities(
            SimpleNamespace(get_input_entity=get_input_entity, get_dialogs=get_dialogs),
            (-1001,),
        )
    )

    assert resolved == {-1001: "input-peer:-1001"}
    assert errors == {}
    assert calls["dialogs"] == 1


def test_prime_source_entities_reports_inaccessible_source_without_aborting_others() -> None:
    module = load_module()

    async def get_input_entity(source_id):  # noqa: ANN001
        if source_id == -1001:
            raise ValueError("missing entity")
        return f"input-peer:{source_id}"

    async def get_dialogs(limit=None):  # noqa: ANN001
        return []

    resolved, errors = asyncio.run(
        module._prime_source_entities(
            SimpleNamespace(get_input_entity=get_input_entity, get_dialogs=get_dialogs),
            (-1001, -1002),
        )
    )

    assert resolved == {-1002: "input-peer:-1002"}
    assert "-1001" in errors
    assert "dialogs" in errors["-1001"] or "resolve source" in errors["-1001"]


def test_prime_source_entities_can_use_ordered_public_usernames(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("SOURCE_CHANNEL_USERNAMES", "public_source")

    async def get_input_entity(value):  # noqa: ANN001
        if value == -1001:
            raise ValueError("missing numeric entity")
        return f"input-peer:{value}"

    async def get_dialogs(limit=None):  # noqa: ANN001
        raise AssertionError("username resolution should avoid loading all dialogs")

    resolved, errors = asyncio.run(
        module._prime_source_entities(
            SimpleNamespace(get_input_entity=get_input_entity, get_dialogs=get_dialogs),
            (-1001,),
        )
    )

    assert resolved == {-1001: "input-peer:public_source"}
    assert errors == {}


def test_copy_source_messages_merges_snapshot_ids_with_live_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("ENABLE_SNAPSHOT_RECOVERY", "1")

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    now = datetime.now(timezone.utc)

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return [SimpleNamespace(id=101, message="live", grouped_id=None, date=now)]

    async def fake_load_recent_run_snapshot_ids(client, lookback_minutes):  # noqa: ANN001
        return {"-1001": {101, 102}}

    async def fake_get_messages(source_id, ids):  # noqa: ANN001
        assert source_id == -1001
        assert ids == [102]
        return [SimpleNamespace(id=102, message="recovered", grouped_id=None, date=now)]

    forward_mock = AsyncMock(side_effect=[(1, 0, (101,)), (1, 0, (102,))])
    run_snapshot_mock = AsyncMock(return_value=[9])

    client = SimpleNamespace(get_messages=fake_get_messages)

    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_fetch_recent_messages", fake_fetch_recent_messages)
    monkeypatch.setattr(module, "_load_recent_run_snapshot_ids", fake_load_recent_run_snapshot_ids)
    monkeypatch.setattr(module, "_forward_with_fallback", forward_mock)
    monkeypatch.setattr(module, "_save_run_window_ids", run_snapshot_mock)

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
    )

    result = asyncio.run(module._copy_source_messages(client=client, settings=settings))

    assert forward_mock.await_count == 2
    assert result["copied"] == 2


def test_copy_source_messages_skips_ids_at_or_below_last_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("ENABLE_CURSOR_GATE", "1")

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {"-1001": {"last_id": 200, "recent": []}}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    now = datetime.now(timezone.utc)

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return [
            SimpleNamespace(id=190, message="old", grouped_id=None, date=now),
            SimpleNamespace(id=200, message="edge", grouped_id=None, date=now),
            SimpleNamespace(id=201, message="new", grouped_id=None, date=now),
        ]

    forward_mock = AsyncMock(return_value=(1, 0, (201,)))
    run_snapshot_mock = AsyncMock(return_value=[9])

    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_fetch_recent_messages", fake_fetch_recent_messages)
    monkeypatch.setattr(module, "_load_recent_run_snapshot_ids", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "_forward_with_fallback", forward_mock)
    monkeypatch.setattr(module, "_save_run_window_ids", run_snapshot_mock)

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
    )

    result = asyncio.run(module._copy_source_messages(client=SimpleNamespace(), settings=settings))

    assert forward_mock.await_count == 1
    assert result["copied"] == 1


def test_copy_source_messages_ignores_recovered_id_below_last_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("ENABLE_SNAPSHOT_RECOVERY", "1")

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {"-1001": {"last_id": 200, "recent": []}}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    now = datetime.now(timezone.utc)

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return [SimpleNamespace(id=201, message="new", grouped_id=None, date=now)]

    async def fake_load_recent_run_snapshot_ids(client, lookback_minutes):  # noqa: ANN001
        return {"-1001": {199}}

    async def fake_get_messages(source_id, ids):  # noqa: ANN001
        assert ids == [199]
        return [SimpleNamespace(id=199, message="recovered", grouped_id=None, date=now)]

    forward_mock = AsyncMock(side_effect=[(1, 0, (201,))])
    run_snapshot_mock = AsyncMock(return_value=[9])

    client = SimpleNamespace(get_messages=fake_get_messages)

    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_fetch_recent_messages", fake_fetch_recent_messages)
    monkeypatch.setattr(module, "_load_recent_run_snapshot_ids", fake_load_recent_run_snapshot_ids)
    monkeypatch.setattr(module, "_forward_with_fallback", forward_mock)
    monkeypatch.setattr(module, "_save_run_window_ids", run_snapshot_mock)

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
    )

    result = asyncio.run(module._copy_source_messages(client=client, settings=settings))

    assert forward_mock.await_count == 1
    assert result["copied"] == 1


def test_copy_source_messages_self_heals_when_cursor_ahead(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("ENABLE_CURSOR_GATE", "1")

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {"-1001": {"last_id": 500, "recent": []}}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    now = datetime.now(timezone.utc)

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return [
            SimpleNamespace(id=101, message="a", grouped_id=None, date=now),
            SimpleNamespace(id=102, message="b", grouped_id=None, date=now),
        ]

    forward_mock = AsyncMock(side_effect=[(1, 0, (101,)), (1, 0, (102,))])
    run_snapshot_mock = AsyncMock(return_value=[9])

    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_fetch_recent_messages", fake_fetch_recent_messages)
    monkeypatch.setattr(module, "_load_recent_run_snapshot_ids", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "_forward_with_fallback", forward_mock)
    monkeypatch.setattr(module, "_save_run_window_ids", run_snapshot_mock)

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
    )

    result = asyncio.run(module._copy_source_messages(client=SimpleNamespace(), settings=settings))

    assert result["fetched"] == 2
    assert result["copied"] == 2
    assert forward_mock.await_count == 2


def test_repost_messages_without_forward_preserves_inline_entities() -> None:
    module = load_module()
    hyperlink_entities = [SimpleNamespace(offset=0, length=11, url="https://test1.com")]

    class FakeClient:
        def __init__(self) -> None:
            self.sent_kwargs = None

        async def get_messages(self, source_id, ids):  # noqa: ANN001
            assert source_id == -1001
            assert ids == [123]
            return [
                SimpleNamespace(
                    id=123,
                    message="GkyFilehost",
                    entities=hyperlink_entities,
                    media=None,
                    gif=False,
                )
            ]

        async def send_message(self, **kwargs):  # noqa: ANN003
            self.sent_kwargs = kwargs
            return FakeSentMessage(1)

        async def send_file(self, **kwargs):  # noqa: ANN003
            raise AssertionError("send_file should not be called for text-only message")

    client = FakeClient()
    copied, skipped, processed = asyncio.run(
        module._repost_messages_without_forward(
            client=client,
            destination_channel_id=-2002,
            source_id=-1001,
            message_ids=[123],
        )
    )

    assert copied == 1
    assert skipped == 0
    assert processed == (123,)
    assert client.sent_kwargs is not None
    assert client.sent_kwargs["message"] == "GkyFilehost"
    assert client.sent_kwargs["formatting_entities"] == hyperlink_entities


def test_repost_messages_without_forward_preserves_entities_without_stripping_text() -> None:
    module = load_module()
    hyperlink_entities = [SimpleNamespace(offset=0, length=11, url="https://test1.com")]

    class FakeClient:
        def __init__(self) -> None:
            self.sent_kwargs = None

        async def get_messages(self, source_id, ids):  # noqa: ANN001
            return [
                SimpleNamespace(
                    id=123,
                    message="GkyFilehost  ",
                    entities=hyperlink_entities,
                    media=None,
                    gif=False,
                )
            ]

        async def send_message(self, **kwargs):  # noqa: ANN003
            self.sent_kwargs = kwargs
            return FakeSentMessage(1)

        async def send_file(self, **kwargs):  # noqa: ANN003
            raise AssertionError("send_file should not be called for text-only message")

    client = FakeClient()
    copied, skipped, processed = asyncio.run(
        module._repost_messages_without_forward(
            client=client,
            destination_channel_id=-2002,
            source_id=-1001,
            message_ids=[123],
        )
    )

    assert copied == 1
    assert skipped == 0
    assert processed == (123,)
    assert client.sent_kwargs is not None
    assert client.sent_kwargs["message"] == "GkyFilehost  "
    assert client.sent_kwargs["formatting_entities"] == hyperlink_entities


def test_forward_with_fallback_copies_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.delenv("COPY_MODE", raising=False)

    class FakeClient:
        def __init__(self) -> None:
            self.forward_called = False
            self.sent_kwargs = None

        async def forward_messages(self, **kwargs):  # noqa: ANN003
            self.forward_called = True
            raise AssertionError("forward_messages must not be used in copy mode")

        async def get_messages(self, source_id, ids):  # noqa: ANN001
            return [SimpleNamespace(id=5, message="hello", entities=None, media=None, gif=False)]

        async def send_message(self, **kwargs):  # noqa: ANN003
            self.sent_kwargs = kwargs
            return FakeSentMessage(7)

    client = FakeClient()
    copied, skipped, processed = asyncio.run(
        module._forward_with_fallback(
            client=client,
            destination_channel_id=-2002,
            source_id=-1001,
            message_ids=[5],
            as_album=False,
        )
    )

    assert client.forward_called is False
    assert client.sent_kwargs is not None
    assert client.sent_kwargs["message"] == "hello"
    assert (copied, skipped, processed) == (1, 0, (5,))


def test_forward_with_fallback_forwards_when_copy_mode_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("COPY_MODE", "forward")

    class FakeClient:
        def __init__(self) -> None:
            self.forward_kwargs = None

        async def forward_messages(self, **kwargs):  # noqa: ANN003
            self.forward_kwargs = kwargs
            return SimpleNamespace(id=9)

        async def get_messages(self, source_id, ids):  # noqa: ANN001
            raise AssertionError("copy path must not run when COPY_MODE=forward")

    client = FakeClient()
    result = asyncio.run(
        module._forward_with_fallback(
            client=client,
            destination_channel_id=-2002,
            source_id=-1001,
            message_ids=[5],
            as_album=False,
        )
    )

    assert client.forward_kwargs is not None
    assert client.forward_kwargs["from_peer"] == -1001
    assert result == (1, 0, (5,))


def test_repost_messages_without_forward_sends_album_as_group(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.delenv("COPY_MODE", raising=False)

    class MessageMediaPhoto:
        pass

    class FakeClient:
        def __init__(self) -> None:
            self.send_file_kwargs = None
            self.send_message_calls = 0

        async def get_messages(self, source_id, ids):  # noqa: ANN001
            return [
                SimpleNamespace(id=11, message="first", entities=None, media=MessageMediaPhoto(), gif=False),
                SimpleNamespace(id=12, message="", entities=None, media=MessageMediaPhoto(), gif=False),
            ]

        async def send_file(self, **kwargs):  # noqa: ANN003
            self.send_file_kwargs = kwargs
            return FakeSentMessage(21)

        async def send_message(self, **kwargs):  # noqa: ANN003
            self.send_message_calls += 1
            return FakeSentMessage(22)

    client = FakeClient()
    copied, skipped, processed = asyncio.run(
        module._repost_messages_without_forward(
            client=client,
            destination_channel_id=-2002,
            source_id=-1001,
            message_ids=[11, 12],
        )
    )

    assert client.send_message_calls == 0
    assert client.send_file_kwargs is not None
    assert len(client.send_file_kwargs["file"]) == 2
    assert client.send_file_kwargs["caption"] == ["first", None]
    assert (copied, skipped, processed) == (2, 0, (11, 12))


def test_repost_messages_without_forward_sends_unsupported_media_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    monkeypatch.delenv("COPY_MODE", raising=False)

    class MessageMediaPoll:
        pass

    class FakeClient:
        def __init__(self) -> None:
            self.send_file_calls = 0
            self.send_message_kwargs = None

        async def get_messages(self, source_id, ids):  # noqa: ANN001
            return [
                SimpleNamespace(
                    id=31,
                    message="vote now",
                    entities=None,
                    media=MessageMediaPoll(),
                    gif=False,
                )
            ]

        async def send_file(self, **kwargs):  # noqa: ANN003
            self.send_file_calls += 1
            raise AssertionError("send_file rejects poll media")

        async def send_message(self, **kwargs):  # noqa: ANN003
            self.send_message_kwargs = kwargs
            return FakeSentMessage(32)

    client = FakeClient()
    copied, skipped, processed = asyncio.run(
        module._repost_messages_without_forward(
            client=client,
            destination_channel_id=-2002,
            source_id=-1001,
            message_ids=[31],
        )
    )

    assert client.send_file_calls == 0
    assert client.send_message_kwargs is not None
    assert client.send_message_kwargs["message"] == "vote now"
    assert (copied, skipped, processed) == (1, 0, (31,))


def test_serialize_query_uses_json_for_appwrite_2() -> None:
    module = load_module()

    assert module.QUERY_FORMAT == module.QUERY_FORMAT_JSON
    assert module._serialize_query(module._query_equal("state_key", "global")) == (
        '{"method":"equal","attribute":"state_key","values":["global"]}'
    )
    assert module._serialize_query(module._query_limit(1)) == '{"method":"limit","values":[1]}'
    assert module._serialize_query(module._query_order_desc("$createdAt")) == (
        '{"method":"orderDesc","attribute":"$createdAt"}'
    )


def test_legacy_query_string_round_trip() -> None:
    module = load_module()

    assert module._legacy_query_string(module._query_equal("state_key", "global")) == (
        'equal("state_key",["global"])'
    )
    assert module._legacy_query_string(module._query_limit(1)) == "limit(1)"
    assert module._legacy_query_string(module._query_order_desc("$createdAt")) == 'orderDesc("$createdAt")'


def test_appwrite_list_documents_retries_with_legacy_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    seen_queries: list[str] = []

    def fake_request_json(method, path, payload=None, queries=None):  # noqa: ANN001
        if path.startswith("/databases/"):
            raise RuntimeError("legacy endpoint not available")
        seen_queries.append(queries[0])
        if queries[0].startswith("{"):
            raise RuntimeError('HTTP 400 Bad Request: {"message":"Invalid query: Syntax error"}')
        return {"rows": [{"$id": "row_1"}]}

    monkeypatch.setattr(module, "_appwrite_request_json", fake_request_json)

    rows = module._appwrite_list_documents(
        database_id="db1",
        collection_id="state",
        queries=[module._query_equal("state_key", "global")],
    )

    assert rows == [{"$id": "row_1"}]
    assert module.QUERY_FORMAT == module.QUERY_FORMAT_LEGACY
    assert seen_queries == [
        '{"method":"equal","attribute":"state_key","values":["global"]}',
        'equal("state_key",["global"])',
    ]


def test_load_state_returns_existing_row_id_for_compacted_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_STATE_COLLECTION_ID", "state")

    payload = module.STATE_MARKER + json.dumps(
        {"sources": {"-1001": {"last_id": 42}}, "meta": {"state_compacted": 1}},
        separators=(",", ":"),
    )

    monkeypatch.setattr(
        module,
        "_appwrite_list_documents",
        lambda database_id, collection_id, queries: [
            {"$id": "69ac4bd17494ba894b83", "state_key": "global", "payload": payload}
        ],
    )

    state, state_id = asyncio.run(module._load_state(client=SimpleNamespace()))

    assert state_id == "69ac4bd17494ba894b83"
    assert state["sources"]["-1001"]["last_id"] == 42


def test_load_state_keeps_row_id_when_payload_is_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_STATE_COLLECTION_ID", "state")

    monkeypatch.setattr(
        module,
        "_appwrite_list_documents",
        lambda database_id, collection_id, queries: [{"$id": "row_abc", "state_key": "global"}],
    )

    state, state_id = asyncio.run(module._load_state(client=SimpleNamespace()))

    assert state == {"sources": {}}
    assert state_id == "row_abc"


def test_save_state_reuses_existing_global_row_instead_of_creating(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://example.test")
    monkeypatch.setenv("APPWRITE_API_KEY", "key")
    monkeypatch.setenv("APPWRITE_DATABASE_ID", "db")
    monkeypatch.setenv("APPWRITE_STATE_COLLECTION_ID", "state")

    patched_paths: list[str] = []

    def fake_request_json(method, path, payload=None, queries=None):  # noqa: ANN001
        assert method == "PATCH", "must never create a duplicate global row"
        patched_paths.append(path)
        return {"$id": "existing_row"}

    monkeypatch.setattr(module, "_appwrite_request_json", fake_request_json)
    monkeypatch.setattr(
        module,
        "_find_existing_state_record_id",
        lambda database_id, collection_id: "existing_row",
    )

    state_id, did_update = asyncio.run(
        module._save_state(
            client=SimpleNamespace(),
            state={"sources": {"-1001": {"last_id": 7, "recent": [7]}}},
            state_message_id=None,
        )
    )

    assert did_update is True
    assert state_id == "existing_row"
    assert patched_paths == ["/tablesdb/db/tables/state/rows/existing_row"]




def test_format_source_run_line_reports_every_counter() -> None:
    module = load_module()

    line = module._format_source_run_line(
        "-1001234567890",
        {
            "status": "ok",
            "fetched": 12,
            "copied": 3,
            "gate_skipped": 7,
            "recent_skipped": 1,
            "blocked_ad": 1,
            "blocked_gif": 0,
            "filtered_links": 2,
            "window_count": 12,
            "window_min_id": 100,
            "window_max_id": 111,
            "cursor_id": 111,
        },
    )

    assert line.startswith("source=-1001234567890 f=12 c=3 g=7 r=1")
    assert "a=1" in line
    assert "i=0" not in line
    assert "w=100-111(12)" in line
    assert "cursor=" not in line
    assert "status=" not in line
    assert "\n" not in line


def test_format_source_run_line_includes_error_status() -> None:
    module = load_module()

    line = module._format_source_run_line(
        "-1001",
        {"status": "error", "error": "AuthKeyError: revoked"},
    )

    assert "status=error" in line
    assert "error=AuthKeyError: revoked" in line


def test_format_source_run_line_handles_empty_source() -> None:
    module = load_module()

    line = module._format_source_run_line("-1001", {"status": "ok"})

    assert "f=0" in line
    assert "w=none" in line
    assert "status=" not in line


def test_per_source_log_line_budget_fits_appwrite_log_limit() -> None:
    module = load_module()

    total = 0
    for index in range(74):
        line = module._format_source_run_line(
            f"-100{index:010d}",
            {
                "fetched": 99,
                "copied": 99,
                "gate_skipped": 0,
                "recent_skipped": 0,
                "blocked_ad": 0,
                "blocked_gif": 0,
                "window_count": 99,
                "window_min_id": 123456789,
                "window_max_id": 123456888,
                "cursor_id": 123456888,
            },
        )
        total += len(line) + 1

    # Appwrite trims execution logs at 8000 characters; leave room for the run summary.
    assert total < 6000


def test_source_sort_key_orders_channel_ids_numerically() -> None:
    module = load_module()

    keys = ["-1002", "-10010", "-1001"]

    assert sorted(keys, key=module._source_sort_key) == ["-10010", "-1002", "-1001"]


def test_safe_log_skips_native_echo_when_logger_handles_it(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    captured: list[str] = []
    printed: list[str] = []

    context = SimpleNamespace(log=lambda message: captured.append(message))
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(str(args[0])))

    module._safe_log(context, "hello", echo_native=False)

    assert captured == ["hello"]
    assert printed == []


def test_safe_log_prints_when_context_has_no_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    printed: list[str] = []

    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: printed.append(str(args[0])))

    module._safe_log(SimpleNamespace(), "fallback", echo_native=False)

    assert printed == ["fallback"]
