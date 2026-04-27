import asyncio
import base64
import contextlib
import hashlib
import logging
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from WebexWSClient import WebexWSClient

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
    def __init__(self, session_id: str, client: WebexWSClient, device_name: str) -> None:
        self.session_id = session_id
        self.client = client
        self.device_name = device_name
        self.ws_task: Optional[asyncio.Task] = None
        self.bot_id_by_email: Dict[str, str] = {}
        self.room_targets: Dict[str, tuple[str, str]] = {}
        self.collectors_by_room: Dict[str, list[dict]] = defaultdict(list)
        self.lock = asyncio.Lock()
        self.last_used_at = datetime.now(timezone.utc)

    def touch(self, when: Optional[datetime] = None) -> None:
        self.last_used_at = when or datetime.now(timezone.utc)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return current - self.last_used_at > SESSION_IDLE_TIMEOUT

    async def stop(self) -> None:
        self.client.running = False
        if self.client.websocket:
            with contextlib.suppress(Exception):
                await self.client.websocket.close()

        if self.ws_task:
            self.ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.ws_task

        self.ws_task = None
        self.bot_id_by_email.clear()
        self.room_targets.clear()
        self.collectors_by_room.clear()


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

            client = WebexWSClient(access_token=user_token, device_name=DEFAULT_DEVICE_NAME)
            if not client.my_id:
                raise ValueError("Invalid Webex user token.")

            session = RelaySession(session_id=session_id, client=client, device_name=DEFAULT_DEVICE_NAME)
            session.ws_task = await _start_ws_loop(session)
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


async def _resolve_person_id(client: WebexWSClient, email: str) -> Optional[str]:
    people = await asyncio.to_thread(client.api.people.list, email=email)
    for person in people:
        return person.id
    return None


async def _fetch_message_text(client: WebexWSClient, message_id: str) -> dict:
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


def _activity_uuid_to_message_id(activity_uuid: str) -> str:
    """Convert a raw Webex activity UUID to the global message ID the messages API expects."""
    raw = f"ciscospark://us/MESSAGE/{activity_uuid}"
    return base64.b64encode(raw.encode()).decode().rstrip("=")


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


def _drain_collector_queue(queue: asyncio.Queue) -> list[dict]:
    items: list[dict] = []
    while True:
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    items.sort(key=_message_sort_key)
    return items


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


async def _on_webex_event(session: RelaySession, event: dict) -> None:
    data = event.get("data", {})
    event_type = data.get("eventType") or event.get("eventType") or event.get("type")
    if event_type and event_type != "conversation.activity":
        return

    activity = data.get("activity", {}) or event.get("activity", {})
    verb = str(activity.get("verb") or "").lower()
    parent = activity.get("parent", {})
    parent_type = str(parent.get("type") or "").lower()
    parent_id = parent.get("id")
    activity_id = activity.get("id")
    object_id = activity.get("object", {}).get("id")
    room_id_hint = activity.get("target", {}).get("globalId")

    if not room_id_hint:
        return

    async with session.lock:
        target = session.room_targets.get(room_id_hint)
    if not target:
        return

    expected_bot_id, expected_bot_email = target

    if verb not in {"post", "update", "replace"}:
        return

    actor = activity.get("actor", {})
    actor_id = actor.get("id")
    if not _is_bot_actor(actor, expected_bot_id, expected_bot_email):
        return

    object_id = activity.get("object", {}).get("id")
    activity_id = activity.get("id")
    if object_id:
        message_id = object_id
    elif activity_id:
        message_id = _activity_uuid_to_message_id(activity_id)
    else:
        return

    try:
        message = await _fetch_message_text(session.client, message_id)
    except Exception as exc:
        logger.warning("Could not fetch message %s: %s", message_id, exc)
        return

    parent = activity.get("parent", {})
    message["activity_id"] = activity_id
    message["parent_activity_id"] = parent.get("id")
    message["parent_type"] = parent.get("type")
    message["verb"] = verb
    if not message.get("created"):
        message["created"] = activity.get("published")

    room_id = message.get("room_id")
    text = (message.get("text") or "").strip()
    markdown = (message.get("markdown") or "").strip()
    if not room_id or not (text or markdown):
        return

    async with session.lock:
        collectors = list(session.collectors_by_room.get(room_id, []))

    for collector in collectors:
        if collector.get("bot_id") != expected_bot_id:
            continue

        sent_created = collector.get("sent_created")
        if sent_created and not _message_is_after_sent(message, sent_created):
            continue

        queue: asyncio.Queue = collector["queue"]
        queue.put_nowait(message)


async def _start_ws_loop(session: RelaySession) -> asyncio.Task:
    async def _listener(event: dict) -> None:
        await _on_webex_event(session, event)

    session.client.add_event_listener(_listener)
    session.client.running = True
    return asyncio.create_task(session.client._run_loop(), name=f"webex-ws-loop-{session.session_id[:8]}")


app = FastAPI(title="Webex Bot Relay API", version="1.0.0")


@app.on_event("startup")
async def startup_event() -> None:
    state.sessions.cleanup_task = asyncio.create_task(
        state.sessions.run_cleanup_loop(),
        name="relay-session-cleanup",
    )


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await state.shutdown()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "active_sessions": state.sessions.active_count(),
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    trace_id = str(uuid.uuid4())[:8]

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
        logger.exception("chat[%s] send_message failed", trace_id)
        raise HTTPException(status_code=502, detail=f"Failed to send message to bot: {exc}") from exc

    room_id = getattr(sent, "roomId", None)
    sent_created = getattr(sent, "created", None)
    if not room_id:
        raise HTTPException(status_code=500, detail="Webex did not return a roomId for the sent message.")

    async with session.lock:
        session.room_targets[room_id] = (bot_id, bot_email)

    collector_queue: asyncio.Queue = asyncio.Queue()
    collector = {
        "queue": collector_queue,
        "bot_id": bot_id,
        "sent_created": sent_created,
    }

    async with session.lock:
        session.collectors_by_room[room_id].append(collector)
        session.touch()

    await asyncio.sleep(request.collect_ms / 1000.0)

    async with session.lock:
        collectors = session.collectors_by_room.get(room_id, [])
        with contextlib.suppress(ValueError):
            collectors.remove(collector)
        if not collectors:
            session.collectors_by_room.pop(room_id, None)
        session.touch()

    events_payload = _drain_collector_queue(collector_queue)
    logger.info(
        "chat[%s] collected_events=%s session=%s room_id=%s",
        trace_id,
        len(events_payload),
        session.session_id[:8],
        room_id,
    )
    return _build_chat_response(room_id, bot_email, events_payload)
