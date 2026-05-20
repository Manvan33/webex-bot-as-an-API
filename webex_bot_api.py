import asyncio
import base64
import contextlib
import itertools
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from webex_api_client import WebexApiClient

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("WebexBotAPI")


class ChatRequest(BaseModel):
    user_token: str = Field(min_length=10)
    bot_email: str = Field(min_length=3)
    message: str = Field(min_length=1, max_length=7439)
    collect_ms: int = Field(default=4000, ge=100, le=120000)
    delete_room: bool = Field(default=False)


class ChatEvent(BaseModel):
    message_id: str
    activity_id: Optional[str] = None
    verb: str
    parent_type: Optional[str] = None
    parent_activity_id: Optional[str] = None
    created: Optional[str] = None
    text: str
    markdown: Optional[str] = None


class ChatResponse(BaseModel):
    reply: str
    replies: list[str] = Field(default_factory=list)
    events: list[ChatEvent] = Field(default_factory=list)
    room_id: str
    bot_email: str


# Module-level bot ID cache (bot_email → person_id). Global across all requests.
_bot_id_cache: Dict[str, str] = {}
_bot_id_lock = asyncio.Lock()


UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


async def _resolve_bot_id(client: WebexApiClient, bot_email: str) -> Optional[str]:
    """Resolve bot email to person ID, using module-level cache."""
    async with _bot_id_lock:
        cached = _bot_id_cache.get(bot_email)
    if cached:
        return cached

    people = await asyncio.to_thread(client.api.people.list, email=bot_email)
    for person in people:
        async with _bot_id_lock:
            _bot_id_cache[bot_email] = person.id
        return person.id
    return None


