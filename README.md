# Webex Bot Relay API

This API lets you send messages to a Webex bot and collect the bot's reply messages in the same HTTP request.

## What It Does

- Uses your personal Webex token to authenticate.
- Sends messages to a bot email address.
- Waits for the requested collect window, then fetches recent room messages over the Webex REST API.
- Reuses one in-memory Webex session per user token.
- Closes idle sessions 5 minutes after the last /chat call.
- Returns all matching bot events collected during the requested time window.

## Docker

Build the image:

```bash
docker build -t webex-bot-relay-api .
```

Run the container (standalone):

```bash
docker run -p 8000:8000 webex-bot-relay-api
```

The API is then available at `http://localhost:8000`.

Run the container behind **Traefik** (assuming Traefik is already running and attached to an external network called `traefik`):

```bash
docker run -d \
  --name webex-bot-relay-api \
  --network traefik \
  --label "traefik.enable=true" \
  --label "traefik.http.routers.webex-bot-relay.rule=Host(\`webex-api.example.com\`)" \
  --label "traefik.http.routers.webex-bot-relay.entrypoints=websecure" \
  --label "traefik.http.routers.webex-bot-relay.tls.certresolver=letsencrypt" \
  --label "traefik.http.services.webex-bot-relay.loadbalancer.server.port=8000" \
  webex-bot-relay-api
```

Replace `webex-api.example.com` with your actual domain. Traefik will terminate TLS and proxy `https://webex-api.example.com` to port `8000` inside the container.

## Setup (local)

1. Install uv if you do not already have it.
2. Create a local environment and install dependencies:

  ```bash
  uv sync
  ```

3. Start the API:

  ```bash
  uv run uvicorn webex_bot_api:app --host 0.0.0.0 --port 8000 --reload
  ```

4. Optional: run the interactive client:

  ```bash
  uv run python main.py
  ```

## Endpoints

### GET /health

Returns the current process health and active in-memory session count.

### POST /chat

Sends a message to a bot, reusing an existing session for the supplied Webex token if one is already active. The API waits for `collect_ms`, then fetches recent room messages once and returns bot replies sent after the original message.

Request body:

```json
{
  "user_token": "<YOUR_PERSONAL_WEBEX_TOKEN>",
  "bot_email": "my-bot@webex.bot",
  "message": "Hello",
  "collect_ms": 4000
}
```

Response body:

```json
{
  "reply": "Final bot text seen in the collect window",
  "replies": [
    "First bot text",
    "Final bot text seen in the collect window"
  ],
  "events": [
    {
      "message_id": "Y2lzY29zcGFyazovL3VzL01FU1NBR0Uv...",
      "activity_id": "d7e...",
      "verb": "post",
      "parent_type": null,
      "parent_activity_id": null,
      "created": "2026-04-22T12:34:56.000Z",
      "text": "First bot text"
    }
  ],
  "room_id": "Y2lzY29zcGFyazovL3VzL1JPT00v...",
  "bot_email": "my-bot@webex.bot"
}
```

## Quick Test With curl

Chat:

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_token": "YOUR_PERSONAL_WEBEX_TOKEN",
    "bot_email": "my-bot@webex.bot",
    "message": "Hello bot",
    "collect_ms": 4000
  }'
```

## Notes

- Sessions are stored in memory and are process-local.
- The API always uses the internal Webex device name `api-relay-client`.
- Reusing the same `user_token` reuses the same Webex session until it has been idle for more than 5 minutes.
- If the process restarts, all sessions are lost and will be recreated on the next `/chat` call.
- If your bot responds after the `collect_ms` delay ends, that reply will not appear in the current response.
- If your bot responds with attachments only and no text or markdown, the `events` list may be empty.
