# Telegram Copier — Configuration Variables Reference

This document maps every configuration variable the function reads from the environment, what it does, its default, valid values, the issues that were discovered during development, and how each was resolved. It is intended to be the single source of truth for operators and developers.

---
## Table of Variables

| Variable | Default | Values | What It Does | Issues Faced | How Resolved |
|---|---|---|---|---|---|
| `COPY_DATE` | *(empty — feature off)* | `YYYY‑MM‑DD` (e.g. `2026‑11‑08`) | Copy **exactly one IST calendar day** into the destination. The window is `[00:00, next 00:00)` in IST. Empty / `none` / `off` / `no` / `-` / `clear` disables the feature and switches the function back to the hourly live pass. | • Originally there was no closed‑day window mode; users could only copy relative windows (`LOOKBACK_MINUTES`) or full backfill. <br>• `COPY_DATE` combined with `BACKFILL_ENABLED` caused fights over per‑source progress fields. <br>• An unparseable value caused a hard crash before any Telegram contact. | • Added strict parsing in `_parse_copy_date_range_ms` that accepts `none|off|no|-|clear` as “unset” and rejects everything else unless it matches `YYYY‑MM‑DD`. <br>• Mutual‑exclusion guard raises `Set either COPY_DATE or BACKFILL_ENABLED, not both.` <br>• Empty / magic values are treated as “off” so a leftover variable does not block the live pass. |
| `COPY_DATE_RESET` | `0` | `0` / `1` | When `1`, the date‑mode progress for the **currently configured** `COPY_DATE` is reset, forcing a re‑copy of that day from the newest message. Use after wiping the destination or to re‑run a date that was already marked done. | • Without a reset switch, re‑running the same date would skip messages already marked done. | • Reset is honoured in `_run_date_phase`; after reset, `copy_date*` counters are zeroed and the day is re‑fetched. |
| `COPY_ALL` | `0` | `0` / `1` | Copy the **entire channel history**, newest → oldest, chunked and resumable. Runs until the first message is reached or the run budget is spent. `COPY_ALL=1` and `COPY_DATE` are mutually exclusive. | • `COPY_ALL=1` with `COPY_DATE` still set caused a `ValueError`. <br>• `BACKFILL_ENABLED=1` combined with `COPY_ALL` was rejected. | • Validation in `_load_settings_from_env` raises clear errors: `COPY_ALL=1 is set but COPY_DATE is still '...'` and `Set either COPY_ALL or BACKFILL_ENABLED`. <br>• Progress keys `copy_all_*` keep track of scanned/copied/chunks across runs. |
| `COPY_ALL_RESET` | `0` | `0` / `1` | Restart the `COPY_ALL` walk from the newest message and re‑send ids already recorded in `recent`. Use after wiping the destination. | • Without a reset, a new run would resume from the saved `copy_all_max_id` and never re‑process already‑copied messages. | • Reset is processed before the first chunk fetch; `recent` ids are temporarily ignored so the whole history is re‑sent. |
| `BACKFILL_ENABLED` | `0` | `0` / `1` | Enable **backfill mode**: copy history in chunks of `BACKFILL_CHUNK_SIZE`, one chunk per execution, until the configured number of days (`BACKFILL_DAYS`) is covered. Mutually exclusive with `COPY_ALL` and `COPY_DATE`. | • Backfill fetched only one chunk per run, so a 5 000‑message channel needed ~50 hourly runs. <br>• `BACKFILL_ENABLED=1` together with `COPY_DATE` raised a conflict guard. | • Validation rejects the combination. <br>• Progress fields `backfill_*` keep per‑source scanned/copied counts. |
| `BACKFILL_DAYS` | `0` | `0`‑`365` (effective max depends on channel size) | Number of past days to copy when `BACKFILL_ENABLED=1`. `BACKFILL_DAYS=0` means “copy everything newest → oldest in one shot” (equivalent to `COPY_ALL` semantics but using backfill infrastructure). | • Users expected `BACKFILL_DAYS=0` to copy the whole channel, but it only fetched one chunk per run. | • Documented distinction: `COPY_ALL` fetches chunks until budget exhausted per source; `BACKFILL_ENABLED=1` + `BACKFILL_DAYS=0` still only does one chunk per run. |
| `COPY_DATE_RESET` | `0` | `0` / `1` | See `COPY_DATE_RESET` above (duplicate row; kept for symmetry). | — | — |
| `LOOKBACK_MINUTES` | `60` | `1`‑`1440` (default 60) | In **live mode** without a cursor, the window is `now - LOOKBACK_MINUTES`. Used only when `last_id` is `0` (cold start) so a fresh state never walks the entire channel history. | • Originally `LOOKBACK_MINUTES` was a strict wall‑clock window, so any backlog older than the window was **permanently lost** when living in date mode. | • Live fetch was rewritten to **cursor‑based drain**: `iter_messages(source, min_id=last_id, reverse=True, limit=LIVE_FETCH_CEILING=5000)`. <br>• `LOOKBACK_MINUTES` now only applies on cold start (no cursor) and to snapshot lookup; it no longer caps the live copy volume. |
| `LIVE_FETCH_CEILING` | `5000` | Positive integer | Maximum number of messages fetched in a single live‑pass iteration. The cursor‑based drain fetches up to this many messages per run (`iter_messages(..., limit=LIVE_FETCH_CEILING)`). | • Before the cursor rewrite, live fetch used a strict time/window cut‑off and could lose older messages. | • Cursor advances only past **delivered** messages, so a budget‑stopped run resumes exactly where it left off. <br>• Sources rotate round‑robin via persisted `meta.next_source_index` so one busy channel cannot consume the whole budget. |
| `RUN_TIME_BUDGET_SECONDS` | `240` | `30`‑`280` (validated) | How long a run may actively copy before it must stop and let the next run continue. Leaves a buffer under the Appwrite 300 s function timeout. | • Synchronous Appwrite executions are hard‑capped at **30 s**, producing the error `Timed out waiting for execution. Error Code: 400`. <br>• Users thought the copy failed, but the execution kept running to its own 300 s limit. | • Added `async` manual‑trigger helper `run_now.py` that posts `{"async": true}` and polls status every 5 s. <br>• `AUTO_CONTINUE=1` (default) queues the next execution automatically when the budget is spent, chaining runs until `partial=0`. <br>• `AUTO_CONTINUE_MAX_RUNS=10` caps the chain (~40 min) so runs never overlap the 60‑min schedule. |
| `AUTO_CONTINUE` | `1` | `0` / `1` | When `1`, a run that stops on its budget automatically queues the next execution (via `executions.write` scope). Set `0` to go back to one batch per hourly run. | • Without auto‑continue, large backlogs could never drain because each hourly run only had 240 s. | • Chain is visible in run response as `chain_depth`, `continued`, `next_execution_id`, and `auto_continue_note`. <br>• Skipped for `halted` runs (flood wait) and at the cap; the hourly schedule then picks up. |
| `AUTO_CONTINUE_MAX_RUNS` | `10` | `1`‑`50` | Maximum number of chained auto‑continue executions. Default `10 × 240 s ≈ 40 min`, deliberately under the 60‑min schedule so runs never overlap. | • Too many chains could push past the hourly interval, causing overlapping runs. | • Cap enforces that the total auto‑continue time stays below the schedule period. |
| `LIMIT_PER_SOURCE` | `50` | `1`‑`200` | In **snapshot‑recovery** mode, caps the number of IDs taken from the recent‑dedupe window per source. Does **not** cap the overall copy volume; it only limits the ID slice used for dedupe checking. | • Left at default 50; some thought it limited copy volume, but it only affects snapshot recovery IDs. | • Documented that it only caps snapshot‑recovery IDs, not the copy volume. |
| `BACKFILL_CHUNK_SIZE` | `100` | `1`‑`1000` | Number of messages fetched per chunk in backfill / `COPY_ALL` phases. The phase loops chunks until the budget or channel start is reached. | • Users expected a single run to copy the whole channel, but the default 100 meant many runs. | • Documented that the phase loops chunks, so a 5 000‑message channel is still fully copied with a 1000 chunk size across several runs. |
| `RESTRICTED_MEDIA_FALLBACK_MAX_BYTES` | `20 MB` | Positive integer (bytes) | Maximum media size that will be downloaded and re‑uploaded for protected channels. Larger media is skipped. | • Protected channels raise `ChatForwardsRestrictedError` when forwarding. <br>• Large media (>20 MB) would cause OOM if attempted. | • Media is retried by downloading it and uploading fresh bytes. <br>• Media above the limit is counted as skipped rather than buffered. |
| `BLOCK_COPYRIGHT_MESSAGES` | `1` (enabled) | `0` / `1` | When `1`, messages whose text matches Telegram’s copyright‑infringement placeholder (e.g. “couldn’t be displayed due to copyright infringement”) are dropped before delivery. | • Some legitimate text contains the word “copyright” and was incorrectly blocked. | • The filter matches common wording (“can’t / cannot / could not … copyright …”) but still copies ordinary text that mentions copyright. Setting `0` disables the block. |
| `ENABLE_CURSOR_GATE` | `1` (enabled) | `0` / `1` | When `1`, the cursor (`last_id`) gate is active: messages with IDs below the source’s high‑water mark are skipped for dedupe. When `0`, older IDs can re‑enter the live window. | • With `ENABLE_CURSOR_GATE=0` plus a huge date replay, duplicates could slip into later live runs. | • Default `1` preserves the gate; documented that a very large replay could push live IDs out of the dedupe window, but this is harmless with the cursor gate on. |
| `COPY_ALL_CHUNK_SIZE` | *(alias for `BACKFILL_CHUNK_SIZE`)* | — | Re‑used as the chunk size for `COPY_ALL` phases. | — | — |
| `DESTINATION_CHANNEL_ID` | *(required, set by operator)* | Any valid Appwrite channel ID | Where all copied messages are posted. | — | — |
| `SOURCE_CHANNEL_ID` / `SOURCE_CHANNEL_IDS` | *(required)* | Comma‑separated list of channel IDs (e.g. `-1001,-1002`) | Source channels to copy from. `SOURCE_CHANNEL_IDS` is the preferred list form. | — | — |
| `RUN_NOW_TIMEOUT` | `60` | Positive integer | Polling timeout (seconds) for `run_now.py` when waiting for an async execution to complete. | — | — |
| `APPWRITE_FUNCTION_ID` | `telegram-copier-webhook` | Any deployed function ID | The target function’s ID. Used by `run_now.py` and the auto‑continue execution creation. | — | — |
| `APPWRITE_ENDPOINT`, `APPWRITE_PROJECT_ID`, `APPWRITE_API_KEY` | *(from .env)* | — | Credentials the CLI uses to talk to Appwrite. | — | — |