def _extract_uuid_from_person_id(value: Optional[str]) -> Optional[str]:
    if not value:
        return None

    candidate = value.strip()
    if UUID_RE.match(candidate):
        return candidate.lower()

    try:
        padded = candidate + "=" * (-len(candidate) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
        if "/PEOPLE/" in decoded:
            tail = decoded.rsplit("/PEOPLE/", 1)[-1].strip()
            if UUID_RE.match(tail):
                return tail.lower()
    except Exception:
        pass

    return None


def _is_bot_actor(actor: dict, expected_id: str, expected_email: str) -> bool:
    if not expected_id:
        return False

    actor_id = str(actor.get("id") or "").strip()
    actor_entry_uuid = str(actor.get("entryUUID") or "").strip().lower()
    actor_email = str(actor.get("emailAddress") or "").strip().lower()

    expected_id = expected_id.strip()
    expected_uuid = _extract_uuid_from_person_id(expected_id)
    expected_email = expected_email.strip().lower()

    if actor_id == expected_id:
        return True
    if expected_uuid and actor_id.lower() == expected_uuid:
        return True
    if expected_uuid and actor_entry_uuid == expected_uuid:
        return True
    if expected_email and actor_email == expected_email:
        return True

    return False


def _parse_webex_time(value: Optional[object]) -> Optional[datetime]:
    if not value:
        return None

    if isinstance(value, datetime):
        # Normalize naive datetimes to UTC for safe comparisons.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    if not isinstance(value, str):
        return None

    try:
        # Webex timestamps are ISO-8601 and typically end with "Z".
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _message_is_after_sent(message: dict, sent_created: Optional[object]) -> bool:
    if not sent_created:
        return True

    sent_dt = _parse_webex_time(sent_created)
    msg_dt = _parse_webex_time(message.get("created"))
    if not sent_dt or not msg_dt:
        return True

    return msg_dt >= sent_dt


def _message_sort_key(message: dict) -> datetime:
    parsed = _parse_webex_time(message.get("created"))
    if parsed:
        return parsed
    return datetime.min.replace(tzinfo=timezone.utc)


def _message_matches_bot(message: dict, expected_bot_id: str, expected_bot_email: str) -> bool:
    actor = {
        "id": message.get("person_id"),
        "entryUUID": _extract_uuid_from_person_id(message.get("person_id")),
        "emailAddress": message.get("person_email"),
    }
    return _is_bot_actor(actor, expected_bot_id, expected_bot_email)


async def _list_recent_room_messages(client: WebexApiClient, room_id: str, limit: int = 50) -> list[dict]:
    def _fetch_messages() -> list[dict]:
        messages_iter = client.api.messages.list(roomId=room_id)
        payload: list[dict] = []
        for message in itertools.islice(messages_iter, limit):
            payload.append(
                {
                    "id": getattr(message, "id", None),
                    "text": getattr(message, "text", None) or "",
                    "markdown": getattr(message, "markdown", None) or None,
                    "created": getattr(message, "created", None),
                    "room_id": getattr(message, "roomId", None),
                    "person_id": getattr(message, "personId", None),
                    "person_email": getattr(message, "personEmail", None),
                    "activity_id": None,
                    "parent_type": None,
                    "parent_activity_id": None,
                    "verb": "post",
                }
            )
        return payload

    messages = await asyncio.to_thread(_fetch_messages)
    messages.sort(key=_message_sort_key)
    return messages


def _build_chat_response(room_id: str, bot_email: str, events_payload: list[dict]) -> ChatResponse:
    texts = [(event.get("markdown") or event.get("text") or "").strip() for event in events_payload]
    texts = [text for text in texts if text]

    canonical_reply = texts[-1] if texts else ""

    events: list[ChatEvent] = []
    for event in events_payload:
        events.append(
            ChatEvent(
                message_id=str(event.get("id") or ""),
                activity_id=event.get("activity_id"),
                verb=str(event.get("verb") or ""),
                parent_type=event.get("parent_type"),
                parent_activity_id=event.get("parent_activity_id"),
                created=(str(event.get("created")) if event.get("created") is not None else None),
                text=str(event.get("text") or ""),
                markdown=event.get("markdown") or None,
            )
        )

    return ChatResponse(
        reply=canonical_reply,
        replies=texts,
        events=events,
        room_id=room_id,
        bot_email=bot_email,
    )


app = FastAPI(title="Webex Bot Relay API", version="1.0.0")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    bot_email = request.bot_email.strip().lower()
    if not bot_email:
        raise HTTPException(status_code=400, detail="bot_email is required.")

    # Create a per-request Webex client
    client = await asyncio.to_thread(
        WebexApiClient, access_token=request.user_token, device_name="api-relay-client"
    )
    if not client.my_id:
        raise HTTPException(status_code=401, detail="Invalid Webex user token.")

    # Resolve bot person ID (cached globally)
    bot_id = await _resolve_bot_id(client, bot_email)
    if not bot_id:
        raise HTTPException(status_code=404, detail="Bot email not found in Webex people directory.")

    # Create a temporary room for this interaction
    short_id = str(uuid.uuid4())[:8]
    bot_name = bot_email.split("@")[0]  # extract local part of email
    room_title = f"relay-{bot_name}-{short_id}"
    try:
        room = await asyncio.to_thread(client.create_room, room_title)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to create temporary room: {exc}") from exc

    room_id = getattr(room, "id", None)
    if not room_id:
        raise HTTPException(status_code=500, detail="Webex did not return a room ID for the created room.")

    try:
        # Invite the bot to the room
        try:
            await asyncio.to_thread(client.add_member, room_id, person_email=bot_email)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to invite bot to room: {exc}") from exc

        # Brief wait for bot membership propagation
        await asyncio.sleep(2)

        # Send the query with @mention so bot is notified
        mention_text = f"<@personEmail:{bot_email}> {request.message}"
        try:
            sent = await asyncio.to_thread(
                client.send_message,
                room_id=room_id,
                text=request.message,
                markdown=mention_text,
            )
        except Exception as exc:
            logger.exception("send_message failed")
            raise HTTPException(status_code=502, detail=f"Failed to send message to bot: {exc}") from exc

        sent_created = getattr(sent, "created", None)

        # Wait for the bot to reply
        await asyncio.sleep(request.collect_ms / 1000.0)

        # Fetch all messages from the temporary room
        try:
            room_messages = await _list_recent_room_messages(client, room_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to fetch room messages: {exc}") from exc

        # Filter to bot replies after our sent message
        events_payload = [
            message
            for message in room_messages
            if _message_matches_bot(message, bot_id, bot_email)
            and _message_is_after_sent(message, sent_created)
            and ((message.get("text") or "").strip() or (message.get("markdown") or "").strip())
        ]
        events_payload.sort(key=_message_sort_key)

        return _build_chat_response(room_id, bot_email, events_payload)

    finally:
        # Cleanup: delete room if requested
        if request.delete_room:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(client.delete_room, room_id)
