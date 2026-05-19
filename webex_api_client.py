import logging

from webexteamssdk import ApiError, WebexTeamsAPI


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("WebexApiClient")


class WebexApiClient:
    def __init__(self, access_token, device_name="python-ws-client"):
        self.access_token = access_token
        self.device_name = device_name
        self.api = WebexTeamsAPI(access_token=access_token, disable_ssl_verify=True)

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

    def create_room(self, title: str):
        """Create a new Webex room and return the room object."""
        try:
            return self.api.rooms.create(title=title)
        except ApiError as e:
            logger.error(f"Failed to create room: {e}")
            raise

    def add_member(self, room_id: str, person_email: str = None, person_id: str = None):
        """Add a person to a room by email or person ID."""
        try:
            return self.api.memberships.create(
                roomId=room_id,
                personEmail=person_email,
                personId=person_id,
            )
        except ApiError as e:
            logger.error(f"Failed to add member: {e}")
            raise

    def delete_room(self, room_id: str):
        """Delete a Webex room."""
        try:
            return self.api.rooms.delete(room_id)
        except ApiError as e:
            logger.error(f"Failed to delete room: {e}")
            raise
