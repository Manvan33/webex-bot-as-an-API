"""
Tests for webex_bot_api — unit tests for helper functions and integration tests
for the FastAPI endpoints. All Webex SDK calls are mocked.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from webex_bot_api import (
    _build_chat_response,
    _extract_uuid_from_person_id,
    _is_bot_actor,
    _message_is_after_sent,
    _message_matches_bot,
    _parse_webex_time,
    app,
)

# ─── shared constants ─────────────────────────────────────────────────────────

FAKE_UUID = "12345678-1234-1234-8abc-123456789012"
# base64("ciscospark://us/PEOPLE/12345678-1234-1234-8abc-123456789012")
FAKE_PERSON_ID_B64 = "Y2lzY29zcGFyazovL3VzL1BFT1BMRS8xMjM0NTY3OC0xMjM0LTEyMzQtOGFiYy0xMjM0NTY3ODkwMTI="

BOT_EMAIL = "bot@webex.bot"
BOT_ID = FAKE_UUID
VALID_TOKEN = "a" * 20
VALID_REQUEST = {
    "user_token": VALID_TOKEN,
    "bot_email": BOT_EMAIL,
    "message": "Hello bot",
    "collect_ms": 500,
}
BOT_REPLY = {
    "id": "msg-bot-1",
    "text": "Hello human",
    "markdown": None,
    "created": "2026-05-19T10:00:01.000Z",
    "room_id": "fake-room-id",
    "person_id": BOT_ID,
    "person_email": BOT_EMAIL,
    "activity_id": None,
    "parent_type": None,
    "parent_activity_id": None,
    "verb": "post",
}


# ─── _extract_uuid_from_person_id ─────────────────────────────────────────────


class TestExtractUuid:
    def test_raw_uuid_returned_lowercased(self):
        assert _extract_uuid_from_person_id(FAKE_UUID) == FAKE_UUID.lower()

    def test_base64_encoded_webex_person_id(self):
        result = _extract_uuid_from_person_id(FAKE_PERSON_ID_B64)
        assert result == FAKE_UUID.lower()

    def test_none_returns_none(self):
        assert _extract_uuid_from_person_id(None) is None

    def test_random_string_returns_none(self):
        assert _extract_uuid_from_person_id("not-a-person-id") is None


# ─── _parse_webex_time ────────────────────────────────────────────────────────


class TestParseWebexTime:
    def test_z_suffix_timestamp(self):
        dt = _parse_webex_time("2026-05-19T12:00:00.000Z")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.year == 2026

    def test_aware_datetime_passthrough(self):
        dt_in = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert _parse_webex_time(dt_in) == dt_in

    def test_naive_datetime_gets_utc(self):
        dt_in = datetime(2026, 1, 1)
        result = _parse_webex_time(dt_in)
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_none_returns_none(self):
        assert _parse_webex_time(None) is None

    def test_non_string_non_datetime_returns_none(self):
        assert _parse_webex_time(42) is None  # type: ignore[arg-type]


# ─── _message_is_after_sent ───────────────────────────────────────────────────


class TestMessageIsAfterSent:
    SENT = "2026-05-19T10:00:00.000Z"

    def test_message_after_sent(self):
        msg = {"created": "2026-05-19T10:00:01.000Z"}
        assert _message_is_after_sent(msg, self.SENT) is True

    def test_message_at_same_time(self):
        msg = {"created": "2026-05-19T10:00:00.000Z"}
        assert _message_is_after_sent(msg, self.SENT) is True

    def test_message_before_sent(self):
        msg = {"created": "2026-05-19T09:59:59.000Z"}
        assert _message_is_after_sent(msg, self.SENT) is False

    def test_no_sent_time_always_true(self):
        msg = {"created": "2026-05-19T10:00:01.000Z"}
        assert _message_is_after_sent(msg, None) is True


# ─── _is_bot_actor / _message_matches_bot ─────────────────────────────────────


class TestIsBotActor:
    def test_match_by_exact_id(self):
        actor = {"id": BOT_ID, "entryUUID": "", "emailAddress": ""}
        assert _is_bot_actor(actor, BOT_ID, BOT_EMAIL) is True

    def test_match_by_email(self):
        actor = {"id": "", "entryUUID": "", "emailAddress": BOT_EMAIL}
        assert _is_bot_actor(actor, BOT_ID, BOT_EMAIL) is True

    def test_no_match(self):
        actor = {"id": "other-id", "entryUUID": "", "emailAddress": "other@webex.bot"}
        assert _is_bot_actor(actor, BOT_ID, BOT_EMAIL) is False

    def test_empty_expected_id_returns_false(self):
        actor = {"id": BOT_ID, "entryUUID": "", "emailAddress": BOT_EMAIL}
        assert _is_bot_actor(actor, "", BOT_EMAIL) is False


class TestMessageMatchesBot:
    def test_match_by_person_email(self):
        msg = {"person_id": None, "person_email": BOT_EMAIL}
        assert _message_matches_bot(msg, BOT_ID, BOT_EMAIL) is True

    def test_match_by_person_id(self):
        msg = {"person_id": BOT_ID, "person_email": "other@webex.bot"}
        assert _message_matches_bot(msg, BOT_ID, BOT_EMAIL) is True

    def test_no_match(self):
        msg = {"person_id": "other-id", "person_email": "other@webex.bot"}
        assert _message_matches_bot(msg, BOT_ID, BOT_EMAIL) is False


# ─── _build_chat_response ─────────────────────────────────────────────────────


def _make_event(text="hello", markdown=None, created="2026-05-19T10:00:00.000Z", msg_id="msg1"):
    return {
        "id": msg_id,
        "text": text,
        "markdown": markdown,
        "created": created,
        "activity_id": None,
        "parent_type": None,
        "parent_activity_id": None,
        "verb": "post",
    }


class TestBuildChatResponse:
    def test_single_reply(self):
        resp = _build_chat_response("room1", BOT_EMAIL, [_make_event("Hi")])
        assert resp.reply == "Hi"
        assert resp.replies == ["Hi"]
        assert len(resp.events) == 1

    def test_multiple_replies_canonical_is_last(self):
        events = [_make_event("First", msg_id="m1"), _make_event("Second", msg_id="m2")]
        resp = _build_chat_response("room1", BOT_EMAIL, events)
        assert resp.reply == "Second"
        assert resp.replies == ["First", "Second"]

    def test_markdown_preferred_over_text(self):
        event = _make_event(text="plain", markdown="**bold**")
        resp = _build_chat_response("room1", BOT_EMAIL, [event])
        assert resp.reply == "**bold**"

    def test_no_events_empty_response(self):
        resp = _build_chat_response("room1", BOT_EMAIL, [])
        assert resp.reply == ""
        assert resp.replies == []
        assert resp.events == []

    def test_room_id_and_bot_email_in_response(self):
        resp = _build_chat_response("room-xyz", BOT_EMAIL, [_make_event("Hello")])
        assert resp.room_id == "room-xyz"
        assert resp.bot_email == BOT_EMAIL


# ─── FastAPI endpoint fixtures ────────────────────────────────────────────────


def _make_mock_client():
    """Build a MagicMock WebexApiClient with sensible defaults."""
    mock_client = MagicMock()
    mock_client.my_id = "user-id"
    mock_client.my_email = "user@example.com"

    mock_room = MagicMock()
    mock_room.id = "fake-room-id"
    mock_client.create_room.return_value = mock_room
    mock_client.add_member.return_value = MagicMock()
    mock_client.delete_room.return_value = None

    mock_sent = MagicMock()
    mock_sent.roomId = "fake-room-id"
    mock_sent.created = "2026-05-19T10:00:00.000Z"
    mock_client.send_message.return_value = mock_sent

    return mock_client


@pytest.fixture
def mock_client():
    return _make_mock_client()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Replace asyncio.sleep with an instant no-op for all tests."""

    async def _instant(_delay):
        pass

    monkeypatch.setattr("webex_bot_api.asyncio.sleep", _instant)


