import getpass
import json
from typing import Optional

import requests


MITM_PROXY = None
# MITM_PROXY = "http://127.0.0.1:8111"


def _build_http_client() -> requests.Session:
    session = requests.Session()
    # Force proxy usage even for localhost targets.
    session.trust_env = False
    if MITM_PROXY is not None:
        session.proxies.update({
            "http": MITM_PROXY,
            "https": MITM_PROXY,
        })
    return session


def send_message(
    base_url: str,
    user_token: str,
    bot_email: str,
    message: str,
    collect_ms: int,
    http_client: requests.Session,
) -> Optional[dict]:
    payload = {
        "user_token": user_token,
        "bot_email": bot_email,
        "message": message,
        "collect_ms": collect_ms,
    }

    try:
        response = http_client.post(
            f"{base_url}/chat",
            json=payload,
            timeout=(collect_ms / 1000.0) + 45,
        )
    except requests.exceptions.ReadTimeout:
        print(
            "Failed to call /chat: client-side read timeout. "
            "The API may still be processing; check uvicorn logs for trace details."
        )
        return None
    except requests.RequestException as exc:
        print(f"Failed to call /chat: {exc}")
        return None

    if response.ok:
        data = response.json()
        return data

    _print_error("/chat", response)
    return None


def _print_error(endpoint: str, response: requests.Response) -> None:
    detail = None
    try:
        body = response.json()
        detail = body.get("detail")
    except ValueError:
        detail = response.text

    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail)

    print(f"{endpoint} failed [{response.status_code}]: {detail}")


def _extract_replies(payload: object) -> list[str]:
    if isinstance(payload, str):
        text = payload.strip()
        return [text] if text else []

    if isinstance(payload, list):
        replies: list[str] = []
        for item in payload:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    replies.append(text)
        return replies

    if not isinstance(payload, dict):
        return []

    replies: list[str] = []

    raw_replies = payload.get("replies")
    if isinstance(raw_replies, list):
        for item in raw_replies:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    replies.append(text)

    if not replies:
        reply = payload.get("reply")
        if isinstance(reply, str):
            text = reply.strip()
            if text:
                replies.append(text)

    return replies


def main() -> None:
    print("Webex Bot Relay Interactive Client")
    print(f"Proxy: {MITM_PROXY}")
    base_url = input("API URL [http://10.228.225.55:8000]: ").strip() or "http://10.228.225.55:8000"
    user_token = getpass.getpass("Personal Webex token: ").strip()
    if not user_token:
        print("A Webex token is required. Exiting.")
        return

    collect_seconds_raw = input("Collect window in seconds [4]: ").strip() or "4"
    http_client = _build_http_client()

    try:
        collect_seconds = max(1, int(collect_seconds_raw))
    except ValueError:
        print("Invalid collect window. Using default of 4 seconds.")
        collect_seconds = 4

    collect_ms = collect_seconds * 1000

    bot_email = input("Bot email [enterprise-chat-ai@webex.bot]: ").strip() or "enterprise-chat-ai@webex.bot"

    print("\nChat ready. Type a message and press Enter.")
    print("Commands: /exit, /quit, /bot <email>")

    while True:
        text = input("\nYou: ").strip()
        if not text:
            continue

        if text.lower() in {"/exit", "/quit"}:
            print("Bye")
            break

        if text.lower().startswith("/bot "):
            new_email = text[5:].strip()
            if new_email:
                bot_email = new_email
                print(f"Bot email set to: {bot_email}")
            else:
                print("Usage: /bot my-bot@webex.bot")
            continue

        reply = send_message(base_url, user_token, bot_email, text, collect_ms, http_client)
        if reply is not None:
            replies = _extract_replies(reply)

            if not replies:
                print("Bot: [empty reply]")
                continue

            print(f"Bot: {replies[0]}")
            for index, extra in enumerate(replies[1:], start=2):
                print(f"Bot ({index}): {extra}")


if __name__ == "__main__":
    main()
