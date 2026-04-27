import os
import sys
import logging
from pathlib import Path
import asyncio


# Add parent directory to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from WebexWSClient import WebexWSClient

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("UserImplementation")

def main():
    user_token = os.getenv("TEST_USER_WEBEX_TOKEN")
    if not user_token:
        print("Error: TEST_USER_WEBEX_TOKEN environment variable not set.")
        sys.exit(1)

    bot_email = os.getenv("BOT_EMAIL", "board-provisioning@webex.bot")

    print(f"Using bot email: {bot_email}")

    client = WebexWSClient(access_token=user_token, device_name="user-runner")

    if not client.my_id:
        print("Error: Failed to initialize client (invalid token?).")
        sys.exit(1)

    print(f"Sending 'Hello' to {bot_email}...")
    try:
        # Send message
        client.send_message(to_person_email=bot_email, text="Hello")
        print("Message sent.")

        # Listen for reply for 10 seconds
        async def on_message(event):
            data = event.get("data", {})
            activity = data.get("activity", {})
            if data.get("eventType") == "conversation.activity" and activity.get("verb") == "post":
                actor = activity.get("actor", {})
                print(f"Received event from {actor.get('id')}: {activity.get('object', {}).get('displayName', 'message')}")

        client.add_event_listener(on_message)

        # Run loop for 10 seconds
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def run_and_stop():
            client.running = True
            task = asyncio.create_task(client._run_loop())
            print("Listening for replies (10s)...")
            await asyncio.sleep(10)
            print("Stopping...")
            client.running = False
            if client.websocket:
                await client.websocket.close()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        try:
            loop.run_until_complete(run_and_stop())
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
