import asyncio
import concurrent.futures
import inspect
import json
import os
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


STATE_MARKER = "telegram_copier_state_v1:"
RUN_MARKER = "telegram_copier_run_v1:"
TELEGRAM_LINK_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/\S+", re.IGNORECASE)
YOUTUBE_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?youtube\.com[^\s]*", re.IGNORECASE)
AD_BLOCK_STRICT_PATTERN = re.compile(r"(?:#ad\b|insideads[\w-]*)", re.IGNORECASE)
AD_BLOCK_GENERIC_WORD_PATTERN = re.compile(r"\bad\b", re.IGNORECASE)
# Promotional/ad footer signature detection: marketing text paired with a Telegram
# share link. These footers are stripped from copied messages (word + attached
# link); a message left empty after stripping is treated as a pure ad and skipped.
AD_FOOTER_LINK_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?!joinchat/\+?)([A-Za-z0-9_]+)",
    re.IGNORECASE,
)
AD_FOOTER_KEYWORD_PATTERN = re.compile(
    r"(?:backup|vault|18\s*\+|powered\s+by|movie\s*search|reQwest)",
    re.IGNORECASE,
)
# Footer/promotional signature phrases always stripped from copied message text.
FOOTER_PHRASES = (
    "𝙰𝚍𝚞𝚕𝚝 𝙱𝚊𝚌𝚔𝚞𝚙 ➥「18+ BACK-UP🔞 (https://t.me/backupx08)」",
    "𝙿𝚘𝚠𝚎𝚛e𝚍 𝙱𝚢➥@ULTRONE_BACKUP",
    "𝙼𝚘𝚟𝚒𝚎𝚜 𝚂𝚎𝚊𝚛𝚌𝚑 𝙶𝚛𝚘𝚞𝚙 ➥@ULTRON_MOVIE_REQWEST",
    "@DVDWO_BACKUP",
)
IST_TIMEZONE = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class AppwriteSettings:
    api_id: int
    api_hash: str
    session_string: str
    source_channel_ids: tuple[int, ...]
    destination_channel_id: int
    limit_per_source: int
    lookback_minutes: int
    backfill_enabled: bool = False
    backfill_since_ms: int = 0
    backfill_chunk_size: int = 100
    backfill_time_budget_seconds: int = 240
    copy_date: str = ""
    copy_date_start_ms: int = 0
    copy_date_end_ms: int = 0
    copy_all: bool = False


@dataclass(frozen=True)
class FloodHalt(Exception):
    seconds: int
    processed_ids: tuple[int, ...]
    copied: int
    skipped: int


# Keep a wider recent window so strict lookback processing does not re-copy on normal schedules.
MAX_RECENT_IDS = 2000
MAX_STATE_PAYLOAD_CHARS = 3800
MAX_RUN_MESSAGE_CHARS = 3800
DEFAULT_FUNCTION_NAME = "telegram_copier_run_v1"
BUILD_VERSION = "2026-11-15-auto-continue-v6"
# A run that stops on RUN_TIME_BUDGET_SECONDS queues the next execution instead
# of waiting for the hourly schedule. One execution is capped by the function
# timeout, so a 5,000-message backlog is drained by a chain of runs.
AUTO_CONTINUE_DEFAULT_MAX_RUNS = 10
# Upper bound on messages fetched for one source in one live run. The cursor only
# advances past what a run actually delivered, so anything above this ceiling is
# picked up by the next hourly run instead of being dropped.
LIVE_FETCH_CEILING = 5000
DB_LOG_LAST_ERROR = ""
DB_STATE_LAST_ERROR = ""
STATE_ROW_ID = "state_global_v1"
# Appwrite 2.x rejects the legacy `equal("attr",["v"])` query strings and wants
# JSON query objects. Older servers need the legacy strings. Detect once, then
# switch format on an "invalid query" error.
QUERY_FORMAT_JSON = "json"
QUERY_FORMAT_LEGACY = "legacy"
QUERY_FORMAT = QUERY_FORMAT_JSON
MAX_DB_RECENT_IDS = 400


def _appwrite_endpoint() -> str:
    return (
        os.getenv("APPWRITE_FUNCTION_API_ENDPOINT", "").strip()
        or os.getenv("APPWRITE_API_ENDPOINT", "").strip()
        or os.getenv("APPWRITE_ENDPOINT", "").strip()
    )


def _appwrite_project_id() -> str:
    return os.getenv("APPWRITE_FUNCTION_PROJECT_ID", "").strip() or os.getenv("APPWRITE_PROJECT_ID", "").strip()


def _appwrite_api_key() -> str:
    return (
        os.getenv("APPWRITE_FUNCTION_API_KEY", "").strip()
        or os.getenv("APPWRITE_KEY", "").strip()
        or os.getenv("APPWRITE_API_KEY", "").strip()
    )


def _appwrite_jwt() -> str:
    return os.getenv("APPWRITE_FUNCTION_JWT", "").strip() or os.getenv("APPWRITE_JWT", "").strip()


def _has_appwrite_auth() -> bool:
    return bool(_appwrite_api_key() or _appwrite_jwt())


def _appwrite_auth_mode() -> str:
    if _appwrite_api_key():
        return "api_key"
    if _appwrite_jwt():
        return "jwt"
    return "none"


def _log_database_id() -> str:
    return os.getenv("APPWRITE_LOGS_DATABASE_ID", "").strip() or os.getenv("APPWRITE_DATABASE_ID", "").strip()


def _log_collection_id() -> str:
    return os.getenv("APPWRITE_LOGS_COLLECTION_ID", "").strip() or "telegram_copier_logs"


def _state_database_id() -> str:
    return os.getenv("APPWRITE_STATE_DATABASE_ID", "").strip() or _log_database_id()


def _state_collection_id() -> str:
    return os.getenv("APPWRITE_STATE_COLLECTION_ID", "").strip() or "telegram_copier_state"


def _is_db_logging_enabled() -> bool:
    return bool(_appwrite_endpoint() and _has_appwrite_auth() and _log_database_id())


def _is_db_state_enabled() -> bool:
    return bool(_appwrite_endpoint() and _has_appwrite_auth() and _state_database_id())


def _db_logging_disable_reason() -> str:
    missing: list[str] = []
    if not _appwrite_endpoint():
        missing.append("endpoint")
    if not _has_appwrite_auth():
        missing.append("auth")
    if not _log_database_id():
        missing.append("database_id")
    if not missing:
        return ""
    return "db_logging_disabled_missing_" + ",".join(missing)


def _db_state_disable_reason() -> str:
    missing: list[str] = []
    if not _appwrite_endpoint():
        missing.append("endpoint")
    if not _has_appwrite_auth():
        missing.append("auth")
    if not _state_database_id():
        missing.append("database_id")
    if not missing:
        return ""
    return "db_state_disabled_missing_" + ",".join(missing)


def _appwrite_env_diagnostics() -> dict[str, Any]:
    return {
        "build_version": BUILD_VERSION,
        "appwrite_endpoint_set": int(bool(_appwrite_endpoint())),
        "appwrite_project_id_set": int(bool(_appwrite_project_id())),
        "appwrite_auth_set": int(bool(_has_appwrite_auth())),
        "appwrite_database_id_set": int(bool(_log_database_id())),
        "appwrite_state_database_id_set": int(bool(_state_database_id())),
        "appwrite_logs_collection_id": _log_collection_id(),
        "appwrite_state_collection_id": _state_collection_id(),
    }


def _appwrite_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    project_id = _appwrite_project_id()
    if project_id:
        headers["X-Appwrite-Project"] = project_id
    api_key = _appwrite_api_key()
    jwt = _appwrite_jwt()
    if api_key:
        headers["X-Appwrite-Key"] = api_key
    elif jwt:
        headers["X-Appwrite-JWT"] = jwt
    return headers


def _appwrite_build_url(path: str, queries: list[str] | None = None) -> str:
    base = _appwrite_endpoint().rstrip("/")
    # Support endpoints both with and without trailing /v1.
    if base.endswith("/v1"):
        url = f"{base}{path}"
    else:
        url = f"{base}/v1{path}"
    if queries:
        query_pairs = [("queries[]", query) for query in queries]
        url += "?" + urllib.parse.urlencode(query_pairs)
    return url


def _appwrite_request_json(method: str, path: str, payload: dict[str, Any] | None = None, queries: list[str] | None = None) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        _appwrite_build_url(path=path, queries=queries),
        data=body,
        headers=_appwrite_headers(),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8") or "{}"
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
            return {}
    except urllib.error.HTTPError as exc:
        details = ""
        try:
            details = (exc.read() or b"").decode("utf-8", errors="replace").strip()
        except Exception:
            details = ""
        if details:
            raise RuntimeError(f"HTTP {exc.code} {exc.reason}: {details[:600]}") from exc
        raise RuntimeError(f"HTTP {exc.code} {exc.reason}") from exc


def _appwrite_create_document(database_id: str, collection_id: str, data: dict[str, Any]) -> dict[str, Any]:
    payload = {"documentId": "unique()", "data": data}
    try:
        return _appwrite_request_json(
            method="POST",
            path=f"/databases/{database_id}/collections/{collection_id}/documents",
            payload=payload,
        )
    except Exception:
        # TablesDB fallback for newer Appwrite projects.
        try:
            # Prefer server-generated row ID to avoid collisions.
            return _appwrite_request_json(
                method="POST",
                path=f"/tablesdb/{database_id}/tables/{collection_id}/rows",
                payload={"data": data},
            )
        except Exception:
            pass
        last_exc: Exception | None = None
        for _ in range(5):
            tables_payload = {"rowId": uuid.uuid4().hex, "data": data}
            try:
                return _appwrite_request_json(
                    method="POST",
                    path=f"/tablesdb/{database_id}/tables/{collection_id}/rows",
                    payload=tables_payload,
                )
            except Exception as exc:
                last_exc = exc
                if "row_already_exists" in str(exc).lower() or "already exists" in str(exc).lower():
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Failed to create Appwrite row after retries.")


def _query_equal(attribute: str, *values: Any) -> dict[str, Any]:
    return {"method": "equal", "attribute": attribute, "values": list(values)}


def _query_limit(limit: int) -> dict[str, Any]:
    return {"method": "limit", "values": [int(limit)]}


def _query_order_desc(attribute: str) -> dict[str, Any]:
    return {"method": "orderDesc", "attribute": attribute}


def _legacy_query_string(query: dict[str, Any]) -> str:
    method = str(query.get("method", ""))
    attribute = query.get("attribute")
    values = query.get("values")
    if method in ("limit", "offset") and isinstance(values, list) and values:
        return f"{method}({int(values[0])})"
    if attribute is not None and values is not None:
        return f'{method}("{attribute}",{json.dumps(values, separators=(",", ":"))})'
    if attribute is not None:
        return f'{method}("{attribute}")'
    if values is not None:
        return f"{method}({json.dumps(values, separators=(",", ":"))})"
    return f"{method}()"


def _serialize_query(query: dict[str, Any] | str) -> str:
    if isinstance(query, str):
        return query
    if QUERY_FORMAT == QUERY_FORMAT_LEGACY:
        return _legacy_query_string(query)
    return json.dumps(query, separators=(",", ":"), ensure_ascii=True)


def _is_invalid_query_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "invalid query" in text or "query syntax" in text


def _appwrite_list_documents(
    database_id: str,
    collection_id: str,
    queries: list[dict[str, Any] | str],
) -> list[dict[str, Any]]:
    global QUERY_FORMAT
    response: dict[str, Any] | None = None
    last_exc: Exception | None = None
    for attempt in (1, 2):
        serialized = [_serialize_query(query) for query in queries]
        try:
            response = _appwrite_request_json(
                method="GET",
                path=f"/databases/{database_id}/collections/{collection_id}/documents",
                queries=serialized,
            )
            break
        except Exception:
            try:
                response = _appwrite_request_json(
                    method="GET",
                    path=f"/tablesdb/{database_id}/tables/{collection_id}/rows",
                    queries=serialized,
                )
                break
            except Exception as rows_exc:
                last_exc = rows_exc
        if attempt == 1 and last_exc is not None and _is_invalid_query_error(last_exc):
            # Retry once with the other query dialect.
            QUERY_FORMAT = QUERY_FORMAT_LEGACY if QUERY_FORMAT == QUERY_FORMAT_JSON else QUERY_FORMAT_JSON
            continue
        break
    if response is None:
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Appwrite list request failed without response.")
    documents = response.get("documents", [])
    if isinstance(documents, list) and documents:
        return [doc for doc in documents if isinstance(doc, dict)]
    rows = response.get("rows", [])
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, dict)]
    if isinstance(documents, list):
        return [doc for doc in documents if isinstance(doc, dict)]
    return []


def _serialize_message_ids(ids: list[int]) -> str:
    return ",".join(str(int(mid)) for mid in ids if isinstance(mid, int))


def _parse_serialized_message_ids(raw: str) -> list[int]:
    parsed: list[int] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            parsed.append(int(token))
        except ValueError:
            continue
    return parsed


def _parse_message_ids_json_value(value: Any) -> list[int]:
    if isinstance(value, list):
        return [int(mid) for mid in value if isinstance(mid, int)]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [int(mid) for mid in parsed if isinstance(mid, int)]
        except Exception:
            return []
    return []


def _strip_unknown_attribute_from_error(payload: dict[str, Any], error_text: str) -> bool:
    normalized = (error_text or "").replace('\\"', '"')
    match = re.search(r'Unknown attribute:\s*"([^"]+)"', normalized, re.IGNORECASE)
    if not match:
        return False
    attr = match.group(1)
    if attr in payload:
        payload.pop(attr, None)
        return True
    return False


def _coerce_json_like_fields_to_text(payload: dict[str, Any], fields: tuple[str, ...]) -> bool:
    changed = False
    for field in fields:
        if field not in payload:
            continue
        value = payload.get(field)
        if value is None or isinstance(value, str):
            continue
        try:
            payload[field] = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
            changed = True
        except Exception:
            continue
    return changed


def _adjust_json_field_by_error(payload: dict[str, Any], field: str, error_text: str) -> bool:
    normalized = (error_text or "").replace('\\"', '"').lower()
    if field not in payload:
        return False

    value = payload.get(field)
    if field == "message_ids_json" and "message_ids_json['" in normalized and "valid string" in normalized:
        if isinstance(value, list):
            payload[field] = [str(item) for item in value]
            return True
        return False

    if f'"{field.lower()}" must be an array' in normalized:
        if isinstance(value, list):
            return False
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    payload[field] = parsed
                    return True
            except Exception:
                parsed_ids = _parse_serialized_message_ids(value)
                payload[field] = parsed_ids
                return True
        payload[field] = []
        return True

    if (
        f'"{field.lower()}" must be a string' in normalized
        or f'"{field.lower()}" must be text' in normalized
        or (field.lower() in normalized and "valid string" in normalized)
    ):
        if isinstance(value, str):
            return False
        try:
            payload[field] = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
            return True
        except Exception:
            return False

    return False


def _build_log_part_payload(
    run_ts_ms: int,
    lookback_minutes: int,
    run_status: str,
    source_key: str,
    part: str,
    message_ids: list[int],
) -> dict[str, Any]:
    return {
        "function_name": DEFAULT_FUNCTION_NAME,
        "run_ts_ms": int(run_ts_ms),
        "status": run_status,
        "lookback_minutes": int(lookback_minutes),
        "source_id": int(source_key),
        "part": part,
        "message_ids_json": [int(mid) for mid in message_ids if isinstance(mid, int)],
        "message_ids": _serialize_message_ids(message_ids),
        "created_at": _now_utc_iso(),
    }


def log_copier_execution(log_data: dict[str, Any]) -> bool:
    """Persist log to Appwrite DB. Never raises and retries once."""
    global DB_LOG_LAST_ERROR
    if not _is_db_logging_enabled():
        DB_LOG_LAST_ERROR = _db_logging_disable_reason() or "db_logging_disabled_missing_env_or_auth"
        return False

    database_id = _log_database_id()
    collection_id = _log_collection_id()
    current = dict(log_data)
    for attempt in (1, 2, 3):
        try:
            _appwrite_create_document(database_id=database_id, collection_id=collection_id, data=current)
            DB_LOG_LAST_ERROR = ""
            return True
        except Exception as exc:
            if _strip_unknown_attribute_from_error(current, str(exc)):
                continue
            if _adjust_json_field_by_error(current, "message_ids_json", str(exc)):
                continue
            if attempt == 1:
                # Compact payload for strict schemas (small varchar / type mismatch).
                current = {
                    "function_name": str(current.get("function_name", DEFAULT_FUNCTION_NAME))[:120],
                    "run_ts_ms": int(current.get("run_ts_ms", int(time.time() * 1000))),
                    "status": str(current.get("status", "completed"))[:60],
                    "lookback_minutes": int(current.get("lookback_minutes", 60)),
                    "source_id": int(current.get("source_id", 0)),
                    "part": str(current.get("part", "1/1"))[:24],
                    "message_ids_json": [],
                    "message_ids": str(current.get("message_ids", ""))[:240],
                    "created_at": _now_utc_iso(),
                }
                time.sleep(0.25)
                continue
            DB_LOG_LAST_ERROR = f"{type(exc).__name__}: {exc}"
            return False
    return False


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ist_iso() -> str:
    return datetime.now(IST_TIMEZONE).isoformat()


def _state_payload_text(state: dict[str, Any]) -> str:
    return STATE_MARKER + json.dumps(state, separators=(",", ":"), sort_keys=True)


