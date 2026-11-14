# Appwrite Database Indexes for Telegram Copier

## 📋 Documentation Index

- [Problem Overview](#problem-overview)
- [Errors Seen](#errors-seen)
- [Root Cause Analysis](#root-cause-analysis)
- [Required Indexes](#required-indexes)
- [Step-by-Step Fix](#step-by-step-fix)
- [Verification](#verification)
- [Troubleshooting](#troubleshooting)
- [Additional Notes](#additional-notes)

---

## Problem Overview

The Appwrite function execution was failing with `HTTP 408 Request Timeout` errors, specifically:

```
RuntimeError: HTTP 408 Request Timeout: {"message":"Database timed out. Try adjusting your queries or adding an index.","code":408,"type":"database_timeout","version":"2.7.2"}
```

Additionally, there were Telegram forwarding errors:

```
ChatForwardsRestrictedError: You can't forward messages from a protected chat (caused by SendMediaRequest)
```

---

## Errors Seen

### Error 1: Database Timeout (HTTP 408)

```
RuntimeError: HTTP 408 Request Timeout: {"message":"Database timed out. Try adjusting your queries or adding an index.","code":408,"type":"database_timeout","version":"2.7.2"}
Source errors: {"-1002892627236": "ChatForwardsRestrictedError: You can't forward messages from a protected chat (caused by SendMediaRequest)"}
```

### Error 2: Chat Forward Restriction

```
ChatForwardsRestrictedError: You can't forward messages from a protected chat (caused by SendMediaRequest)
```

---

## Root Cause Analysis

### Database Timeout (408)

**Cause**: Appwrite collections `telegram_copier_state` and `telegram_copier_logs` lacked attribute indexes. Without indexes, Appwrite performs full collection scans on every query, which times out on datasets beyond a small size.

**Affected Code Queries**:

1. **`_find_existing_state_record_id`** (line ~665):
   ```python
   queries=[_query_equal("state_key", "global"), _query_limit(1)]
   ```
   - Searches `telegram_copier_state` for `state_key = "global"`
   - Without index: full collection scan → timeout

2. **`_load_recent_run_snapshot_ids`** (line ~781):
   ```python
   queries=[
       _query_order_desc("$createdAt"),
       _query_limit(500),
       _query_equal("function_name", DEFAULT_FUNCTION_NAME),
   ]
   ```
   - Orders `telegram_copier_logs` by `$createdAt` descending
   - Limits to 500 results
   - Filters by `function_name`
   - Without indexes: full collection scan on all three conditions → timeout

### ChatForwardsRestrictedError

**Cause**: Telegram API restrictions on forwarding messages from protected chats (channels with restrictive forward settings).

**Resolution**: The code already has fallback handling (lines 2565-2602 in `main.py`). When this error occurs, it automatically falls back to `_repost_messages_without_forward` which reposts content directly without the "Forwarded from" header. **No code change needed** - this is working as designed.

---

## Required Indexes

To fix the database timeout, add these 3 indexes across 2 collections:

| # | Collection | Attribute | Type | Purpose |
|---|---|---|---|---|
| 1 | `telegram_copier_state` | `state_key` | String | Enable fast lookup of state row by `state_key` |
| 2 | `telegram_copier_logs` | `function_name` | String | Enable fast filtering by function name |
| 3 | `telegram_copier_logs` | `$createdAt` | Timestamp | Enable fast ordering by creation time |

---

## Step-by-Step Fix

### Step 1: Add Index via Appwrite Console

1. Go to [Appwrite Console](https://cloud.appwrite.io) → Your Project → **Database**
2. Select your database
3. Click `telegram_copier_state` collection → **Settings** (gear icon) → **Add attribute index**
   - Attribute: `state_key`
   - Type: String
4. Click `telegram_copier_logs` collection → **Settings** → **Add attribute index**
   - Attribute: `function_name`
   - Type: String
5. Click `telegram_copier_logs` → **Settings** → **Add attribute index**
   - Attribute: `$createdAt`
   - Type: Timestamp

### Step 2: Or Add Index via CLI

```bash
# Index 1: state_key on telegram_copier_state
appwrite databases indexes create \
  --database_id YOUR_DATABASE_ID \
  --collection_id telegram_copier_state \
  --attribute state_key

# Index 2: function_name on telegram_copier_logs
appwrite databases indexes create \
  --database_id YOUR_DATABASE_ID \
  --collection_id telegram_copier_logs \
  --attribute function_name

# Index 3: $createdAt on telegram_copier_logs
appwrite databases indexes create \
  --database_id YOUR_DATABASE_ID \
  --collection_id telegram_copier_logs \
  --attribute $createdAt
```

### Step 3: Wait 1-2 Minutes

- Indexes need time to build the data structure
- Do not test immediately - wait for propagation

### Step 4: Test the Function

1. Go to your Appwrite function: `telegram-copier-webhook`
2. Click "Run" or trigger the function
3. Check execution logs

### Step 5: Verify Fix

**✅ Success Indicators**:
- No `HTTP 408 Request Timeout` in logs
- No `Database timed out` message
- Function completes execution normally
- State and logs are persisted correctly

**❌ Failure Indicators** (if still occurring):
- Same 408 timeout errors
- Full collection scan warnings in Appwrite logs

---

## Verification

After adding indexes and running the function once, check:

1. **Execution Logs**: Should not contain `RuntimeError: HTTP 408 Request Timeout`
2. **Database Tabs**: `telegram_copier_state` and `telegram_copier_logs` should show successful document writes
3. **Function Status**: Returns normal success response

The existing code queries will automatically leverage these indexes - **no code modifications needed**.

---

## Troubleshooting

If 408 timeouts persist after adding indexes:

| # | Check | Action |
|---|---|---|
| 1 | **Index names** | Verify exact attribute names match (case-sensitive): `state_key`, `function_name`, `$createdAt` |
| 2 | **Collection names** | Ensure collections are named exactly: `telegram_copier_state`, `telegram_copier_logs` |
| 3 | **Appwrite plan** | Confirm your plan supports the index types you created |
| 4 | **Propagation delay** | Wait full 2 minutes after creating indexes before testing |
| 5 | **Query limit** | If you have millions of log entries, consider reducing `_query_limit(500)` in the code to a smaller value |
| 6 | **Function timeout** | `APPWRITE_FUNCTION_TIMEOUT` is set to 300s in `appwrite.json` - ensure this is sufficient |
| 7 | **Cache clear** | If using Appwrite self-hosted, may need to restart services |

**Most common issue**: Indexes created but not waited out for full propagation (step 3).

---

## Additional Notes

### Why These Specific Indexes?

The codebase uses these exact query patterns (from `main.py`):

```python
# Query pattern 1 - State lookup
queries=[_query_equal("state_key", "global"), _query_limit(1)]

# Query pattern 2 - Recent logs retrieval  
queries=[
    _query_order_desc("$createdAt"),
    _query_limit(500),
    _query_equal("function_name", DEFAULT_FUNCTION_NAME),
]
```

These queries will automatically use the indexes once created. No query pattern changes are needed.

### Index Types Explained

- **String index**: B-tree index for exact match queries (`equal` method)
- **Timestamp index**: B-tree index for ordered queries (`orderDesc` method) on `$createdAt`

### What If I Have Very Large Datasets?

If you have millions of log entries:

1. The indexes will still help, but query time may vary
2. Consider reducing `_query_limit(500)` to `_query_limit(100)` or lower
3. Add time-based partitioning if using a supported database backend
4. Monitor function execution times and adjust accordingly

### Index Maintenance

- Indexes persist across function executions
- No periodic maintenance needed in Appwrite
- Can be viewed/managed in Appwrite Console → Database → [collection] → Settings
- Dropping and recreating indexes is possible if needed (use with caution)

---

## Quick Checklist

- [ ] Add index: `telegram_copier_state` → `state_key` (String)
- [ ] Add index: `telegram_copier_logs` → `function_name` (String)
- [ ] Add index: `telegram_copier_logs` → `$createdAt` (Timestamp)
- [ ] Wait 1-2 minutes for propagation
- [ ] Run function execution
- [ ] Verify no 408 timeout errors in logs
- [ ] Run function 2-3 more times to confirm stability

**Completed**: Once all checks pass, the database timeout issue is fully resolved!