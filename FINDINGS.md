# Webex Bot API — Engineering Findings

## 1. Webex actor identity uses two different ID formats

**Symptom**: Bot post events were classified as non-bot and skipped.

**Root cause**: The Webex People API returns person IDs as base64-encoded global URIs
(`ciscospark://us/PEOPLE/<uuid>`), but the WebSocket event payload uses the raw UUID as `actor.id`
and `actor.entryUUID`.  Strict equality between the stored bot ID and the event actor ID always
failed.

**Fix**: Added `_is_bot_actor()` which normalises both sides before comparing:
- Decodes Webex person URIs to extract the inner UUID.
- Compares against `actor.id`, `actor.entryUUID`, and `actor.emailAddress`.

---

## 2. Bot post events do not always include `activity.object.id`

**Symptom**: Valid bot replies logged `Skipping post with no object id` and were dropped.

**Root cause**: Webex WebSocket events for bot replies sometimes omit `activity.object.id`.
The `activity.id` field (the activity's own UUID) is always present and refers to the same message.

However, `activity.id` is a raw UUID, while the Webex Messages REST API (`messages.get`) requires
a base64-encoded global message ID in the format `ciscospark://us/MESSAGE/<uuid>`.

**Fix**: Added `_activity_uuid_to_message_id()` which encodes a raw activity UUID into the expected
global ID format.  The event handler now falls back to this encoded ID when `object.id` is absent.

---

## 3. The `messages.list` pager materialises all pages by default

**Symptom**: Polling fallback fetched 400+ messages and blocked the event loop for ~30 seconds.

**Root cause**: `webexteamssdk` list generators lazily fetch pages on iteration.  Wrapping them in
`list()` without a cap consumes all pages — potentially hundreds of API calls.

**Fix**: Replaced `list(...)` with `list(itertools.islice(..., limit))` so the pager stops after
a fixed number of items.

---

## 4. `logging.basicConfig` silently no-ops on repeated calls

**Symptom**: Setting `level=logging.DEBUG` in `webex_bot_api.py` had no effect since
`WebexWSClient.py` calls `basicConfig(level=logging.INFO)` first during import.
`basicConfig` is a no-op once any handler is already attached to the root logger.

**Fix**: Add `force=True` to the `basicConfig` call, or pass `--log-level debug` to uvicorn to
override the root level at startup.

---

## 5. `asyncio.wait_for` raises `TimeoutError`, not `asyncio.TimeoutError` on Python 3.11+

**Symptom**: In Python 3.13 (current runtime), the waiter timeout surfaced as an unhandled
exception crashing the request with HTTP 500.

**Root cause**: Python 3.11 unified `asyncio.TimeoutError` with the built-in `TimeoutError`.
A bare `except asyncio.TimeoutError` still catches it, but the traceback shows `TimeoutError`
which can cause confusion.

**Fix**: Already handled correctly — the `except asyncio.TimeoutError` clause works on all
supported versions.  No code change needed, but worth noting for future debugging.
