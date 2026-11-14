# Resumable History Backfill

This document covers the backfill feature added to the Appwrite user-session sync function
(`appwrite/functions/webhook/src/main.py`).

The feature removes two limits:

1. **24-hour ceiling.** The scheduled path still scans only `LOOKBACK_MINUTES` (max `1440`).
   Backfill reaches older history by walking a source channel newest to oldest with a
   persisted cursor.
2. **300-second function timeout.** Each run stops at `RUN_TIME_BUDGET_SECONDS` (default
   `240`) and resumes from the stored cursor on the next execution.

Backfill runs after the normal lookback pass in the same execution. It never advances the
live cursor, so scheduled copying keeps working while history is backfilled.

## Environment Variables

| Variable | Default | Range / values | Purpose |
| --- | --- | --- | --- |
| `BACKFILL_ENABLED` | `0` | `0`/`1` | Enable resumable history backfill. |
| `BACKFILL_SINCE_IST` | *(empty)* | date / ISO | Oldest message date to copy, for example `2026-11-08` or `2026-11-08 22:00`. Treated as IST when no offset is given. Blank uses `BACKFILL_DAYS`. |
| `BACKFILL_DAYS` | `2` | `0..3650` | Backfill floor in days when `BACKFILL_SINCE_IST` is blank. `0` means full channel history. |
| `BACKFILL_CHUNK_SIZE` | `100` | `1..1000` | Messages fetched per source per run. |
| `RUN_TIME_BUDGET_SECONDS` | `240` | `30..280` | Stop before the 300-second function timeout. The next run resumes automatically. |
| `BACKFILL_RESET` | `0` | `0`/`1` | Clear stored backfill progress for one run. Use after changing `BACKFILL_SINCE_IST`. |

Existing variables keep their behavior: `LOOKBACK_MINUTES`, `LIMIT_PER_SOURCE`,
`ENABLE_CURSOR_GATE`, `FORCE_FULL_LOOKBACK_COPY`, `COPY_MODE`, and the ad/GIF filters.

## State Fields

Backfill progress is stored on the existing per-source state entry, next to `last_id` and
`recent`:

- `backfill_max_id` — fetch upper bound for the next chunk. `0` means start at the newest message.
- `backfill_done` — `1` when the configured floor is reached. Completed sources fall back to the normal lookback path.
- `backfill_floor_ms` — resolved oldest timestamp, persisted on the first backfill run.
- `backfill_scanned` — total messages fetched by backfill for this source.
- `backfill_copied` — total messages copied by backfill for this source.
- `backfill_updated_at_ist` — last backfill progress update.

`_set_source_state` now merges fields instead of replacing the entry, so live cursor updates
do not erase backfill progress.

## How It Works

1. The normal lookback pass runs first, limited by `RUN_TIME_BUDGET_SECONDS`.
2. If time remains and `BACKFILL_ENABLED=1`, each source runs one backfill chunk.
3. A chunk is fetched with `client.iter_messages(entity, limit=BACKFILL_CHUNK_SIZE, offset_id=backfill_max_id)`.
4. Messages are processed in ascending ID order so albums stay grouped and destination order within a chunk is chronological.
5. Ads and GIFs are blocked with the same rules as the live path. Recently processed IDs are skipped using `recent`.
6. If the chunk completes, `backfill_max_id` becomes the smallest fetched ID. If the deadline hits mid-chunk, `backfill_max_id` moves to just above the newest unprocessed ID and the next run refetches the remainder.
7. When a fetch returns fewer than `BACKFILL_CHUNK_SIZE` messages, or crosses `backfill_floor_ms`, `backfill_done=1` is stored.
8. Flood waits and source errors stop or skip backfill without losing live progress. State is saved before the function returns.

Backfill never raises `last_id`. Live lookback processing therefore cannot skip messages that
backfill has not reached yet.

## Runbook

1. Deploy the function:

   ```
   appwrite push functions
   ```

2. Open Appwrite Cloud -> Functions -> `Telegram Copier User Sync` -> Variables and set:

   ```
   BACKFILL_ENABLED=1
   BACKFILL_SINCE_IST=2026-11-08
   RUN_TIME_BUDGET_SECONDS=240
   BACKFILL_CHUNK_SIZE=100
   ```

   For a relative window use `BACKFILL_DAYS=7` instead of `BACKFILL_SINCE_IST`.
   For full history use `BACKFILL_DAYS=0`.

3. Run the function manually. Repeat executions until the response reports
   `partial=0` and `backfill_sources_done` equal to the number of configured sources.

4. Set `BACKFILL_ENABLED=0` after the backfill finishes. Leaving it on is safe because
   completed sources skip backfill, but disabling it keeps logs and results smaller.

5. To restart backfill, or to apply a new `BACKFILL_SINCE_IST`, set `BACKFILL_RESET=1`
   for one run.

## Result Fields

Successful runs include:

- `backfill_enabled`
- `backfill_copied`
- `backfill_scanned`
- `backfill_sources_processed`
- `backfill_sources_done`
- `partial`
- `deadline_reached`
- `time_budget_seconds`

`partial=1` or `deadline_reached=1` means the run stopped at the time budget. Run again;
progress is stored. A backfill flood wait returns `halted=1` and `flood_wait_seconds`.

State metadata also records `backfill_enabled`, `backfill_sources_done`, `backfill_copied`,
`backfill_scanned`, and `partial` for dashboard reads.

## Duplicates

- Backfill skips IDs in the per-source `recent` list, which keeps the last `2000` processed IDs.
- Deep backfill can repost IDs older than that window. Use a bounded `BACKFILL_SINCE_IST` to limit the range.
- `FORCE_FULL_LOOKBACK_COPY=1` disables the recent-ID skip and can create duplicates. Do not combine it with a long backfill unless duplicates are acceptable.

## Validation

Local checks:

```
python -m pytest -q
python -m py_compile appwrite/functions/webhook/src/main.py
```

The test suite covers settings parsing, state merge, chunk fetch with `offset_id` and the
date floor, multi-run resume to completion, recent-ID dedupe, deadline stop without progress
loss, and the end-to-end live-plus-backfill run. Latest baseline: `108 passed`.
