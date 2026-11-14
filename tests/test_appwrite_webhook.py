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
        "BACKFILL_ENABLED",
        "BACKFILL_SINCE_IST",
        "BACKFILL_DAYS",
        "BACKFILL_CHUNK_SIZE",
        "BACKFILL_RESET",
        "RUN_TIME_BUDGET_SECONDS",
        "COPY_DATE",
        "COPY_DATE_RESET",
        "COPY_ALL",
        "COPY_ALL_RESET",
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


def test_sanitize_message_text_preserves_telegram_links_by_default() -> None:
    module = load_module()
    text = (
        "visit https://t.me/some/path and https://t.me/+qKumi0QiYOsyMDY9 "
        "but keep https://example.com/x now"
    )
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


def test_sanitize_message_text_strips_footer_phrases() -> None:
    module = load_module()
    text = (
        "Movie update out now 🔥\n\n"
        "𝙰𝚍𝚞𝚕𝚝 𝙱𝚊𝚌𝚔𝚞𝚙 ➥「18+ BACK-UP🔞 (https://t.me/backupx08)」\n"
        "𝙿𝚘𝚠𝚎𝚛e𝚍 𝙱𝚢➥@ULTRONE_BACKUP\n"
        "𝙼𝚘𝚟𝚒𝚎𝚜 𝚂𝚎𝚊𝚛𝚌𝚑 𝙶𝚛𝚘𝚞𝚙 ➥@ULTRON_MOVIE_REQWEST"
    )
    assert module._sanitize_message_text(text) == "Movie update out now 🔥"


def test_sanitize_message_text_strips_footer_across_lines() -> None:
    module = load_module()
    text = (
        "Info here\n"
        "𝙰𝚍𝚞𝚕𝚝 𝙱𝚊𝚌𝚔𝚞𝚙 ➥「18+ BACK-UP🔞 (https://t.me/backupx08)」\n"
        "Upper line\n"
        "𝙿𝚘𝚠𝚎𝚛e𝚍 𝙱𝚢➥@ULTRONE_BACKUP\n"
        "End"
    )
    assert module._sanitize_message_text(text) == "Info here\nUpper line\nEnd"


def test_sanitize_message_text_preserves_interior_blank_lines() -> None:
    module = load_module()
    text = (
        "Title\n"
        "\n"
        "Paragraph\n"
        "𝙿𝚘𝚠𝚎𝚛e𝚍 𝙱𝚢➥@ULTRONE_BACKUP"
    )
    assert module._sanitize_message_text(text) == "Title\n\nParagraph"


def test_sanitize_message_text_keeps_blank_lines_for_optional_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    monkeypatch.setenv("FILTER_BAD_WORDS", "mc")
    text = "Title\n\nparagraph mc"
    assert module._sanitize_message_text(text) == "Title\n\nparagraph"


def test_sanitize_message_text_strips_dvdwo_backup_handle() -> None:
    module = load_module()
    text = "Post here\n@DVDWO_BACKUP"
    assert module._sanitize_message_text(text) == "Post here"


def test_sanitize_message_text_preserves_standalone_telegram_link_line() -> None:
    module = load_module()
    text = "Info top\nhttps://t.me/joinchat/abc123\nInfo bottom"
    # Normal t.me links are preserved by default (only ad-footer lines are dropped).
    assert module._sanitize_message_text(text) == text


def test_should_skip_message_text_blocks_ad_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    assert module._should_skip_message_text("#ad InsideAds (https://t.me/InsideAds_bot/open?startapp=abc)")
    assert module._should_skip_message_text("insideAds special")
    assert not module._should_skip_message_text("this is an ad")
    monkeypatch.setenv("BLOCK_GENERIC_AD_WORD", "1")
    assert module._should_skip_message_text("this is an ad")
    assert not module._should_skip_message_text("this is a normal update")


def test_sanitize_message_text_strips_ad_footer_signatures_with_links() -> None:
    module = load_module()
    text = (
        "Batman 2022 full movie out now 🍿\n"
        "Type /start to get link\n\n"
        "𝙰𝚍𝚞𝚕𝚝 𝙱𝚊𝚌𝚔𝚞𝚙 ➥「18+ BACK-UP🔞 (https://t.me/backupx08)」\n"
        "𝙿𝚘𝚠𝚎𝚛𝚎𝚍 𝚋𝚢➥ 𝐌𝐎𝐕𝐈𝐄 𝐕𝐀𝐔𝐋𝐓 𝐁𝐀𝐂𝐊𝐔𝐏😃 (https://t.me/MOVIEVAULTBACKUP)"
    )
    assert module._sanitize_message_text(text) == (
        "Batman 2022 full movie out now 🍿\nType /start to get link"
    )


def test_should_skip_message_text_skips_pure_ad_footer_only() -> None:
    module = load_module()
    # A message that is *only* the promotional/ad footer is skipped entirely.
    assert module._should_skip_message_text(
        "𝙰𝚍𝚞𝚕𝚝 𝙱𝚊𝚌𝚔𝚞𝚙 ➥「18+ BACK-UP🔞 (https://t.me/backupx08)」"
    )
    assert module._should_skip_message_text(
        "𝙿𝚘𝚠𝚎𝚛𝚎𝚍 𝚋𝚢➥ 𝐌𝐎𝐕𝐈𝐄 𝐕𝐀𝐔𝐋𝐓 𝐁𝐀𝐂𝐊𝐔𝐏😃 (https://t.me/MOVIEVAULTBACKUP)"
    )
    # A real message that merely appends a footer is still copied (footer stripped).
    assert not module._should_skip_message_text(
        "Movie update out now 🔥\n"
        "𝙰𝚍𝚞𝚕𝚝 𝙱𝚊𝚌𝚔𝚞𝚙 ➥「18+ BACK-UP🔞 (https://t.me/backupx08)」"
    )
    assert not module._should_skip_message_text("")


def test_should_skip_message_text_strict_mode_skips_any_ad_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    monkeypatch.setenv("BLOCK_ANY_FOOTER_AD_MESSAGE", "1")
    # Strict mode skips a real message that merely appends an ad footer.
    assert module._should_skip_message_text(
        "Movie update out now 🔥\n"
        "𝙰𝚍𝚞𝚕𝚝 𝙱𝚊𝚌𝚔𝚞𝚙 ➥「18+ BACK-UP🔞 (https://t.me/backupx08)」"
    )
    assert not module._should_skip_message_text("Movie update out now 🔥")


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