def _copy_state(state: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(json.dumps(state))
    except Exception:
        return {"sources": {}}


def _compact_state_for_save(state: dict[str, Any]) -> dict[str, Any]:
    compact = _copy_state(state)

    meta = compact.get("meta")
    if isinstance(meta, dict):
        source_errors = meta.get("source_errors")
        if isinstance(source_errors, dict):
            # Keep enough detail for the dashboard while preventing one bad source
            # from making the shared checkpoint exceed Appwrite's attribute limit.
            compact_errors: dict[str, str] = {}
            for source_key in sorted(source_errors.keys())[:8]:
                compact_errors[str(source_key)[:80]] = str(source_errors[source_key])[:160]
            meta["source_errors"] = compact_errors
            meta["source_errors_count"] = len(source_errors)
            meta["source_errors_truncated"] = int(len(source_errors) > len(compact_errors))

    sources = compact.get("sources")
    if isinstance(sources, dict):
        for source_key, source_entry in sources.items():
            if not isinstance(source_entry, dict):
                sources[source_key] = {"last_id": 0, "recent": []}
                continue
            recent = source_entry.get("recent", [])
            if isinstance(recent, list):
                source_entry["recent"] = [
                    int(mid) for mid in recent if isinstance(mid, int)
                ][-MAX_DB_RECENT_IDS:]

    return compact


def _build_state_payload(state: dict[str, Any]) -> str:
    payload = _state_payload_text(state)
    if len(payload) <= MAX_STATE_PAYLOAD_CHARS:
        return payload

    compact = _compact_state_for_save(state)
    payload = _state_payload_text(compact)
    if len(payload) <= MAX_STATE_PAYLOAD_CHARS:
        return payload

    # Last-resort compact form to avoid runtime failure due to Telegram message limits.
    minimal_sources: dict[str, dict[str, int]] = {}
    for source_key, source_entry in (compact.get("sources") or {}).items():
        if not isinstance(source_entry, dict):
            continue
        last_id = source_entry.get("last_id", 0)
        minimal_sources[str(source_key)] = {"last_id": int(last_id) if isinstance(last_id, int) else 0}

    minimal_state = {
        "sources": minimal_sources,
        "meta": {
            "status": ((compact.get("meta") or {}).get("status") if isinstance(compact.get("meta"), dict) else None),
            "last_run_ts_ms": (
                (compact.get("meta") or {}).get("last_run_ts_ms") if isinstance(compact.get("meta"), dict) else None
            ),
            "source_errors_count": len((compact.get("meta") or {}).get("source_errors", {})) if isinstance(compact.get("meta"), dict) and isinstance((compact.get("meta") or {}).get("source_errors"), dict) else 0,
            "state_compacted": 1,
            "compacted_at_ist": _now_ist_iso(),
            "compacted_at_utc": _now_utc_iso(),
        },
    }
    return _state_payload_text(minimal_state)


def _build_state_payload_with_limit(state: dict[str, Any], max_chars: int) -> str:
    payload = _build_state_payload(state)
    if len(payload) <= max_chars:
        return payload

    sources = state.get("sources", {}) if isinstance(state.get("sources"), dict) else {}
    minimal_sources: dict[str, dict[str, int]] = {}
    for source_key in sorted(sources.keys()):
        source_entry = sources.get(source_key)
        if not isinstance(source_entry, dict):
            continue
        last_id = source_entry.get("last_id", 0)
        minimal_sources[str(source_key)] = {"last_id": int(last_id) if isinstance(last_id, int) else 0}

    candidate = _state_payload_text({"sources": minimal_sources, "meta": {"state_compacted": 1}})
    if len(candidate) <= max_chars:
        return candidate

    # Last resort: drop sources from the tail until the payload fits the column.
    source_keys = sorted(minimal_sources.keys())
    while source_keys:
        source_keys.pop()
        current = {key: minimal_sources[key] for key in source_keys}
        candidate = _state_payload_text(
            {"sources": current, "meta": {"state_compacted": 1, "truncated": 1}}
        )
        if len(candidate) <= max_chars:
            return candidate

    # Even an empty source map may exceed tiny limits; hard-slice as a final guard.
    if len(candidate) > max_chars:
        return candidate[:max_chars]
    return candidate


def _is_unique_constraint_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "document_unique_constraint_violation",
            "row_unique_constraint_violation",
            "unique attribute constraint",
            "row_already_exists",
            "document_already_exists",
        )
    )


def _find_existing_state_record_id(database_id: str, collection_id: str) -> str | None:
    """Find the existing global state row after a create/update race."""
    try:
        documents = _appwrite_list_documents(
            database_id=database_id,
            collection_id=collection_id,
            queries=[_query_equal("state_key", "global"), _query_limit(1)],
        )
    except Exception:
        return None
    if not documents:
        return None
    record_id = documents[0].get("$id")
    return str(record_id) if isinstance(record_id, str) and record_id else None


def _split_id_tokens(tokens: list[str], max_chars: int) -> list[list[str]]:
    if not tokens:
        return [[]]
    parts: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for token in tokens:
        token_len = len(token) + (1 if current else 0)
        if current and current_len + token_len > max_chars:
            parts.append(current)
            current = [token]
            current_len = len(token)
            continue
        current.append(token)
        current_len += token_len
    if current:
        parts.append(current)
    return parts


async def _save_run_window_ids(
    client: Any,
    run_ts_ms: int,
    lookback_minutes: int,
    run_status: str,
    ids_by_source: dict[str, list[int]],
) -> list[int]:
    saved_markers: list[int] = []
    marker = 0
    ids_max_chars_raw = os.getenv("DB_LOG_IDS_MAX_CHARS", "800").strip()
    try:
        ids_max_chars = int(ids_max_chars_raw)
    except ValueError:
        ids_max_chars = 800
    ids_max_chars = max(64, min(2800, ids_max_chars))
    for source_key in sorted(ids_by_source.keys()):
        raw_ids = ids_by_source.get(source_key, [])
        tokens = [str(int(mid)) for mid in raw_ids if isinstance(mid, int)]
        if not tokens:
            continue
        parts = _split_id_tokens(tokens, max_chars=ids_max_chars)
        total_parts = len(parts)

        for idx, part_tokens in enumerate(parts, start=1):
            part_info = f"{idx}/{total_parts}"
            part_ids = [int(token) for token in part_tokens if token]
            payload = _build_log_part_payload(
                run_ts_ms=run_ts_ms,
                lookback_minutes=lookback_minutes,
                run_status=run_status,
                source_key=source_key,
                part=part_info,
                message_ids=part_ids,
            )
            if log_copier_execution(payload):
                marker += 1
                saved_markers.append(marker)
    return saved_markers


def _parse_run_snapshot_message(text: str) -> tuple[int, str, list[int]] | None:
    if not text.startswith(RUN_MARKER):
        return None
    raw = text[len(RUN_MARKER) :]
    parts = raw.split(";")
    fields: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip()] = value.strip()
    run_ts_raw = fields.get("run_ts_ms")
    source_id = fields.get("source_id")
    ids_raw = fields.get("ids", "")
    if not run_ts_raw or not source_id:
        return None
    try:
        run_ts_ms = int(run_ts_raw)
    except ValueError:
        return None
    parsed_ids: list[int] = []
    if ids_raw and ids_raw != "-":
        for token in ids_raw.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                parsed_ids.append(int(token))
            except ValueError:
                continue
    return run_ts_ms, str(source_id), parsed_ids


async def _load_recent_run_snapshot_ids(client: Any, lookback_minutes: int) -> dict[str, set[int]]:
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - (lookback_minutes * 60 * 1000)
    by_source: dict[str, set[int]] = {}

    try:
        if _is_db_logging_enabled():
            documents = _appwrite_list_documents(
                database_id=_log_database_id(),
                collection_id=_log_collection_id(),
                queries=[
                    _query_order_desc("$createdAt"),
                    _query_limit(500),
                    _query_equal("function_name", DEFAULT_FUNCTION_NAME),
                ],
            )
            for doc in documents:
                run_ts_ms = doc.get("run_ts_ms")
                source_id = doc.get("source_id")
                ids_raw = doc.get("message_ids", "")
                ids_json = doc.get("message_ids_json")
                if not isinstance(run_ts_ms, int) or source_id is None:
                    continue
                if run_ts_ms < cutoff_ms:
                    continue
                source_key = str(source_id)
                if source_key not in by_source:
                    by_source[source_key] = set()
                parsed_ids_json = _parse_message_ids_json_value(ids_json)
                if parsed_ids_json:
                    by_source[source_key].update(parsed_ids_json)
                else:
                    by_source[source_key].update(_parse_serialized_message_ids(str(ids_raw)))
            return by_source
    except Exception:
        # Snapshot recovery is best-effort and should never block main forwarding.
        pass

    # Backward compatibility fallback: read previous snapshots from Saved Messages.
    try:
        async for message in client.iter_messages("me", search=RUN_MARKER, limit=500):
            text = (getattr(message, "message", "") or "").strip()
            parsed = _parse_run_snapshot_message(text)
            if parsed is None:
                continue
            run_ts_ms, source_key, ids = parsed
            if run_ts_ms < cutoff_ms:
                continue
            if source_key not in by_source:
                by_source[source_key] = set()
            by_source[source_key].update(ids)
    except Exception:
        return {}
    return by_source


def _parse_source_ids() -> tuple[int, ...]:
    source_ids_raw = os.getenv("SOURCE_CHANNEL_IDS", "").strip()
    source_id_raw = os.getenv("SOURCE_CHANNEL_ID", "").strip()

    if source_ids_raw:
        return tuple(int(part.strip()) for part in source_ids_raw.split(",") if part.strip())
    if source_id_raw:
        return (int(source_id_raw),)
    return ()


def _parse_backfill_since_ms(raw: str, days: int) -> int:
    """Resolve the backfill floor to epoch milliseconds.

    `BACKFILL_SINCE_IST` accepts an ISO date/time or `YYYY-MM-DD[ HH:MM[:SS]]`
    and is treated as IST when no offset is given. When unset, the floor is
    `now - BACKFILL_DAYS` days. `BACKFILL_DAYS=0` means full channel history.
    """
    text = raw.strip()
    if text:
        parsed: datetime | None = None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            raise ValueError(
                "BACKFILL_SINCE_IST must be an ISO date or YYYY-MM-DD[ HH:MM[:SS]]."
            )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST_TIMEZONE)
        return int(parsed.timestamp() * 1000)
    if days <= 0:
        return 0
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)


def _parse_copy_date_range_ms(raw: str) -> tuple[int, int]:
    """Resolve `COPY_DATE` (YYYY-MM-DD, IST) to `[day_start_ms, day_end_ms)`.

    A plain date is treated as IST so the window matches the operator's day,
    the same rule `BACKFILL_SINCE_IST` follows. An empty value disables the
    date-only copy mode and returns `(0, 0)`.
    """
    text = raw.strip()
    if not text:
        return 0, 0
    try:
        day = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("COPY_DATE must be YYYY-MM-DD (for example 2026-11-08).") from exc
    start = day.replace(tzinfo=IST_TIMEZONE)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _load_settings_from_env() -> AppwriteSettings:
    api_id_raw = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    session_string = os.getenv("SESSION_STRING", "").strip()
    destination_raw = os.getenv("DESTINATION_CHANNEL_ID", "").strip()
    limit_raw = os.getenv("LIMIT_PER_SOURCE", "50").strip()
    lookback_raw = os.getenv("LOOKBACK_MINUTES", "60").strip()
    backfill_enabled_raw = os.getenv("BACKFILL_ENABLED", "0").strip()
    backfill_since_raw = os.getenv("BACKFILL_SINCE_IST", "").strip()
    backfill_days_raw = os.getenv("BACKFILL_DAYS", "2").strip() or "2"
    backfill_chunk_raw = os.getenv("BACKFILL_CHUNK_SIZE", "100").strip() or "100"
    time_budget_raw = os.getenv("RUN_TIME_BUDGET_SECONDS", "240").strip() or "240"
    copy_date = os.getenv("COPY_DATE", "").strip()
    if copy_date.lower() in ("none", "off", "no", "-", "clear"):
        # Appwrite variables cannot always be blanked or deleted from the UI;
        # these words mean "not set" so date mode stays off.
        copy_date = ""
    copy_all = _env_flag("COPY_ALL")

    if not api_id_raw:
        raise ValueError("API_ID is required.")
    if not api_hash:
        raise ValueError("API_HASH is required.")
    if not session_string:
        raise ValueError("SESSION_STRING is required for user-session mode.")
    if not destination_raw:
        raise ValueError("DESTINATION_CHANNEL_ID is required.")

    source_channel_ids = _parse_source_ids()
    if not source_channel_ids:
        raise ValueError("Set SOURCE_CHANNEL_ID or SOURCE_CHANNEL_IDS.")

    try:
        api_id = int(api_id_raw)
        destination_channel_id = int(destination_raw)
        limit_per_source = int(limit_raw)
        lookback_minutes = int(lookback_raw)
        backfill_days = int(backfill_days_raw)
        backfill_chunk_size = int(backfill_chunk_raw)
        backfill_time_budget_seconds = int(time_budget_raw)
    except ValueError as exc:
        raise ValueError(
            "API_ID, DESTINATION_CHANNEL_ID, LIMIT_PER_SOURCE, LOOKBACK_MINUTES, "
            "BACKFILL_DAYS, BACKFILL_CHUNK_SIZE, and RUN_TIME_BUDGET_SECONDS must be integers."
        ) from exc

    if limit_per_source < 1 or limit_per_source > 200:
        raise ValueError("LIMIT_PER_SOURCE must be between 1 and 200.")
    if lookback_minutes < 1 or lookback_minutes > 1440:
        raise ValueError("LOOKBACK_MINUTES must be between 1 and 1440.")
    if backfill_days < 0 or backfill_days > 3650:
        raise ValueError("BACKFILL_DAYS must be between 0 and 3650.")
    if backfill_chunk_size < 1 or backfill_chunk_size > 1000:
        raise ValueError("BACKFILL_CHUNK_SIZE must be between 1 and 1000.")
    if backfill_time_budget_seconds < 30 or backfill_time_budget_seconds > 280:
        raise ValueError("RUN_TIME_BUDGET_SECONDS must be between 30 and 280.")
    if copy_date and _env_flag("BACKFILL_ENABLED"):
        raise ValueError("Set either COPY_DATE or BACKFILL_ENABLED, not both.")
    if copy_all and copy_date:
        raise ValueError(
            f"COPY_ALL=1 is set but COPY_DATE is still '{copy_date}'. "
            "Delete the COPY_DATE variable (or set it to 'none') to copy the whole channel, "
            "or set COPY_ALL=0 to copy only that date."
        )
    if copy_all and _env_flag("BACKFILL_ENABLED"):
        # Both walk history with their own progress keys; running them together
        # would spend the run budget twice on the same messages.
        raise ValueError("Set either COPY_ALL or BACKFILL_ENABLED, not both.")
    copy_date_start_ms, copy_date_end_ms = _parse_copy_date_range_ms(copy_date)

    backfill_since_ms = (
        _parse_backfill_since_ms(backfill_since_raw, backfill_days) if _env_flag("BACKFILL_ENABLED") else 0
    )

    return AppwriteSettings(
        api_id=api_id,
        api_hash=api_hash,
        session_string=session_string,
        source_channel_ids=source_channel_ids,
        destination_channel_id=destination_channel_id,
        limit_per_source=limit_per_source,
        lookback_minutes=lookback_minutes,
        backfill_enabled=_env_flag("BACKFILL_ENABLED"),
        backfill_since_ms=backfill_since_ms,
        backfill_chunk_size=backfill_chunk_size,
        backfill_time_budget_seconds=backfill_time_budget_seconds,
        copy_date=copy_date,
        copy_date_start_ms=copy_date_start_ms,
        copy_date_end_ms=copy_date_end_ms,
        copy_all=copy_all,
    )


def _respond_json(context: Any, payload: dict[str, Any], status_code: int = 200):
    res = context.res
    try:
        return res.json(payload, status_code)
    except TypeError:
        try:
            return res.json(payload)
        except TypeError:
            try:
                return res.send(json.dumps(payload), status_code)
            except Exception:
                return payload


def _safe_log(context: Any, message: str, is_error: bool = False, echo_native: bool = True) -> None:
    logged = False
    logger = getattr(context, "error", None) if is_error else getattr(context, "log", None)
    if callable(logger):
        try:
            logger(message)
            logged = True
        except Exception:
            logged = False
    # Native logs fallback (shown by Appwrite as "Native logs detected")
    if echo_native or not logged:
        print(message, flush=True)


def _source_sort_key(source_key: str) -> tuple[int, Any]:
    try:
        return (0, int(source_key))
    except (TypeError, ValueError):
        return (1, str(source_key))


def _format_source_run_line(source_key: str, stats: dict[str, Any]) -> str:
    window_count = int(stats.get("window_count", 0) or 0)
    window_min = int(stats.get("window_min_id", 0) or 0)
    window_max = int(stats.get("window_max_id", 0) or 0)
    window_text = f"{window_min}-{window_max}({window_count})" if window_count else "none"
    parts = [
        f"source={source_key}",
        f"f={int(stats.get('fetched', 0) or 0)}",
        f"c={int(stats.get('copied', 0) or 0)}",
    ]
    for label, field in (
        ("g", "gate_skipped"),
        ("r", "recent_skipped"),
        ("a", "blocked_ad"),
        ("i", "blocked_gif"),
    ):
        value = int(stats.get(field, 0) or 0)
        if value:
            parts.append(f"{label}={value}")
    parts.append(f"w={window_text}")
    status = stats.get("status") or "ok"
    if status != "ok":
        parts.append(f"status={status}")
    error = stats.get("error")
    if error:
        parts.append(f"error={str(error).replace(chr(10), ' ')[:160]}")
    return " ".join(parts)


