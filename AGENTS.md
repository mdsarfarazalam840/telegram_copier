# Handover Notes (Telegram Copier - Appwrite User Sync)

## Scope
- This project supports two runtime paths:
  - Local/docker long-running Telethon client (`main.py`)
  - Appwrite function-based scheduled sync (user session) in:
    - `appwrite/functions/webhook/src/main.py`
- Current production focus is Appwrite scheduled user-session sync.

## Appwrite Function Behavior
- Uses `SESSION_STRING` (Telethon StringSession), not bot token.
- Schedule is configured in `appwrite.json` (`telegram-copier-webhook` function).
- Reads source channels and copies/reposts into destination channel.
- Maintains dedupe/checkpoint state in Telegram Saved Messages with marker:
  - `telegram_copier_state_v1:...`
- Creates per-run snapshot messages in Telegram Saved Messages with marker:
  - `telegram_copier_run_v1:...` (contains last lookback-window IDs by source, chunked if large)

## State Model (Saved Messages)
- `sources.<channel_id>.last_id`
- `sources.<channel_id>.recent` (last processed IDs window)
- `sources.<channel_id>.updated_at_ist` (primary timestamp)
- `sources.<channel_id>.updated_at_utc` (legacy compatibility)
- Backfill progress lives on the same source entry:
  - `backfill_max_id` (next fetch upper bound; `0` = start at newest)
  - `backfill_done` (`1` when the source history floor is reached)
  - `backfill_floor_ms` (resolved oldest timestamp, persisted on first run)
  - `backfill_scanned`, `backfill_copied`, `backfill_updated_at_ist`
- `meta.last_run_ts_ms`
- `meta.last_run_at_ist` (primary timestamp)
- `meta.last_run_at_utc` (legacy compatibility)
- `meta.status` (`running`, `completed`, `completed_with_errors`, `halted`)
- Optional run counters in `meta` (`copied`, `fetched`, etc.)

## Key Runtime Rules
- Dedupe:
  - Skip already processed IDs (`recent`) and older IDs (`last_id`).
  - `FORCE_FULL_LOOKBACK_COPY=1` bypasses both guards: each run copies ALL messages in the lookback window regardless of cursor/recent state (ad/GIF blocking still applies). Use for manual backfill/resync; leave off for normal scheduled runs to avoid duplicates.
- Live fetch (default hourly mode):
  - With a stored cursor the fetch is `iter_messages(min_id=last_id, reverse=True, limit=LIVE_FETCH_CEILING)`: oldest-first, no time window, so a 1,000- or 5,000-message backlog is drained across runs instead of being dropped.
  - `LOOKBACK_MINUTES` (default 60) applies only when a source has no cursor yet (first run) and to snapshot lookup.
  - `LIVE_FETCH_CEILING` (5000) bounds one source's fetch per run; the cursor only advances past delivered messages, so the next run resumes exactly where the budget stopped.
  - Sources rotate round-robin (`meta.next_source_index`) so one busy channel cannot consume every run's budget.
  - Forward/repost processing is gated by source cursor (`last_id`) and recent dedupe to prevent duplicate pushes.
  - Processing set is built from:
    - live fetch from source channels for lookback window, plus
    - IDs recovered from Saved Messages run snapshots (`telegram_copier_run_v1`) from the same lookback window.
  - This recovery merge reduces risk of missing messages between runs.
  - Cursor self-heal:
    - If source cursor is ahead of all fetched IDs for a run, cursor gating is temporarily relaxed for that source in that run (still deduped by `recent`) to restore flow.
- Auto-continue:
  - A run that ends with `partial=1` queues the next asynchronous execution (`AUTO_CONTINUE=1`, default), capped at `AUTO_CONTINUE_MAX_RUNS` (default 10, ≈40 min).
  - Requires the `executions.write` function scope; the request uses the function's own ephemeral key (`APPWRITE_FUNCTION_API_KEY`) against `APPWRITE_FUNCTION_API_ENDPOINT` and `APPWRITE_FUNCTION_ID` (default `telegram-copier-webhook`).
  - Skipped on flood halt and at the chain cap; the hourly schedule resumes. Chaining failures never fail the copy run (`continued=0` + `auto_continue_note`).
- Flood wait:
  - Halts gracefully and returns `flood_wait_seconds` + `halted=1`.
- Protected/forward-restricted chats:
  - Fallback repost path attempts direct send (text/media) without forward.