def test_fetch_recent_messages_drains_everything_after_cursor() -> None:
    module = load_module()
    now = datetime.now(timezone.utc)
    calls: dict = {}

    class FakeClient:
        async def iter_messages(self, entity, **kwargs):  # noqa: ANN001, ANN003
            calls["entity"] = entity
            calls.update(kwargs)
            # Oldest-first cursor drain: messages far outside LOOKBACK_MINUTES
            # are still returned, so a large backlog is never dropped.
            yield SimpleNamespace(id=101, date=now - timedelta(days=4))
            yield SimpleNamespace(id=102, date=now - timedelta(days=3))
            yield SimpleNamespace(id=103, date=now - timedelta(days=2))

    result = asyncio.run(
        module._fetch_recent_messages(
            client=FakeClient(),
            source_id=-1001,
            last_seen_id=100,
            cutoff_dt=now - timedelta(minutes=60),
        )
    )

    assert calls == {
        "entity": -1001,
        "min_id": 100,
        "reverse": True,
        "limit": module.LIVE_FETCH_CEILING,
    }
    assert [msg.id for msg in result] == [101, 102, 103]


def test_fetch_recent_messages_cold_start_uses_lookback_window() -> None:
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
            last_seen_id=0,
            cutoff_dt=now - timedelta(minutes=60),
        )
    )

    assert [msg.id for msg in result] == [100, 109, 110]


def test_rotated_live_source_ids_cycles_start_source() -> None:
    module = load_module()
    state: dict = {}

    first = module._rotated_live_source_ids(state, (-1001, -1002, -1003))
    second = module._rotated_live_source_ids(state, (-1001, -1002, -1003))
    third = module._rotated_live_source_ids(state, (-1001, -1002, -1003))
    fourth = module._rotated_live_source_ids(state, (-1001, -1002, -1003))

    assert first == (-1001, -1002, -1003)
    assert second == (-1002, -1003, -1001)
    assert third == (-1003, -1001, -1002)
    assert fourth == first


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


def test_copy_source_messages_force_full_copies_ignores_recent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    monkeypatch.setenv("FORCE_FULL_LOOKBACK_COPY", "1")

    # State already records id=1 as processed (cursor=1, recent=[1]).
    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {"-1001": {"last_id": 1, "recent": [1]}}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    now = datetime.now(timezone.utc)

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return [
            SimpleNamespace(id=1, message="post one", grouped_id=None, date=now),
            SimpleNamespace(id=2, message="post two", grouped_id=None, date=now),
        ]

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

    # Even though id=1 is already in recent/cursor, force-full copies it again.
    assert forward_mock.await_count == 2
    assert result["copied"] == 2
    assert result["skipped_duplicate_recent"] == 0
    assert result["skipped_cursor_gate"] == 0


def test_sanitize_message_text_strips_exact_ad_footer_phrase() -> None:
    module = load_module()
    ad_phrase = "𝙰𝚍𝚞𝚕𝚝 𝙱𝚊𝚌𝚔𝚞𝚙 ➥「18+ BACK-UP🔞 (https://t.me/backupx08)」"
    # The banned ad word/link is stripped, while the rest of the message is kept.
    assert module._sanitize_message_text(f"Movie update out now 🔥\n{ad_phrase}") == "Movie update out now 🔥"
    # A message that is *only* the ad phrase is skipped entirely.
    assert module._should_skip_message_text(ad_phrase)