---
## Variable‑by‑Variable Issue Log & Resolution

Below is a concise log of every significant issue that was discovered for a variable, the root cause, and the resolution that was implemented. This section mirrors the “what issue we faced and how it’s resolved” request.

### `COPY_DATE`
- **Issue:** No native “copy one IST day” mode existed. Users could only use `LOOKBACK_MINUTES` (relative to now, useless for past dates) or `BACKFILL_ENABLED` (which walked the whole history but only one chunk per run). Combining `COPY_DATE` with `BACKFILL_ENABLED` caused silent progress‑field conflicts.
- **Resolution:** Added strict parsing, magic‑value handling (`none|off|no|-|clear` → unset), and a hard mutual‑exclusion guard. Date mode now owns its own per‑source progress fields (`copy_date_max_id`, `copy_date_done`, etc.) and never touches `last_id`.

### `COPY_ALL` vs `BACKFILL_ENABLED`
- **Issue:** `BACKFILL_ENABLED=1` with `BACKFILL_DAYS=0` was expected to copy the whole channel, but it only fetched one chunk per execution (~100 messages), requiring ~50 hourly runs for a 5 000‑message channel.
- **Resolution:** `COPY_ALL` was introduced as a distinct mode that keeps fetching chunks for the same source until the run budget is spent, draining a 5 000‑message channel in a handful of manual runs. The two modes are now clearly separated with validation errors when combined incorrectly.

