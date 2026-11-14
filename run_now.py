"""Trigger one manual copy run asynchronously and report its result.

The Appwrite console runs functions synchronously, and synchronous executions
are hard-capped at 30 seconds. A copy run needs up to RUN_TIME_BUDGET_SECONDS
(240 s), so the console always answers `Timed out waiting for execution` while
the run itself keeps going. This script creates an asynchronous execution
instead and polls it until it finishes, which is the same thing the hourly
schedule and the Relayroom dashboard do.

Usage (from telegram_copier/):

    python run_now.py

Environment (loaded from .env when python-dotenv is installed):

    APPWRITE_ENDPOINT     default https://cloud.appwrite.io/v1
    APPWRITE_PROJECT_ID   required
    APPWRITE_API_KEY      required, needs functions.write/executions.write
    APPWRITE_FUNCTION_ID  default telegram-copier-webhook
    RUN_NOW_TIMEOUT       default 420 seconds of polling
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional for this helper
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()


def _required(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise SystemExit(f"{name} is not set. Add it to the environment or .env.")
    return value


def _request(method: str, url: str, headers: dict[str, str], payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Appwrite {method} {url} failed: {exc.code} {detail}") from exc


def main() -> int:
    endpoint = (os.getenv("APPWRITE_ENDPOINT") or "https://cloud.appwrite.io/v1").rstrip("/")
    project_id = _required("APPWRITE_PROJECT_ID")
    api_key = _required("APPWRITE_API_KEY")
    function_id = (os.getenv("APPWRITE_FUNCTION_ID") or "telegram-copier-webhook").strip()
    poll_seconds = int(os.getenv("RUN_NOW_TIMEOUT") or 420)

    headers = {
        "X-Appwrite-Project": project_id,
        "X-Appwrite-Key": api_key,
        "Content-Type": "application/json",
    }
    base = f"{endpoint}/functions/{function_id}/executions"

    execution = _request(
        "POST",
        base,
        headers,
        {"async": True, "method": "POST", "path": "/", "body": json.dumps({"source": "run_now"})},
    )
    execution_id = execution.get("$id")
    print(f"created execution {execution_id} (status={execution.get('status')})")
    print("polling; a run stops at RUN_TIME_BUDGET_SECONDS and resumes next run")

    deadline = time.monotonic() + poll_seconds
    status = str(execution.get("status") or "waiting")
    while status in {"waiting", "processing", "scheduled"} and time.monotonic() < deadline:
        time.sleep(5)
        execution = _request("GET", f"{base}/{execution_id}", headers)
        status = str(execution.get("status") or "")
        print(f"  status={status}", flush=True)

    print(f"\nstatus: {status}")
    for key in ("duration", "trigger", "createdAt"):
        if execution.get(key) is not None:
            print(f"{key}: {execution[key]}")

    body = execution.get("responseBody") or ""
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            print(f"response: {body[:2000]}")
        else:
            interesting = {
                key: parsed.get(key)
                for key in (
                    "build_version",
                    "status",
                    "copied",
                    "fetched",
                    "skipped",
                    "partial",
                    "sources_with_new_messages",
                    "sources",
                    "halted",
                    "flood_wait_seconds",
                    "chain_depth",
                    "continued",
                    "next_execution_id",
                    "auto_continue_note",
                    "state_saved",
                )
                if key in parsed
            }
            print("response: " + json.dumps(interesting, indent=2))
    else:
        print("response: (body is empty once an execution has finished; read the run record in the DB)")

    if "continued=1" in body or '"continued": 1' in body:
        print("follow-up execution queued: the backlog keeps draining without new triggers")

    if status not in {"completed", "failed"}:
        print(f"\nstill running after {poll_seconds}s; check the execution in the Appwrite console.")
        return 1
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
