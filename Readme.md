## Auto-Anime Bot

Auto-Anime Bot is a Telegram automation bot that downloads, optionally encodes, and uploads anime (and manga) releases to Telegram channels. It supports torrents/magnets, RSS feeds, automatic encoding with FFmpeg, Google Drive backups, scheduled posts, and a small web endpoint for liveliness checks.

## Key features

- Automatic detection and upload of anime files to configured Telegram channels.
- Support for multiple resolutions: HDRip (raw), 1080p, 720p, 480p, 360p, 240p and 144p.
- Optional on-the-fly encoding via FFmpeg for custom quality profiles.
- Torrent/magnet downloader support and RSS feed monitoring.
- Google Drive upload support (optional) with resumable uploads.
- Channel mapping per anime and backup/restore support.
- Simple web health endpoint for platform hosts (Koyeb/Heroku) at `/` and `/status`.

## Repo layout (important files)

- `bot/` — main bot package and modules.
- `bot/core/` — core components (downloader, uploader, encoder, DB, util functions).
- `config.env` — example configuration (environment variables). Copy this to your environment file and update values.
- `auth.py` — helper to generate Google Drive OAuth token (runs a local web flow).
- `session_gen.py` — interactive script to generate a Pyrogram user session string.
- `run.sh` — simple start wrapper (if present).
- `Procfile` — process declaration for platforms like Heroku.
- `requirements.txt` — Python dependencies.

## Requirements

- Python 3.11+ (project created for modern Python; 3.10 may work but not tested here).
- FFmpeg (for encoding/transcoding and signature operations).
- mediainfo (used to read media duration/metadata).
- A MongoDB instance (URI required in config).
- (Optional) Google credentials + `token.pickle` for Drive uploads.

## Installation (local)

1. Clone repository and enter folder:

```bash
git clone <repo-url> my-auto-anime-bot
cd my-auto-anime-bot
```

2. Create and activate a virtual environment, then install dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

3. Install system tools (example on Debian/Ubuntu):

```bash
sudo apt update
sudo apt install -y ffmpeg mediainfo
```

## Configuration

Copy `config.env` and update values or set environment variables directly. Important variables:

- `API_ID`, `API_HASH` — from my.telegram.org.
- `BOT_TOKEN` — Telegram bot token from BotFather.
- `MONGO_URI` — MongoDB connection string (required).
- `MONGO_NAME` — (optional) database name.
- `USER_SESSION` — user session string (see below to generate).
- `MAIN_CHANNEL`, `BACKUP_CHANNEL`, `LOG_CHANNEL`, `FILE_STORE` — Telegram channel/chat IDs.
- `GDRIVE_UPLOAD` — "on" or "off" to enable Google Drive uploads.
- `GDRIVE_OAUTH_CREDENTIALS` — path to OAuth client secret (used by `auth.py`).
- `GDRIVE_CREDENTIALS_FILE` / `GDRIVE_FOLDER_ID` — service account or folder id for service uploads.
- `FFCODE_*` — custom FFmpeg command templates (already present in `config.env`).

Keep secrets out of source control. Use platform environment variables when deploying.

## Generating the user session

If the bot needs a user session string for certain features, create it with `session_gen.py`:

```bash
python3 session_gen.py
```

Follow the prompts and copy the printed string into `config.env` as `USER_SESSION`.

## Google Drive authentication (optional)

To enable Drive uploads set `GDRIVE_UPLOAD="on"` and provide OAuth credentials or a service account file. To create `token.pickle` for the OAuth client flow run:

```bash
python3 auth.py
```

This runs a local server for the OAuth flow and writes `token.pickle`.

## Running the bot

Run the bot with the package entrypoint:

```bash
python3 -m bot
```

You can also use `run.sh` (if executable) or a process manager.

## Deploying (Heroku / Koyeb / other PaaS)

- Heroku: Use the Deploy button at the top of this README or push with the Heroku CLI. Ensure you set required environment variables on the app (API_ID, API_HASH, BOT_TOKEN, MONGO_URI, MAIN_CHANNEL, FILE_STORE, etc.).
- Koyeb / similar: the repository includes a small web server in `bot/web.py` so the platform can route health checks.

When deploying, set `PORT` environment variable if required by the host.


## Runtime notes & tips

- Ensure `ffmpeg` and `mediainfo` are installed and in PATH.
- MongoDB must be reachable by the bot; connection errors will prevent startup.
- If using Google Drive with OAuth, run `auth.py` locally to obtain `token.pickle` and upload it to your host (securely).
- Tune `FFCODE_*` variables in `config.env` to control encoding behavior.

## Technology & tech stack

