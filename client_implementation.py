import logging
import os
import sys
import time
from pathlib import Path


sys.path.append(str(Path(__file__).resolve().parent.parent))

from webex_api_client import WebexApiClient


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("UserImplementation")


def main():
    user_token = os.getenv("TEST_USER_WEBEX_TOKEN")
    if not user_token:
        print("Error: TEST_USER_WEBEX_TOKEN environment variable not set.")
        sys.exit(1)

    bot_email = os.getenv("BOT_EMAIL", "board-provisioning@webex.bot")

    print(f"Using bot email: {bot_email}")

    client = WebexApiClient(access_token=user_token, device_name="user-runner")
    if not client.my_id:
        print("Error: Failed to initialize client (invalid token?).")
        sys.exit(1)

    print(f"Sending 'Hello' to {bot_email}...")
    try:
        sent = client.send_message(to_person_email=bot_email, text="Hello")
        print("Message sent.")

        room_id = getattr(sent, "roomId", None)
        sent_created = getattr(sent, "created", None)
        if not room_id:
            print("Error: roomId missing from sent message.")
            sys.exit(1)

        print("Waiting 10 seconds before fetching room messages...")
        time.sleep(10)

        messages = list(client.api.messages.list(roomId=room_id, max=20))
        print(f"Fetched {len(messages)} recent messages.")

        for message in reversed(messages):
            if getattr(message, "personEmail", "") != bot_email:
                continue

            created = getattr(message, "created", None)
            if sent_created and created and created < sent_created:
                continue

            text = getattr(message, "markdown", None) or getattr(message, "text", None) or ""
            if text:
                print(f"Bot reply: {text}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