- Sanitization:
  - Removes promotional/ad footer signature lines (marketing text + attached Telegram link),
    e.g. the banned `𝙰𝚍𝚞𝚕𝚝 𝙱𝚊𝚌𝚔𝚞𝚙 ➥「18+ BACK-UP🔞 (https://t.me/backupx08)」` block.
  - Also removes `youtube.com` URLs/tokens and words `mc`, `bc` (case-insensitive whole words).
  - Normal Telegram links are preserved unless `STRIP_T_LINKS=1`; ad-footer links are removed as part of the dropped footer line.
- Hard skips (not copied/forwarded):
  - Any message text matching ad markers (`#ad`, `insideads*`)
  - Any message that is *only* promotional/ad footer content; with `BLOCK_ANY_FOOTER_AD_MESSAGE=1`, any message containing one
  - Optional stricter rule: standalone `ad` only when `BLOCK_GENERIC_AD_WORD=1`
  - GIF messages/media (`.gif`)
- Media fallback safety:
  - `MessageMediaWebPage` is treated as non-sendable media and sent as text.
- State save safety:
  - Oversized state payloads are compacted automatically before save to avoid `MessageTooLongError`.
- Snapshot-recovery safety:
  - Loading run snapshots from Saved Messages is best-effort; failures do not block main forwarding.
- Auth/session safety:
  - `AuthKeyDuplicatedError` means Telegram invalidated the session; rotate `SESSION_STRING` and keep it exclusive to this runtime.

## Critical Env Vars (Appwrite Function)
- `API_ID`
- `API_HASH`
- `SESSION_STRING`
- `DESTINATION_CHANNEL_ID`
- `SOURCE_CHANNEL_ID` or `SOURCE_CHANNEL_IDS`
- `SOURCE_CHANNEL_USERNAMES` (optional ordered fallback for public sources not present in the session entity cache)
- `LIMIT_PER_SOURCE` (1..200)
- `LOOKBACK_MINUTES` (1..1440)
- `FORCE_FULL_LOOKBACK_COPY` (optional; `1` copies everything in the lookback window each run, ignoring cursor + recent dedup)
- `BACKFILL_ENABLED` (optional; `1` enables resumable history backfill older than the lookback window)
- `BACKFILL_SINCE_IST` (optional; oldest date to backfill, e.g. `2026-11-08` or `2026-11-08 22:00`)
- `BACKFILL_DAYS` (optional, default `2`; backfill floor in days when `BACKFILL_SINCE_IST` is blank; `0` = full history)
- `BACKFILL_CHUNK_SIZE` (optional, default `100`, max `1000`; messages fetched per source per run)
- `RUN_TIME_BUDGET_SECONDS` (optional, default `240`, range `30..280`; stops before the 300 s function timeout)
- `AUTO_CONTINUE` (optional, default `1`; a budget-stopped run queues the next execution)
- `AUTO_CONTINUE_MAX_RUNS` (optional, default `10`, range `1..50`; chain cap)
- `BACKFILL_RESET` (optional; `1` clears stored backfill progress for one run)
- `BLOCK_FOOTER_AD_MESSAGES` (optional; default `1` skips messages that are only ad-footer content)
- `BLOCK_ANY_FOOTER_AD_MESSAGE` (optional; `1` skips any message containing an ad-footer signature)
- `BLOCK_GENERIC_AD_WORD` (optional; `1` enables standalone `ad` blocking)

## Runtime Metrics
- Core:
  - `copied`, `fetched`, `skipped_invalid`, `filtered_links`, `sources_with_new_messages`, `source_errors_count`
- State/run tracking:
  - `state_saved`, `state_message_id`, `updated_sources`
  - `run_snapshot_count`, `run_snapshot_message_ids`
- Skip breakdown:
  - `skipped_invalid_runtime`
  - `skipped_duplicate_recent`
  - `skipped_cursor_gate`
  - `skipped_blocked_ad`
  - `skipped_blocked_gif`

