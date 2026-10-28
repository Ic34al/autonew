# Commands Reference — Auto-Anime Bot

This document lists the main user and admin commands exposed or used by the bot and where to find their implementation. For exact behavior, check the referenced module and function.

> Files of interest: `bot/modules/cmds.py`, `bot/modules/up_posts.py`, `bot/__main__.py`, and `bot/core/auto_animes.py`.

## Admin-only commands

These commands require your Telegram user ID to be inside the `ADMINS` list (in `config.env`) or configured in the database.

- /restart
  - Purpose: Gracefully restart the bot. Saves state/files and restarts the Python process.
  - Implemented: `bot/__main__.py` (decorated with `@new_task` and admin filter).
  - Example: send `/restart` as an admin in private chat with the bot.

- /batchlink
  - Purpose: Process multiple torrent or magnet links in batch (upload multiple items programmatically).
  - Implemented: `bot/modules/cmds.py` (private admin handler).
  - Usage: Send a private message to the bot containing `batchlink` plus the links (see code for exact expected format).

- /genlink
  - Purpose: Generate a single upload link/handler for a torrent or file.
  - Implemented: `bot/modules/cmds.py`.

- /forcetask
  - Purpose: Force-run a scheduled or queued task immediately.
  - Implemented: `bot/modules/cmds.py`.

- /delallmangas
  - Purpose: Remove all manga entries from the database. Dangerous: only run if you know consequences.
  - Implemented: `bot/modules/cmds.py`.

- /delallmangabanners
  - Purpose: Remove stored manga banners.
  - Implemented: `bot/modules/cmds.py`.

- /listadmins
  - Purpose: Show current admin list (static + DB-managed).
  - Implemented: `bot/modules/cmds.py`.

- /deladmin <user_id>
  - Purpose: Remove an admin from DB-managed admin list.
  - Implemented: `bot/modules/cmds.py`.

- /shell <command>
  - Purpose: Run a shell command (stdout/stderr returned to admin). Dangerous — admin-only.
  - Implemented: `bot/modules/up_posts.py`.
  - Example: `/shell ls -la /tmp`

- /gdrive <on|off>
  - Purpose: Enable or disable Google Drive uploads for processed anime/manga.
  - Implemented: `bot/modules/cmds.py`.
  - Usage: `/gdrive on` or `/gdrive off`. Send with no arguments to check current status.

- /fontchanger <on|off>
  - Purpose: Enable or disable font obfuscation for post captions to avoid copyright issues.
  - Implemented: `bot/modules/cmds.py`.
  - Usage: `/fontchanger on` or `/fontchanger off`. Send with no arguments to check current status.
  - When enabled: Post captions will have characters replaced with symbols:
    - O → 0 (zero)
    - R → π (pi symbol)
    - E → £ (pound sign)
    - S → $ (dollar sign)
    - l → I (capital i)
    - C → ¢ (cent sign)
  - This applies to all anime/manga episode posts sent to main and backup channels.

## Public / user-facing commands

- /ongoing
  - Purpose: List ongoing uploads, encodes, or queued operations.
  - Implemented: `bot/modules/up_posts.py`.

Other user-facing flows are implemented via inline buttons (e.g., "Read Now", "Join Channel") and via messages posted in channels. Check `bot/core/text_utils.py` for caption templates and `bot/core/auto_animes.py` for how messages/buttons are constructed.

## Programmatic / background functions (useful for developers)

- `fetch_animes()` and `fetch_manga()` — background fetchers triggered by scheduler (`bot/core/auto_animes.py`).
- `process_batch_anime(name, torrent_url, audio)` — process and upload an anime release.
- `process_manga_chapter(title, chapter_url, manual=False)` — parse and upload a manga chapter.
- `FFEncoder` — class in `bot/core/ffencoder.py` used for encoding tasks.
- `TgUploader` — class in `bot/core/tguploader.py` used for uploads to Telegram.

## How to run commands manually (admin examples)

1. Open a private chat with the bot (make sure your user id is an admin).
2. Send `/batchlink` followed by links or upload a file as the code expects. Look at `cmds.py` for exact parsing.
3. Use `/genlink` similarly for single items.
4. Use `/shell <command>` only for safe diagnostic commands.

## Where to look to extend or change commands

- `bot/modules/cmds.py` — main admin command handlers and utilities.
- `bot/modules/up_posts.py` — scheduled tasks and helper admin commands.
- `bot/core/auto_animes.py` — the heavy-lifting logic for anime/manga processing and posting.
- `bot/core/text_utils.py` — edit caption templates and button text.
- `bot/core/database.py` — DB APIs for adding/removing mappings, banners, and other persistent settings.

## Safety notes

- Commands that modify DB or remove data (like `/delallmangas`) are destructive. Verify backups before using.
- Admin modules expect caller checks — do not expose your bot token or admin ids in public.