def _extract_state(raw_text: str) -> dict[str, Any]:
    if not raw_text.startswith(STATE_MARKER):
        return {}
    try:
        payload = json.loads(raw_text[len(STATE_MARKER) :])
    except json.JSONDecodeError:
        return {}

    # Backward compatibility:
    # - old format: {"-100123": 456}
    # - new format: {"sources": {"-100123": {"last_id": 456, "recent": [..]}}}
    if isinstance(payload, dict) and all(isinstance(v, int) for v in payload.values()):
        return {"sources": {str(k): {"last_id": int(v), "recent": []} for k, v in payload.items()}}
    if isinstance(payload, dict) and isinstance(payload.get("sources"), dict):
        return payload
    return {"sources": {}}


def _get_source_state(state: dict[str, Any], source_key: str) -> tuple[int, list[int]]:
    sources = state.setdefault("sources", {})
    entry = sources.get(source_key)
    if not isinstance(entry, dict):
        return 0, []
    last_id = int(entry.get("last_id", 0)) if isinstance(entry.get("last_id", 0), int) else 0
    recent_raw = entry.get("recent", [])
    recent = [int(x) for x in recent_raw if isinstance(x, int)]
    return last_id, recent


def _set_source_state(state: dict[str, Any], source_key: str, last_id: int, recent: list[int]) -> None:
    dedup_recent: list[int] = []
    seen: set[int] = set()
    for mid in recent:
        if mid in seen:
            continue
        seen.add(mid)
        dedup_recent.append(mid)
    if len(dedup_recent) > MAX_RECENT_IDS:
        dedup_recent = dedup_recent[-MAX_RECENT_IDS:]
    sources = state.setdefault("sources", {})
    entry = sources.get(source_key)
    if not isinstance(entry, dict):
        entry = {}
        sources[source_key] = entry
    # Merge instead of replace so backfill progress fields survive live updates.
    entry["last_id"] = int(last_id)
    entry["recent"] = dedup_recent
    entry["updated_at_ist"] = _now_ist_iso()
    entry["updated_at_utc"] = _now_utc_iso()


def _set_run_meta(state: dict[str, Any], **kwargs: Any) -> None:
    meta = state.setdefault("meta", {})
    for key, value in kwargs.items():
        meta[key] = value


def _rotated_live_source_ids(state: dict[str, Any], source_ids: tuple[int, ...]) -> tuple[int, ...]:
    """Order sources round-robin so one busy channel cannot starve the others.

    The run budget stops delivery mid-source, so without rotation whichever
    source is listed first keeps the whole budget run after run and the rest are
    never reached while it still has a backlog.
    """
    if not source_ids:
        return ()
    meta = state.get("meta")
    raw_index = meta.get("next_source_index", 0) if isinstance(meta, dict) else 0
    try:
        index = int(raw_index) % len(source_ids)
    except (TypeError, ValueError):
        index = 0
    _set_run_meta(state, next_source_index=(index + 1) % len(source_ids))
    return tuple(source_ids[index:]) + tuple(source_ids[:index])


def _is_ad_footer_line(line: str) -> bool:
    """True when a line is a promotional/ad footer (marketing text + Telegram link)."""
    stripped = line.strip()
    if not stripped:
        return False
    link_match = AD_FOOTER_LINK_PATTERN.search(stripped)
    if link_match is None:
        return False
    username = link_match.group(1)
    return bool(
        AD_FOOTER_KEYWORD_PATTERN.search(stripped)
        or AD_FOOTER_KEYWORD_PATTERN.search(username)
    )


def _scrub_ad_footer_lines(text: str) -> str:
    """Drop promotional/ad footer lines while keeping all other content."""
    return "\n".join("" if _is_ad_footer_line(line) else line for line in text.split("\n"))


def _block_footer_ad_messages() -> bool:
    return os.getenv("BLOCK_FOOTER_AD_MESSAGES", "1").strip().lower() in ("1", "true", "yes", "on")


def _sanitize_message_text(text: str) -> str:
    modified = False
    strip_t_links = os.getenv("STRIP_T_LINKS", "0").strip().lower() in ("1", "true", "yes", "on")
    strip_youtube = os.getenv("STRIP_YOUTUBE_LINKS", "0").strip().lower() in ("1", "true", "yes", "on")
    bad_words_csv = os.getenv("FILTER_BAD_WORDS", "").strip()

    # Footer/promotional signature phrases are always stripped. Removing a
    # phrase drops that line only when the line consisted entirely of footer
    # text, so intentional blank lines elsewhere in the message are preserved.
    lines = text.split("\n")
    out_lines: list[str] = []
    for line in lines:
        scrubbed = line
        for phrase in FOOTER_PHRASES:
            updated = scrubbed.replace(phrase, "")
            if updated != scrubbed:
                modified = True
            scrubbed = updated
        # Drop whole promotional/ad footer lines (word + attached Telegram link).
        if _is_ad_footer_line(scrubbed):
            scrubbed = ""
            modified = True
        if scrubbed.strip() == "" and line.strip() != "":
            continue
        out_lines.append(scrubbed)
    cleaned = "\n".join(out_lines)

    # Optional filters; defaults preserve links exactly as in source.
    if strip_t_links:
        updated = TELEGRAM_LINK_PATTERN.sub("", cleaned)
        modified = modified or updated != cleaned
        cleaned = updated
    if strip_youtube:
        updated = YOUTUBE_PATTERN.sub("", cleaned)
        modified = modified or updated != cleaned
        cleaned = updated
    if bad_words_csv:
        bad_words = [re.escape(part.strip()) for part in bad_words_csv.split(",") if part.strip()]
        if bad_words:
            bad_words_pattern = re.compile(r"\b(?:" + "|".join(bad_words) + r")\b", re.IGNORECASE)
            updated = bad_words_pattern.sub("", cleaned)
            modified = modified or updated != cleaned
            cleaned = updated

    if not modified:
        return text

    # Normalize spaces introduced by removals (intentional blank lines are kept).
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    return cleaned.strip()


COPYRIGHT_NOTICE_PATTERNS = (
    "couldn't be displayed on your device",
    "could not be displayed on your device",
    "can't be displayed on your device",
    "cannot be displayed on your device",
)


