import logging

from webexteamssdk import ApiError, WebexTeamsAPI


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("WebexApiClient")


class WebexApiClient:
    def __init__(self, access_token, device_name="python-ws-client"):
        self.access_token = access_token
        self.device_name = device_name
        self.api = WebexTeamsAPI(access_token=access_token)

        try:
            me = self.api.people.me()
            self.my_id = me.id
            self.my_email = me.emails[0] if me.emails else ""
            logger.info(f"Initialized WebexApiClient for {self.my_email}")
        except Exception as e:
            logger.error(f"Failed to get own details: {e}")
            self.my_id = None
            self.my_email = None

    def send_message(self, room_id=None, text=None, to_person_email=None, **kwargs):
        """Send a message using WebexTeamsAPI."""
        try:
            return self.api.messages.create(roomId=room_id, text=text, toPersonEmail=to_person_email, **kwargs)
        except ApiError as e:
            logger.error(f"Failed to send message: {e}")
            raise
