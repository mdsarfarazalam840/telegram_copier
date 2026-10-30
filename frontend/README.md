# Relayroom: Appwrite frontend

The dashboard uses **Appwrite Web SDK 29.1.0** with project-user sessions. There is no demo-data mode and no server API key in the browser.

`appwrite.js` exports the shared client for **Tele-copier**, project `6996dedc00271499aa0b`, endpoint `https://fra.cloud.appwrite.io/v1`. On each page startup, the app calls `client.ping()` once and displays success/failure in the connection panel, even before login or database configuration. A successful ping checks API connectivity; it does not verify database permissions or a Telegram sync. The connection form defaults to this project; previously saved browser settings remain editable.

## Start

Requires Node.js 22.12+ (tested with Node.js 24).

```bash
cd telegram_copier/frontend
npm ci
npm run dev
```

Open **http://127.0.0.1:4173**. If another preview is already using port 4173, stop that preview first. The old `python -m http.server` command cannot serve the unbuilt npm imports. For static hosting, run `npm run build` and publish **frontend/dist/**. To preview the build, run `npm run preview` (port 4174).

## What you need to do in Appwrite Console

1. **Project → Settings:** copy the API endpoint including region and `/v1`. Use the API endpoint, not a function domain or Console URL. Confirm project ID `6996dedc00271499aa0b` is the deployed project.
2. **Project → Platforms:** add a Web platform for the hostname you use, e.g. `127.0.0.1`, `localhost`, and your eventual frontend domain as needed. Enter hostnames, not URLs with paths/ports.
3. **Auth:** enable Email/Password login and create a project user (or use an existing project user). An Appwrite Console login is not automatically a user account inside the project.
4. **Databases:** find the database IDs and the logs/state collection or table IDs. These must match the variables on your active sync function deployment:
   - Logs database: `APPWRITE_LOGS_DATABASE_ID`, falling back to `APPWRITE_DATABASE_ID`.
   - State database: `APPWRITE_STATE_DATABASE_ID`, falling back to the logs database.
   - Logs collection/table: `APPWRITE_LOGS_COLLECTION_ID` (default `telegram_copier_logs`).
   - State collection/table: `APPWRITE_STATE_COLLECTION_ID` (default `telegram_copier_state`).
5. **Database permissions:** grant **Read** on both collections/tables to this specific project user, or a team that contains the user. Collection/table-level Read covers the function-created records; record-level permissions alone would need to be present on every record. Browser users do not need Create/Update/Delete permission. Do not make this data public.
6. **Functions → Telegram Copier User Sync → Settings → Execute access:** add the same project user or team. The function ID is normally `telegram-copier-webhook`. Keep it enabled with an active deployment. The repository's `appwrite.json` has `execute: []`; preserve your intended user/team Execute access if subsequently deploying this configuration.
7. Confirm that scheduled runs are writing to the configured database, with a state record whose `state_key` is `global`, and logs whose `function_name` is `telegram_copier_run_v1`. The existing function needs its database read/write scopes and variables as described in `../APPWRITE_DEPLOY.md`. Merely enabling function execution logging is not the same as database logging.

## Connect in the frontend

Expand **Connect your Appwrite project**, enter the identifiers above, select **Collections** or **TablesDB** to match your deployment, then click **Save & connect**. Leave the state database blank if it is the same as the logs database. Sign in with the project user's email/password.

Public identifiers are stored in browser local storage. Relayroom does not save passwords. Appwrite manages session persistence; if the browser blocks cross-site sessions, use a same-site Appwrite custom API domain for your deployed frontend or adjust browser site permissions. Never enter an Appwrite API key, Telegram API hash, or session string into the frontend.

The connection label, refresh timestamp, and error messages indicate whether each read worked. The dashboard refreshes every 30 seconds while visible, when the tab becomes visible again, and when **Refresh** is clicked. An API/network error marks previously loaded records as potentially stale.

## Data sources and limitations of the existing deployed backend