## Recent Fixes
- Added robust source error reporting (`source_errors`, `source_errors_count`).
- Added reliable state lookup via search in Saved Messages.
- Added explicit `state_message_id` + `state_saved` metrics.
- Added fallback for `MessageMediaWebPage` so it no longer crashes on repost.
- Fixed lookback fetch traversal to honor full `LOOKBACK_MINUTES` window.
- Fixed loop bug that could process only the last grouped item in some runs.
- Added Telegram link removal for full `t.me/`/`telegram.me/` URLs.
- Added ad-message blocking.
- Added GIF-message blocking.
- Added per-run Saved Messages snapshot (`telegram_copier_run_v1`) with 60-min IDs by source.
- Added merge logic to combine live fetch + recent run snapshot IDs (last 60 min) before processing.
- Added state compaction on save to prevent 500 errors from oversized state message payloads.
- Added best-effort snapshot loading (failure won’t block main forwarding).
- Added cursor self-heal when `last_id` is ahead of fetched range.
- Added explicit `AuthKeyDuplicatedError` handling with actionable runtime error.
- Added detailed skip-reason counters in run result/logs.
- Added per-source execution logs: `Source coverage: checked=<n>/<total>` plus one compact line per configured source (`source=<id> f= c= g= r= a= i= w=`). Per-source stats also returned as `source_run_stats`. Kept compact because Appwrite trims execution logs at 8000 chars.
- Added `FORCE_FULL_LOOKBACK_COPY=1` to copy every message in the lookback window on a run, bypassing both the cursor gate and the recent-processed dedup (ad/GIF blocking still applies). Use for manual backfill/resync; turn off for normal scheduled runs.
- Added promotional/ad footer signature stripping: banned ad words + attached Telegram links (e.g. `18+ BACK-UP🔞` with `t.me/backupx08`) are dropped from copied text, and pure ad-footer messages are skipped. Added `BLOCK_ANY_FOOTER_AD_MESSAGE=1` to skip any message containing a footer signature.
- Preserved normal Telegram links by default (strip only via `STRIP_T_LINKS=1`) while still removing ad-footer links.
- Added resumable history backfill (`BACKFILL_ENABLED`) beyond the 24-hour `LOOKBACK_MINUTES` cap. Each source keeps a `backfill_max_id` cursor and is processed newest-to-oldest in chunks. Backfill never advances the live cursor. `RUN_TIME_BUDGET_SECONDS` stops a run before the 300 s function timeout and the next execution resumes from the stored cursor. `BACKFILL_SINCE_IST` / `BACKFILL_DAYS` set the floor, and `BACKFILL_RESET=1` restarts progress.

## Deployment Status
- As of 2026-11-09: resumable backfill (`BACKFILL_ENABLED`) and `RUN_TIME_BUDGET_SECONDS` added; tests passing (`python -m pytest -q`, 108 passed).
- Deployment from this workspace is pending (run `appwrite push functions` to publish).

## Appwrite 2.x Platform Notes
- Query dialect: Appwrite 2.6.2 rejects legacy strings (`equal("state_key",["global"])`, `limit(1)`) with `Invalid query: Syntax error`. Use JSON query objects: `{"method":"equal","attribute":"state_key","values":["global"]}`, `{"method":"limit","values":[1]}`, `{"method":"orderDesc","attribute":"$createdAt"}`. The function sends JSON first and flips to legacy only if the server rejects it.
- State 409: a failed list call left `_load_state` without a row id, so `_save_state` created `state_key=global` and hit `row_unique_constraint_violation`. The loader now returns the real `$id` even when the payload is compacted, and the saver reuses the existing `state_key=global` row.
- Schedule floor: the current plan rejects intervals below 60 minutes (`Your current plan allows intervals of 60 minutes or longer`). `appwrite.json` uses `"interval": 60`.
- Coverage: hourly schedule plus `LOOKBACK_MINUTES=120` keeps the last hour fully covered and tolerates one delayed run.
- Cursor gate: `ENABLE_CURSOR_GATE=1` is required alongside DB state, otherwise each hourly run re-copies the whole lookback window.
- Copy mode: `COPY_MODE` defaults to `copy` (own post, no forward attribution). `COPY_MODE=0` or `forward` restores forwarding.

## Validation
- Frontend: `cd frontend`, then `npm ci`, `npm test`, `npm run build`, and `npm run test:browser` (requires `npx playwright install chromium`). Browser tests mock Appwrite HTTP; they do not establish production connectivity.
- Relayroom uses the Appwrite Web SDK to read existing DB state/snapshots and submit asynchronous manual syncs. Connection IDs are entered in its setup form; API keys and Telegram secrets must never be added to frontend files. See `frontend/README.md` for setup and data-availability limitations.
- Relayroom paginates source checkpoints/history in the UI and uses the latest final state metadata as a counter fallback when Appwrite state/log timestamps are not identical. The webhook state compactor retains a bounded `meta.source_errors` summary for future diagnostics; redeploy the webhook after this change.
- Local tests:
  - `python -m pytest -q`
- Latest baseline in this workspace: all tests passing.

## Ops Checklist
1. `appwrite push functions`
2. Ensure latest deployment is active.
3. Verify function variables are set correctly.
4. Run one manual execution and inspect logs:
   - `copied`, `fetched`, `source_errors_count`, `state_saved`, `state_message_id`
   - skip breakdown: `skipped_invalid_runtime`, `skipped_duplicate_recent`, `skipped_cursor_gate`, `skipped_blocked_ad`, `skipped_blocked_gif`
5. Confirm destination messages and Saved Messages state updates.