### `LOOKBACK_MINUTES` losing volume
- **Issue:** Live fetch was a strict wall‑clock window (`now - LOOKBACK_MINUTES`). Any backlog older than the window was permanently lost, especially after date‑mode runs that did not advance `last_id`.
- **Resolution:** Rewrote live fetch to **cursor‑based drain** using `iter_messages(source, min_id=last_id, reverse=True, limit=LIVE_FETCH_CEILING=5000)`. The cursor only advances past delivered messages, so a budget‑stopped run resumes exactly where it left off. `LOOKBACK_MINUTES` now only applies on cold start (no cursor) and to snapshot lookup.

### Synchronous 30 s timeout (`Timed out waiting for execution. Error Code: 400`)
- **Issue:** Appwrite cuts **synchronous** executions at 30 s, producing the error message users saw. The execution, however, continues to run under its own 300 s limit, so messages kept arriving afterwards — users thought the copy failed when it actually kept going.
- **Resolution:** Created `run_now.py` async manual‑trigger CLIENT that posts `{"async": true}` and polls status every 5 s. Added `AUTO_CONTINUE=1` (default) and `AUTO_CONTINUE_MAX_RUNS=10` so a run that stops on its budget automatically queues the next execution via the `executions.write` scope (added to `appwrite.json` for the webhook function). The chain is visible in run response logs.