@pytest.fixture(autouse=True)
def clear_bot_cache():
    """Ensure bot ID cache is clean for each test."""
    from webex_bot_api import _bot_id_cache
    _bot_id_cache.clear()
    yield
    _bot_id_cache.clear()


@pytest.fixture
async def async_client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


def _patch_client_and_bot(mock_client):
    """Return context managers that patch WebexApiClient construction and bot ID resolution."""
    return (
        patch("webex_bot_api.WebexApiClient", return_value=mock_client),
        patch("webex_bot_api._resolve_bot_id", new=AsyncMock(return_value=BOT_ID)),
    )


# ─── GET /health ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health(async_client):
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


# ─── POST /chat – happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_happy_path(async_client, mock_client):
    p_client, p_bot = _patch_client_and_bot(mock_client)
    with (
        p_client,
        p_bot,
        patch("webex_bot_api._list_recent_room_messages", new=AsyncMock(return_value=[BOT_REPLY])),
    ):
        resp = await async_client.post("/chat", json=VALID_REQUEST)

    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "Hello human"
    assert body["replies"] == ["Hello human"]
    assert body["room_id"] == "fake-room-id"
    assert body["bot_email"] == BOT_EMAIL
    assert len(body["events"]) == 1


# ─── POST /chat – delete_room=True deletes the room ──────────────────────────


@pytest.mark.asyncio
async def test_chat_delete_room_called_when_true(async_client, mock_client):
    payload = {**VALID_REQUEST, "delete_room": True}
    p_client, p_bot = _patch_client_and_bot(mock_client)
    with (
        p_client,
        p_bot,
        patch("webex_bot_api._list_recent_room_messages", new=AsyncMock(return_value=[BOT_REPLY])),
    ):
        resp = await async_client.post("/chat", json=payload)

    assert resp.status_code == 200
    mock_client.delete_room.assert_called_once_with("fake-room-id")


# ─── POST /chat – delete_room=False leaves room intact ───────────────────────