- **Global scheduled data:** Web SDK `Databases.listDocuments` or `TablesDB.listRows` reads persisted state and snapshots. This works without deploying a new function when the existing database writes are healthy and permissions are set.
- **Latest counters:** read from the terminal run's `state.meta`, only when its timestamp matches that run. Running checkpoints may carry old counters; those are deliberately not displayed. Compacted states can omit counters.
- **Dashboard counter association:** the frontend now falls back to the latest final `state.meta` counters when the state and snapshot log timestamps differ slightly. It still does not invent values; missing backend fields remain `—`.
- **History/chart:** snapshot rows are grouped by `run_ts_ms`. Message IDs are deduplicated per source across chunks/retries. These are **observed message IDs, not copied-message counts**. Overlapping lookback windows can observe the same message on multiple runs. The backend skips empty snapshot parts, so runs with no observed messages may have no history row. Current state can still identify the latest run.
- **Skip reasons, duration, errors, channel names:** the existing deployed function does not persist all of these in its state/log schema. Missing values remain `—`; the frontend cannot reconstruct them from fetch counts. A backend telemetry/schema extension would be needed for a complete historical delivery/skip breakdown. The current `state_saved` checkpoint is also not proof that all sources succeeded.
- **History pagination:** reads up to 2,000 snapshot rows for the selected 24h/7d/30d period and explicitly labels a capped result as partial. A large run may span the cap, so use a shorter window for fuller coverage. Requests use built-in `$createdAt` ordering/filtering and the existing `state_key` lookup.
- **Manual sync:** calls `Functions.createExecution` asynchronously and polls `getExecution` every 2.5 seconds for up to 6 minutes. This avoids holding an HTTP request open for a potentially 300-second function. No automatic submission retries are performed. On timeout or a lost response, inspect **Execution tracker** / Refresh before submitting again. A browser-only button cannot prevent another user or a schedule from starting a concurrent execution.
- **Execution visibility:** `Functions.listExecutions` lists only the current user's executions, not global scheduled history. Asynchronous response bodies and server logs are not generally available to the Web SDK. An execution finishing means the function finished; it does not alone establish that every Telegram message was copied. Consult the refreshed persisted run status and counters.
- **Read-only monitoring:** page load and periodic refresh never trigger a Telegram sync. Only clicking **Run sync now** submits an execution.
- **Pagination:** Source channels are shown six at a time and Run history five at a time. The 24h/7d/30d selector changes the loaded history window; the arrow controls paginate the records already loaded from Appwrite.
- **Source errors:** `completed_with_errors` is emitted by the worker when a source-channel operation fails. The frontend now shows the persisted source ID/error summary when present. After deploying the updated worker, state compaction retains up to eight source errors (with a count when more exist). Older states may only show that an error occurred because the previous worker intentionally removed `source_errors` before saving.

## Verify your live setup

1. Sign in and verify **Connected** plus a recent refresh timestamp.
2. Confirm the latest record timestamp, source cursors and copied/fetched counters against an existing Appwrite database state record. Check 24h/7d/30d and **View recorded details**.
3. Wait for a scheduled run; within the next refresh its persisted data should appear. If the function wrote no final metrics, `—` is expected.
4. Click **Run sync now** only when you want a real Telegram sync. Confirm one execution moves from queued/processing to completed or failed, then verify the persisted state and destination channel.
5. Sign out and confirm all loaded counters/source data clear. Sign back in to verify session restoration.
6. If the latest run says **Completed with source errors**, open the run details and check the source error note. Verify each source numeric ID is accessible to the Telegram session and that the account can read it. If the note says details were not persisted, deploy the updated `appwrite/functions/webhook` worker and inspect the next run.

Troubleshooting: **401** usually means no project session or insufficient access; **403** means permissions; **404** means an ID/API type mismatch; **429** requires waiting. Network/CORS errors require checking the API endpoint and Web platform hostname. A successful empty list can mean either no data or no readable records, so inspect permissions as well as database contents.

## Tests

```bash
npm test
npm run build
npx playwright install chromium
npm run test:browser
```

Browser tests exercise the built app and the real Web SDK with **mocked HTTP responses**: authentication, Collections/TablesDB, pagination, missing metrics, permissions, async execution tracking, session restoration, refresh, and mobile layout. They do not execute the deployed Telegram function. Production connectivity still needs your identifiers and project-user sign-in; it has not been verified by these tests.

3D rendering uses MIT-licensed Three.js, bundled locally and loaded separately so WebGL failure cannot prevent data loading. SDK references: [Functions](https://appwrite.io/docs/references/cloud/client-web/functions), [Auth](https://appwrite.io/docs/products/auth), [Permissions](https://appwrite.io/docs/advanced/platform/permissions).