This project uses an async-first Python stack and common multimedia tooling:

- Language: Python 3.11+ (async/await heavy codebase)
- Telegram client: Pyrogram (bot & user session usage)
- Database: MongoDB (motor.AsyncIOMotorClient)
- HTTP/health: aiohttp (small web server in `bot/web.py`)
- Event scheduling: APScheduler (async scheduler)
- Concurrency: asyncio + uvloop (optional speedups)
- Encoding: FFmpeg (external binary called via command templates in `config.env`)
- Media metadata: mediainfo (CLI) and helpers in `bot/func.py`
- Image/PDF: Pillow (PIL) and FPDF for PDF generation
- Drive integration: googleapiclient + oauthlib (`bot/core/gdrive.py`)
- Torrent helper: torrentp and async HTTP clients (aiohttp)

System packages to install on host (example Debian/Ubuntu):

```bash
sudo apt update
sudo apt install -y ffmpeg mediainfo
```

## Architecture & components

- Fetchers: RSS and torrent fetchers collect releases and put them into the processing pipeline (`bot/core/tordownload.py`, `bot/core/auto_animes.py`).
- Processor: optional encoding (FFEncoder in `bot/core/ffencoder.py`) and filename/caption generation (`bot/core/text_utils.py`).
- Uploader: Telegram uploads handled in `bot/core/tguploader.py`. Google Drive backup via `bot/core/gdrive.py`.
- Persistence: `bot/core/database.py` stores mappings for anime/manga channels, banners, backup mappings and settings.
- Orchestration: `bot/__main__.py` starts schedulers, background tasks and the web health server.

## Commands & admin utilities (expanded)

Below are the most important commands and programmatic entrypoints (check the referenced files for exact signatures and extra flags):

- /restart (admin) — gracefully restart the bot, saving state when possible. (`bot/__main__.py`)
- /batchlink (admin) — accept/process multiple torrent links in bulk. (`bot/modules/cmds.py`)
- /genlink (admin) — generate an upload link/handler for specific files.
- /forcetask (admin) — force a queued task or schedule to run immediately.
- /delallmangas (admin) — delete all manga entries from DB (dangerous).
- /delallmangabanners (admin) — clear all manga banners.
- /listadmins (admin) — display configured admin users.
- /deladmin <id> (admin) — remove an admin id.
- /shell <command> (admin) — run a shell command from the bot; only enable for trusted admins.
- /ongoing — list ongoing uploads/encodes/queue state.

Developer helpers (programmatic):

- `fetch_animes()` / `fetch_manga()` — called periodically to poll RSS and add to the queue.
- `process_manga_chapter()` — handles chapter normalization, channel creation, PDF/image uploads.
- `FFEncoder` — async encoding worker, reports progress through messages and `prog.txt`.

If you want a printable, copy/paste command block or a `--help` output file, I can extract docstrings and create one.

## Security & privacy notes

- Never commit `config.env`, OAuth secrets, or `token.pickle` to public repos.
- Use platform environment variables for production secrets (Heroku/Koyeb/Container secrets).
- Keep admin commands (especially `/shell`) limited to trusted users.

## Manga support (how it works)

This bot includes a dedicated manga pipeline and several utilities for fetching and posting manga chapters automatically:

- RSS driven: the bot can monitor manga RSS feeds (configured in `RSS_ITEMS_MANGA`) and will fetch new chapters.
- Chapter processing: downloaded chapters are parsed, filenames are generated and normalized using helpers in `bot/core/text_utils.py` and `bot/core/base_clean.py`.
- Per-manga channels: for many manga titles the bot can create or reuse a dedicated Telegram channel to host chapters. Helpers like `get_or_create_manga_channel` and `create_manga_channel` in `bot/core/auto_animes.py` handle channel creation and setup.
- Posting format: manga posts include a richly formatted caption (see `MANGA_CAPTION_FORMAT` in `bot/core/text_utils.py`) and inline buttons such as "Read Now" and "Join Channel".
- Chapter delivery options: pages may be uploaded as photos (one message per page) or combined into a PDF (the code uses `FPDF` to optionally create downloadable PDFs). The bot also supports storing chapter files in a configured file store channel.
- Banners & thumbnails: per-manga banners can be set and are stored in the database; the bot can attach a banner/thumbnail to posts.
- Google Drive backup: when `GDRIVE_UPLOAD` is enabled, processed manga files can be uploaded to Drive using `bot/core/gdrive.py`.

Internal functions and files that implement manga flow:

- `bot/core/auto_animes.py` — contains `fetch_manga`, `process_manga_chapter`, and channel management functions.
- `bot/core/text_utils.py` — caption templates, metadata extraction (AniList lookups), and filename helpers.
- `bot/core/database.py` — stores per-manga mappings, banners, backup mappings and settings.