def _block_copyright_messages() -> bool:
    return os.getenv("BLOCK_COPYRIGHT_MESSAGES", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _is_copyright_blocked_text(text: str) -> bool:
    """True for the placeholder Telegram shows instead of copyright-blocked media.

    Restricted media is delivered as a notice such as "This message couldn't be
    displayed on your device due to copyright infringement.". Forwarding it only
    copies the notice, never the real post, so it is dropped.
    """
    value = (text or "").lower()
    if "copyright infringement" not in value:
        return False
    normalized = " ".join(value.replace("\u2019", "'").replace("\u2018", "'").split())
    return any(pattern in normalized for pattern in COPYRIGHT_NOTICE_PATTERNS)


def _should_skip_message_text(text: str) -> bool:
    value = text or ""
    if _block_copyright_messages() and _is_copyright_blocked_text(value):
        return True
    if AD_BLOCK_STRICT_PATTERN.search(value):
        return True
    block_generic_ad_word = os.getenv("BLOCK_GENERIC_AD_WORD", "0").strip().lower() in ("1", "true", "yes", "on")
    if block_generic_ad_word and AD_BLOCK_GENERIC_WORD_PATTERN.search(value):
        return True
    # Strict mode: skip ANY message that contains an ad/footer signature, even if
    # it also carries real content (BLOCK_ANY_FOOTER_AD_MESSAGE=1).
    if os.getenv("BLOCK_ANY_FOOTER_AD_MESSAGE", "0").strip().lower() in ("1", "true", "yes", "on"):
        if any(_is_ad_footer_line(line) for line in value.split("\n")):
            return True
    # Default no-copy rule: a message that is *only* promotional/ad footer content
    # (marketing text + Telegram link, nothing else) is skipped entirely instead of
    # being copied. Normal messages merely appending a footer are still copied with
    # the footer stripped (see _sanitize_message_text).
    if _block_footer_ad_messages() and value.strip():
        leftovers = _scrub_ad_footer_lines(value)
        leftovers = TELEGRAM_LINK_PATTERN.sub("", leftovers)
        if leftovers.strip() == "":
            return True
    return False


def _is_cursor_gate_enabled() -> bool:
    return os.getenv("ENABLE_CURSOR_GATE", "1").strip().lower() in ("1", "true", "yes", "on")


def _force_full_lookback_copy() -> bool:
    """When enabled, every run copies ALL messages inside the lookback window,
    ignoring the saved cursor and the recent processed-list (no dedupe skip).
    Useful for manual backfill / resync executions. Ad/GIF blocking still applies.
    """
    return os.getenv("FORCE_FULL_LOOKBACK_COPY", "0").strip().lower() in ("1", "true", "yes", "on")


def _is_copy_mode() -> bool:
    """Repost content as the destination channel's own post (no forward header).

    Default is copy mode. Set COPY_MODE=0 (or COPY_MODE=forward) to restore
    Telegram forward attribution.
    """
    value = os.getenv("COPY_MODE", "copy").strip().lower()
    return value not in ("0", "false", "no", "off", "forward", "forwarded")


def _is_snapshot_recovery_enabled() -> bool:
    return os.getenv("ENABLE_SNAPSHOT_RECOVERY", "0").strip().lower() in ("1", "true", "yes", "on")


def _is_telegram_state_fallback_enabled() -> bool:
    return os.getenv("ENABLE_TELEGRAM_STATE_FALLBACK", "0").strip().lower() in ("1", "true", "yes", "on")


async def _resolve_awaitable(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _prime_source_entities(client: Any, source_ids: tuple[int, ...]) -> tuple[dict[int, Any], dict[str, str]]:
    """Resolve numeric peers after a fresh StringSession starts.

    Telethon cannot build an InputPeerChannel from a bare numeric ID unless the
    session already has that channel's access hash in its entity cache. Loading
    dialogs once populates that cache for channels the account can access.
    """
    get_input_entity = getattr(client, "get_input_entity", None)
    if not callable(get_input_entity):
        return {}, {}

    resolved: dict[int, Any] = {}
    errors: dict[str, str] = {}
    unresolved: list[int] = []
    username_values = [
        value.strip()
        for value in os.getenv("SOURCE_CHANNEL_USERNAMES", "").split(",")
        if value.strip()
    ]
    usernames = {
        source_id: username_values[index]
        for index, source_id in enumerate(source_ids)
        if index < len(username_values)
    }
    for source_id in source_ids:
        try:
            resolved[source_id] = await _resolve_awaitable(get_input_entity(source_id))
        except Exception:
            unresolved.append(source_id)

    if unresolved:
        still_unresolved: list[int] = []
        for source_id in unresolved:
            username = usernames.get(source_id)
            if not username:
                still_unresolved.append(source_id)
                continue
            try:
                resolved[source_id] = await _resolve_awaitable(get_input_entity(username))
            except Exception:
                still_unresolved.append(source_id)
        unresolved = still_unresolved

    if unresolved:
        get_dialogs = getattr(client, "get_dialogs", None)
        if callable(get_dialogs):
            try:
                await _resolve_awaitable(get_dialogs(limit=None))
            except Exception as exc:
                for source_id in unresolved:
                    errors[str(source_id)] = (
                        f"Could not load Telegram dialogs while resolving source {source_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                return resolved, errors
        for source_id in unresolved:
            try:
                resolved[source_id] = await _resolve_awaitable(get_input_entity(source_id))
            except Exception as exc:
                # Keep the original peer error visible in the run result while
                # allowing other configured sources to continue.
                errors[str(source_id)] = (
                    f"Could not resolve source {source_id}. Ensure the Telegram session can access it "
                    f"and that it appears in the account's dialogs. Original error: {exc}"
                )
    return resolved, errors


async def _fetch_recent_messages(
    client: Any,
    source_id: int,
    last_seen_id: int,
    cutoff_dt: datetime,
    source_entity: Any | None = None,
) -> list[Any]:
    """Fetch the messages this live run has to deliver for one source.

    With a stored cursor the fetch is cursor-based (`min_id`), oldest-first, and
    has no time window: everything posted after the last handled message is
    fetched, up to `LIVE_FETCH_CEILING`. A 1,000- or 5,000-message backlog is
    therefore never dropped just because it is older than `LOOKBACK_MINUTES`,
    and because the cursor only advances past messages a run actually delivered,
    the next hourly run resumes exactly where this one stopped.

    With no cursor yet (fresh state, new source) the `LOOKBACK_MINUTES` window
    still applies, so a first run never walks a whole channel history.
    """
    target = source_entity if source_entity is not None else source_id
    messages: list[Any] = []
    if last_seen_id > 0:
        async for msg in client.iter_messages(
            target,
            min_id=int(last_seen_id),
            reverse=True,
            limit=LIVE_FETCH_CEILING,
        ):
            msg_id = int(getattr(msg, "id", 0))
            if msg_id > last_seen_id:
                messages.append(msg)
    else:
        async for msg in client.iter_messages(target):
            msg_id = int(getattr(msg, "id", 0))
            if msg_id <= 0:
                continue
            msg_date = getattr(msg, "date", None)
            if msg_date is None:
                continue
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            # Dates are descending as well; once below cutoff, the rest are out of range.
            if msg_date < cutoff_dt:
                break
            messages.append(msg)

    messages.sort(key=lambda m: int(getattr(m, "id", 0)))
    return messages


def _has_sendable_media(message: Any) -> bool:
    """True when the message has media that can be resent with send_file."""
    return _sendable_media_kind(message) is not None


def _sendable_media_kind(message: Any) -> str | None:
    """Return the media kind Telethon send_file can resend, else None.

    Polls, geo points, dice, contacts and games are media too, but send_file
    rejects them, so those must be reposted as text.
    """
    media = getattr(message, "media", None)
    if not media:
        return None
    class_name = media.__class__.__name__
    if class_name == "MessageMediaPhoto":
        return "photo"
    if class_name == "MessageMediaDocument":
        return "document"
    return None


def _is_gif_message(message: Any) -> bool:
    if bool(getattr(message, "gif", False)):
        return True
    media = getattr(message, "media", None)
    if not media:
        return False
    document = getattr(media, "document", None)
    if not document:
        return False
    mime_type = (getattr(document, "mime_type", "") or "").lower()
    if mime_type == "image/gif":
        return True
    has_video_attr = False
    has_animated_attr = False
    video_supports_streaming = None
    for attr in (getattr(document, "attributes", None) or []):
        if attr.__class__.__name__ == "DocumentAttributeAnimated":
            has_animated_attr = True
            return True
        if attr.__class__.__name__ == "DocumentAttributeVideo":
            has_video_attr = True
            video_supports_streaming = bool(getattr(attr, "supports_streaming", False))
        file_name = (getattr(attr, "file_name", "") or "").lower()
        if file_name.endswith(".gif"):
            return True
    # Telegram GIFs are frequently delivered as non-streaming MP4 animations.
    if mime_type == "video/mp4" and (has_animated_attr or (has_video_attr and video_supports_streaming is False)):
        return True
    return False


async def _load_state(client: Any) -> tuple[dict[str, Any], str | int | None]:
    if _is_db_state_enabled():
        try:
            documents = _appwrite_list_documents(
                database_id=_state_database_id(),
                collection_id=_state_collection_id(),
                queries=[_query_equal("state_key", "global"), _query_limit(1)],
            )
            if documents:
                document = documents[0]
                document_id = document.get("$id")
                row_id = str(document_id) if isinstance(document_id, str) and document_id else None
                payload_json = document.get("payload_json")
                if isinstance(payload_json, str):
                    try:
                        payload_json = json.loads(payload_json)
                    except Exception:
                        payload_json = None
                if isinstance(payload_json, dict):
                    if isinstance(payload_json.get("sources"), dict):
                        return payload_json, row_id
                payload_text = document.get("payload", "")
                if isinstance(payload_text, str) and payload_text:
                    parsed = _extract_state(payload_text)
                    if parsed:
                        return parsed, row_id
                # The row exists but holds no usable checkpoint. Return its real
                # row ID so saves update it instead of creating a duplicate
                # `state_key=global` row and colliding on the unique index.
                return {"sources": {}}, row_id
        except Exception:
            # Non-fatal: fallback to legacy Telegram state.
            pass

    if not _is_telegram_state_fallback_enabled():
        return {}, None

    # Legacy fallback for migration safety (opt-in).
    try:
        async for message in client.iter_messages("me", search=STATE_MARKER, limit=1):
            text = (message.message or "").strip()
            if text.startswith(STATE_MARKER):
                return _extract_state(text), message.id
    except Exception:
        pass

    try:
        messages = await client.get_messages("me", limit=200)
    except Exception:
        messages = []
    for message in messages:
        text = (getattr(message, "message", "") or "").strip()
        if text.startswith(STATE_MARKER):
            return _extract_state(text), getattr(message, "id", None)
    return {}, None


async def _save_state(client: Any, state: dict[str, Any], state_message_id: str | int | None) -> tuple[str | int | None, bool]:
    global DB_STATE_LAST_ERROR
    state_payload = _build_state_payload(state)
    if _is_db_state_enabled():
        database_id = _state_database_id()
        collection_id = _state_collection_id()
        last_db_exc: Exception | None = None
        state_payload_max_raw = os.getenv("DB_STATE_PAYLOAD_MAX_CHARS", "3600").strip()
        try:
            state_payload_max = int(state_payload_max_raw)
        except ValueError:
            state_payload_max = 1800
        state_payload_max = max(120, min(MAX_STATE_PAYLOAD_CHARS, state_payload_max))
        state_payload_db = _build_state_payload_with_limit(state=state, max_chars=state_payload_max)
        state_json = _compact_state_for_save(state)
        payload = {
            "state_key": "global",
            # Store as JSON text because the deployed Appwrite schema uses a longtext column.
            "payload_json": json.dumps(state_json, separators=(",", ":"), ensure_ascii=True),
            "payload": state_payload_db,
            "run_ts_ms": int(time.time() * 1000),
        }
        if not isinstance(state_message_id, str):
            # Reuse the existing `state_key=global` row instead of guessing an ID
            # and colliding with its unique index.
            state_message_id = _find_existing_state_record_id(database_id, collection_id) or STATE_ROW_ID
        for attempt in (1, 2, 3):
            try:
                if isinstance(state_message_id, str):
                    try:
                        _appwrite_request_json(
                            method="PATCH",
                            path=f"/tablesdb/{database_id}/tables/{collection_id}/rows/{state_message_id}",
                            payload={"data": payload},
                        )
                    except Exception as patch_exc:
                        patch_text = str(patch_exc).lower()
                        if "404" in patch_text or "not found" in patch_text:
                            try:
                                _appwrite_request_json(
                                    method="PATCH",
                                    path=f"/databases/{database_id}/collections/{collection_id}/documents/{state_message_id}",
                                    payload={"data": payload},
                                )
                            except Exception as legacy_patch_exc:
                                legacy_patch_text = str(legacy_patch_exc).lower()
                                if "404" in legacy_patch_text or "not found" in legacy_patch_text:
                                    try:
                                        _appwrite_request_json(
                                            method="POST",
                                            path=f"/tablesdb/{database_id}/tables/{collection_id}/rows",
                                            payload={"rowId": state_message_id, "data": payload},
                                        )
                                    except Exception as create_tables_exc:
                                        create_tables_text = str(create_tables_exc).lower()
                                        if _is_unique_constraint_error(create_tables_exc):
                                            raise
                                        try:
                                            _appwrite_request_json(
                                                method="POST",
                                                path=f"/databases/{database_id}/collections/{collection_id}/documents",
                                                payload={"documentId": state_message_id, "data": payload},
                                            )
                                        except Exception as create_doc_exc:
                                            if _is_unique_constraint_error(create_doc_exc):
                                                raise
                                            raise
                                else:
                                    raise
                        else:
                            raise
                    DB_STATE_LAST_ERROR = ""
                    return state_message_id, True
                created = _appwrite_create_document(database_id=database_id, collection_id=collection_id, data=payload)
                created_id = created.get("$id")
                DB_STATE_LAST_ERROR = ""
                return (str(created_id) if isinstance(created_id, str) else state_message_id), True
            except Exception as exc:
                last_db_exc = exc
                if _is_unique_constraint_error(exc):
                    # Another deployment may have created the unique `global`
                    # record using a different ID. Re-discover it and patch it
                    # instead of retrying the same conflicting create.
                    existing_id = _find_existing_state_record_id(database_id, collection_id)
                    if existing_id:
                        state_message_id = existing_id
                    continue
                if _strip_unknown_attribute_from_error(payload, str(exc)):
                    continue
                if _adjust_json_field_by_error(payload, "payload_json", str(exc)):
                    continue
                if attempt == 1:
                    # Retry with ultra-compact payload for strict schemas.
                    payload["payload"] = _build_state_payload_with_limit(state=state, max_chars=240)
                    time.sleep(0.25)
                    continue
                DB_STATE_LAST_ERROR = f"{type(exc).__name__}: {exc}"
                return state_message_id, False
        if last_db_exc is not None:
            DB_STATE_LAST_ERROR = f"{type(last_db_exc).__name__}: {last_db_exc}"
            return state_message_id, False

    if not _is_telegram_state_fallback_enabled():
        DB_STATE_LAST_ERROR = _db_state_disable_reason() or "db_state_unavailable_and_telegram_fallback_disabled"
        return state_message_id, False

    # Legacy fallback (opt-in).
    try:
        from telethon.errors.rpcerrorlist import MessageNotModifiedError

        if isinstance(state_message_id, int) and state_message_id:
            try:
                await client.edit_message("me", state_message_id, state_payload)
                return state_message_id, True
            except MessageNotModifiedError:
                return state_message_id, False
            except Exception:
                sent = await client.send_message("me", state_payload)
                return getattr(sent, "id", None), True
        sent = await client.send_message("me", state_payload)
        return getattr(sent, "id", None), True
    except Exception:
        DB_STATE_LAST_ERROR = "telegram_fallback_failed"
        return state_message_id, False


def _group_message_ids(messages: list[Any]) -> list[tuple[bool, list[int]]]:
    grouped: list[tuple[bool, list[int]]] = []
    current_album_key = None
    current_album_ids: list[int] = []

    def flush_album() -> None:
        nonlocal current_album_key, current_album_ids
        if current_album_ids:
            grouped.append((True, current_album_ids))
            current_album_key = None
            current_album_ids = []

    for message in messages:
        grouped_id = getattr(message, "grouped_id", None)
        message_id = int(getattr(message, "id", 0))
        if message_id <= 0:
            continue

        if grouped_id is None:
            flush_album()
            grouped.append((False, [message_id]))
            continue

        if current_album_key is None or current_album_key != grouped_id:
            flush_album()
            current_album_key = grouped_id
            current_album_ids = [message_id]
            continue

        current_album_ids.append(message_id)

    flush_album()
    return grouped


def _get_backfill_state(
    state: dict[str, Any], source_key: str, prefix: str = "backfill"
) -> dict[str, Any]:
    """Read newest-to-oldest walk progress stored under `prefix_*` keys."""
    entry = (state.get("sources") or {}).get(source_key)
    if not isinstance(entry, dict):
        return {"max_id": 0, "done": 0, "floor_ms": 0, "scanned": 0, "copied": 0}
    return {
        "max_id": int(entry.get(f"{prefix}_max_id", 0) or 0),
        "done": int(entry.get(f"{prefix}_done", 0) or 0),
        "floor_ms": int(entry.get(f"{prefix}_floor_ms", 0) or 0),
        "scanned": int(entry.get(f"{prefix}_scanned", 0) or 0),
        "copied": int(entry.get(f"{prefix}_copied", 0) or 0),
    }


def _set_backfill_state(
    state: dict[str, Any],
    source_key: str,
    *,
    max_id: int,
    done: int,
    floor_ms: int,
    scanned: int,
    copied: int,
    prefix: str = "backfill",
) -> None:
    sources = state.setdefault("sources", {})
    entry = sources.get(source_key)
    if not isinstance(entry, dict):
        entry = {"last_id": 0, "recent": []}
        sources[source_key] = entry
    entry[f"{prefix}_max_id"] = int(max_id)
    entry[f"{prefix}_done"] = int(done)
    entry[f"{prefix}_floor_ms"] = int(floor_ms)
    entry[f"{prefix}_scanned"] = int(scanned)
    entry[f"{prefix}_copied"] = int(copied)
    entry[f"{prefix}_updated_at_ist"] = _now_ist_iso()


def _get_date_state(state: dict[str, Any], source_key: str, copy_date: str) -> dict[str, int]:
    """Progress for the currently configured `COPY_DATE`, keyed on the date itself.

    A different date returns an empty progress record, so changing `COPY_DATE`
    always restarts the window without needing an explicit reset.
    """
    entry = (state.get("sources") or {}).get(source_key)
    if not isinstance(entry, dict) or str(entry.get("copy_date", "")) != copy_date:
        return {"max_id": 0, "done": 0, "scanned": 0, "copied": 0}
    return {
        "max_id": int(entry.get("copy_date_max_id", 0) or 0),
        "done": int(entry.get("copy_date_done", 0) or 0),
        "scanned": int(entry.get("copy_date_scanned", 0) or 0),
        "copied": int(entry.get("copy_date_copied", 0) or 0),
    }


def _set_date_state(
    state: dict[str, Any],
    source_key: str,
    copy_date: str,
    *,
    max_id: int,
    done: int,
    scanned: int,
    copied: int,
) -> None:
    sources = state.setdefault("sources", {})
    entry = sources.get(source_key)
    if not isinstance(entry, dict):
        entry = {"last_id": 0, "recent": []}
        sources[source_key] = entry
    entry["copy_date"] = copy_date
    entry["copy_date_max_id"] = int(max_id)
    entry["copy_date_done"] = int(done)
    entry["copy_date_scanned"] = int(scanned)
    entry["copy_date_copied"] = int(copied)
    entry["copy_date_updated_at_ist"] = _now_ist_iso()


def _run_deadline_monotonic(budget_seconds: int) -> float:
    return time.monotonic() + max(1, int(budget_seconds))


def _deadline_reached(deadline_monotonic: float) -> bool:
    return time.monotonic() >= deadline_monotonic


async def _fetch_backfill_chunk(
    client: Any,
    source_id: int,
    max_id: int,
    floor_dt: datetime | None,
    chunk_size: int,
    source_entity: Any | None = None,
) -> tuple[list[Any], bool]:
    """Fetch one newest-to-oldest backfill chunk ending below `max_id`."""
    target = source_entity if source_entity is not None else source_id
    kwargs: dict[str, Any] = {"limit": int(chunk_size)}
    if max_id > 0:
        kwargs["offset_id"] = int(max_id)

    messages: list[Any] = []
    reached_floor = False
    async for msg in client.iter_messages(target, **kwargs):
        msg_id = int(getattr(msg, "id", 0))
        if msg_id <= 0:
            continue
        msg_date = getattr(msg, "date", None)
        if msg_date is None:
            continue
        if msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=timezone.utc)
        if floor_dt is not None and msg_date < floor_dt:
            reached_floor = True
            break
        messages.append(msg)

    done = reached_floor or len(messages) < int(chunk_size)
    messages.sort(key=lambda m: int(getattr(m, "id", 0)))
    return messages, done


async def _fetch_date_chunk(
    client: Any,
    source_id: int,
    start_dt: datetime,
    end_dt: datetime,
    max_id: int,
    chunk_size: int,
    source_entity: Any | None = None,
) -> tuple[list[Any], bool]:
    """Fetch one newest-to-oldest chunk of the COPY_DATE window `[start_dt, end_dt)`.

    `offset_date` is what makes this a dated fetch instead of a relative one:
    the first chunk starts at the end of the requested day, later chunks resume
    with `offset_id` so a busy day can be copied across several executions.
    """
    target = source_entity if source_entity is not None else source_id
    kwargs: dict[str, Any] = {"limit": int(chunk_size)}
    if max_id > 0:
        kwargs["offset_id"] = int(max_id)
    else:
        kwargs["offset_date"] = end_dt

    messages: list[Any] = []
    fetched = 0
    lowest_window_id = 0
    lowest_older_id = 0
    async for msg in client.iter_messages(target, **kwargs):
        fetched += 1
        msg_id = int(getattr(msg, "id", 0))
        if msg_id <= 0:
            continue
        msg_date = getattr(msg, "date", None)
        if msg_date is None:
            continue
        if msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=timezone.utc)
        if msg_date >= end_dt:
            # Boundary noise at/above the window; never part of the day.
            continue
        if msg_date < start_dt:
            lowest_older_id = msg_id if not lowest_older_id else min(lowest_older_id, msg_id)
            continue
        messages.append(msg)
        lowest_window_id = msg_id if not lowest_window_id else min(lowest_window_id, msg_id)

    # Two rules keep a day complete:
    # 1. Never stop at the first message dated before the day. Channels whose
    #    dates are not perfectly ordered used to end the window there, which is
    #    the "only some messages were copied" symptom.
    # 2. `done` counts every message the walk returned, not just the in-window
    #    ones: a full batch is never treated as the end of the day just because
    #    part of it fell outside the window.
    crossed_start = lowest_older_id > 0 and (
        lowest_window_id == 0 or lowest_older_id < lowest_window_id
    )
    done = fetched == 0 or fetched < int(chunk_size) or crossed_start
    messages.sort(key=lambda m: int(getattr(m, "id", 0)))
    return messages, done


async def _deliver_group(
    client: Any,
    destination_channel_id: int,
    source_id: int,
    messages: list[Any],
    group_ids: list[int],
    source_entity_kwargs: dict[str, Any],
    recent_ids: list[int],
    recent_set: set[int],
) -> tuple[int, int, set[int]]:
    """Block ads/GIFs, then forward one album or single message.

    Returns `(copied, skipped, handled_ids)`. Used by the date-only phase; it
    mirrors the delivery rules of `_run_backfill_phase` so both paths filter and
    group messages identically.
    """
    group_by_id = {int(getattr(msg, "id", 0)): msg for msg in messages}
    blocked_ids = sorted(
        {
            mid
            for mid in group_ids
            if _should_skip_message_text((getattr(group_by_id.get(mid), "message", "") or "").strip())
        }
        | {mid for mid in group_ids if _is_gif_message(group_by_id.get(mid))}
    )
    remaining = [mid for mid in group_ids if mid not in set(blocked_ids)]
    handled: set[int] = set(blocked_ids)
    copied = 0
    skipped = 0
    if blocked_ids:
        recent_ids.extend(blocked_ids)
        recent_set.update(blocked_ids)
        skipped += len(blocked_ids)
    if not remaining:
        return 0, skipped, handled

    if len(remaining) > 1:
        album_ids = set(remaining)
        album_messages = [msg for msg in messages if int(getattr(msg, "id", 0)) in album_ids]
        needs_sanitize = any(
            _sanitize_message_text(getattr(msg, "message", None) or "")
            != (getattr(msg, "message", "") or "")
            for msg in album_messages
        )
        if needs_sanitize:
            for msg in album_messages:
                copied_count, skipped_count, _filtered, processed_ids = await _copy_single_with_optional_sanitize(
                    client=client,
                    destination_channel_id=destination_channel_id,
                    source_id=source_id,
                    message=msg,
                    **source_entity_kwargs,
                )
                copied += copied_count
                skipped += skipped_count
                handled.update(processed_ids)
        else:
            copied_count, skipped_count, processed_ids = await _forward_with_fallback(
                client=client,
                destination_channel_id=destination_channel_id,
                source_id=source_id,
                message_ids=remaining,
                as_album=True,
                **source_entity_kwargs,
            )
            copied += copied_count
            skipped += skipped_count
            handled.update(processed_ids)
    else:
        single_message = group_by_id.get(remaining[0])
        if single_message is None:
            return 0, skipped, handled | set(remaining)
        copied_count, skipped_count, _filtered, processed_ids = await _copy_single_with_optional_sanitize(
            client=client,
            destination_channel_id=destination_channel_id,
            source_id=source_id,
            message=single_message,
            **source_entity_kwargs,
        )
        copied += copied_count
        skipped += skipped_count
        handled.update(processed_ids)

    if handled:
        ordered = sorted(handled)
        recent_ids.extend(ordered)
        recent_set.update(ordered)
    return copied, skipped, handled


async def _run_date_phase(
    client: Any,
    settings: AppwriteSettings,
    state: dict[str, Any],
    resolved_source_entities: dict[int, Any],
    entity_errors: dict[str, str],
    deadline_monotonic: float,
) -> dict[str, Any]:
    """Copy exactly one IST calendar day, newest-to-oldest and resumable.

    The live cursor (`last_id`) is deliberately never advanced, matching the
    backfill rule: replaying history must not make the scheduled lookback pass
    skip messages it has not reached yet.
    """
    stats: dict[str, Any] = {
        "enabled": int(bool(settings.copy_date)),
        "sources_done": 0,
        "sources_processed": 0,
        "copied": 0,
        "skipped": 0,
        "scanned": 0,
        "chunks": 0,
        "deadline_reached": 0,
        "reset": 0,
        "errors": {},
        "sources": {},
    }
    if not settings.copy_date:
        return stats

    start_dt = datetime.fromtimestamp(settings.copy_date_start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(settings.copy_date_end_ms / 1000, tz=timezone.utc)
    reset_requested = _env_flag("COPY_DATE_RESET")

    for source_id in settings.source_channel_ids:
        key = str(source_id)
        source_stats: dict[str, Any] = {"status": "ok", "fetched": 0, "copied": 0, "skipped": 0}
        stats["sources"][key] = source_stats
        if key in entity_errors:
            source_stats["status"] = "entity_error"
            source_stats["error"] = entity_errors.get(key, "")
            continue
        if _deadline_reached(deadline_monotonic):
            stats["deadline_reached"] = 1
            source_stats["status"] = "deadline"
            break

        progress = _get_date_state(state, key, settings.copy_date)
        if reset_requested:
            progress = {"max_id": 0, "done": 0, "scanned": 0, "copied": 0}
            stats["reset"] = 1
        if progress["done"]:
            stats["sources_done"] += 1
            source_stats["status"] = "already_done"
            continue

        source_entity = resolved_source_entities.get(source_id, source_id)
        source_entity_kwargs = (
            {"source_entity": source_entity} if source_id in resolved_source_entities else {}
        )
        max_id = int(progress.get("max_id") or 0)
        scanned_total = int(progress.get("scanned") or 0)
        copied_total = int(progress.get("copied") or 0)
        skipped_total = 0
        day_done = 0
        deadline_hit = False
        errored = False

        # Keep fetching chunks until the start of the requested day is reached.
        # One chunk per run was not enough: a busy day across several sources
        # needs many chunks, and the scheduled interval would take hours.
        while not _deadline_reached(deadline_monotonic):
            try:
                messages, chunk_done = await _fetch_date_chunk(
                    client=client,
                    source_id=source_id,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    max_id=max_id,
                    chunk_size=settings.backfill_chunk_size,
                    **source_entity_kwargs,
                )
            except Exception as exc:
                source_stats["status"] = "error"
                source_stats["error"] = f"{type(exc).__name__}: {exc}"
                stats["errors"][key] = source_stats["error"]
                _set_date_state(
                    state,
                    key,
                    settings.copy_date,
                    max_id=max_id,
                    done=0,
                    scanned=scanned_total,
                    copied=copied_total,
                )
                errored = True
                break

            if not source_stats.get("chunks"):
                stats["sources_processed"] += 1
            stats["chunks"] += 1
            source_stats["chunks"] = int(source_stats.get("chunks", 0) or 0) + 1

            chunk_ids = {
                int(getattr(msg, "id", 0)) for msg in messages if int(getattr(msg, "id", 0)) > 0
            }

            if not messages:
                day_done = int(chunk_done)
                _set_date_state(
                    state,
                    key,
                    settings.copy_date,
                    max_id=max_id,
                    done=day_done,
                    scanned=scanned_total,
                    copied=copied_total,
                )
                break

            last_seen_id, recent_ids = _get_source_state(state, key)
            recent_set = set(recent_ids)
            handled: set[int] = set()
            copied = 0
            skipped = 0

            for _is_album, group_ids in _group_message_ids(messages):
                if _deadline_reached(deadline_monotonic):
                    deadline_hit = True
                    break
                try:
                    copied_count, skipped_count, processed_ids = await _deliver_group(
                        client=client,
                        destination_channel_id=settings.destination_channel_id,
                        source_id=source_id,
                        messages=messages,
                        group_ids=list(group_ids),
                        source_entity_kwargs=source_entity_kwargs,
                        recent_ids=recent_ids,
                        recent_set=recent_set,
                    )
                except FloodHalt:
                    # Save progress so the next run resumes above the unhandled IDs,
                    # and never touch the live cursor.
                    unhandled = chunk_ids - handled
                    _set_source_state(state, key, last_seen_id, recent_ids)
                    _set_date_state(
                        state,
                        key,
                        settings.copy_date,
                        max_id=(max(unhandled) + 1) if unhandled else max_id,
                        done=0,
                        scanned=scanned_total + len(messages),
                        copied=copied_total + copied,
                    )
                    raise
                except Exception as exc:
                    # One uncopyable message or protected source must not abandon
                    # the whole day: keep draining the remaining groups and sources.
                    error_text = f"{type(exc).__name__}: {exc}"
                    stats["errors"].setdefault(key, error_text)
                    source_stats.setdefault("error", error_text)
                    skipped += len(group_ids)
                    handled.update(group_ids)
                    continue
                copied += copied_count
                skipped += skipped_count
                handled.update(processed_ids)

            scanned_total += len(messages)
            copied_total += copied
            skipped_total += skipped
            stats["scanned"] += len(messages)
            stats["copied"] += copied
            stats["skipped"] += skipped
            source_stats["fetched"] = scanned_total
            source_stats["copied"] = copied_total
            source_stats["skipped"] = skipped_total

            if deadline_hit:
                unhandled = chunk_ids - handled
                max_id = (max(unhandled) + 1) if unhandled else max_id
                day_done = 0
            else:
                max_id = min(chunk_ids) if chunk_ids else max_id
                day_done = int(chunk_done)

            _set_source_state(state, key, last_seen_id, recent_ids)
            _set_date_state(
                state,
                key,
                settings.copy_date,
                max_id=max_id,
                done=day_done,
                scanned=scanned_total,
                copied=copied_total,
            )
            if deadline_hit or day_done:
                break

        if not errored and not day_done and _deadline_reached(deadline_monotonic):
            # Budget ran out right at the next chunk fetch.
            deadline_hit = True

        source_stats["fetched"] = scanned_total
        source_stats["copied"] = copied_total
        source_stats["skipped"] = skipped_total
        if errored:
            continue
        if day_done:
            stats["sources_done"] += 1
            source_stats["status"] = "done"
        if deadline_hit:
            stats["deadline_reached"] = 1
            source_stats["status"] = "deadline"
            break

    return stats


async def _run_full_copy_phase(
    client: Any,
    settings: AppwriteSettings,
    state: dict[str, Any],
    resolved_source_entities: dict[int, Any],
    entity_errors: dict[str, str],
    deadline_monotonic: float,
) -> dict[str, Any]:
    """Copy the whole channel history, newest to oldest, in a single execution.

    `_run_backfill_phase` fetches exactly one chunk per source per run, so a
    5,000-message channel needs 50 scheduled executions. This phase keeps
    fetching chunks for the same source until the time budget or the start of
    the channel is reached, which is what "copy everything" actually needs.

    Progress is stored under `copy_all_*` keys so `backfill_*` and `copy_date*`
    progress is untouched, and the live cursor (`last_id`) is never advanced: the
    scheduled lookback pass must keep seeing messages this phase skipped.
    """
    stats: dict[str, Any] = {
        "enabled": int(bool(settings.copy_all)),
        "sources_done": 0,
        "sources_processed": 0,
        "copied": 0,
        "skipped": 0,
        "scanned": 0,
        "chunks": 0,
        "deadline_reached": 0,
        "reset": 0,
        "errors": {},
        "sources": {},
    }
    if not settings.copy_all:
        return stats

    reset_requested = _env_flag("COPY_ALL_RESET")

    for source_id in settings.source_channel_ids:
        key = str(source_id)
        source_stats: dict[str, Any] = {
            "status": "ok",
            "fetched": 0,
            "copied": 0,
            "skipped": 0,
            "chunks": 0,
        }
        stats["sources"][key] = source_stats
        if key in entity_errors:
            source_stats["status"] = "entity_error"
            source_stats["error"] = entity_errors.get(key, "")
            continue
        if _deadline_reached(deadline_monotonic):
            stats["deadline_reached"] = 1
            source_stats["status"] = "deadline"
            break

        progress = _get_backfill_state(state, key, prefix="copy_all")
        if reset_requested:
            progress = {"max_id": 0, "done": 0, "floor_ms": 0, "scanned": 0, "copied": 0}
            stats["reset"] = 1
        if progress["done"]:
            stats["sources_done"] += 1
            source_stats["status"] = "already_done"
            continue

        source_entity = resolved_source_entities.get(source_id, source_id)
        source_entity_kwargs = (
            {"source_entity": source_entity} if source_id in resolved_source_entities else {}
        )
        max_id = int(progress.get("max_id") or 0)
        source_scanned = int(progress.get("scanned") or 0)
        source_copied = int(progress.get("copied") or 0)
        source_skipped = 0
        source_done = int(progress.get("done") or 0)
        deadline_hit = False
        errored = False

        while not _deadline_reached(deadline_monotonic):
            try:
                messages, chunk_done = await _fetch_backfill_chunk(
                    client=client,
                    source_id=source_id,
                    max_id=max_id,
                    floor_dt=None,
                    chunk_size=settings.backfill_chunk_size,
                    **source_entity_kwargs,
                )
            except Exception as exc:
                source_stats["status"] = "error"
                source_stats["error"] = f"{type(exc).__name__}: {exc}"
                stats["errors"][key] = source_stats["error"]
                _set_backfill_state(
                    state,
                    key,
                    max_id=max_id,
                    done=0,
                    floor_ms=0,
                    scanned=source_scanned,
                    copied=source_copied,
                    prefix="copy_all",
                )
                errored = True
                break

            if not source_stats.get("chunks"):
                stats["sources_processed"] += 1
            stats["chunks"] += 1
            source_stats["chunks"] = int(source_stats.get("chunks", 0) or 0) + 1

            chunk_ids = {
                int(getattr(msg, "id", 0)) for msg in messages if int(getattr(msg, "id", 0)) > 0
            }
            if not messages:
                source_done = int(chunk_done)
                _set_backfill_state(
                    state,
                    key,
                    max_id=max_id,
                    done=source_done,
                    floor_ms=0,
                    scanned=source_scanned,
                    copied=source_copied,
                    prefix="copy_all",
                )
                break

            last_seen_id, recent_ids = _get_source_state(state, key)
            recent_set = set(recent_ids)
            handled: set[int] = set()
            chunk_copied = 0
            chunk_skipped = 0

            for _is_album, group_ids in _group_message_ids(messages):
                if _deadline_reached(deadline_monotonic):
                    deadline_hit = True
                    break
                # IDs delivered before (live pass or an earlier full copy) are
                # not sent again; they still count as handled so the walk moves on.
                # COPY_ALL_RESET means "redo the whole channel", so it re-sends
                # those IDs too (useful when the destination was wiped).
                message_ids = (
                    list(group_ids)
                    if reset_requested
                    else [mid for mid in group_ids if mid not in recent_set]
                )
                handled.update(set(group_ids) - set(message_ids))
                if not message_ids:
                    continue
                try:
                    copied_count, skipped_count, processed_ids = await _deliver_group(
                        client=client,
                        destination_channel_id=settings.destination_channel_id,
                        source_id=source_id,
                        messages=messages,
                        group_ids=list(message_ids),
                        source_entity_kwargs=source_entity_kwargs,
                        recent_ids=recent_ids,
                        recent_set=recent_set,
                    )
                except FloodHalt:
                    unhandled = chunk_ids - handled
                    _set_source_state(state, key, last_seen_id, recent_ids)
                    _set_backfill_state(
                        state,
                        key,
                        max_id=(max(unhandled) + 1) if unhandled else max_id,
                        done=0,
                        floor_ms=0,
                        scanned=source_scanned + len(messages),
                        copied=source_copied + chunk_copied,
                        prefix="copy_all",
                    )
                    raise
                chunk_copied += copied_count
                chunk_skipped += skipped_count
                handled.update(processed_ids)

            source_scanned += len(messages)
            source_copied += chunk_copied
            source_skipped += chunk_skipped
            stats["copied"] += chunk_copied
            stats["skipped"] += chunk_skipped
            stats["scanned"] += len(messages)

            if deadline_hit:
                unhandled = chunk_ids - handled
                max_id = (max(unhandled) + 1) if unhandled else max_id
                source_done = 0
            else:
                max_id = min(chunk_ids)
                source_done = int(chunk_done)

            _set_source_state(state, key, last_seen_id, recent_ids)
            _set_backfill_state(
                state,
                key,
                max_id=max_id,
                done=source_done,
                floor_ms=0,
                scanned=source_scanned,
                copied=source_copied,
                prefix="copy_all",
            )
            if deadline_hit or source_done:
                break

        if not errored and not source_done and _deadline_reached(deadline_monotonic):
            # Budget ran out before (or right at) the next chunk fetch.
            deadline_hit = True

        source_stats["fetched"] = source_scanned
        source_stats["copied"] = source_copied
        source_stats["skipped"] = source_skipped
        if errored:
            break
        if source_done:
            stats["sources_done"] += 1
            source_stats["status"] = "done"
        if deadline_hit:
            stats["deadline_reached"] = 1
            source_stats["status"] = "deadline"
            break

    return stats


async def _run_backfill_phase(
    client: Any,
    settings: AppwriteSettings,
    state: dict[str, Any],
    resolved_source_entities: dict[int, Any],
    entity_errors: dict[str, str],
    deadline_monotonic: float,
) -> dict[str, Any]:
    """Process one resumable history chunk per source inside the time budget."""
    stats: dict[str, Any] = {
        "enabled": 1,
        "sources_done": 0,
        "sources_processed": 0,
        "copied": 0,
        "skipped": 0,
        "scanned": 0,
        "deadline_reached": 0,
        "reset": 0,
        "errors": {},
    }
    if not settings.backfill_enabled:
        stats["enabled"] = 0
        return stats

    force_copy = _force_full_lookback_copy()
    reset_requested = _env_flag("BACKFILL_RESET")

    for source_id in settings.source_channel_ids:
        key = str(source_id)
        if key in entity_errors:
            continue
        if _deadline_reached(deadline_monotonic):
            stats["deadline_reached"] = 1
            break

        progress = _get_backfill_state(state, key)
        if reset_requested:
            progress = {"max_id": 0, "done": 0, "floor_ms": 0, "scanned": 0, "copied": 0}
            stats["reset"] = 1
        if progress["done"]:
            stats["sources_done"] += 1
            continue

        floor_ms = int(progress.get("floor_ms") or 0) or int(settings.backfill_since_ms)
        floor_dt = (
            datetime.fromtimestamp(floor_ms / 1000, tz=timezone.utc) if floor_ms > 0 else None
        )
        source_entity = resolved_source_entities.get(source_id, source_id)
        source_entity_kwargs = (
            {"source_entity": source_entity} if source_id in resolved_source_entities else {}
        )

        try:
            messages, chunk_done = await _fetch_backfill_chunk(
                client=client,
                source_id=source_id,
                max_id=int(progress.get("max_id") or 0),
                floor_dt=floor_dt,
                chunk_size=settings.backfill_chunk_size,
                **source_entity_kwargs,
            )
        except Exception as exc:
            stats["errors"][key] = f"{type(exc).__name__}: {exc}"
            _set_backfill_state(
                state,
                key,
                max_id=int(progress.get("max_id") or 0),
                done=0,
                floor_ms=floor_ms,
                scanned=int(progress.get("scanned") or 0),
                copied=int(progress.get("copied") or 0),
            )
            continue

        stats["sources_processed"] += 1
        stats["scanned"] += len(messages)
        chunk_ids = {
            int(getattr(msg, "id", 0)) for msg in messages if int(getattr(msg, "id", 0)) > 0
        }

        if not messages:
            if chunk_done:
                stats["sources_done"] += 1
            _set_backfill_state(
                state,
                key,
                max_id=int(progress.get("max_id") or 0),
                done=int(chunk_done),
                floor_ms=floor_ms,
                scanned=int(progress.get("scanned") or 0),
                copied=int(progress.get("copied") or 0),
            )
            continue

        last_seen_id, recent_ids = _get_source_state(state, key)
        recent_set = set(recent_ids)
        cursor_id = last_seen_id
        handled_ids: set[int] = set()
        copied = 0
        skipped = 0
        deadline_hit = False
        group_by_id = {int(getattr(msg, "id", 0)): msg for msg in messages}

        for is_album, group_ids in _group_message_ids(messages):
            if _deadline_reached(deadline_monotonic):
                deadline_hit = True
                break
            message_ids = list(group_ids)
            if not force_copy:
                message_ids = [mid for mid in message_ids if mid not in recent_set]
                # Already-processed IDs count as handled so the chunk cursor still advances.
                handled_ids.update(set(group_ids) - set(message_ids))
            if not message_ids:
                continue

            try:
                blocked_ad_ids = [
                    mid
                    for mid in message_ids
                    if _should_skip_message_text(
                        (getattr(group_by_id.get(mid), "message", "") or "").strip()
                    )
                ]
                blocked_gif_ids = [
                    mid for mid in message_ids if _is_gif_message(group_by_id.get(mid))
                ]
                blocked_ids = sorted(set(blocked_ad_ids) | set(blocked_gif_ids))
                if blocked_ids:
                    handled_ids.update(blocked_ids)
                    recent_ids.extend(blocked_ids)
                    recent_set.update(blocked_ids)
                    cursor_id = max(cursor_id, max(blocked_ids))
                    skipped += len(blocked_ids)
                    message_ids = [mid for mid in message_ids if mid not in set(blocked_ids)]
                    if not message_ids:
                        continue

                if is_album:
                    album_ids = set(message_ids)
                    album_messages = [
                        msg for msg in messages if int(getattr(msg, "id", 0)) in album_ids
                    ]
                    needs_sanitize = any(
                        _sanitize_message_text(getattr(msg, "message", None) or "")
                        != (getattr(msg, "message", "") or "")
                        for msg in album_messages
                    )
                    if needs_sanitize:
                        for msg in album_messages:
                            copied_count, skipped_count, _filtered, processed_ids = (
                                await _copy_single_with_optional_sanitize(
                                    client=client,
                                    destination_channel_id=settings.destination_channel_id,
                                    source_id=source_id,
                                    message=msg,
                                    **source_entity_kwargs,
                                )
                            )
                            copied += copied_count
                            skipped += skipped_count
                            if processed_ids:
                                handled_ids.update(processed_ids)
                                recent_ids.extend(processed_ids)
                                recent_set.update(processed_ids)
                                cursor_id = max(cursor_id, max(processed_ids))
                    else:
                        copied_count, skipped_count, processed_ids = await _forward_with_fallback(
                            client=client,
                            destination_channel_id=settings.destination_channel_id,
                            source_id=source_id,
                            message_ids=message_ids,
                            as_album=len(message_ids) > 1,
                            **source_entity_kwargs,
                        )
                        copied += copied_count
                        skipped += skipped_count
                        if processed_ids:
                            handled_ids.update(processed_ids)
                            recent_ids.extend(processed_ids)
                            recent_set.update(processed_ids)
                            cursor_id = max(cursor_id, max(processed_ids))
                else:
                    single_message = group_by_id.get(message_ids[0])
                    if single_message is None:
                        handled_ids.update(message_ids)
                        continue
                    copied_count, skipped_count, _filtered, processed_ids = (
                        await _copy_single_with_optional_sanitize(
                            client=client,
                            destination_channel_id=settings.destination_channel_id,
                            source_id=source_id,
                            message=single_message,
                            **source_entity_kwargs,
                        )
                    )
                    copied += copied_count
                    skipped += skipped_count
                    if processed_ids:
                        handled_ids.update(processed_ids)
                        recent_ids.extend(processed_ids)
                        recent_set.update(processed_ids)
                        cursor_id = max(cursor_id, max(processed_ids))
            except FloodHalt:
                # Keep handled IDs so the next run resumes without re-copying them.
                # Never advance the live cursor here: backfill must not make the
                # normal lookback path skip messages it has not reached yet.
                _set_source_state(state, key, last_seen_id, recent_ids)
                _set_backfill_state(
                    state,
                    key,
                    max_id=int(progress.get("max_id") or 0),
                    done=0,
                    floor_ms=floor_ms,
                    scanned=int(progress.get("scanned") or 0) + len(messages),
                    copied=int(progress.get("copied") or 0) + copied,
                )
                _set_run_meta(
                    state,
                    status="halted",
                    halted_at_ist=_now_ist_iso(),
                    halted_at_utc=_now_utc_iso(),
                    fetched=int(progress.get("scanned") or 0) + len(messages),
                    copied=int(progress.get("copied") or 0) + copied,
                )
                raise

        stats["copied"] += copied
        stats["skipped"] += skipped

        unhandled_ids = chunk_ids - handled_ids
        if unhandled_ids and not deadline_hit:
            # Safety: never stall the cursor on IDs the loop could not classify.
            handled_ids.update(unhandled_ids)
            unhandled_ids = set()

        if unhandled_ids:
            next_max_id = max(unhandled_ids) + 1
            next_done = 0
        else:
            next_max_id = min(chunk_ids)
            next_done = int(chunk_done)

        _set_source_state(state, key, last_seen_id, recent_ids)
        _set_backfill_state(
            state,
            key,
            max_id=next_max_id,
            done=next_done,
            floor_ms=floor_ms,
            scanned=int(progress.get("scanned") or 0) + len(messages),
            copied=int(progress.get("copied") or 0) + copied,
        )
        if next_done:
            stats["sources_done"] += 1
        if deadline_hit:
            stats["deadline_reached"] = 1
            break

    return stats


async def _copy_source_messages(client: Any, settings: AppwriteSettings) -> dict[str, Any]:
    copied_total = 0
    skipped_invalid_total = 0
    skipped_invalid_runtime_total = 0
    skipped_duplicate_recent_total = 0
    skipped_cursor_gate_total = 0
    skipped_blocked_ad_total = 0
    skipped_blocked_gif_total = 0
    filtered_links_total = 0
    fetched_total = 0
    sources_with_new_messages = 0
    state, state_message_id = await _load_state(client)
    if "sources" not in state:
        state = {"sources": {}}
    run_ts_ms = int(time.time() * 1000)
    _set_run_meta(
        state,
        last_run_ts_ms=run_ts_ms,
        last_run_at_ist=_now_ist_iso(),
        last_run_at_utc=_now_utc_iso(),
        status="running",
    )
    state_updated = False
    updated_source_keys: list[str] = []
    source_errors: dict[str, str] = {}
    resolved_source_entities, entity_errors = await _prime_source_entities(
        client=client,
        source_ids=settings.source_channel_ids,
    )
    source_errors.update(entity_errors)
    use_cursor_gate = _is_cursor_gate_enabled()
    force_full_copy = _force_full_lookback_copy()
    use_snapshot_recovery = _is_snapshot_recovery_enabled()
    run_window_ids_by_source: dict[str, list[int]] = {str(source_id): [] for source_id in settings.source_channel_ids}
    recent_snapshot_ids_by_source = (
        await _load_recent_run_snapshot_ids(client=client, lookback_minutes=settings.lookback_minutes)
        if use_snapshot_recovery
        else {}
    )
    # Force save every run so Saved Messages always reflects latest state metadata.
    changed = True
    state_message_id, did_update = await _save_state(client=client, state=state, state_message_id=state_message_id)
    state_updated = state_updated or did_update

    cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=settings.lookback_minutes)
    source_run_stats: dict[str, dict[str, Any]] = {}
    run_deadline = _run_deadline_monotonic(settings.backfill_time_budget_seconds)
    run_deadline_hit = False
    backfill_stats: dict[str, Any] = {"enabled": int(settings.backfill_enabled)}
    date_stats: dict[str, Any] = {
        "enabled": int(bool(settings.copy_date)),
        "copied": 0,
        "skipped": 0,
        "scanned": 0,
        "sources_done": 0,
        "sources_processed": 0,
        "deadline_reached": 0,
        "errors": {},
        "sources": {},
    }
    if settings.copy_date:
        # Date-only mode: replay exactly one IST calendar day and skip the live
        # lookback pass, which can only ever see `LOOKBACK_MINUTES` of history.
        try:
            date_stats = await _run_date_phase(
                client=client,
                settings=settings,
                state=state,
                resolved_source_entities=resolved_source_entities,
                entity_errors=entity_errors,
                deadline_monotonic=run_deadline,
            )
        except FloodHalt as halt:
            date_stats = {
                "enabled": 1,
                "halted": 1,
                "flood_wait_seconds": int(halt.seconds),
                "copied": int(halt.copied),
                "skipped": int(halt.skipped),
                "scanned": 0,
                "sources_done": 0,
                "sources_processed": 0,
                "deadline_reached": 0,
                "errors": {},
                "sources": {},
            }
        except Exception as exc:
            # A date-phase failure must not hide the state saved above.
            date_stats = {
                "enabled": 1,
                "copied": 0,
                "skipped": 0,
                "scanned": 0,
                "sources_done": 0,
                "sources_processed": 0,
                "deadline_reached": 0,
                "errors": {"copy_date": f"{type(exc).__name__}: {exc}"},
                "sources": {},
            }

    copy_all_stats: dict[str, Any] = {
        "enabled": int(settings.copy_all),
        "copied": 0,
        "skipped": 0,
        "scanned": 0,
        "chunks": 0,
        "sources_done": 0,
        "sources_processed": 0,
        "deadline_reached": 0,
        "errors": {},
        "sources": {},
    }
    if settings.copy_all:
        # Full-history mode: never run the lookback pass, which only sees
        # `LOOKBACK_MINUTES` and would waste the run budget on recent messages.
        try:
            copy_all_stats = await _run_full_copy_phase(
                client=client,
                settings=settings,
                state=state,
                resolved_source_entities=resolved_source_entities,
                entity_errors=entity_errors,
                deadline_monotonic=run_deadline,
            )
        except FloodHalt as halt:
            copy_all_stats = {
                "enabled": 1,
                "halted": 1,
                "flood_wait_seconds": int(halt.seconds),
                "copied": int(halt.copied),
                "skipped": int(halt.skipped),
                "scanned": 0,
                "chunks": 0,
                "sources_done": 0,
                "sources_processed": 0,
                "deadline_reached": 0,
                "errors": {},
                "sources": {},
            }
        except Exception as exc:
            # A full-copy failure must not hide the state saved above.
            copy_all_stats = {
                "enabled": 1,
                "copied": 0,
                "skipped": 0,
                "scanned": 0,
                "chunks": 0,
                "sources_done": 0,
                "sources_processed": 0,
                "deadline_reached": 0,
                "errors": {"copy_all": f"{type(exc).__name__}: {exc}"},
                "sources": {},
            }

    live_source_ids: tuple[int, ...] = ()
    if not (settings.copy_date or settings.copy_all):
        live_source_ids = _rotated_live_source_ids(state, settings.source_channel_ids)
    for source_id in live_source_ids:
        key = str(source_id)
        if _deadline_reached(run_deadline):
            run_deadline_hit = True
            break
        counters_before = (
            copied_total,
            fetched_total,
            skipped_cursor_gate_total,
            skipped_duplicate_recent_total,
            skipped_blocked_ad_total,
            skipped_blocked_gif_total,
            filtered_links_total,
        )
        source_stats: dict[str, Any] = {"status": "ok"}
        source_run_stats[key] = source_stats
        try:
            if key in entity_errors:
                source_stats["status"] = "entity_error"
                source_stats["error"] = entity_errors.get(key, "")
                continue
            source_entity = resolved_source_entities.get(source_id, source_id)
            if not isinstance(source_entity, int):
                source_title = getattr(source_entity, "title", None) or getattr(source_entity, "username", None)
                if source_title:
                    source_stats["title"] = str(source_title)
            source_entity_kwargs = (
                {"source_entity": source_entity}
                if source_id in resolved_source_entities
                else {}
            )
            last_seen_id, recent_ids = _get_source_state(state, key)
            recent_set = set(recent_ids)
            cursor_id = last_seen_id
            source_state_dirty = False
            # Fetch recent messages by traversing backward until lookback cutoff.
            messages = await _fetch_recent_messages(
                client=client,
                source_id=source_id,
                last_seen_id=last_seen_id,
                cutoff_dt=cutoff_dt,
                **source_entity_kwargs,
            )
            live_message_ids = {int(getattr(msg, "id", 0)) for msg in messages if int(getattr(msg, "id", 0)) > 0}
            snapshot_ids = sorted(
                mid for mid in recent_snapshot_ids_by_source.get(key, set()) if isinstance(mid, int) and mid > 0
            )
            missing_snapshot_ids = [
                mid for mid in snapshot_ids if mid not in live_message_ids and mid > last_seen_id
            ][: settings.limit_per_source]
            recovered_id_set: set[int] = set()
            if missing_snapshot_ids:
                recovered = await client.get_messages(source_entity, ids=missing_snapshot_ids)
                recovered_list = list(recovered) if isinstance(recovered, (list, tuple)) else [recovered]
                recovered_valid = [
                    msg
                    for msg in recovered_list
                    if msg is not None and int(getattr(msg, "id", 0)) > 0
                ]
                recovered_id_set = {int(getattr(msg, "id", 0)) for msg in recovered_valid if int(getattr(msg, "id", 0)) > 0}
                messages.extend(recovered_valid)
            dedup_by_id: dict[int, Any] = {}
            for msg in messages:
                mid = int(getattr(msg, "id", 0))
                if mid > 0:
                    dedup_by_id[mid] = msg
            messages = sorted(dedup_by_id.values(), key=lambda m: int(getattr(m, "id", 0)))
            max_fetched_id = max((int(getattr(msg, "id", 0)) for msg in messages), default=0)
            # Self-heal: if cursor is ahead of all fetched IDs, fall back to recent-set dedupe for this run.
            cursor_gate_disabled = max_fetched_id > 0 and last_seen_id > max_fetched_id
            run_window_ids_by_source[key] = [int(getattr(msg, "id", 0)) for msg in messages if int(getattr(msg, "id", 0)) > 0]
            fetched_total += len(messages)
            if not messages:
                continue
            sources_with_new_messages += 1

            for is_album, message_ids in _group_message_ids(messages):
                if _deadline_reached(run_deadline):
                    run_deadline_hit = True
                    break
                original_group_ids = list(message_ids)
                # Prevent re-copy loops: process only new IDs for this source cursor.
                # Snapshot recovery still works for missed IDs that are newer than last_seen_id.
                # FORCE_FULL_LOOKBACK_COPY bypasses the cursor gate so every message in
                # the lookback window is processed on each run.
                message_ids = [
                    mid
                    for mid in message_ids
                    if (
                        force_full_copy
                        or (not use_cursor_gate)
                        or cursor_gate_disabled
                        or mid > last_seen_id
                        or (mid in recovered_id_set and mid > last_seen_id)
                    )
                ]
                skipped_cursor_gate_total += len(original_group_ids) - len(message_ids)
                if not message_ids:
                    continue
                # Skip IDs already processed in recent window to avoid duplicates
                # (unless force-full copy is enabled).
                if not force_full_copy:
                    before_recent_filter = len(message_ids)
                    message_ids = [mid for mid in message_ids if mid not in recent_set]
                    skipped_duplicate_recent_total += before_recent_filter - len(message_ids)
                if not message_ids:
                    continue
                group_by_id = {int(getattr(msg, "id", 0)): msg for msg in messages}

                # Drop ad/promotional messages entirely.
                blocked_ad_ids = [
                    mid
                    for mid in message_ids
                    if _should_skip_message_text((getattr(group_by_id.get(mid), "message", "") or "").strip())
                ]
                blocked_gif_ids = [mid for mid in message_ids if _is_gif_message(group_by_id.get(mid))]
                blocked_ids = sorted(set(blocked_ad_ids) | set(blocked_gif_ids))
                if blocked_ids:
                    skipped_blocked_ad_total += len(blocked_ad_ids)
                    skipped_blocked_gif_total += len(blocked_gif_ids)
                    recent_ids.extend(blocked_ids)
                    recent_set.update(blocked_ids)
                    cursor_id = max(cursor_id, max(blocked_ids))
                    source_state_dirty = True
                    skipped_invalid_total += len(blocked_ids)
                    message_ids = [mid for mid in message_ids if mid not in set(blocked_ids)]
                    if not message_ids:
                        continue

                if is_album:
                    album_ids = tuple(message_ids)
                    album_messages = [msg for msg in messages if int(getattr(msg, "id", 0)) in set(album_ids)]
                    if any(
                        _sanitize_message_text(getattr(msg, "message", None) or "")
                        != (getattr(msg, "message", "") or "")
                        for msg in album_messages
                    ):
                        for msg in album_messages:
                            try:
                                copied_count, skipped_count, filtered_count, processed_ids = (
                                    await _copy_single_with_optional_sanitize(
                                        client=client,
                                        destination_channel_id=settings.destination_channel_id,
                                        source_id=source_id,
                                        message=msg,
                                        **source_entity_kwargs,
                                    )
                                )
                                copied_total += copied_count
                                skipped_invalid_total += skipped_count
                                skipped_invalid_runtime_total += skipped_count
                                filtered_links_total += filtered_count
                                if processed_ids:
                                    recent_ids.extend(processed_ids)
                                    recent_set.update(processed_ids)
                                    cursor_id = max(cursor_id, max(processed_ids))
                                    source_state_dirty = True
                            except FloodHalt as halt:
                                copied_total += halt.copied
                                skipped_invalid_total += halt.skipped
                                skipped_invalid_runtime_total += halt.skipped
                                if halt.processed_ids:
                                    recent_ids.extend(halt.processed_ids)
                                    recent_set.update(halt.processed_ids)
                                    cursor_id = max(cursor_id, max(halt.processed_ids))
                                    source_state_dirty = True
                                if cursor_id > last_seen_id or source_state_dirty:
                                    _set_source_state(state, key, cursor_id, recent_ids)
                                    updated_source_keys.append(key)
                                    changed = True
                                if changed:
                                    _set_run_meta(
                                        state,
                                        status="halted",
                                        halted_at_ist=_now_ist_iso(),
                                        halted_at_utc=_now_utc_iso(),
                                        copied=copied_total,
                                        skipped_invalid=skipped_invalid_total,
                                        filtered_links=filtered_links_total,
                                        fetched=fetched_total,
                                        sources_with_new_messages=sources_with_new_messages,
                                    )
                                    state_message_id, did_update = await _save_state(
                                        client=client, state=state, state_message_id=state_message_id
                                    )
                                    state_updated = state_updated or did_update
                                run_snapshot_ids = await _save_run_window_ids(
                                    client=client,
                                    run_ts_ms=run_ts_ms,
                                    lookback_minutes=settings.lookback_minutes,
                                    run_status="halted",
                                    ids_by_source=run_window_ids_by_source,
                                )
                                return {
                                    "copied": copied_total,
                                    "skipped_invalid": skipped_invalid_total,
                                    "skipped_invalid_runtime": skipped_invalid_runtime_total,
                                    "skipped_duplicate_recent": skipped_duplicate_recent_total,
                                    "skipped_cursor_gate": skipped_cursor_gate_total,
                                    "skipped_blocked_ad": skipped_blocked_ad_total,
                                    "skipped_blocked_gif": skipped_blocked_gif_total,
                                    "filtered_links": filtered_links_total,
                                    "fetched": fetched_total,
                                    "sources_with_new_messages": sources_with_new_messages,
                                    "sources": len(settings.source_channel_ids),
                                    "flood_wait_seconds": int(halt.seconds),
                                    "halted": 1,
                                    "state_saved": int(state_updated),
                                    "state_message_id": state_message_id,
                                    "run_snapshot_count": len(run_snapshot_ids),
                                    "run_snapshot_message_ids": run_snapshot_ids,
                                    "source_run_stats": source_run_stats,
                                    "updated_sources": sorted(set(updated_source_keys)),
                                }
                        continue
                else:
                    single_id = message_ids[0]
                    single_message = next((msg for msg in messages if int(getattr(msg, "id", 0)) == single_id), None)
                    if single_message is not None:
                        try:
                            copied_count, skipped_count, filtered_count, processed_ids = (
                                await _copy_single_with_optional_sanitize(
                                        client=client,
                                        destination_channel_id=settings.destination_channel_id,
                                        source_id=source_id,
                                        message=single_message,
                                        **source_entity_kwargs,
                                )
                            )
                            copied_total += copied_count
                            skipped_invalid_total += skipped_count
                            skipped_invalid_runtime_total += skipped_count
                            filtered_links_total += filtered_count
                            if processed_ids:
                                recent_ids.extend(processed_ids)
                                recent_set.update(processed_ids)
                                cursor_id = max(cursor_id, max(processed_ids))
                                source_state_dirty = True
                        except FloodHalt as halt:
                            copied_total += halt.copied
                            skipped_invalid_total += halt.skipped
                            skipped_invalid_runtime_total += halt.skipped
                            if halt.processed_ids:
                                recent_ids.extend(halt.processed_ids)
                                recent_set.update(halt.processed_ids)
                                cursor_id = max(cursor_id, max(halt.processed_ids))
                                source_state_dirty = True
                            if cursor_id > last_seen_id or source_state_dirty:
                                _set_source_state(state, key, cursor_id, recent_ids)
                                updated_source_keys.append(key)
                                changed = True
                            if changed:
                                _set_run_meta(
                                    state,
                                    status="halted",
                                    halted_at_ist=_now_ist_iso(),
                                    halted_at_utc=_now_utc_iso(),
                                    copied=copied_total,
                                    skipped_invalid=skipped_invalid_total,
                                    filtered_links=filtered_links_total,
                                    fetched=fetched_total,
                                    sources_with_new_messages=sources_with_new_messages,
                                )
                                state_message_id, did_update = await _save_state(
                                    client=client, state=state, state_message_id=state_message_id
                                )
                                state_updated = state_updated or did_update
                            run_snapshot_ids = await _save_run_window_ids(
                                client=client,
                                run_ts_ms=run_ts_ms,
                                lookback_minutes=settings.lookback_minutes,
                                run_status="halted",
                                ids_by_source=run_window_ids_by_source,
                            )
                            return {
                                "copied": copied_total,
                                "skipped_invalid": skipped_invalid_total,
                                "skipped_invalid_runtime": skipped_invalid_runtime_total,
                                "skipped_duplicate_recent": skipped_duplicate_recent_total,
                                "skipped_cursor_gate": skipped_cursor_gate_total,
                                "skipped_blocked_ad": skipped_blocked_ad_total,
                                "skipped_blocked_gif": skipped_blocked_gif_total,
                                "filtered_links": filtered_links_total,
                                "fetched": fetched_total,
                                "sources_with_new_messages": sources_with_new_messages,
                                "sources": len(settings.source_channel_ids),
                                "flood_wait_seconds": int(halt.seconds),
                                "halted": 1,
                                "state_saved": int(state_updated),
                                "state_message_id": state_message_id,
                                "run_snapshot_count": len(run_snapshot_ids),
                                "run_snapshot_message_ids": run_snapshot_ids,
                                "source_run_stats": source_run_stats,
                                "updated_sources": sorted(set(updated_source_keys)),
                            }
                        continue

                try:
                    copied_count, skipped_count, processed_ids = await _forward_with_fallback(
                        client=client,
                        destination_channel_id=settings.destination_channel_id,
                        source_id=source_id,
                        message_ids=message_ids,
                        as_album=is_album and len(message_ids) > 1,
                        **source_entity_kwargs,
                    )
                    copied_total += copied_count
                    skipped_invalid_total += skipped_count
                    skipped_invalid_runtime_total += skipped_count
                    if processed_ids:
                        recent_ids.extend(processed_ids)
                        recent_set.update(processed_ids)
                        cursor_id = max(cursor_id, max(processed_ids))
                        source_state_dirty = True
                except FloodHalt as halt:
                    copied_total += halt.copied
                    skipped_invalid_total += halt.skipped
                    skipped_invalid_runtime_total += halt.skipped
                    if halt.processed_ids:
                        recent_ids.extend(halt.processed_ids)
                        recent_set.update(halt.processed_ids)
                        cursor_id = max(cursor_id, max(halt.processed_ids))
                        source_state_dirty = True
                    if cursor_id > last_seen_id or source_state_dirty:
                        _set_source_state(state, key, cursor_id, recent_ids)
                        updated_source_keys.append(key)
                        changed = True
                    if changed:
                        _set_run_meta(
                            state,
                            status="halted",
                            halted_at_ist=_now_ist_iso(),
                            halted_at_utc=_now_utc_iso(),
                            copied=copied_total,
                            skipped_invalid=skipped_invalid_total,
                            filtered_links=filtered_links_total,
                            fetched=fetched_total,
                            sources_with_new_messages=sources_with_new_messages,
                        )
                        state_message_id, did_update = await _save_state(
                            client=client, state=state, state_message_id=state_message_id
                        )
                        state_updated = state_updated or did_update
                    run_snapshot_ids = await _save_run_window_ids(
                        client=client,
                        run_ts_ms=run_ts_ms,
                        lookback_minutes=settings.lookback_minutes,
                        run_status="halted",
                        ids_by_source=run_window_ids_by_source,
                    )
                    return {
                        "copied": copied_total,
                        "skipped_invalid": skipped_invalid_total,
                        "skipped_invalid_runtime": skipped_invalid_runtime_total,
                        "skipped_duplicate_recent": skipped_duplicate_recent_total,
                        "skipped_cursor_gate": skipped_cursor_gate_total,
                        "skipped_blocked_ad": skipped_blocked_ad_total,
                        "skipped_blocked_gif": skipped_blocked_gif_total,
                        "filtered_links": filtered_links_total,
                        "fetched": fetched_total,
                        "sources_with_new_messages": sources_with_new_messages,
                        "sources": len(settings.source_channel_ids),
                        "flood_wait_seconds": int(halt.seconds),
                        "halted": 1,
                        "state_saved": int(state_updated),
                        "state_message_id": state_message_id,
                        "run_snapshot_count": len(run_snapshot_ids),
                        "run_snapshot_message_ids": run_snapshot_ids,
                        "source_run_stats": source_run_stats,
                        "updated_sources": sorted(set(updated_source_keys)),
                    }

            if cursor_id > last_seen_id or source_state_dirty:
                _set_source_state(state, key, cursor_id, recent_ids)
                updated_source_keys.append(key)
                changed = True
        except Exception as source_exc:
            source_stats["status"] = "error"
            source_stats["error"] = f"{type(source_exc).__name__}: {source_exc}"
            source_errors[key] = source_stats["error"]
            continue
        finally:
            source_stats["fetched"] = fetched_total - counters_before[1]
            source_stats["copied"] = copied_total - counters_before[0]
            source_stats["gate_skipped"] = skipped_cursor_gate_total - counters_before[2]
            source_stats["recent_skipped"] = skipped_duplicate_recent_total - counters_before[3]
            source_stats["blocked_ad"] = skipped_blocked_ad_total - counters_before[4]
            source_stats["blocked_gif"] = skipped_blocked_gif_total - counters_before[5]
            source_stats["filtered_links"] = filtered_links_total - counters_before[6]
            window_ids = run_window_ids_by_source.get(key, [])
            source_stats["window_count"] = len(window_ids)
            source_stats["window_min_id"] = min(window_ids) if window_ids else 0
            source_stats["window_max_id"] = max(window_ids) if window_ids else 0
            source_entry = (state.get("sources") or {}).get(key)
            if isinstance(source_entry, dict):
                source_stats["cursor_id"] = int(source_entry.get("last_id", 0) or 0)

        if run_deadline_hit:
            break

    if (
        settings.backfill_enabled
        and not settings.copy_date
        and not settings.copy_all
        and not run_deadline_hit
    ):
        try:
            backfill_stats = await _run_backfill_phase(
                client=client,
                settings=settings,
                state=state,
                resolved_source_entities=resolved_source_entities,
                entity_errors=entity_errors,
                deadline_monotonic=run_deadline,
            )
        except FloodHalt as halt:
            backfill_stats = {
                "enabled": 1,
                "halted": 1,
                "flood_wait_seconds": int(halt.seconds),
                "sources_done": 0,
                "sources_processed": 0,
                "copied": int(halt.copied),
                "skipped": int(halt.skipped),
                "scanned": 0,
                "deadline_reached": 0,
                "errors": {},
            }
        except Exception as exc:
            # Backfill failures must not lose the live-run progress saved below.
            backfill_stats = {
                "enabled": 1,
                "sources_done": 0,
                "sources_processed": 0,
                "copied": 0,
                "skipped": 0,
                "scanned": 0,
                "deadline_reached": 0,
                "errors": {"backfill": f"{type(exc).__name__}: {exc}"},
            }
        backfill_errors = backfill_stats.get("errors") or {}
        if isinstance(backfill_errors, dict):
            for error_key, error_text in backfill_errors.items():
                source_errors.setdefault(str(error_key), str(error_text))
        backfill_copied = int(backfill_stats.get("copied") or 0)
        backfill_skipped = int(backfill_stats.get("skipped") or 0)
        backfill_scanned = int(backfill_stats.get("scanned") or 0)
        copied_total += backfill_copied
        skipped_invalid_total += backfill_skipped
        skipped_invalid_runtime_total += backfill_skipped
        fetched_total += backfill_scanned

    for source_key, source_item in (copy_all_stats.get("sources") or {}).items():
        source_run_stats.setdefault(str(source_key), source_item)
    if copy_all_stats.get("enabled"):
        copied_total += int(copy_all_stats.get("copied") or 0)
        skipped_invalid_total += int(copy_all_stats.get("skipped") or 0)
        skipped_invalid_runtime_total += int(copy_all_stats.get("skipped") or 0)
        fetched_total += int(copy_all_stats.get("scanned") or 0)
        copy_all_errors = copy_all_stats.get("errors") or {}
        if isinstance(copy_all_errors, dict):
            for error_key, error_text in copy_all_errors.items():
                source_errors.setdefault(str(error_key), str(error_text))
    for source_key, source_item in (date_stats.get("sources") or {}).items():
        source_run_stats.setdefault(str(source_key), source_item)
    if date_stats.get("enabled"):
        copied_total += int(date_stats.get("copied") or 0)
        skipped_invalid_total += int(date_stats.get("skipped") or 0)
        skipped_invalid_runtime_total += int(date_stats.get("skipped") or 0)
        fetched_total += int(date_stats.get("scanned") or 0)
        date_errors = date_stats.get("errors") or {}
        if isinstance(date_errors, dict):
            for error_key, error_text in date_errors.items():
                source_errors.setdefault(str(error_key), str(error_text))
    run_partial = int(
        bool(
            run_deadline_hit
            or backfill_stats.get("deadline_reached")
            or date_stats.get("deadline_reached")
            or copy_all_stats.get("deadline_reached")
        )
    )

    if changed:
        if (
            backfill_stats.get("halted")
            or date_stats.get("halted")
            or copy_all_stats.get("halted")
        ):
            run_status = "halted"
        elif run_partial:
            run_status = "partial"
        else:
            run_status = "completed_with_errors" if source_errors else "completed"
        _set_run_meta(
            state,
            status=run_status,
            completed_at_ist=_now_ist_iso(),
            completed_at_utc=_now_utc_iso(),
            copied=copied_total,
            skipped_invalid=skipped_invalid_total,
            filtered_links=filtered_links_total,
            fetched=fetched_total,
            sources_with_new_messages=sources_with_new_messages,
            source_errors=source_errors,
            partial=run_partial,
            deadline_reached=run_partial,
            backfill_enabled=int(settings.backfill_enabled),
            backfill_sources_done=int(backfill_stats.get("sources_done") or 0),
            backfill_copied=int(backfill_stats.get("copied") or 0),
            backfill_scanned=int(backfill_stats.get("scanned") or 0),
            copy_date=settings.copy_date,
            copy_date_copied=int(date_stats.get("copied") or 0),
            copy_date_sources_done=int(date_stats.get("sources_done") or 0),
            copy_all=int(settings.copy_all),
            copy_all_copied=int(copy_all_stats.get("copied") or 0),
            copy_all_sources_done=int(copy_all_stats.get("sources_done") or 0),
        )
        state_message_id, did_update = await _save_state(client=client, state=state, state_message_id=state_message_id)
        state_updated = state_updated or did_update
    else:
        if (
            backfill_stats.get("halted")
            or date_stats.get("halted")
            or copy_all_stats.get("halted")
        ):
            run_status = "halted"
        elif run_partial:
            run_status = "partial"
        else:
            run_status = "completed_with_errors" if source_errors else "completed"

    run_snapshot_ids = await _save_run_window_ids(
        client=client,
        run_ts_ms=run_ts_ms,
        lookback_minutes=settings.lookback_minutes,
        run_status=run_status,
        ids_by_source=run_window_ids_by_source,
    )

    return {
        "copied": copied_total,
        "skipped_invalid": skipped_invalid_total,
        "skipped_invalid_runtime": skipped_invalid_runtime_total,
        "skipped_duplicate_recent": skipped_duplicate_recent_total,
        "skipped_cursor_gate": skipped_cursor_gate_total,
        "skipped_blocked_ad": skipped_blocked_ad_total,
        "skipped_blocked_gif": skipped_blocked_gif_total,
        "filtered_links": filtered_links_total,
        "fetched": fetched_total,
        "sources_with_new_messages": sources_with_new_messages,
        "sources": len(settings.source_channel_ids),
        "partial": run_partial,
        "deadline_reached": run_partial,
        "time_budget_seconds": int(settings.backfill_time_budget_seconds),
        "backfill_enabled": int(settings.backfill_enabled),
        "backfill_sources_done": int(backfill_stats.get("sources_done") or 0),
        "backfill_sources_processed": int(backfill_stats.get("sources_processed") or 0),
        "backfill_copied": int(backfill_stats.get("copied") or 0),
        "backfill_scanned": int(backfill_stats.get("scanned") or 0),
        "copy_date": settings.copy_date,
        "copy_date_enabled": int(date_stats.get("enabled") or 0),
        "copy_date_copied": int(date_stats.get("copied") or 0),
        "copy_date_scanned": int(date_stats.get("scanned") or 0),
        "copy_date_sources_done": int(date_stats.get("sources_done") or 0),
        "copy_date_sources_processed": int(date_stats.get("sources_processed") or 0),
        "copy_date_chunks": int(date_stats.get("chunks") or 0),
        "copy_date_reset": int(date_stats.get("reset") or 0),
        "copy_all": int(settings.copy_all),
        "copy_all_enabled": int(copy_all_stats.get("enabled") or 0),
        "copy_all_copied": int(copy_all_stats.get("copied") or 0),
        "copy_all_scanned": int(copy_all_stats.get("scanned") or 0),
        "copy_all_chunks": int(copy_all_stats.get("chunks") or 0),
        "copy_all_sources_done": int(copy_all_stats.get("sources_done") or 0),
        "copy_all_sources_processed": int(copy_all_stats.get("sources_processed") or 0),
        "copy_all_reset": int(copy_all_stats.get("reset") or 0),
        **(
            {
                "halted": 1,
                "flood_wait_seconds": int(
                    backfill_stats.get("flood_wait_seconds")
                    or date_stats.get("flood_wait_seconds")
                    or copy_all_stats.get("flood_wait_seconds")
                    or 0
                ),
            }
            if (
                backfill_stats.get("halted")
                or date_stats.get("halted")
                or copy_all_stats.get("halted")
            )
            else {}
        ),
        "source_errors_count": len(source_errors),
        "source_errors": source_errors,
        "state_saved": int(state_updated),
        "state_message_id": state_message_id,
        "state_backend": "appwrite_db" if isinstance(state_message_id, str) else "telegram_saved_messages",
        "db_logging_enabled": int(_is_db_logging_enabled()),
        "db_state_enabled": int(_is_db_state_enabled()),
        "db_auth_mode": _appwrite_auth_mode(),
        "db_log_last_error": DB_LOG_LAST_ERROR,
        "db_state_last_error": DB_STATE_LAST_ERROR,
        **_appwrite_env_diagnostics(),
        "run_snapshot_count": len(run_snapshot_ids),
        "run_snapshot_message_ids": run_snapshot_ids,
        "source_run_stats": source_run_stats,
        "updated_sources": sorted(set(updated_source_keys)),
    }


async def _copy_single_with_optional_sanitize(
    client: Any,
    destination_channel_id: int,
    source_id: int,
    message: Any,
    source_entity: Any | None = None,
) -> tuple[int, int, int, tuple[int, ...]]:
    from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError
    from telethon.errors.rpcerrorlist import FloodWaitError

    message_id = int(getattr(message, "id", 0))
    if message_id <= 0:
        return 0, 1, 0, tuple()
    if _is_gif_message(message):
        return 0, 1, 0, (message_id,)

    original_text = getattr(message, "message", None) or ""
    cleaned_text = _sanitize_message_text(original_text)
    has_media = _sendable_media_kind(message) is not None
    is_sanitized = cleaned_text != original_text

    if is_sanitized:
        try:
            if has_media:
                try:
                    await _send_media_with_restriction_fallback(
                        client=client,
                        destination_channel_id=destination_channel_id,
                        message=message,
                        caption=cleaned_text or None,
                    )
                except ChatForwardsRestrictedError:
                    return 0, 1, 0, (message_id,)
                return 1, 0, 1, (message_id,)

            if cleaned_text:
                await client.send_message(
                    entity=destination_channel_id,
                    message=cleaned_text,
                    link_preview=False,
                )
                return 1, 0, 1, (message_id,)

            # Text became empty after removing https://t.* links.
            return 0, 1, 1, (message_id,)
        except FloodWaitError as exc:
            raise FloodHalt(seconds=int(exc.seconds), processed_ids=tuple(), copied=0, skipped=0) from exc

    copied, skipped, processed_ids = await _forward_with_fallback(
        client=client,
        destination_channel_id=destination_channel_id,
        source_id=source_id,
        source_entity=source_entity,
        message_ids=[message_id],
        as_album=False,
    )
    return copied, skipped, 0, processed_ids


async def _forward_with_fallback(
    client: Any,
    destination_channel_id: int,
    source_id: int,
    message_ids: list[int],
    as_album: bool,
    source_entity: Any | None = None,
) -> tuple[int, int, tuple[int, ...]]:
    from telethon.errors import RPCError
    from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError
    from telethon.errors.rpcerrorlist import FloodWaitError
    from telethon.errors.rpcerrorlist import MessageIdInvalidError

    if _is_copy_mode():
        # Own-name repost: the destination post carries no "Forwarded from" header.
        return await _repost_messages_without_forward(
            client=client,
            destination_channel_id=destination_channel_id,
            source_id=source_id,
            source_entity=source_entity,
            message_ids=message_ids,
        )

    def is_forwards_restricted(exc: Exception) -> bool:
        if isinstance(exc, ChatForwardsRestrictedError):
            return True
        if isinstance(exc, RPCError):
            text = f"{exc.__class__.__name__}: {exc}".upper()
            return "CHATFORWARDSRESTRICTED" in text or "CHAT_FORWARDS_RESTRICTED" in text
        return False

    def is_skippable_forward_error(exc: Exception) -> bool:
        if isinstance(exc, MessageIdInvalidError):
            return True
        if isinstance(exc, ChatForwardsRestrictedError):
            return True
        if isinstance(exc, RPCError):
            text = f"{exc.__class__.__name__}: {exc}".upper()
            return (
                "MESSAGEIDINVALID" in text
                or "MESSAGE_ID_INVALID" in text
                or "CHATFORWARDSRESTRICTED" in text
                or "CHAT_FORWARDS_RESTRICTED" in text
            )
        return False

    try:
        forward_source = source_entity if source_entity is not None else source_id
        await client.forward_messages(
            entity=destination_channel_id,
            messages=message_ids if as_album else message_ids[0],
            from_peer=forward_source,
            as_album=as_album if as_album else None,
            drop_author=True,
            drop_media_captions=False,
        )
        return len(message_ids), 0, tuple(message_ids)
    except Exception as exc:
        if isinstance(exc, FloodWaitError):
            raise FloodHalt(seconds=int(exc.seconds), processed_ids=tuple(), copied=0, skipped=0) from exc
        if is_forwards_restricted(exc):
            # Fallback: repost content directly (no forward) for protected chats.
            return await _repost_messages_without_forward(
                client=client,
                destination_channel_id=destination_channel_id,
                source_id=source_id,
                source_entity=source_entity,
                message_ids=message_ids,
            )
        if not is_skippable_forward_error(exc):
            raise
        # Some message IDs cannot be forwarded/copied (deleted/unsupported/restricted).
        # For albums, degrade to per-item forwarding so valid items can still pass through.
        if as_album and len(message_ids) > 1:
            copied = 0
            skipped = 0
            processed: list[int] = []
            for mid in message_ids:
                try:
                    await client.forward_messages(
                        entity=destination_channel_id,
                        messages=mid,
                        from_peer=forward_source,
                        drop_author=True,
                        drop_media_captions=False,
                    )
                    copied += 1
                    processed.append(mid)
                except Exception as inner_exc:
                    if isinstance(inner_exc, FloodWaitError):
                        raise FloodHalt(
                            seconds=int(inner_exc.seconds),
                            processed_ids=tuple(processed),
                            copied=copied,
                            skipped=skipped,
                        ) from inner_exc
                    if not is_skippable_forward_error(inner_exc):
                        raise
                    skipped += 1
                    processed.append(mid)
            return copied, skipped, tuple(processed)
        # Single message that is invalid/restricted: skip and mark as processed for dedupe.
        return 0, len(message_ids), tuple(message_ids)


# Media from a protected ("no-forward") chat can only be copied by re-uploading
# freshly downloaded bytes. Anything larger is skipped rather than buffered in
# the function's memory.
RESTRICTED_MEDIA_FALLBACK_MAX_BYTES = 20 * 1024 * 1024


async def _send_media_with_restriction_fallback(
    client: Any,
    destination_channel_id: int,
    message: Any,
    caption: str | None,
    entities: Any = None,
) -> None:
    """Send one message's media, working around protected-chat restrictions.

    Passing `message.media` through keeps the file reference of the chat it came
    from, so Telegram answers `ChatForwardsRestrictedError` for channels with
    forwarding disabled (caused by SendMediaRequest). The retry uploads bytes
    downloaded from the source, which is the only way such media can be copied.
    """
    from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError
    from telethon.errors.rpcerrorlist import MediaCaptionTooLongError

    restricted_exc: Exception | None = None

    send_kwargs: dict[str, Any] = {
        "entity": destination_channel_id,
        "file": message.media,
        "caption": caption,
    }
    if entities:
        send_kwargs["formatting_entities"] = entities
    try:
        await client.send_file(**send_kwargs)
        return
    except ChatForwardsRestrictedError as exc:
        restricted_exc = exc
    except MediaCaptionTooLongError:
        # Telegram caps captions at 200 characters for send_file – truncate and retry.
        if caption and len(caption) > 200:
            caption = caption[:197] + "..."
            send_kwargs["caption"] = caption
            try:
                await client.send_file(**send_kwargs)
                return
            except Exception:
                pass  # fall through to the byte‑based fallback below

    media_size = int(getattr(getattr(message, "file", None), "size", 0) or 0)
    if media_size > RESTRICTED_MEDIA_FALLBACK_MAX_BYTES:
        if restricted_exc is not None:
            raise restricted_exc
    media_bytes = await client.download_media(message, file=bytes)
    if not media_bytes:
        if restricted_exc is not None:
            raise restricted_exc
    # If the caption is still too long after the first retry, truncate it again.
    if caption and len(caption) > 200:
        caption = caption[:197] + "..."
        send_kwargs["caption"] = caption
    retry_kwargs = dict(send_kwargs)
    retry_kwargs["file"] = media_bytes
    await client.send_file(**retry_kwargs)


async def _repost_messages_without_forward(
    client: Any,
    destination_channel_id: int,
    source_id: int,
    message_ids: list[int],
    source_entity: Any | None = None,
) -> tuple[int, int, tuple[int, ...]]:
    from telethon.errors.rpcerrorlist import ChatForwardsRestrictedError
    from telethon.errors.rpcerrorlist import FloodWaitError

    fetched = await client.get_messages(source_entity if source_entity is not None else source_id, ids=message_ids)
    messages = list(fetched) if isinstance(fetched, (list, tuple)) else [fetched]
    by_id = {int(getattr(msg, "id", 0)): msg for msg in messages if getattr(msg, "id", None)}

    ordered_ids = [int(mid) for mid in message_ids]
    copied = 0
    skipped = 0
    processed: list[int] = []

    # Album path: keep multi-media posts grouped instead of splitting them into
    # separate destination messages (GIF members are included in the album).
    if len(ordered_ids) > 1 and all(
        by_id.get(mid) is not None and _sendable_media_kind(by_id[mid]) for mid in ordered_ids
    ):
        album_messages = [by_id[mid] for mid in ordered_ids]
        album_captions = [
            _sanitize_message_text(getattr(message, "message", None) or "") or None
            for message in album_messages
        ]
        try:
            await client.send_file(
                entity=destination_channel_id,
                file=[message.media for message in album_messages],
                caption=album_captions,
            )
            return len(ordered_ids), 0, tuple(ordered_ids)
        except FloodWaitError as inner_exc:
            raise FloodHalt(seconds=int(inner_exc.seconds), processed_ids=tuple(), copied=0, skipped=0) from inner_exc
        except Exception:
            # Degrade to per-message sends so one bad item cannot drop the album.
            pass

    for mid in ordered_ids:
        msg = by_id.get(int(mid))
        if msg is None:
            skipped += 1
            processed.append(int(mid))
            continue
        if _is_gif_message(msg):
            skipped += 1
            processed.append(int(mid))
            continue

        raw_text = getattr(msg, "message", None) or ""
        text = raw_text
        cleaned_text = _sanitize_message_text(text)
        entities = getattr(msg, "entities", None) if cleaned_text == text else None
        media_kind = _sendable_media_kind(msg)
        try:
            if media_kind:
                await _send_media_with_restriction_fallback(
                    client=client,
                    destination_channel_id=destination_channel_id,
                    message=msg,
                    caption=cleaned_text or None,
                    entities=entities,
                )
                copied += 1
                processed.append(int(mid))
                continue

            # Non-sendable media (poll, geo, dice, contact) falls back to its text.
            if cleaned_text:
                send_message_kwargs = {
                    "entity": destination_channel_id,
                    "message": cleaned_text,
                }
                if entities:
                    send_message_kwargs["formatting_entities"] = entities
                await client.send_message(**send_message_kwargs)
                copied += 1
                processed.append(int(mid))
                continue

            skipped += 1
            processed.append(int(mid))
        except FloodWaitError as inner_exc:
            raise FloodHalt(
                seconds=int(inner_exc.seconds),
                processed_ids=tuple(processed),
                copied=copied,
                skipped=skipped,
            ) from inner_exc
        except ChatForwardsRestrictedError:
            # Protected media that cannot be re-uploaded either (too large or
            # download-blocked): skip this item instead of failing the source.
            skipped += 1
            processed.append(int(mid))

    return copied, skipped, tuple(processed)


async def _run_once(settings: AppwriteSettings) -> dict[str, Any]:
    try:
        from telethon import TelegramClient
        from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError
        from telethon.sessions import StringSession
    except Exception as exc:
        raise RuntimeError(
            "Telethon runtime dependency is unavailable. "
            "Verify function build/deploy installed requirements.txt correctly."
        ) from exc

    client = TelegramClient(StringSession(settings.session_string), settings.api_id, settings.api_hash)
    try:
        await client.connect()
    except AuthKeyDuplicatedError as exc:
        raise RuntimeError(
            "SESSION_STRING invalidated by Telegram (AuthKeyDuplicatedError). "
            "Generate a NEW session string and set it in Appwrite. "
            "Ensure the same session is not used from multiple IPs/runtimes."
        ) from exc
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("SESSION_STRING is not authorized. Create a valid user session string first.")
        return await _copy_source_messages(client=client, settings=settings)
    finally:
        await client.disconnect()


def _run_once_sync(settings: AppwriteSettings) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_once(settings))

    # If host runtime already has an active loop, run in an isolated worker thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(lambda: asyncio.run(_run_once(settings)))
        return future.result()