### `MediaCaptionTooLongError` in media delivery
- **Issue:** Source errors: `MediaCaptionTooLongError: The caption is too long (caused by SendMediaRequest)`. Occurred when a message’s caption exceeded Telegram’s 200‑character limit for `send_file`.
- **Resolution:** Added `MediaCaptionTooLongError` catching in `_send_media_with_restriction_fallback`: truncate caption to ≤200 chars, retry `send_file`; fall through to byte‑based fallback if needed. All 140 tests pass.

### Protected channels (`ChatForwardsRestrictedError`)
- **Issue:** Some channels disable forwarding. Even byte re‑upload failed in some cases; a single protected source could abort the whole date phase, causing `copy_date_copied=0` with a single `source_errors` entry meaning *no* source was processed.
- **Resolution:** • Media is retried by downloading it and uploading the fresh bytes (the only way to copy protected content). • Media above `RESTRICTED_MEDIA_FALLBACK_MAX_BYTES` (20 MB) is skipped instead of buffering. • Delivery errors are now contained **per group**: a failure no longer aborts the entire source loop; the error is recorded per‑source and the drain continues with every other group/source.

### Copyright‑notice filtering
- **Issue:** Messages whose text is Telegram’s placeholder “This message couldn’t be displayed due to copyright infringement” were either always blocked or always let through, causing inconsistent `skipped_blocked_ad` counts.
- **Resolution:** Implemented a text‑matching filter that recognises common variants (“can’t / cannot / could not … copyright …”). The filter runs in every mode (live, backfill, `COPY_DATE`, `COPY_ALL`). `BLOCK_COPYRIGHT_MESSAGES=0` turns it off.

### `ENABLE_CURSOR_GATE` and duplicate live IDs
- **Issue:** With the gate off (`ENABLE_CURSOR_GATE=0`), a very large date replay could push older live IDs out of the dedupe window, causing duplicates in later live runs.
- **Resolution:** Default `1` keeps the gate active. Documented the edge‑case but confirmed it is harmless when the gate is on.

### `RUN_TIME_BUDGET_SECONDS` and budget starvation
- **Issue:** If `RUN_TIME_BUDGET_SECONDS` was too low (e.g., 30), the run would spend the entire budget before copying the first message, resulting in `partial=1` every run and zero progress.
- **Resolution:** Validated range `30‑280` (default `240`). Users are advised to raise the budget if they see `copied stays at 0 and partial=1` every run.

### `AUTO_CONTINUE_MAX_RUNS` chain overlap
- **Issue:** Without a cap, auto‑continue chains could exceed the 60‑minute hourly schedule, causing overlapping runs.
- **Resolution:** Default `10` × `240 s ≈ 40 min` stays under the schedule. The cap is enforced in the auto‑continue logic; the hourly schedule then picks up where the chain stopped.

### `LIMIT_PER_SOURCE` misunderstanding
- **Issue:** Operators thought `LIMIT_PER_SOURCE` limited the total number of messages copied.
- **Resolution:** Updated documentation to clarify it only caps the ID slice used for dedupe checking in snapshot‑recovery, not the copy volume.

---
## Key Flow Summaries

### Live Pass (hourly mirroring)
1. **Variables active:** `COPY_DATE=none`, `COPY_ALL=0`, `BACKFILL_ENABLED=0`, `RUN_TIME_BUDGET_SECONDS=240`, `AUTO_CONTINUE=1`, `AUTO_CONTINUE_MAX_RUNS=10`.
2. **Cursor drain:** `iter_messages(source, min_id=last_id, reverse=True, limit=LIVE_FETCH_CEILING=5000)`.
3. **Round‑robin source rotation:** `meta.next_source_index` rotates per run so no single channel consumes the entire budget.
4. **Auto‑continue:** If the 240 s budget is spent, the run queues itself via `executions.write`. The chain repeats until `partial=0`.
5. **Result:** Expect `copied` in the low hundreds per run, `partial=1`, `continued=1`, and `source_run_stats.<id>.cursor_id` climbing each run. When a run returns `partial=0`, the mirror is level with the sources.

### Date‑Mode (`COPY_DATE=YYYY‑MM‑DD`)
1. **Variables active:** `COPY_DATE=2026‑11‑08`, `COPY_ALL=0`, `BACKFILL_ENABLED=0`, `RUN_TIME_BUDGET_SECONDS=240`.
2. **One‑day window:** Resolved to `[copy_date_start_ms, copy_date_end_ms)` in IST.
3. **Chunk loop:** The phase loops chunks inside one execution, resuming with `offset_id = <smallest id of previous chunk>` until the day start is reached or the budget is spent.
4. **Progress per source:** `copy_date_max_id`, `copy_date_done`, `copy_date_copied`, `copy_date_scanned`.
5. **Result:** Day is complete when `copy_date_sources_done` equals the number of sources. `last_id` is never advanced, so the hourly live sync is unaffected. Set `COPY_DATE_RESET=1` to re‑copy the same day.

