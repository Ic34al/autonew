# Deployment Guide

This document explains how to deploy Auto-Anime Bot to common platforms (Heroku, Koyeb), run locally (venv), and a Docker Compose example for local testing.

## Before you begin

1. Copy `config.env` into a safe file and populate required values (see list below).
2. Install system dependencies on your host:

```bash
sudo apt update
sudo apt install -y ffmpeg mediainfo python3-venv
```

3. Create a Python virtual environment and install the requirements (for local runs):

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Required environment variables

(Place these in `config.env` for local runs or set them as config vars / secrets on PaaS)

- API_ID — Telegram API ID (from my.telegram.org)
- API_HASH — Telegram API Hash (from my.telegram.org)
- BOT_TOKEN — Bot token from BotFather
- MONGO_URI — MongoDB connection string (required)
- MONGO_NAME — DB name (optional)
- USER_SESSION — optional Pyrogram user session string (use `session_gen.py` to get one)
- MAIN_CHANNEL — main post channel id (e.g., -100...) 
- BACKUP_CHANNEL — backup channel id (optional)
- LOG_CHANNEL — channel id where bot sends logs (optional)
- FILE_STORE — channel id used as file storage (optional)
- ADMINS — space-separated admin user ids
- FSUB_CHATS — space-separated chat ids that users must join (optional)
- BRAND_UNAME — brand text used in captions
- GDRIVE_UPLOAD — "on" or "off"
- GDRIVE_OAUTH_CREDENTIALS — path to OAuth client secrets for `auth.py` (used locally to create `token.pickle`)
- GDRIVE_CREDENTIALS_FILE — optional service account file
- GDRIVE_FOLDER_ID — (optional) folder id for Drive uploads
- PORT — web server port for health checks (default used in `bot/web.py`)
- FFCODE_HDRi / FFCODE_1080 / FFCODE_720 / ... — FFmpeg command templates (defaults in `config.env`)

Keep secrets out of version control.

## Generating user session & Drive token

- Generate a Pyrogram user session (if needed):

```bash
python3 session_gen.py
# paste the result into config.env as USER_SESSION
```

- Generate Google Drive token (interactive OAuth):

```bash
python3 auth.py
# creates token.pickle locally
```

Upload `token.pickle` to your host only if you trust the environment.

## Run locally (simple)

1. Ensure `config.env` is present and populated.
2. Activate venv and run:

```bash
python3 -m bot
```

Monitor `log.txt` for startup messages.

## Deploy to Heroku

1. Create a new Heroku app.
2. Add the repository (or use the Deploy button on GitHub).
3. Set config vars (`Settings` → `Reveal Config Vars`) using the env variable names above.
4. Add a `Procfile` (if not present) with:

```
web: python -m bot
```

5. Set buildpacks / stacks as needed. Heroku's default Python buildpack works.
6. Deploy and open the logs to ensure the bot starts. Set a worker dyno or put bot in `web` dyno to keep it running.

Notes:
- If `GDRIVE_UPLOAD` is used with OAuth (not service account), you must create `token.pickle` before deploying and add it to the app (use config or a secrets store).

## Deploy to Koyeb (or similar PaaS)

Koyeb can deploy from a GitHub repo or Docker image. Basic steps:

1. Create a new app on Koyeb and link to the repo or build from a Dockerfile.
2. Set environment variables on the service settings (API_ID, API_HASH, BOT_TOKEN, MONGO_URI, MAIN_CHANNEL, FILE_STORE, etc.).
3. Configure port; set `PORT` env if required. `bot/web.py` listens on the configured port for health checks.
4. Deploy and check logs.

## Docker Compose (local testing)

Create a `docker-compose.yml` with MongoDB and the bot service. Example:

```yaml
version: '3.8'
services:
  mongo:
    image: mongo:6
    restart: unless-stopped
    volumes:
      - mongo_data:/data/db
    ports:
      - 27017:27017

  bot:
    build: .
    restart: unless-stopped
    environment:
      - API_ID=${API_ID}
      - API_HASH=${API_HASH}
      - BOT_TOKEN=${BOT_TOKEN}
      - MONGO_URI=mongodb://mongo:27017
      - MONGO_NAME=${MONGO_NAME}
      - MAIN_CHANNEL=${MAIN_CHANNEL}
      - FILE_STORE=${FILE_STORE}
      - GDRIVE_UPLOAD=${GDRIVE_UPLOAD}
    depends_on:
      - mongo
    volumes:
      - ./:/app

volumes:
  mongo_data:
```

Run:

```bash
docker-compose up --build
```

## Health checks & process management

- The repo includes a small web server in `bot/web.py` with `/` and `/status` endpoints for platform health checks.
- Use systemd, supervisord, or a container orchestrator to keep the bot running in production.

## Troubleshooting

- Bot fails to connect to MongoDB: verify `MONGO_URI` and network connectivity.
- FFmpeg errors: ensure `ffmpeg` is installed and `FFCODE_*` commands are valid.
- Drive uploads failing: ensure `token.pickle` or service account file exists and the `GDRIVE_UPLOAD` flag is correct.