import asyncio
import json
import logging
import uuid
import signal
import requests
from webexteamssdk import WebexTeamsAPI, ApiError
import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedError

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("WebexWSClient")

class WebexWSClient:
    def __init__(self, access_token, device_name="python-ws-client"):
        self.access_token = access_token
        self.device_name = device_name
        self.api = WebexTeamsAPI(access_token=access_token)

        try:
            me = self.api.people.me()
            self.my_id = me.id
            self.my_email = me.emails[0] if me.emails else ""
            logger.info(f"Initialized WebexWSClient for {self.my_email}")
        except Exception as e:
            logger.error(f"Failed to get own details: {e}")
            self.my_id = None
            self.my_email = None

        self.device_info = None
        self.websocket = None
        self.running = False
        self.loop = None
        self.handlers = []

    def _get_device_info(self):
        wdm_url = "https://wdm-a.wbx2.com/wdm/api/v1/devices"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }

        device_data = {
            "deviceName": self.device_name,
            "deviceType": "DESKTOP",
            "localizedModel": "python",
            "model": "python",
            "name": self.device_name,
            "systemName": self.device_name,
            "systemVersion": "1.0"
        }

        try:
            response = requests.get(wdm_url, headers=headers)
            if response.status_code == 200:
                devices = response.json().get("devices", [])
                for device in devices:
                    if device.get("name") == self.device_name:
                        logger.info(f"Using existing device: {device.get('url')}")
                        return device

            logger.info("No existing device found, creating a new one.")
            create_response = requests.post(wdm_url, headers=headers, json=device_data)
            if create_response.status_code == 200:
                device = create_response.json()
                logger.info(f"Created new device: {device.get('url')}")
                return device
            else:
                logger.error(f"Failed to create device: {create_response.status_code} - {create_response.text}")
                return None
        except Exception as e:
            logger.error(f"Error getting device info: {e}")
            return None

    async def _connect_websocket(self):
        if not self.device_info:
            self.device_info = self._get_device_info()
            if not self.device_info:
                raise Exception("Failed to get device info")

        ws_url = self.device_info.get("webSocketUrl")
        if not ws_url:
            raise Exception("No webSocketUrl in device info")

        logger.info(f"Connecting to WebSocket: {ws_url[:50]}...")
        self.websocket = await websockets.connect(
            ws_url,
            ping_interval=30,
            ping_timeout=10,
        )

        auth_message = {
            "id": str(uuid.uuid4()),
            "type": "authorization",
            "data": {
                "token": f"Bearer {self.access_token}"
            }
        }
        await self.websocket.send(json.dumps(auth_message))
        logger.info("WebSocket connected and authorized")

    async def _process_websocket_message(self, message):
        try:
            msg = json.loads(message)
            event_type = msg.get("data", {}).get("eventType")

            # Dispatch to handlers
            for handler_type, callback in self.handlers:
                if handler_type is None or handler_type == event_type:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(msg)
                    else:
                        callback(msg)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse WebSocket message: {e}")
        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def _run_loop(self):
        reconnect_delay = 5
        max_reconnect_delay = 300

        while self.running:
            try:
                await self._connect_websocket()
                reconnect_delay = 5

                logger.info("Listening for Webex events...")

                async for message in self.websocket:
                    if not self.running:
                        break
                    await self._process_websocket_message(message)

            except ConnectionClosedError as e:
                logger.warning(f"WebSocket connection closed error: {e}")
            except ConnectionClosed as e:
                logger.warning(f"WebSocket connection closed: {e}")
            except Exception as e:
                logger.error(f"WebSocket error: {e}")

            if self.running:
                logger.info(f"Reconnecting in {reconnect_delay} seconds...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    def add_event_listener(self, callback, event_type=None):
        """
        Register a callback for events.
        :param callback: Function to call when event occurs.
        :param event_type: Filter by eventType (e.g. 'conversation.activity'). If None, all events are sent.
        """
        self.handlers.append((event_type, callback))

    def send_message(self, room_id=None, text=None, to_person_email=None, **kwargs):
        """
        Send a message using WebexTeamsAPI.
        """
        try:
            return self.api.messages.create(roomId=room_id, text=text, toPersonEmail=to_person_email, **kwargs)
        except ApiError as e:
            logger.error(f"Failed to send message: {e}")
            raise

    def run(self):
        self.running = True
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        def signal_handler(sig, frame):
            logger.info(f"\nReceived signal {sig}, shutting down...")
            self.running = False
            if self.websocket:
                asyncio.run_coroutine_threadsafe(self.websocket.close(), self.loop)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        try:
            self.loop.run_until_complete(self._run_loop())
        except KeyboardInterrupt:
            pass
        finally:
            self.loop.close()
            logger.info("Client stopped")