def test_copy_source_messages_force_full_still_blocks_ad_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    monkeypatch.setenv("FORCE_FULL_LOOKBACK_COPY", "1")

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    now = datetime.now(timezone.utc)
    ad_phrase = "𝙰𝚍𝚞𝚕𝚝 𝙱𝚊𝚌𝚔𝚞𝚙 ➥「18+ BACK-UP🔞 (https://t.me/backupx08)」"

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return [
            SimpleNamespace(id=1, message=ad_phrase, grouped_id=None, date=now),
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

    # Even in force-full mode, the pure ad message is still blocked and NOT copied.
    assert forward_mock.await_count == 1
    assert result["copied"] == 1
    assert result["skipped_blocked_ad"] == 1
    assert result["skipped_blocked_gif"] == 0


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


def test_repost_messages_without_forward_sends_gif_album_as_group(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.delenv("COPY_MODE", raising=False)

    class MessageMediaPhoto:
        pass

    class FakeClient:
        def __init__(self) -> None:
            self.send_file_calls = []
            self.send_message_calls = 0

        async def get_messages(self, source_id, ids):  # noqa: ANN001
            return [
                SimpleNamespace(id=11, message="first", entities=None, media=MessageMediaPhoto(), gif=False),
                SimpleNamespace(id=12, message="", entities=None, media=MessageMediaPhoto(), gif=True),
            ]

        async def send_file(self, **kwargs):  # noqa: ANN003
            self.send_file_calls.append(kwargs)
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

    # The album is sent as one group even when it contains a GIF, so both
    # members are copied (GIF members are not skipped).
    assert len(client.send_file_calls) == 1
    assert len(client.send_file_calls[0]["file"]) == 2
    assert client.send_message_calls == 0
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


def test_load_settings_parses_backfill_env(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    clear_env(monkeypatch)
    monkeypatch.setenv("API_ID", "123456")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("SESSION_STRING", "session")
    monkeypatch.setenv("SOURCE_CHANNEL_ID", "-1001")
    monkeypatch.setenv("DESTINATION_CHANNEL_ID", "-1002")
    monkeypatch.setenv("BACKFILL_ENABLED", "1")
    monkeypatch.setenv("BACKFILL_SINCE_IST", "2026-11-08 22:30")
    monkeypatch.setenv("BACKFILL_CHUNK_SIZE", "250")
    monkeypatch.setenv("RUN_TIME_BUDGET_SECONDS", "200")

    settings = module._load_settings_from_env()

    assert settings.backfill_enabled is True
    assert settings.backfill_chunk_size == 250
    assert settings.backfill_time_budget_seconds == 200
    expected_ms = int(
        datetime(2026, 11, 8, 22, 30, tzinfo=module.IST_TIMEZONE).timestamp() * 1000
    )
    assert settings.backfill_since_ms == expected_ms


def test_load_settings_rejects_invalid_backfill_since(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    clear_env(monkeypatch)
    monkeypatch.setenv("API_ID", "123456")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("SESSION_STRING", "session")
    monkeypatch.setenv("SOURCE_CHANNEL_ID", "-1001")
    monkeypatch.setenv("DESTINATION_CHANNEL_ID", "-1002")
    monkeypatch.setenv("BACKFILL_ENABLED", "1")
    monkeypatch.setenv("BACKFILL_SINCE_IST", "not-a-date")

    with pytest.raises(ValueError, match="BACKFILL_SINCE_IST"):
        module._load_settings_from_env()


def test_set_source_state_preserves_backfill_progress() -> None:
    module = load_module()
    state = {
        "sources": {
            "-1001": {
                "backfill_max_id": 900,
                "backfill_done": 0,
                "backfill_scanned": 25,
                "backfill_copied": 20,
            }
        }
    }

    module._set_source_state(state, "-1001", last_id=1000, recent=[999, 1000])

    entry = state["sources"]["-1001"]
    assert entry["last_id"] == 1000
    assert entry["backfill_max_id"] == 900
    assert entry["backfill_scanned"] == 25
    assert entry["backfill_copied"] == 20


def test_fetch_backfill_chunk_honors_offset_limit_and_floor() -> None:
    module = load_module()
    now = datetime.now(timezone.utc)
    messages = [
        SimpleNamespace(id=10, date=now - timedelta(hours=1)),
        SimpleNamespace(id=9, date=now - timedelta(hours=2)),
        SimpleNamespace(id=8, date=now - timedelta(hours=3)),
        SimpleNamespace(id=7, date=now - timedelta(hours=4)),
    ]

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def iter_messages(self, target, limit=None, offset_id=None):  # noqa: ANN001
            self.calls.append({"target": target, "limit": limit, "offset_id": offset_id})
            batch = [m for m in messages if offset_id is None or m.id < offset_id]
            batch.sort(key=lambda m: m.id, reverse=True)
            for msg in batch[:limit]:
                yield msg

    client = FakeClient()
    chunk, done = asyncio.run(
        module._fetch_backfill_chunk(
            client=client,
            source_id=-1001,
            max_id=9,
            floor_dt=now - timedelta(hours=3, minutes=30),
            chunk_size=5,
        )
    )

    assert client.calls == [{"target": -1001, "limit": 5, "offset_id": 9}]
    assert [msg.id for msg in chunk] == [8]
    assert done is True


def test_backfill_phase_chunks_resumes_and_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    now = datetime.now(timezone.utc)
    messages = [
        SimpleNamespace(id=5, date=now - timedelta(minutes=5), message="", grouped_id=None),
        SimpleNamespace(id=4, date=now - timedelta(minutes=10), message="", grouped_id=None),
        SimpleNamespace(id=3, date=now - timedelta(minutes=15), message="", grouped_id=None),
        SimpleNamespace(id=2, date=now - timedelta(minutes=20), message="", grouped_id=None),
        SimpleNamespace(id=1, date=now - timedelta(minutes=25), message="", grouped_id=None),
    ]

    class FakeClient:
        async def iter_messages(self, target, limit=None, offset_id=None):  # noqa: ANN001
            batch = [m for m in messages if offset_id is None or m.id < offset_id]
            batch.sort(key=lambda m: m.id, reverse=True)
            for msg in batch[:limit]:
                yield msg

    copied_ids: list[int] = []

    async def fake_forward(*args, **kwargs):  # noqa: ANN001, ANN002
        message_ids = list(kwargs.get("message_ids") or [])
        copied_ids.extend(message_ids)
        return len(message_ids), 0, tuple(message_ids)

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return []

    monkeypatch.setattr(module, "_forward_with_fallback", fake_forward)
    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_fetch_recent_messages", fake_fetch_recent_messages)
    monkeypatch.setattr(module, "_load_recent_run_snapshot_ids", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "_save_run_window_ids", AsyncMock(return_value=[1]))

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
        backfill_enabled=True,
        backfill_since_ms=0,
        backfill_chunk_size=2,
        backfill_time_budget_seconds=240,
    )

    state: dict = {"sources": {}}
    first = asyncio.run(
        module._run_backfill_phase(
            client=FakeClient(),
            settings=settings,
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )
    assert first["copied"] == 2
    assert state["sources"]["-1001"]["backfill_max_id"] == 4
    assert state["sources"]["-1001"]["backfill_done"] == 0

    second = asyncio.run(
        module._run_backfill_phase(
            client=FakeClient(),
            settings=settings,
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )
    assert second["copied"] == 2
    assert state["sources"]["-1001"]["backfill_max_id"] == 2

    third = asyncio.run(
        module._run_backfill_phase(
            client=FakeClient(),
            settings=settings,
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )
    assert third["copied"] == 1
    assert state["sources"]["-1001"]["backfill_done"] == 1

    fourth = asyncio.run(
        module._run_backfill_phase(
            client=FakeClient(),
            settings=settings,
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )
    assert fourth["copied"] == 0
    assert fourth["sources_done"] == 1
    assert sorted(copied_ids) == [1, 2, 3, 4, 5]


def test_backfill_phase_skips_recent_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    now = datetime.now(timezone.utc)
    messages = [
        SimpleNamespace(id=2, date=now - timedelta(minutes=5), message="", grouped_id=None),
        SimpleNamespace(id=1, date=now - timedelta(minutes=10), message="", grouped_id=None),
    ]

    class FakeClient:
        async def iter_messages(self, target, limit=None, offset_id=None):  # noqa: ANN001
            for msg in messages:
                yield msg

    copied_ids: list[int] = []

    async def fake_forward(*args, **kwargs):  # noqa: ANN001, ANN002
        message_ids = list(kwargs.get("message_ids") or [])
        copied_ids.extend(message_ids)
        return len(message_ids), 0, tuple(message_ids)

    monkeypatch.setattr(module, "_forward_with_fallback", fake_forward)

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
        backfill_enabled=True,
        backfill_chunk_size=10,
    )
    state = {"sources": {"-1001": {"last_id": 2, "recent": [2]}}}

    stats = asyncio.run(
        module._run_backfill_phase(
            client=FakeClient(),
            settings=settings,
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    assert stats["copied"] == 1
    assert copied_ids == [1]
    assert state["sources"]["-1001"]["backfill_done"] == 1


def test_copy_source_messages_runs_backfill_after_live_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    now = datetime.now(timezone.utc)
    history = [
        SimpleNamespace(id=2, date=now - timedelta(hours=30), message="", grouped_id=None),
        SimpleNamespace(id=1, date=now - timedelta(hours=31), message="", grouped_id=None),
    ]

    class FakeClient:
        async def iter_messages(self, target, limit=None, offset_id=None):  # noqa: ANN001
            if limit is None:
                return
            batch = [m for m in history if offset_id is None or m.id < offset_id]
            batch.sort(key=lambda m: m.id, reverse=True)
            for msg in batch[:limit]:
                yield msg

    saved_states: list[dict] = []

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        saved_states.append(json.loads(json.dumps(state)))
        return 1, True

    async def fake_fetch_recent_messages(client, source_id, last_seen_id, cutoff_dt):  # noqa: ANN001
        return []

    async def fake_forward(*args, **kwargs):  # noqa: ANN001, ANN002
        message_ids = list(kwargs.get("message_ids") or [])
        return len(message_ids), 0, tuple(message_ids)

    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_fetch_recent_messages", fake_fetch_recent_messages)
    monkeypatch.setattr(module, "_load_recent_run_snapshot_ids", AsyncMock(return_value={}))
    monkeypatch.setattr(module, "_forward_with_fallback", fake_forward)
    monkeypatch.setattr(module, "_save_run_window_ids", AsyncMock(return_value=[1]))

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
        backfill_enabled=True,
        backfill_since_ms=0,
        backfill_chunk_size=10,
    )

    result = asyncio.run(module._copy_source_messages(client=FakeClient(), settings=settings))

    assert result["backfill_copied"] == 2
    assert result["copied"] == 2
    assert result["backfill_sources_done"] == 1
    assert result["partial"] == 0
    final_state = saved_states[-1]
    assert final_state["sources"]["-1001"]["backfill_done"] == 1
    # Backfill must not move the live cursor.
    assert final_state["sources"]["-1001"]["last_id"] == 0


def test_backfill_phase_stops_at_deadline_without_losing_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    now = datetime.now(timezone.utc)
    messages = [
        SimpleNamespace(id=2, date=now - timedelta(minutes=5), message="", grouped_id=None),
        SimpleNamespace(id=1, date=now - timedelta(minutes=10), message="", grouped_id=None),
    ]

    class FakeClient:
        async def iter_messages(self, target, limit=None, offset_id=None):  # noqa: ANN001
            for msg in messages:
                yield msg

    deadline_checks = {"count": 0}

    def fake_deadline_reached(deadline_monotonic: float) -> bool:  # noqa: ANN001
        deadline_checks["count"] += 1
        # First check (source loop) passes; the group-loop check reports expiry.
        return deadline_checks["count"] > 1

    monkeypatch.setattr(module, "_deadline_reached", fake_deadline_reached)

    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
        backfill_enabled=True,
        backfill_chunk_size=10,
    )
    state: dict = {"sources": {}}

    stats = asyncio.run(
        module._run_backfill_phase(
            client=FakeClient(),
            settings=settings,
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    assert stats["deadline_reached"] == 1
    assert stats["copied"] == 0
    assert state["sources"]["-1001"]["backfill_done"] == 0
    # Next run refetches from the newest ID, because nothing was handled.
    assert state["sources"]["-1001"]["backfill_max_id"] == 3


def test_parse_copy_date_range_ms_uses_ist() -> None:
    module = load_module()

    start_ms, end_ms = module._parse_copy_date_range_ms("2026-11-08")

    assert end_ms - start_ms == 24 * 60 * 60 * 1000
    # 2026-11-08 00:00 IST == 2026-11-07 18:30 UTC
    assert datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc) == datetime(
        2026, 11, 7, 18, 30, tzinfo=timezone.utc
    )
    assert module._parse_copy_date_range_ms("") == (0, 0)


def test_parse_copy_date_range_ms_rejects_bad_input() -> None:
    module = load_module()

    with pytest.raises(ValueError, match="COPY_DATE"):
        module._parse_copy_date_range_ms("08-11-2026")


def test_load_settings_parses_copy_date(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    clear_env(monkeypatch)
    monkeypatch.setenv("API_ID", "123456")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("SESSION_STRING", "session")
    monkeypatch.setenv("SOURCE_CHANNEL_ID", "-1001")
    monkeypatch.setenv("DESTINATION_CHANNEL_ID", "-1002")
    monkeypatch.setenv("COPY_DATE", "2026-11-08")

    settings = module._load_settings_from_env()

    assert settings.copy_date == "2026-11-08"
    assert settings.copy_date_end_ms - settings.copy_date_start_ms == 24 * 60 * 60 * 1000


def test_load_settings_rejects_copy_date_with_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    clear_env(monkeypatch)
    monkeypatch.setenv("API_ID", "123456")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("SESSION_STRING", "session")
    monkeypatch.setenv("SOURCE_CHANNEL_ID", "-1001")
    monkeypatch.setenv("DESTINATION_CHANNEL_ID", "-1002")
    monkeypatch.setenv("COPY_DATE", "2026-11-08")
    monkeypatch.setenv("BACKFILL_ENABLED", "1")

    with pytest.raises(ValueError, match="COPY_DATE"):
        module._load_settings_from_env()


class FakeDateClient:
    """Minimal Telethon stand-in that honours offset_date and offset_id."""

    def __init__(self, messages) -> None:  # noqa: ANN001
        self.messages = sorted(messages, key=lambda m: m.id, reverse=True)
        self.calls: list[dict] = []

    async def iter_messages(self, entity, limit=None, offset_id=0, offset_date=None):  # noqa: ANN001
        self.calls.append({"offset_id": offset_id, "offset_date": offset_date})
        yielded = 0
        for msg in self.messages:
            if offset_id and msg.id >= offset_id:
                continue
            if offset_date is not None and msg.date >= offset_date:
                continue
            if limit is not None and yielded >= limit:
                break
            yielded += 1
            yield msg


def _day_messages() -> list:
    """Messages around 2026-11-08 IST: ids 2, 3, 4 fall inside the day."""
    return [
        SimpleNamespace(id=1, message="before day", grouped_id=None, media=None, date=datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)),
        SimpleNamespace(id=2, message="day start", grouped_id=None, media=None, date=datetime(2026, 11, 7, 18, 30, tzinfo=timezone.utc)),
        SimpleNamespace(id=3, message="midday", grouped_id=None, media=None, date=datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)),
        SimpleNamespace(id=4, message="day end", grouped_id=None, media=None, date=datetime(2026, 11, 8, 18, 29, tzinfo=timezone.utc)),
        SimpleNamespace(id=5, message="next day", grouped_id=None, media=None, date=datetime(2026, 11, 8, 18, 30, tzinfo=timezone.utc)),
        SimpleNamespace(id=6, message="later", grouped_id=None, media=None, date=datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)),
    ]


def _date_settings(module, chunk_size: int = 100):  # noqa: ANN001
    start_ms, end_ms = module._parse_copy_date_range_ms("2026-11-08")
    return module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
        copy_date="2026-11-08",
        copy_date_start_ms=start_ms,
        copy_date_end_ms=end_ms,
        backfill_chunk_size=chunk_size,
    )


def test_run_date_phase_copies_only_requested_day(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    copied_ids: list[int] = []

    async def fake_copy_single(client, destination_channel_id, source_id, message, **kwargs):  # noqa: ANN001
        copied_ids.append(int(message.id))
        return 1, 0, 0, (int(message.id),)

    monkeypatch.setattr(module, "_copy_single_with_optional_sanitize", fake_copy_single)

    state: dict = {"sources": {}}
    client = FakeDateClient(_day_messages())

    stats = asyncio.run(
        module._run_date_phase(
            client=client,
            settings=_date_settings(module),
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    assert copied_ids == [2, 3, 4]
    assert stats["copied"] == 3
    assert stats["sources_done"] == 1
    assert client.calls[0]["offset_date"] is not None
    entry = state["sources"]["-1001"]
    assert entry["copy_date"] == "2026-11-08"


def test_run_date_phase_drains_whole_day_in_one_run(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    copied_ids: list[int] = []

    async def fake_copy_single(client, destination_channel_id, source_id, message, **kwargs):  # noqa: ANN001
        copied_ids.append(int(message.id))
        return 1, 0, 0, (int(message.id),)

    monkeypatch.setattr(module, "_copy_single_with_optional_sanitize", fake_copy_single)

    state: dict = {"sources": {}}
    client = FakeDateClient(_day_messages())

    stats = asyncio.run(
        module._run_date_phase(
            client=client,
            settings=_date_settings(module, chunk_size=2),
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    # The day holds 3 messages and the chunk is 2: the phase must keep fetching
    # instead of stopping after one chunk.
    assert sorted(copied_ids) == [2, 3, 4]
    assert stats["copied"] == 3
    assert stats["chunks"] == 2
    assert stats["sources_done"] == 1
    assert stats["deadline_reached"] == 0
    assert len(client.calls) == 2
    entry = state["sources"]["-1001"]
    assert entry["copy_date_done"] == 1
    assert entry["copy_date_copied"] == 3
    assert entry["last_id"] == 0


class FakeMultiEntityDateClient:
    """Same contract as FakeDateClient, but with a message list per entity."""

    def __init__(self, by_entity) -> None:  # noqa: ANN001
        self.by_entity = {
            entity: sorted(messages, key=lambda m: m.id, reverse=True)
            for entity, messages in by_entity.items()
        }

    async def iter_messages(self, entity, limit=None, offset_id=0, offset_date=None):  # noqa: ANN001
        yielded = 0
        for msg in self.by_entity.get(entity, []):
            if offset_id and msg.id >= offset_id:
                continue
            if offset_date is not None and msg.date >= offset_date:
                continue
            if limit is not None and yielded >= limit:
                break
            yielded += 1
            yield msg


def test_run_date_phase_copies_all_sources_in_one_run(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    copied: list[tuple[int, int]] = []

    async def fake_copy_single(client, destination_channel_id, source_id, message, **kwargs):  # noqa: ANN001
        copied.append((int(source_id), int(message.id)))
        return 1, 0, 0, (int(message.id),)

    monkeypatch.setattr(module, "_copy_single_with_optional_sanitize", fake_copy_single)

    start_ms, end_ms = module._parse_copy_date_range_ms("2026-11-08")
    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001, -1002),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
        copy_date="2026-11-08",
        copy_date_start_ms=start_ms,
        copy_date_end_ms=end_ms,
        backfill_chunk_size=2,
    )

    in_day = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    client = FakeMultiEntityDateClient(
        {
            -1001: [
                SimpleNamespace(id=10 + i, message=f"a{i}", grouped_id=None, media=None, date=in_day)
                for i in range(3)
            ],
            -1002: [
                SimpleNamespace(id=20 + i, message=f"b{i}", grouped_id=None, media=None, date=in_day)
                for i in range(3)
            ],
        }
    )
    state: dict = {"sources": {}}

    stats = asyncio.run(
        module._run_date_phase(
            client=client,
            settings=settings,
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    assert stats["copied"] == 6
    assert stats["sources_done"] == 2
    assert stats["sources_processed"] == 2
    assert sorted(src for src, _mid in copied) == [-1002, -1002, -1002, -1001, -1001, -1001]
    assert state["sources"]["-1001"]["copy_date_done"] == 1
    assert state["sources"]["-1002"]["copy_date_done"] == 1
    # The live cursor must never move for a historical replay.
    assert state["sources"]["-1001"]["last_id"] == 0
    assert state["sources"]["-1002"]["last_id"] == 0


BEFORE_DAY = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
IN_DAY = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _out_of_order_messages() -> list:
    """A day whose posts are not perfectly ordered by date.

    id 9 carries a backdated date, so a fetch that stops at the first message
    older than the day drops ids 8 and 7 with it.
    """
    return [
        SimpleNamespace(id=10, message="a", grouped_id=None, media=None, date=IN_DAY),
        SimpleNamespace(id=9, message="backdated", grouped_id=None, media=None, date=BEFORE_DAY),
        SimpleNamespace(id=8, message="b", grouped_id=None, media=None, date=IN_DAY),
        SimpleNamespace(id=7, message="c", grouped_id=None, media=None, date=IN_DAY),
        SimpleNamespace(id=6, message="floor", grouped_id=None, media=None, date=BEFORE_DAY),
    ]


def _date_bounds(module):  # noqa: ANN001
    start_ms, end_ms = module._parse_copy_date_range_ms("2026-11-08")
    return (
        datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc),
        datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc),
    )


def test_fetch_date_chunk_keeps_going_past_a_backdated_message() -> None:
    module = load_module()
    start_dt, end_dt = _date_bounds(module)
    client = FakeDateClient(_out_of_order_messages())

    messages, done = asyncio.run(
        module._fetch_date_chunk(
            client=client,
            source_id=-1001,
            start_dt=start_dt,
            end_dt=end_dt,
            max_id=0,
            chunk_size=3,
        )
    )

    assert [int(m.id) for m in messages] == [8, 10]
    # A full batch is not the end of the day just because one member is older.
    assert done is False


def test_fetch_date_chunk_finishes_after_paging_below_the_day() -> None:
    module = load_module()
    start_dt, end_dt = _date_bounds(module)
    client = FakeDateClient(_out_of_order_messages())

    messages, done = asyncio.run(
        module._fetch_date_chunk(
            client=client,
            source_id=-1001,
            start_dt=start_dt,
            end_dt=end_dt,
            max_id=8,
            chunk_size=3,
        )
    )

    assert [int(m.id) for m in messages] == [7]
    assert done is True


def test_run_date_phase_copies_every_in_day_message_of_an_unordered_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    copied_ids: list[int] = []

    async def fake_copy_single(client, destination_channel_id, source_id, message, **kwargs):  # noqa: ANN001
        copied_ids.append(int(message.id))
        return 1, 0, 0, (int(message.id),)

    monkeypatch.setattr(module, "_copy_single_with_optional_sanitize", fake_copy_single)
    state: dict = {"sources": {}}

    stats = asyncio.run(
        module._run_date_phase(
            client=FakeDateClient(_out_of_order_messages()),
            settings=_date_settings(module, chunk_size=3),
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    assert sorted(copied_ids) == [7, 8, 10]
    assert stats["copied"] == 3
    assert stats["chunks"] == 2
    assert stats["sources_done"] == 1
    assert state["sources"]["-1001"]["copy_date_done"] == 1


def test_should_skip_message_text_blocks_copyright_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    clear_env(monkeypatch)
    notice = (
        "\u200bThis message couldn't be displayed on your device due to copyright "
        "infringement."
    )

    assert module._is_copyright_blocked_text(notice)
    assert module._should_skip_message_text(notice)
    assert module._should_skip_message_text(
        "This message couldn\u2019t be displayed on your device due to copyright infringement"
    )
    assert module._should_skip_message_text(
        "This message cannot be displayed on your device due to copyright infringement."
    )
    assert not module._should_skip_message_text("New movie out now")
    assert not module._should_skip_message_text("Copyright infringement is a crime")

    monkeypatch.setenv("BLOCK_COPYRIGHT_MESSAGES", "0")
    assert not module._should_skip_message_text(notice)


def test_run_date_phase_skips_copyright_notice_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    clear_env(monkeypatch)
    copied_ids: list[int] = []

    async def fake_copy_single(client, destination_channel_id, source_id, message, **kwargs):  # noqa: ANN001
        copied_ids.append(int(message.id))
        return 1, 0, 0, (int(message.id),)

    monkeypatch.setattr(module, "_copy_single_with_optional_sanitize", fake_copy_single)
    notice = (
        "This message couldn't be displayed on your device due to copyright infringement."
    )
    client = FakeDateClient(
        [
            SimpleNamespace(id=11, message=notice, grouped_id=None, media=None, date=IN_DAY),
            SimpleNamespace(id=12, message="real post", grouped_id=None, media=None, date=IN_DAY),
        ]
    )
    state: dict = {"sources": {}}

    stats = asyncio.run(
        module._run_date_phase(
            client=client,
            settings=_date_settings(module),
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    assert copied_ids == [12]
    assert stats["copied"] == 1
    assert stats["skipped"] == 1


def _forwards_restricted_error() -> Exception:
    from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError

    try:
        return ChatForwardsRestrictedError(request=None)
    except TypeError:
        return ChatForwardsRestrictedError()


def test_repost_retries_protected_media_with_downloaded_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    monkeypatch.delenv("COPY_MODE", raising=False)

    class MessageMediaPhoto:
        pass

    class FakeClient:
        def __init__(self) -> None:
            self.files = []
            self.downloads = 0

        async def get_messages(self, source_id, ids):  # noqa: ANN001
            return [
                SimpleNamespace(
                    id=41,
                    message="photo",
                    entities=None,
                    media=MessageMediaPhoto(),
                    gif=False,
                    file=SimpleNamespace(size=2048),
                )
            ]

        async def send_file(self, **kwargs):  # noqa: ANN003
            self.files.append(kwargs["file"])
            if not isinstance(kwargs["file"], (bytes, bytearray)):
                # Re-uploading the source file reference of a protected chat.
                raise _forwards_restricted_error()
            return FakeSentMessage(51)

        async def download_media(self, message, file=None):  # noqa: ANN001
            self.downloads += 1
            return b"fresh-bytes"

    client = FakeClient()
    result = asyncio.run(
        module._repost_messages_without_forward(
            client=client,
            destination_channel_id=-2002,
            source_id=-1001,
            message_ids=[41],
        )
    )

    assert client.downloads == 1
    assert client.files[-1] == b"fresh-bytes"
    assert result == (1, 0, (41,))


def test_repost_skips_protected_media_it_cannot_reupload(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.delenv("COPY_MODE", raising=False)

    class MessageMediaPhoto:
        pass

    class FakeClient:
        def __init__(self) -> None:
            self.downloads = 0

        async def get_messages(self, source_id, ids):  # noqa: ANN001
            return [
                SimpleNamespace(
                    id=41,
                    message="huge video",
                    entities=None,
                    media=MessageMediaPhoto(),
                    gif=False,
                    file=SimpleNamespace(size=module.RESTRICTED_MEDIA_FALLBACK_MAX_BYTES + 1),
                ),
                SimpleNamespace(
                    id=42,
                    message="no bytes",
                    entities=None,
                    media=MessageMediaPhoto(),
                    gif=False,
                    file=SimpleNamespace(size=2048),
                ),
            ]

        async def send_file(self, **kwargs):  # noqa: ANN003
            raise _forwards_restricted_error()

        async def download_media(self, message, file=None):  # noqa: ANN001
            self.downloads += 1
            return None

    client = FakeClient()
    result = asyncio.run(
        module._repost_messages_without_forward(
            client=client,
            destination_channel_id=-2002,
            source_id=-1001,
            message_ids=[41, 42],
        )
    )

    # Oversized media is never buffered; a failed download is skipped, not raised.
    assert client.downloads == 1
    assert result == (0, 2, (41, 42))


def test_run_date_phase_survives_a_forwards_restricted_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    delivered: list[tuple[int, int]] = []

    async def flaky_deliver(
        client, destination_channel_id, source_id, messages, group_ids, **kwargs  # noqa: ANN001, ANN003
    ):
        if int(source_id) == -1001:
            raise _forwards_restricted_error()
        for mid in group_ids:
            delivered.append((int(source_id), int(mid)))
        return len(group_ids), 0, set(group_ids)

    monkeypatch.setattr(module, "_deliver_group", flaky_deliver)

    start_ms, end_ms = module._parse_copy_date_range_ms("2026-11-08")
    settings = module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001, -1002),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
        copy_date="2026-11-08",
        copy_date_start_ms=start_ms,
        copy_date_end_ms=end_ms,
        backfill_chunk_size=10,
    )
    in_day = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    client = FakeMultiEntityDateClient(
        {
            -1001: [SimpleNamespace(id=10, message="a", grouped_id=None, media=None, date=in_day)],
            -1002: [SimpleNamespace(id=20, message="b", grouped_id=None, media=None, date=in_day)],
        }
    )
    state: dict = {"sources": {}}

    stats = asyncio.run(
        module._run_date_phase(
            client=client,
            settings=settings,
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    # The protected source is recorded and skipped; the other source still copies
    # and the day finishes for both.
    assert delivered == [(-1002, 20)]
    assert stats["copied"] == 1
    assert stats["skipped"] == 1
    assert stats["sources_done"] == 2
    assert "-1001" in stats["errors"]
    assert "ChatForwardsRestrictedError" in stats["errors"]["-1001"]


def test_run_date_phase_resumes_across_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    copied_ids: list[int] = []

    async def fake_copy_single(client, destination_channel_id, source_id, message, **kwargs):  # noqa: ANN001
        copied_ids.append(int(message.id))
        return 1, 0, 0, (int(message.id),)

    monkeypatch.setattr(module, "_copy_single_with_optional_sanitize", fake_copy_single)

    state: dict = {"sources": {}}
    settings = _date_settings(module, chunk_size=2)

    # Run 1 starts with the budget already spent: nothing is fetched and the
    # day must not be marked done.
    first = asyncio.run(
        module._run_date_phase(
            client=FakeDateClient(_day_messages()),
            settings=settings,
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=module.time.monotonic() - 1,
        )
    )
    assert first["deadline_reached"] == 1
    assert copied_ids == []
    assert state.get("sources", {}).get("-1001", {}).get("copy_date_done", 0) == 0

    # Run 2 resumes and finishes the whole day in a single execution.
    second = asyncio.run(
        module._run_date_phase(
            client=FakeDateClient(_day_messages()),
            settings=settings,
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    assert sorted(copied_ids) == [2, 3, 4]
    assert second["sources_done"] == 1
    assert state["sources"]["-1001"]["copy_date_done"] == 1
    assert state["sources"]["-1001"]["last_id"] == 0

    # Run 3 has nothing left to do and must not resend anything.
    third = asyncio.run(
        module._run_date_phase(
            client=FakeDateClient(_day_messages()),
            settings=settings,
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )
    assert third["sources_done"] == 1
    assert third["copied"] == 0
    assert sorted(copied_ids) == [2, 3, 4]


def test_copy_source_messages_date_mode_skips_live_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        return 1, True

    async def fake_prime(client, source_ids):  # noqa: ANN001
        return {}, {}

    async def fake_copy_single(client, destination_channel_id, source_id, message, **kwargs):  # noqa: ANN001
        return 1, 0, 0, (int(message.id),)

    async def fail_live(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("live lookback pass must not run in COPY_DATE mode")

    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_prime_source_entities", fake_prime)
    monkeypatch.setattr(module, "_fetch_recent_messages", fail_live)
    monkeypatch.setattr(module, "_copy_single_with_optional_sanitize", fake_copy_single)
    monkeypatch.setattr(module, "_save_run_window_ids", AsyncMock(return_value=[]))

    result = asyncio.run(
        module._copy_source_messages(
            client=FakeDateClient(_day_messages()), settings=_date_settings(module)
        )
    )

    assert result["copy_date"] == "2026-11-08"
    assert result["copied"] == 3
    assert result["fetched"] == 3
    assert result["copy_date_sources_done"] == 1
    assert result["partial"] == 0


def _history_messages(count: int) -> list:
    base = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    return [
        SimpleNamespace(
            id=i,
            message=f"post {i}",
            grouped_id=None,
            media=None,
            date=base + timedelta(minutes=i),
        )
        for i in range(1, count + 1)
    ]


def _copy_all_settings(module, chunk_size: int = 2):  # noqa: ANN001
    return module.AppwriteSettings(
        api_id=1,
        api_hash="x",
        session_string="y",
        source_channel_ids=(-1001,),
        destination_channel_id=-2002,
        limit_per_source=50,
        lookback_minutes=60,
        backfill_chunk_size=chunk_size,
        copy_all=True,
    )


def test_load_settings_parses_copy_all(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    clear_env(monkeypatch)
    monkeypatch.setenv("API_ID", "123456")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("SESSION_STRING", "session")
    monkeypatch.setenv("SOURCE_CHANNEL_ID", "-1001")
    monkeypatch.setenv("DESTINATION_CHANNEL_ID", "-1002")
    monkeypatch.setenv("COPY_ALL", "1")

    settings = module._load_settings_from_env()

    assert settings.copy_all is True
    assert settings.backfill_enabled is False
    assert settings.copy_date == ""


def test_load_settings_rejects_copy_all_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    clear_env(monkeypatch)
    monkeypatch.setenv("API_ID", "123456")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("SESSION_STRING", "session")
    monkeypatch.setenv("SOURCE_CHANNEL_ID", "-1001")
    monkeypatch.setenv("DESTINATION_CHANNEL_ID", "-1002")
    monkeypatch.setenv("COPY_ALL", "1")

    monkeypatch.setenv("COPY_DATE", "2026-11-08")
    with pytest.raises(ValueError, match="COPY_DATE is still '2026-11-08'"):
        module._load_settings_from_env()

    monkeypatch.delenv("COPY_DATE")
    monkeypatch.setenv("BACKFILL_ENABLED", "1")
    with pytest.raises(ValueError, match="COPY_ALL"):
        module._load_settings_from_env()


def test_load_settings_treats_copy_date_off_words_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    clear_env(monkeypatch)
    monkeypatch.setenv("API_ID", "123456")
    monkeypatch.setenv("API_HASH", "hash")
    monkeypatch.setenv("SESSION_STRING", "session")
    monkeypatch.setenv("SOURCE_CHANNEL_ID", "-1001")
    monkeypatch.setenv("DESTINATION_CHANNEL_ID", "-1002")
    monkeypatch.setenv("COPY_ALL", "1")

    for off_word in ("none", "NONE", "off", "no", "-", "clear"):
        monkeypatch.setenv("COPY_DATE", off_word)
        settings = module._load_settings_from_env()
        assert settings.copy_date == ""
        assert settings.copy_all is True
        assert settings.copy_date_start_ms == 0


def test_run_full_copy_phase_drains_entire_history_in_one_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    copied_ids: list[int] = []

    async def fake_copy_single(client, destination_channel_id, source_id, message, **kwargs):  # noqa: ANN001
        copied_ids.append(int(message.id))
        return 1, 0, 0, (int(message.id),)

    monkeypatch.setattr(module, "_copy_single_with_optional_sanitize", fake_copy_single)

    state: dict = {"sources": {}}
    client = FakeDateClient(_history_messages(7))

    stats = asyncio.run(
        module._run_full_copy_phase(
            client=client,
            settings=_copy_all_settings(module, chunk_size=2),
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    assert sorted(copied_ids) == list(range(1, 8))
    assert stats["copied"] == 7
    assert stats["sources_done"] == 1
    assert stats["deadline_reached"] == 0
    entry = state["sources"]["-1001"]
    assert entry["copy_all_done"] == 1
    assert entry["copy_all_copied"] == 7
    # A history replay must never move the live cursor.
    assert entry["last_id"] == 0
    # Backfill progress keys stay untouched.
    assert "backfill_done" not in entry


def test_run_full_copy_phase_resumes_and_raises_no_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    copied_ids: list[int] = []

    async def fake_copy_single(client, destination_channel_id, source_id, message, **kwargs):  # noqa: ANN001
        copied_ids.append(int(message.id))
        return 1, 0, 0, (int(message.id),)

    monkeypatch.setattr(module, "_copy_single_with_optional_sanitize", fake_copy_single)

    state: dict = {"sources": {}}
    client = FakeDateClient(_history_messages(7))

    # An exhausted budget never marks the walk done and never copies anything.
    first = asyncio.run(
        module._run_full_copy_phase(
            client=client,
            settings=_copy_all_settings(module, chunk_size=2),
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=module.time.monotonic() - 1,
        )
    )
    assert first["deadline_reached"] == 1
    assert first["copied"] == 0
    assert state.get("sources", {}).get("-1001", {}).get("copy_all_done", 0) == 0

    # Resume: ids 7 and 6 were already delivered by the previous run, so the
    # stored cursor is 6 and the walk continues with 5..1 exactly once.
    state["sources"]["-1001"] = {"last_id": 0, "recent": [7, 6], "copy_all_max_id": 6}
    second = asyncio.run(
        module._run_full_copy_phase(
            client=client,
            settings=_copy_all_settings(module, chunk_size=2),
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    assert state["sources"]["-1001"]["copy_all_done"] == 1
    assert sorted(copied_ids) == [1, 2, 3, 4, 5]
    assert second["copied"] == 5
    assert len(copied_ids) == len(set(copied_ids))


def test_run_full_copy_phase_reset_recopies_recent_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    copied_ids: list[int] = []

    async def fake_copy_single(client, destination_channel_id, source_id, message, **kwargs):  # noqa: ANN001
        copied_ids.append(int(message.id))
        return 1, 0, 0, (int(message.id),)

    monkeypatch.setattr(module, "_copy_single_with_optional_sanitize", fake_copy_single)
    monkeypatch.setenv("COPY_ALL_RESET", "1")

    state: dict = {"sources": {"-1001": {"last_id": 0, "recent": [3, 2]}}}

    stats = asyncio.run(
        module._run_full_copy_phase(
            client=FakeDateClient(_history_messages(3)),
            settings=_copy_all_settings(module, chunk_size=10),
            state=state,
            resolved_source_entities={},
            entity_errors={},
            deadline_monotonic=float("inf"),
        )
    )

    assert stats["reset"] == 1
    assert stats["copied"] == 3
    assert sorted(copied_ids) == [1, 2, 3]
    assert state["sources"]["-1001"]["copy_all_done"] == 1
    assert state["sources"]["-1001"]["last_id"] == 0


def test_copy_source_messages_copy_all_skips_live_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    saved_states: list[dict] = []

    async def fake_load_state(client):  # noqa: ANN001
        return {"sources": {}}, 1

    async def fake_save_state(client, state, state_message_id):  # noqa: ANN001
        saved_states.append(json.loads(json.dumps(state)))
        return 1, True

    async def fake_prime(client, source_ids):  # noqa: ANN001
        return {}, {}

    async def fake_copy_single(client, destination_channel_id, source_id, message, **kwargs):  # noqa: ANN001
        return 1, 0, 0, (int(message.id),)

    async def fail_live(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("live lookback pass must not run in COPY_ALL mode")

    monkeypatch.setattr(module, "_load_state", fake_load_state)
    monkeypatch.setattr(module, "_save_state", fake_save_state)
    monkeypatch.setattr(module, "_prime_source_entities", fake_prime)
    monkeypatch.setattr(module, "_fetch_recent_messages", fail_live)
    monkeypatch.setattr(module, "_copy_single_with_optional_sanitize", fake_copy_single)
    monkeypatch.setattr(module, "_save_run_window_ids", AsyncMock(return_value=[]))

    result = asyncio.run(
        module._copy_source_messages(
            client=FakeDateClient(_history_messages(5)),
            settings=_copy_all_settings(module, chunk_size=2),
        )
    )

    assert result["copy_all"] == 1
    assert result["copy_all_enabled"] == 1
    assert result["copied"] == 5
    assert result["fetched"] == 5
    assert result["copy_all_sources_done"] == 1
    assert result["partial"] == 0
    final_state = saved_states[-1]
    assert final_state["sources"]["-1001"]["copy_all_done"] == 1
    assert final_state["sources"]["-1001"]["last_id"] == 0


def test_maybe_continue_run_queues_followup_when_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    calls: dict = {}

    def fake_spawn(chain):  # noqa: ANN001
        calls["chain"] = chain
        return True, "exec-123"

    monkeypatch.delenv("AUTO_CONTINUE", raising=False)
    monkeypatch.delenv("AUTO_CONTINUE_MAX_RUNS", raising=False)
    monkeypatch.setattr(module, "_spawn_followup_execution", fake_spawn)

    queued, execution_id, note = module._maybe_continue_run({"partial": 1}, 0)

    assert queued is True
    assert execution_id == "exec-123"
    assert calls["chain"] == 1
    assert "chain=1" in note


def test_maybe_continue_run_skips_completed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setattr(
        module, "_spawn_followup_execution", lambda chain: pytest.fail("must not chain")
    )

    queued, execution_id, note = module._maybe_continue_run({"partial": 0}, 0)

    assert queued is False
    assert execution_id == ""
    assert "budget" in note


def test_maybe_continue_run_respects_chain_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("AUTO_CONTINUE_MAX_RUNS", "3")
    monkeypatch.setattr(
        module, "_spawn_followup_execution", lambda chain: pytest.fail("must not chain")
    )

    queued, _execution_id, note = module._maybe_continue_run({"partial": 1}, 2)

    assert queued is False
    assert "AUTO_CONTINUE_MAX_RUNS=3" in note


def test_maybe_continue_run_honours_auto_continue_off(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setenv("AUTO_CONTINUE", "0")
    monkeypatch.setattr(
        module, "_spawn_followup_execution", lambda chain: pytest.fail("must not chain")
    )

    queued, _execution_id, note = module._maybe_continue_run({"partial": 1}, 0)

    assert queued is False
    assert "AUTO_CONTINUE" in note


def test_maybe_continue_run_skips_halted_run(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    monkeypatch.setattr(
        module, "_spawn_followup_execution", lambda chain: pytest.fail("must not chain")
    )

    queued, _execution_id, note = module._maybe_continue_run({"partial": 1, "halted": 1}, 0)

    assert queued is False
    assert "flood" in note


def test_request_chain_depth_reads_trigger_body() -> None:
    module = load_module()

    assert module._request_chain_depth(SimpleNamespace(req=SimpleNamespace(body='{"chain": 4}'))) == 4
    assert module._request_chain_depth(SimpleNamespace(req=SimpleNamespace(body=b'{"chain": 2}'))) == 2
    assert module._request_chain_depth(SimpleNamespace(req=SimpleNamespace(body='{"source": "run_now"}'))) == 0
    assert module._request_chain_depth(SimpleNamespace(req=SimpleNamespace(body=""))) == 0
    assert module._request_chain_depth(SimpleNamespace(req=SimpleNamespace(body="not json"))) == 0
    assert module._request_chain_depth(SimpleNamespace(req=None)) == 0