If you want a specific manga to be posted into an existing channel, add a mapping using the admin utilities (see Admin commands below) or directly in the database collection used by `database.py`.

## Admin commands & utilities (overview)

There are several admin-only actions and commands implemented in the codebase (see `bot/modules/cmds.py` and `bot/modules/up_posts.py`). Below are common admin actions:

- batchlink — generate or process a batch of torrent links (admin only).
- genlink — generate a one-off upload link/handler.
- forcetask — force a scheduled or queued job to run now.
- delallmangas — removes all manga entries (admin only).
- delallmangabanners — removes all stored manga banners.
- listadmins / deladmin — manage admin list.
- shell — run a shell command from the bot (admin only; see `up_posts.py`).
- ongoing / upcoming — helpers to list or post schedules.

Exact command names, signatures and behavior are implemented inside the modules. If you want a custom admin command or to expose more control, add functions to `bot/modules/cmds.py` following the existing patterns.

## Environment variables (quick reference)

Most configurable settings are read from `config.env` via `python-dotenv`. Important variables used by the bot include (not exhaustive):

- API_ID, API_HASH — Telegram API credentials (required).
- BOT_TOKEN — Bot token from BotFather (required).
- USER_SESSION — optional Pyrogram user session string (generated with `session_gen.py`).
- MONGO_URI — MongoDB connection string (required).
- MONGO_NAME — MongoDB database name (optional).
- MAIN_CHANNEL — main public channel/chat id for posts.
- BACKUP_CHANNEL — secondary backup channel id.
- LOG_CHANNEL — channel id where bot posts logs/status.
- FILE_STORE — channel id used as file store.
- FILE_STORE_LINK — link to the file store channel.
- ADMINS — space-separated list of admin user IDs.
- FSUB_CHATS — list of follow/subscribe channels required to use the bot.
- SEND_SCHEDULE — enable/disable scheduled post behavior.
- BRAND_UNAME — short footer/brand text for captions.
- GDRIVE_UPLOAD — "on"/"off" to enable Google Drive uploads.
- GDRIVE_OAUTH_CREDENTIALS — path to OAuth client credentials for `auth.py`.
- GDRIVE_CREDENTIALS_FILE — path to service account file (optional).
- GDRIVE_FOLDER_ID — default folder id for Drive uploads.
- PORT — web server port for PaaS health checks.
- FFCODE_HDRi, FFCODE_1080, ... — ffmpeg templates used by the encoder.

See `config.env` in the repository for a concrete example and default FFMPEG strings.

## Generating a session & Drive token (recap)

- Generate user session (if needed):

```bash
python3 session_gen.py
```

- Create Google Drive token (interactive OAuth flow):

```bash
python3 auth.py
```

This creates `token.pickle` which the `bot/core/gdrive.py` loader will use.

## Example: How a manga chapter is handled (high level)

1. A new chapter appears on a monitored RSS feed (configured in `RSS_ITEMS_MANGA`).
2. `fetch_manga` (in `bot/core/auto_animes.py`) picks up the feed entry and downloads images or archives.
3. `process_manga_chapter` prepares filenames, creates/uses a manga channel, builds a caption (using `MANGA_CAPTION_FORMAT`), optionally converts pages into a PDF, and uploads the chapter.
4. If enabled, the bot uploads the file to Google Drive and stores the link in the database.
5. The bot posts a mirror message to the main channel (if configured) and records backup mappings in the DB.

## Troubleshooting manga uploads

- Missing pages or corrupt images: check the downloader logs and verify that the feed source is reachable. The downloader uses async HTTP and torrent helpers — network issues will cause retries.
- PDF generation errors: the bot uses `FPDF` to assemble PDFs and `PIL.Image` for image handling; ensure these packages are installed (they're in `requirements.txt`).
- Channel creation errors: when the bot creates a new channel it must use an account with permission to create channels and invite the bot or bot admin to manage it.

## Testing locally

1. Fill in `config.env` with required variables.
2. Run the bot locally with `python3 -m bot`.
3. Tail `log.txt` for startup logs and feed fetch activity.

## Troubleshooting

- Bot won't start: check logs (`log.txt` is written by default) and confirm environment variables are present.
- Encoding failures: verify FFmpeg command strings in `config.env` and ensure `ffmpeg` is on PATH.
- Drive upload failing: confirm `token.pickle` or service account file and that `GDRIVE_UPLOAD` is set to `on`.

## Contributing

Contributions are welcome. Open issues for bugs or feature requests and submit PRs for small, focused changes.