def _auto_continue_enabled() -> bool:
    raw = (os.getenv("AUTO_CONTINUE") or "").strip().lower()
    if not raw:
        return True
    return raw in {"1", "true", "yes", "on"}


def _auto_continue_max_runs() -> int:
    raw = (os.getenv("AUTO_CONTINUE_MAX_RUNS") or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return AUTO_CONTINUE_DEFAULT_MAX_RUNS
    return max(1, min(50, value))


def _request_chain_depth(context: Any) -> int:
    """Depth of the auto-continue chain that triggered this execution (0 = first)."""
    body = getattr(getattr(context, "req", None), "body", None)
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError:
            return 0
    payload: Any = body
    if isinstance(body, str):
        text = body.strip()
        if not text:
            return 0
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return max(0, int(payload.get("chain") or 0))
    except (TypeError, ValueError):
        return 0


def _spawn_followup_execution(chain: int) -> tuple[bool, str]:
    """Queue one more asynchronous execution of this function.

    Uses the function's own ephemeral API key (`APPWRITE_FUNCTION_API_KEY`),
    which needs the `executions.write` scope. Best effort: a failure here never
    affects the copy that just finished.
    """
    import urllib.error
    import urllib.request

    endpoint = (_appwrite_endpoint() or "").rstrip("/")
    project_id = _appwrite_project_id()
    api_key = _appwrite_api_key()
    if not endpoint or not project_id or not api_key:
        return False, "missing endpoint/project/api key; will use the hourly schedule instead"

    function_id = (os.getenv("APPWRITE_FUNCTION_ID") or "").strip() or "telegram-copier-webhook"
    payload = json.dumps(
        {
            "async": True,
            "method": "POST",
            "path": "/",
            "body": json.dumps({"source": "auto_continue", "chain": int(chain)}),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint}/functions/{function_id}/executions",
        data=payload,
        method="POST",
        headers={
            "X-Appwrite-Project": project_id,
            "X-Appwrite-Key": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001 - never fail a copy run over the chain
        return False, f"{type(exc).__name__}: {exc}"
    return True, str(data.get("$id") or "")


def _maybe_continue_run(result: dict[str, Any], chain: int) -> tuple[bool, str, str]:
    """Queue the next run when this one stopped on its budget.

    Returns `(queued, execution_id, note)`.
    """
    if not _auto_continue_enabled():
        return False, "", "AUTO_CONTINUE is off"
    if result.get("halted"):
        return False, "", "run halted on flood wait; the hourly run resumes it"
    if not result.get("partial"):
        return False, "", "run finished without hitting the budget"
    limit = _auto_continue_max_runs()
    if chain + 1 >= limit:
        return False, "", f"chain limit AUTO_CONTINUE_MAX_RUNS={limit} reached; hourly run resumes"
    queued, detail = _spawn_followup_execution(chain + 1)
    if not queued:
        return False, "", detail
    return True, detail, f"queued follow-up run chain={chain + 1}"


def main(context: Any):
    _safe_log(context, "User sync execution started.")
    try:
        settings = _load_settings_from_env()
        _safe_log(
            context,
            "Starting user sync run "
            f"(sources={len(settings.source_channel_ids)}, destination={settings.destination_channel_id})",
        )
        result = _run_once_sync(settings)
        chain_depth = _request_chain_depth(context)
        continued, next_execution_id, continue_note = _maybe_continue_run(result, chain_depth)
        result["chain_depth"] = chain_depth
        result["continued"] = int(continued)
        result["next_execution_id"] = next_execution_id
        result["auto_continue_note"] = continue_note
        _safe_log(
            context,
            "Sync completed successfully: "
            f"build_version={result.get('build_version')}, "
            f"copied={result.get('copied', 0)}, "
            f"fetched={result.get('fetched', 0)}, "
            f"skipped_invalid={result.get('skipped_invalid', 0)}, "
            f"skipped_invalid_runtime={result.get('skipped_invalid_runtime', 0)}, "
            f"skipped_duplicate_recent={result.get('skipped_duplicate_recent', 0)}, "
            f"skipped_cursor_gate={result.get('skipped_cursor_gate', 0)}, "
            f"skipped_blocked_ad={result.get('skipped_blocked_ad', 0)}, "
            f"skipped_blocked_gif={result.get('skipped_blocked_gif', 0)}, "
            f"filtered_links={result.get('filtered_links', 0)}, "
            f"sources_with_new_messages={result.get('sources_with_new_messages', 0)}, "
            f"source_errors_count={result.get('source_errors_count', 0)}, "
            f"halted={result.get('halted', 0)}, "
            f"flood_wait_seconds={result.get('flood_wait_seconds', 0)}, "
            f"partial={result.get('partial', 0)}, "
            f"backfill_enabled={result.get('backfill_enabled', 0)}, "
            f"backfill_copied={result.get('backfill_copied', 0)}, "
            f"backfill_scanned={result.get('backfill_scanned', 0)}, "
            f"backfill_sources_done={result.get('backfill_sources_done', 0)}, "
            f"copy_date={result.get('copy_date') or '-'}, "
            f"copy_date_copied={result.get('copy_date_copied', 0)}, "
            f"copy_date_scanned={result.get('copy_date_scanned', 0)}, "
            f"copy_date_sources_done={result.get('copy_date_sources_done', 0)}, "
            f"copy_all={result.get('copy_all', 0)}, "
            f"copy_all_copied={result.get('copy_all_copied', 0)}, "
            f"copy_all_scanned={result.get('copy_all_scanned', 0)}, "
            f"copy_all_chunks={result.get('copy_all_chunks', 0)}, "
            f"copy_all_sources_done={result.get('copy_all_sources_done', 0)}, "
            f"state_saved={result.get('state_saved', 0)}, "
            f"state_backend={result.get('state_backend')}, "
            f"db_logging_enabled={result.get('db_logging_enabled', 0)}, "
            f"db_state_enabled={result.get('db_state_enabled', 0)}, "
            f"db_auth_mode={result.get('db_auth_mode')}, "
            f"appwrite_endpoint_set={result.get('appwrite_endpoint_set', 0)}, "
            f"appwrite_auth_set={result.get('appwrite_auth_set', 0)}, "
            f"appwrite_database_id_set={result.get('appwrite_database_id_set', 0)}, "
            f"appwrite_state_collection_id={result.get('appwrite_state_collection_id')}, "
            f"run_snapshot_count={result.get('run_snapshot_count', 0)}, "
            f"updated_sources={len(result.get('updated_sources', []))}, "
            f"chain_depth={result.get('chain_depth', 0)}, "
            f"continued={result.get('continued', 0)}, "
            f"next_execution_id={result.get('next_execution_id') or '-'}, "
            f"auto_continue_note={result.get('auto_continue_note')}, "
            f"state_message_id={result.get('state_message_id')}",
        )
        if result.get("db_log_last_error"):
            _safe_log(context, f"DB log write issue: {result.get('db_log_last_error')}", is_error=True)
        if result.get("db_state_last_error"):
            _safe_log(context, f"DB state write issue: {result.get('db_state_last_error')}", is_error=True)
        source_stats_map = result.get("source_run_stats") or {}
        total_sources = len(settings.source_channel_ids)
        with_messages = sum(
            1 for item in source_stats_map.values() if int(item.get("fetched", 0) or 0) > 0
        )
        copied_sources = [
            key for key, item in source_stats_map.items() if int(item.get("copied", 0) or 0) > 0
        ]
        entity_error_sources = [
            key for key, item in source_stats_map.items() if item.get("status") == "entity_error"
        ]
        failed_sources = [
            key for key, item in source_stats_map.items() if item.get("status") == "error"
        ]
        _safe_log(
            context,
            "Source coverage: "
            f"checked={len(source_stats_map)}/{total_sources}, "
            f"with_messages_in_window={with_messages}, "
            f"copied_sources={len(copied_sources)}, "
            f"entity_errors={len(entity_error_sources)}, "
            f"source_errors={len(failed_sources)}",
        )
        if copied_sources:
            _safe_log(context, f"Copied sources: {','.join(sorted(copied_sources, key=_source_sort_key))}")
        if entity_error_sources:
            _safe_log(
                context,
                "Entity error sources: "
                + ",".join(sorted(entity_error_sources, key=_source_sort_key)),
                is_error=True,
            )
        if result.get("source_errors"):
            _safe_log(context, f"Source errors: {json.dumps(result.get('source_errors'), ensure_ascii=True)}", is_error=True)
        _safe_log(
            context,
            "Per-source legend: f=fetched in lookback window c=copied g=cursor gate skip "
            "r=recent dup skip a=ad block i=gif block w=id window(min-max); zero fields omitted",
        )
        for source_key in sorted(source_stats_map.keys(), key=_source_sort_key):
            _safe_log(
                context,
                _format_source_run_line(source_key, source_stats_map[source_key]),
                echo_native=False,
            )
        return _respond_json(context, {"ok": True, **result})
    except Exception as exc:
        _safe_log(context, f"Sync failed: {type(exc).__name__}: {exc}", is_error=True)
        _safe_log(context, traceback.format_exc(), is_error=True)
        return _respond_json(context, {"ok": False, "error": str(exc)}, 500)