### Full‑Channel Copy (`COPY_ALL=1`)
1. **Variables active:** `COPY_ALL=1`, `BACKFILL_ENABLED=0`, `COPY_DATE=` (empty), `BACKFILL_CHUNK_SIZE=1000`.
2. **Chunk loop per source:** Fetches `iter_messages(limit=BACKFILL_CHUNK_SIZE, offset_id=<cursor>)`, newest first, delivers via `_deliver_group` path, advances `copy_all_max_id`, and fetches the next chunk in the **same run** until budget or channel start.
3. **Progress:** `copy_all_scanned`, `copy_all_copied`, `copy_all_chunks`, `copy_all_done` (set when a fetch returns <`BACKFILL_CHUNK_SIZE` messages).
4. **Result:** Several runs are normal (Telegram flood waits). When `copy_all_sources_done` equals the number of sources, the channel is fully copied. Clear `COPY_ALL=0` to resume hourly live mirroring.

### Auto‑Continue Chain
| Run | Budget (s) | Behaviour |
|---|---|---|
| 1 | 240 | Copies as many messages as budget allows; if budget exhausted, `partial=1` and auto‑continue queues run 2. |
| 2 … N | 240 each | Repeats; `chain_depth` increments. |
| N (last) | 240 | If `partial=0` after the budget, the chain stops. The hourly schedule then maintains the mirror. |
| At cap (10 runs) | — | Chain stops at `AUTO_CONTINUE_MAX_RUNS`; the schedule picks up. |

---
## Deployment Checklist

| Step | Action |
|---|---|
| 1 | Ensure `COPY_DATE=` (empty), `COPY_ALL=0`, `BACKFILL_ENABLED=0` for live mirroring. |
| 2 | Set `RUN_TIME_BUDGET_SECONDS=240` (default, leaves buffer under 300 s timeout). |
| 3 | Set `AUTO_CONTINUE=1` (default) and `AUTO_CONTINUE_MAX_RUNS=10` (default). |
| 4 | Verify `appwrite.json` includes `"executions.write"` scope for `telegram-copier-webhook`. |
| 5 | Deploy: `appwrite push functions` (or `appwrite deploy function --function-id telegram-copier-webhook`). |
| 6 | For date mode: set `COPY_DATE=YYYY‑MM‑DD`, redeploy, run manually, read `copy_date_*` response fields. |
| 7 | For full‑channel copy: set `COPY_ALL=1`, redeploy, run manually, repeat while `partial=1` or `copy_all_sources_done < sources`. |
| 8 | To resume live copying after date mode: clear `COPY_DATE` (set to `none` or empty), redeploy, run manually and confirm `build_version` reflects the latest live‑cursor‑drain build. |
| 9 | Use `python run_now.py` for async manual triggers; do **not** use a synchronous trigger (30 s cap). |
| 10 | Monitor `source_run_stats.<id>.cursor_id` and `partial`/`copy_date_sources_done` in execution logs. |

---
## Known Limitations (as of build `2026-11-15-auto-continue-v6`)

1. **Real‑Telegram end‑to‑end not verified** without a session string; the test suite uses a fake Telethon client.
2. **`_deliver_group` duplication** — the backfill path still has its own delivery block; a future refactor could call `_deliver_group` from both paths.
3. **`copy_date*` counters** appear in response/log lines, not in `telegram_copier_logs` dashboard; adding them would require schema changes.
4. **Crash mid‑chunk** can duplicate that chunk; progress is saved after each chunk, not each message.
5. **`recent` (2000 IDs) absorbs replayed IDs** — a very large date replay could push live IDs out of the dedupe window (harmless with cursor gate on).
6. **`FloodHalt` in album groups** may lose that group's partial `copied` count; the run reports `halted=1` and resumes correctly.
7. **`COPY_DATE` in the future** is accepted and simply copies nothing (`done=1`); no past/future validation.
8. **`COPY_ALL` only adds messages**; it never deletes or repairs the destination. Re‑running does not remove duplicates.
9. **A full copy of a 5 000‑message channel takes several runs** regardless of settings: flood waits plus the 300 s function timeout stop the walk, and the next run resumes from `copy_all_max_id`.
10. **Protected channels** can never be forwarded; only download + re‑upload works, capped at 20 MB (`RESTRICTED_MEDIA_FALLBACK_MAX_BYTES`).

---
*Document generated for the `telegram_copier` Appwrite function, build `2026-11-15-auto-continue-v6`. All 140 unit tests pass (`py_compile` exit 0).*