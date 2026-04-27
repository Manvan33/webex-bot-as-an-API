import asyncio
import base64
import contextlib
import hashlib
import itertools
import logging
import re
from datetime import datetime, timedelta, timezone
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


SESSION_IDLE_TIMEOUT = timedelta(minutes=5)
CLEANUP_INTERVAL_SECONDS = 30
DEFAULT_DEVICE_NAME = "api-relay-client"


class RelaySession:
    def __init__(self, session_id: str, client: WebexApiClient, device_name: str) -> None:
        self.session_id = session_id
        self.client = client
        self.device_name = device_name
        self.bot_id_by_email: Dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.last_used_at = datetime.now(timezone.utc)

    def touch(self, when: Optional[datetime] = None) -> None:
        self.last_used_at = when or datetime.now(timezone.utc)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return current - self.last_used_at > SESSION_IDLE_TIMEOUT

    async def stop(self) -> None:
        self.bot_id_by_email.clear()


class SessionManager:
    def __init__(self) -> None:
        self.sessions: Dict[str, RelaySession] = {}
        self.lock = asyncio.Lock()
        self.cleanup_task: Optional[asyncio.Task] = None

    async def get_or_create(self, user_token: str) -> RelaySession:
        session_id = _token_hash(user_token)
        expired_session: Optional[RelaySession] = None
        async with self.lock:
            existing = self.sessions.get(session_id)
            if existing and existing.is_expired():
                expired_session = self.sessions.pop(session_id)
                existing = None

            if existing:
                existing.touch()
                return existing

        if expired_session:
            await expired_session.stop()

        async with self.lock:
            existing = self.sessions.get(session_id)
            if existing:
                existing.touch()
                return existing

            client = WebexApiClient(access_token=user_token, device_name=DEFAULT_DEVICE_NAME)
            if not client.my_id:
                raise ValueError("Invalid Webex user token.")

            session = RelaySession(session_id=session_id, client=client, device_name=DEFAULT_DEVICE_NAME)
            self.sessions[session_id] = session

        return session

    async def expire_idle_sessions(self) -> None:
        now = datetime.now(timezone.utc)
        expired_sessions: list[RelaySession] = []
        async with self.lock:
            for session_id, session in list(self.sessions.items()):
                if session.is_expired(now):
                    expired_sessions.append(self.sessions.pop(session_id))

        for session in expired_sessions:
            await session.stop()

    async def run_cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                await self.expire_idle_sessions()
        except asyncio.CancelledError:
            raise

    async def shutdown(self) -> None:
        if self.cleanup_task:
            self.cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.cleanup_task
            self.cleanup_task = None

        async with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()

        for session in sessions:
            await session.stop()

    def active_count(self) -> int:
        return len(self.sessions)


class AppState:
    def __init__(self) -> None:
        self.sessions = SessionManager()

    async def shutdown(self) -> None:
        await self.sessions.shutdown()


state = AppState()


UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


async def _resolve_person_id(client: WebexApiClient, email: str) -> Optional[str]:
    people = await asyncio.to_thread(client.api.people.list, email=email)
    for person in people:
        return person.id
    return None


async def _fetch_message_text(client: WebexApiClient, message_id: str) -> dict:
    message = await asyncio.to_thread(client.api.messages.get, message_id)
    return {
        "id": message.id,
        "text": getattr(message, "text", None) or "",
        "markdown": getattr(message, "markdown", None) or None,
        "created": getattr(message, "created", None),
        "room_id": getattr(message, "roomId", None),
        "person_id": getattr(message, "personId", None),
        "person_email": getattr(message, "personEmail", None),
    }


def _token_hash(user_token: str) -> str:
    return hashlib.sha256(user_token.encode("utf-8")).hexdigest()


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


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    state.sessions.cleanup_task = asyncio.create_task(
        state.sessions.run_cleanup_loop(),
        name="relay-session-cleanup",
    )
    yield
    await state.shutdown()


app = FastAPI(title="Webex Bot Relay API", version="1.0.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "active_sessions": state.sessions.active_count(),
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    try:
        session = await state.sessions.get_or_create(request.user_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    bot_email = request.bot_email.strip().lower()
    if not bot_email:
        raise HTTPException(status_code=400, detail="bot_email is required.")

    async with session.lock:
        bot_id = session.bot_id_by_email.get(bot_email)

    if not bot_id:
        bot_id = await _resolve_person_id(session.client, bot_email)
        if not bot_id:
            raise HTTPException(status_code=404, detail="Bot email not found in Webex people directory.")
        async with session.lock:
            session.bot_id_by_email[bot_email] = bot_id

    try:
        sent = await asyncio.to_thread(
            session.client.send_message,
            to_person_email=bot_email,
            text=request.message,
        )
    except Exception as exc:
        logger.exception("send_message failed")
        raise HTTPException(status_code=502, detail=f"Failed to send message to bot: {exc}") from exc

    room_id = getattr(sent, "roomId", None)
    sent_created = getattr(sent, "created", None)
    if not room_id:
        raise HTTPException(status_code=500, detail="Webex did not return a roomId for the sent message.")

    async with session.lock:
        session.touch()

    await asyncio.sleep(request.collect_ms / 1000.0)

    try:
        room_messages = await _list_recent_room_messages(session.client, room_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch room messages: {exc}") from exc

    events_payload = [
        message
        for message in room_messages
        if _message_matches_bot(message, bot_id, bot_email)
        and _message_is_after_sent(message, sent_created)
        and ((message.get("text") or "").strip() or (message.get("markdown") or "").strip())
    ]
    events_payload.sort(key=_message_sort_key)

    async with session.lock:
        session.touch()
    return _build_chat_response(room_id, bot_email, events_payload)