@pytest.mark.asyncio
async def test_chat_delete_room_not_called_when_false(async_client, mock_client):
    payload = {**VALID_REQUEST, "delete_room": False}
    p_client, p_bot = _patch_client_and_bot(mock_client)
    with (
        p_client,
        p_bot,
        patch("webex_bot_api._list_recent_room_messages", new=AsyncMock(return_value=[BOT_REPLY])),
    ):
        resp = await async_client.post("/chat", json=payload)

    assert resp.status_code == 200
    mock_client.delete_room.assert_not_called()


# ─── POST /chat – delete_room still called even when send fails ───────────────


@pytest.mark.asyncio
async def test_chat_delete_room_on_send_failure(async_client, mock_client):
    """Room must be cleaned up even when an error occurs mid-flow."""
    payload = {**VALID_REQUEST, "delete_room": True}
    mock_client.send_message.side_effect = Exception("network error")
    p_client, p_bot = _patch_client_and_bot(mock_client)
    with p_client, p_bot:
        resp = await async_client.post("/chat", json=payload)

    assert resp.status_code == 502
    mock_client.delete_room.assert_called_once_with("fake-room-id")


# ─── POST /chat – bot not found in Webex directory ───────────────────────────


@pytest.mark.asyncio
async def test_chat_bot_not_found(async_client, mock_client):
    with (
        patch("webex_bot_api.WebexApiClient", return_value=mock_client),
        patch("webex_bot_api._resolve_bot_id", new=AsyncMock(return_value=None)),
    ):
        resp = await async_client.post("/chat", json=VALID_REQUEST)

    assert resp.status_code == 404
    assert "Bot email not found" in resp.json()["detail"]


# ─── POST /chat – invalid token ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_invalid_token(async_client):
    bad_client = MagicMock()
    bad_client.my_id = None
    with patch("webex_bot_api.WebexApiClient", return_value=bad_client):
        resp = await async_client.post("/chat", json=VALID_REQUEST)

    assert resp.status_code == 401
    assert "Invalid Webex user token" in resp.json()["detail"]


# ─── POST /chat – room creation failure ──────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_room_creation_failure(async_client, mock_client):
    mock_client.create_room.side_effect = Exception("Webex unavailable")
    p_client, p_bot = _patch_client_and_bot(mock_client)
    with p_client, p_bot:
        resp = await async_client.post("/chat", json=VALID_REQUEST)

    assert resp.status_code == 502
    assert "Failed to create temporary room" in resp.json()["detail"]


# ─── POST /chat – invite bot failure ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_invite_failure(async_client, mock_client):
    mock_client.add_member.side_effect = Exception("membership error")
    p_client, p_bot = _patch_client_and_bot(mock_client)
    with p_client, p_bot:
        resp = await async_client.post("/chat", json=VALID_REQUEST)

    assert resp.status_code == 502
    assert "Failed to invite bot" in resp.json()["detail"]


# ─── POST /chat – send message failure ───────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_send_failure(async_client, mock_client):
    mock_client.send_message.side_effect = Exception("send error")
    p_client, p_bot = _patch_client_and_bot(mock_client)
    with p_client, p_bot:
        resp = await async_client.post("/chat", json=VALID_REQUEST)

    assert resp.status_code == 502
    assert "Failed to send message" in resp.json()["detail"]


# ─── POST /chat – bot returns no messages (empty reply) ──────────────────────


@pytest.mark.asyncio
async def test_chat_bot_silent_returns_empty_reply(async_client, mock_client):
    p_client, p_bot = _patch_client_and_bot(mock_client)
    with (
        p_client,
        p_bot,
        patch("webex_bot_api._list_recent_room_messages", new=AsyncMock(return_value=[])),
    ):
        resp = await async_client.post("/chat", json=VALID_REQUEST)

    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == ""
    assert body["replies"] == []


# ─── POST /chat – only messages from bot are included ────────────────────────


@pytest.mark.asyncio
async def test_chat_filters_non_bot_messages(async_client, mock_client):
    user_msg = {**BOT_REPLY, "person_id": "other-id", "person_email": "other@webex.bot", "text": "from user"}
    p_client, p_bot = _patch_client_and_bot(mock_client)
    with (
        p_client,
        p_bot,
        patch("webex_bot_api._list_recent_room_messages", new=AsyncMock(return_value=[user_msg, BOT_REPLY])),
    ):
        resp = await async_client.post("/chat", json=VALID_REQUEST)

    assert resp.status_code == 200
    assert resp.json()["replies"] == ["Hello human"]


# ─── POST /chat – Pydantic validation errors ─────────────────────────────────


@pytest.mark.asyncio
async def test_chat_validation_token_too_short(async_client):
    payload = {**VALID_REQUEST, "user_token": "short"}
    resp = await async_client.post("/chat", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_validation_empty_message(async_client):
    payload = {**VALID_REQUEST, "message": ""}
    resp = await async_client.post("/chat", json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_chat_validation_collect_ms_too_low(async_client):
    payload = {**VALID_REQUEST, "collect_ms": 50}
    resp = await async_client.post("/chat", json=payload)
    assert resp.status_code == 422
