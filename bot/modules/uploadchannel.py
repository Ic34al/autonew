"""Interactive channel creation and serial completed-season RSS backfill.

This module reuses USER_SESSION, AniList relations, persistent anime-channel
mappings, the configured RSS sources, and the existing batch uploader. The
currently releasing season remains owned by the normal scheduled uploader.
"""
import asyncio
import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

import httpx
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ChatPrivileges

from bot import bot, Var, LOGS
from bot.core.database import db
from bot.core.auto_animes import send_sticker_to_channel, process_batch_anime, AUTO_UPLOAD_PRIORITY_LOCK, find_video_files, build_anime_watch_markup
from bot.core.func_utils import getfeeds, encode
from bot.core.tordownload import TorDownloader
from bot.core.ffencoder import FFEncoder
from bot.core.tguploader import TgUploader
from bot.core.text_utils import TextEditor
import os
import shutil
import hashlib
from bot.core.reporter import rep

# Photo and metadata caption are intentionally sent as one Telegram message.
ANILIST_URL = "https://graphql.anilist.co"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
UPLOAD_CHANNEL_SESSIONS = {}
LOGS.info("UPLOADCHANNEL MODULE LOADED; /uploadchannel handler registered.")

SCHEDULE_QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    media(type: ANIME, status: RELEASING, sort: POPULARITY_DESC) {
      id
      title { english romaji native }
      synonyms
      bannerImage
      season
      seasonYear
      description(asHtml: false)
      type
      format
      status
      episodes
      duration
      averageScore
      genres
      startDate { year month day }
      endDate { year month day }
      coverImage { extraLarge large }
      studios(isMain: true) { nodes { name } }
      relations {
        edges {
          relationType
          node {
            id
            title { english romaji native }
            synonyms
            format
            status
            episodes
            seasonYear
            relations {
              edges {
                relationType
                node {
                  id
                  title { english romaji native }
                  format
                  status
                  episodes
                  seasonYear
                }
              }
            }
          }
        }
      }
      nextAiringEpisode { airingAt episode timeUntilAiring }
    }
  }
}
"""


def _escape(value):
    return html.escape(str(value or "N/A"))


def _airing_day(media):
    next_ep = media.get("nextAiringEpisode") or {}
    airing_at = next_ep.get("airingAt")
    if not airing_at:
        return None
    return datetime.fromtimestamp(int(airing_at), LOCAL_TZ).strftime("%A")


def _display_title(media):
    titles = media.get("title") or {}
    return titles.get("english") or titles.get("romaji") or titles.get("native") or "Unknown anime"


async def _fetch_currently_airing():
    timeout = httpx.Timeout(25.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            ANILIST_URL,
            json={"query": SCHEDULE_QUERY, "variables": {"page": 1, "perPage": 50}},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("errors"):
        raise RuntimeError("; ".join(str(e.get("message", "AniList error")) for e in payload["errors"]))
    media = (payload.get("data") or {}).get("Page", {}).get("media") or []
    result = []
    seen = set()
    for item in media:
        day = _airing_day(item)
        title = _display_title(item)
        if not day or not title or item.get("id") in seen:
            continue
        seen.add(item.get("id"))
        item["_weekday"] = day
        result.append(item)
    return result



def _plain_title(value):
    value = str(value or "").casefold()
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", value)
    value = re.sub(r"\b(?:batch|complete|season pack|full season|720p|1080p|480p|1080|720|480)\b", " ", value)
    value = re.sub(r"\bs\d{1,2}\s*[-._ ]*e?\d{1,3}\b|\bseason\s*\d+\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _season_number_from_release(title):
    patterns = (
        r"\bS0*(\d{1,2})(?:E|\s*[-._ ]*\d)",
        r"\bSeason\s*0*(\d{1,2})\b",
        r"\bS0*(\d{1,2})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, str(title or ""), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _prequel_seasons(media):
    """Return TV prequel ancestors oldest-first, bounded to two relation levels."""
    found = {}

    def walk(parent, depth):
        if depth > 2:
            return
        relations = (parent.get("relations") or {}).get("edges") or []
        for edge in relations:
            if str(edge.get("relationType") or "").upper() != "PREQUEL":
                continue
            node = edge.get("node") or {}
            media_id = node.get("id")
            if not media_id or media_id in found:
                continue
            fmt = str(node.get("format") or "").upper()
            if fmt not in ("TV", "TV_SHORT", "ONA"):
                continue
            found[media_id] = node
            walk(node, depth + 1)

    walk(media, 1)
    return sorted(
        found.values(),
        key=lambda item: (
            int(item.get("seasonYear") or 0),
            int(item.get("id") or 0),
        ),
    )


async def _find_720p_season_pack(media, season_number):
    """Find a clearly labelled complete-season 720p torrent in configured RSS feeds."""
    titles = media.get("title") or {}
    aliases = {
        _plain_title(titles.get(key))
        for key in ("english", "romaji", "native")
        if titles.get(key)
    }
    aliases.discard("")
    if not aliases:
        return None

    # Search Nyaa directly for complete 720p season packs as well as the
    # bot's configured RSS feeds. The query is title/season-specific; entries
    # are still strictly validated below before any download starts.
    search_titles = [
        titles.get(key) for key in ("english", "romaji", "native") if titles.get(key)
    ]
    nyaa_feeds = []
    for search_title in search_titles:
        queries = (
            f'"{search_title}" Season {int(season_number)} 720p Batch',
            f'"{search_title}" S{int(season_number):02d} 720p Batch',
            f'"{search_title}" Season {int(season_number)} 720p Complete',
        )
        nyaa_feeds.extend(
            f"https://nyaa.si/?page=rss&q={quote_plus(query)}&c=1_2&f=0"
            for query in queries
        )

    quality_feed = (getattr(Var, "RSS_LOW_END", {}) or {}).get("720")
    feeds = list(dict.fromkeys(
        url for url in nyaa_feeds + [quality_feed] + list(getattr(Var, "RSS_ITEMS_ANIME", []) or [])
        if url
    ))
    for feed_url in feeds:
        try:
            entries = await getfeeds(feed_url)
        except Exception as exc:
            await rep.report(f"Archive RSS scan failed for {feed_url}: {exc}", "warning", log=False)
            continue
        for entry in entries or []:
            release_title = str(getattr(entry, "title", "") or "")
            # Prefer an actual torrent enclosure. Nyaa's RSS alternate link is
            # usually a /view/<id> HTML page, not a torrent file.
            link = None
            for candidate in (getattr(entry, "links", None) or []):
                href = candidate.get("href") if isinstance(candidate, dict) else None
                rel = str(candidate.get("rel") or "").lower() if isinstance(candidate, dict) else ""
                media_type = str(candidate.get("type") or "").lower() if isinstance(candidate, dict) else ""
                if href and (rel == "enclosure" or "bittorrent" in media_type or "x-bittorrent" in media_type):
                    link = href
                    break
            link = link or getattr(entry, "link", None)
            if not link:
                continue
            nyaa_view = re.match(r"^https?://(?:www\.)?nyaa\.si/view/(\d+)(?:[/?#].*)?$", str(link), re.IGNORECASE)
            if nyaa_view:
                link = f"https://nyaa.si/download/{nyaa_view.group(1)}.torrent"
            title_key = _plain_title(release_title)
            if not title_key or not any(
                alias == title_key or alias in title_key or title_key in alias
                for alias in aliases
            ):
                continue
            # Only a season pack qualifies; single-episode entries cannot
            # accidentally create an incomplete range download.
            is_pack = bool(re.search(
                r"(?i)\b(batch|complete|full[ ._-]*season|season[ ._-]*pack)\b|"
                r"\b(?:01|1)[ ._-]*(?:-|to)[ ._-]*\d{1,3}\b",
                release_title,
            ))
            if not is_pack or not re.search(r"(?i)\b720p\b", release_title):
                continue
            release_season = _season_number_from_release(release_title)
            if release_season is None or release_season != int(season_number):
                continue
            return {"title": release_title, "link": link}
    return None



async def _find_episode_torrent(media, season_number, episode_number):
    """Search Nyaa RSS using AniList titles/synonyms and return only an exact episode match."""
    titles = media.get("title") or {}
    raw_aliases = [
        titles.get(key) for key in ("english", "romaji", "native")
        if titles.get(key)
    ]
    # AniList's synonyms often contain the short Japanese/romanized title
    # used by release groups when the English title is long.
    raw_aliases.extend(media.get("synonyms") or [])
    aliases = []
    for value in raw_aliases:
        value = str(value or "").strip()
        normalized = _plain_title(value)
        if value and normalized and normalized not in {
            _plain_title(existing) for existing in aliases
        }:
            aliases.append(value)
    if not aliases:
        await rep.report(
            f"AniList supplied no searchable title aliases for anime ID {media.get('id')}.",
            "warning", log=False,
        )
        return None

    season = int(season_number)
    episode = int(episode_number)
    queries = []
    for alias in aliases:
        # Avoid quoted-only searches: Nyaa title punctuation and release-group
        # formatting varies. Keep each query title-specific and episode-specific.
        queries.extend([
            f"{alias} S{season:02d}E{episode:02d} 720p",
            f"{alias} S{season}E{episode:02d} 720p",
            f"{alias} Season {season} Episode {episode} 720p",
            f"{alias} {episode:02d} 720p",
        ])
    feeds = [
        f"https://nyaa.si/?page=rss&q={quote_plus(query)}&c=1_2&f=0"
        for query in queries
    ]
    feeds.append((getattr(Var, "RSS_LOW_END", {}) or {}).get("720"))
    feeds.extend(list(getattr(Var, "RSS_ITEMS_ANIME", []) or []))

    seen_entries = set()
    scanned = 0
    for feed_url in dict.fromkeys(url for url in feeds if url):
        try:
            entries = await getfeeds(feed_url)
        except Exception as exc:
            await rep.report(
                f"Episode RSS scan failed for {feed_url}: {type(exc).__name__}: {exc}",
                "warning", log=False,
            )
            continue

        for entry in entries or []:
            title = str(getattr(entry, "title", "") or "")
            link = getattr(entry, "link", None)
            entry_key = (title.casefold(), str(link or ""))
            if entry_key in seen_entries:
                continue
            seen_entries.add(entry_key)
            scanned += 1

            normalized_release = _plain_title(title)
            # First prefer a full normalized alias match. For abbreviated
            # release titles, permit a strong token overlap only (>=75% of
            # a meaningful alias and at least four matching words).
            title_match = any(
                _plain_title(alias) in normalized_release
                for alias in aliases
                if _plain_title(alias)
            )
            if not title_match:
                release_tokens = set(normalized_release.split())
                for alias in aliases:
                    alias_tokens = {
                        token for token in _plain_title(alias).split()
                        if len(token) > 2 and token not in {
                            "the", "and", "for", "with", "from", "that", "this",
                            "her", "his", "their", "you", "your", "into",
                        }
                    }
                    if len(alias_tokens) >= 4:
                        overlap = len(alias_tokens & release_tokens)
                        if overlap >= 4 and overlap / len(alias_tokens) >= 0.75:
                            title_match = True
                            break
            if not title_match:
                continue

            ep_patterns = (
                rf"(?i)\bS0*{season}[\s._-]*E0*{episode}\b",
                rf"(?i)\b{season}x0*{episode}\b",
                rf"(?i)\bSeason[\s._-]*0*{season}[\s._-]*Episode[\s._-]*0*{episode}\b",
                rf"(?i)\bEpisode[\s._-]*0*{episode}\b",
                rf"(?i)\bEp[\s._-]*0*{episode}\b",
            )
            episode_match = any(re.search(pattern, title) for pattern in ep_patterns)
            # Many Season 1 releases are numbered simply "- 01". Accept that
            # format only for S1 and only after a strong anime-title match.
            if season == 1 and not episode_match:
                episode_match = bool(re.search(
                    rf"(?i)(?:^|[\s._\-\[])0*{episode}(?:[\s._\-\]\[]|$)",
                    title,
                ))
            if not episode_match:
                continue

            detected_season = _season_number_from_release(title)
            if detected_season is not None and detected_season != season:
                continue
            if season > 1 and detected_season != season:
                continue

            torrent_link = None
            for candidate in (getattr(entry, "links", None) or []):
                href = candidate.get("href") if isinstance(candidate, dict) else None
                rel = str(candidate.get("rel") or "").lower() if isinstance(candidate, dict) else ""
                content_type = str(candidate.get("type") or "").lower() if isinstance(candidate, dict) else ""
                if href and (
                    rel == "enclosure"
                    or "bittorrent" in content_type
                    or "x-bittorrent" in content_type
                ):
                    torrent_link = href
                    break
            torrent_link = torrent_link or link
            if not torrent_link:
                continue

            match = re.match(
                r"^https?://(?:www\.)?nyaa\.si/view/(\d+)",
                str(torrent_link), re.I,
            )
            if match:
                torrent_link = f"https://nyaa.si/download/{match.group(1)}.torrent"
            return {"title": title, "link": str(torrent_link)}

    await rep.report(
        f"No exact RSS torrent match for AniList anime {media.get('id')} "
        f"Season {season} Episode {episode}; searched {len(aliases)} title aliases "
        f"across {len(dict.fromkeys(url for url in feeds if url))} feeds "
        f"and inspected {scanned} unique entries. No upload was started.",
        "warning", log=False,
    )
    return None


async def _upload_single_episode(media, destination_id, season_number, episode_number, destination_client):
    """Download one RSS episode, encode four qualities, and publish four buttons."""
    from bot import Var
    episode = await _find_episode_torrent(media, season_number, episode_number)
    if not episode:
        raise RuntimeError(
            f"RSS source missing or ambiguous for Season {season_number} Episode {episode_number}; "
            "stopping to avoid uploading the wrong episode."
        )
    title = _display_title(media)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{media.get('id')}_{season_number}_{episode_number}")
    workdir = os.path.join("downloads", "uc_episode_" + safe)
    os.makedirs(workdir, exist_ok=True)
    downloaded = await TorDownloader(workdir).download(episode["link"])
    if not downloaded or not os.path.exists(downloaded):
        raise RuntimeError(f"Torrent download failed for {title} S{season_number}E{episode_number}.")
    video_files = find_video_files(os.path.dirname(downloaded) if os.path.isfile(downloaded) else downloaded)
    if len(video_files) != 1:
        raise RuntimeError(
            f"Expected exactly one video for {title} S{season_number}E{episode_number}; found {len(video_files)}."
        )
    source = video_files[0]
    info = TextEditor(title)
    await info.load_info()
    info.pdata["episode_number"] = str(episode_number).zfill(2)
    info.pdata["audio_type"] = (
        "Dual" if re.search(r"(?i)\bdual\b", episode["title"])
        else "Dub" if re.search(r"(?i)\b(dub|english)\b", episode["title"])
        else "Sub"
    )
    # Build file deep-links from the live BOT_TOKEN identity, not the
    # legacy BOT_USERNAME config (which may still name an older bot).
    live_bot = await bot.get_me()
    live_bot_username = str(getattr(live_bot, "username", "") or "").strip().lstrip("@")
    if not live_bot_username:
        raise RuntimeError("The bot authenticated by BOT_TOKEN has no username; cannot create file links.")
    quality_order = ("HDRi", "1080", "720", "480")
    buttons = []
    for quality in quality_order:
        upload_name = await info.get_upname(quality)
        quality_source = os.path.join("encode", f"uc_source_{safe}_{quality}.mkv")
        os.makedirs("encode", exist_ok=True)
        shutil.copy2(source, quality_source)
        encoded_path = await FFEncoder(
            await bot.send_message(Var.LOG_CHANNEL, f"<b>Encoding:</b> {_escape(title)} S{season_number}E{episode_number} — {quality}p"),
            quality_source, upload_name, quality
        ).start_encode()
        if not encoded_path or not os.path.exists(encoded_path):
            raise RuntimeError(f"Encoding failed for S{season_number}E{episode_number} at {quality}.")
        uploaded = await TgUploader(await bot.send_message(
            Var.LOG_CHANNEL, f"<b>Uploading:</b> {_escape(title)} S{season_number}E{episode_number} — {quality}p"
        )).upload(encoded_path, quality)
        if not uploaded:
            raise RuntimeError(f"FILE_STORE upload failed for S{season_number}E{episode_number} at {quality}.")
        token = hashlib.sha256(
            f"{uploaded.id}:{media.get('id')}:{season_number}:{episode_number}:{quality}".encode()
        ).hexdigest()[:24]
        await db.set_batch_file_link(token, [uploaded.id])
        start_payload = f"getbatch-{token}"
        url = f"https://t.me/{live_bot_username}?start={await encode(start_payload)}"
        buttons.append(InlineKeyboardButton(f"{quality}p", url=url))
    caption = (
        f"<b>{_escape(title)} — Season {season_number} Episode {episode_number}</b>\n"
        f"<b>Release:</b> {_escape(episode['title'])}\n"
        f"<b>Available qualities:</b> HDRip, 1080p, 720p, 480p"
    )
    await destination_client.send_message(
        chat_id=destination_id,
        text=caption,
        reply_markup=InlineKeyboardMarkup([buttons]),
        disable_web_page_preview=True,
    )
    shutil.rmtree(workdir, ignore_errors=True)
    return True


def _selection_keyboard(items, selected):
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    rows = []
    for day in weekdays:
        indices = [i for i, item in enumerate(items) if item["_weekday"] == day]
        if not indices:
            continue
        rows.append([InlineKeyboardButton(f"— {day} —", callback_data="uc:noop")])
        for offset in range(0, len(indices), 2):
            row = []
            for index in indices[offset:offset + 2]:
                title = _display_title(items[index])
                if len(title) > 28:
                    title = title[:25] + "..."
                prefix = "✅ " if index in selected else "▫️ "
                row.append(InlineKeyboardButton(prefix + title, callback_data=f"uc:toggle:{index}"))
            rows.append(row)
    rows.append([
        InlineKeyboardButton(f"Create {len(selected)} channel(s)", callback_data="uc:create"),
        InlineKeyboardButton("Cancel", callback_data="uc:cancel"),
    ])
    return InlineKeyboardMarkup(rows)



@bot.on_message(filters.command("setchannelinvite") & filters.private)
async def setchannelinvite_command(client, message):
    if not message.from_user or message.from_user.id not in Var.ADMINS:
        return await message.reply("⛔ This command is restricted to bot administrators.")
    raw = message.text.split(None, 1)
    if len(raw) < 2 or "|" not in raw[1]:
        return await message.reply(
            "Usage: /setchannelinvite Anime Title | https://t.me/+INVITE_LINK"
        )
    anime_title, invite_link = [part.strip() for part in raw[1].split("|", 1)]
    if not anime_title or not re.match(r"^https?://t\.me/(?:\+|joinchat/)", invite_link):
        return await message.reply("Send the exact private channel invite link, not a channel ID.")
    user_client = __import__("pyrogram").Client(
        "autonew_channel_creator",
        api_id=int(Var.API_ID),
        api_hash=Var.API_HASH,
        session_string=Var.USER_SESSION,
        in_memory=True,
        parse_mode=ParseMode.HTML,
    )
    try:
        await user_client.start()
        chat = await user_client.get_chat(invite_link)
        if not chat or not getattr(chat, "id", None) or not str(getattr(chat, "type", "")).lower().endswith("channel"):
            return await message.reply("That invite link did not resolve to a channel.")
        await db.add_anime_channel_mapping(anime_title, chat.id)
        await db.set_anime_channel_invite_link(anime_title, chat.id, invite_link)
        await db.set_anime_channel_selected(anime_title, chat.id)
        await message.reply(
            f"✅ Saved the private channel reference for <b>{_escape(anime_title)}</b> "
            f"({chat.id}). Retry /uploadchannel now."
        )
    except Exception as exc:
        await message.reply(
            f"❌ Could not validate that invite link: {_escape(type(exc).__name__)}: {_escape(exc)}"
        )
    finally:
        if user_client.is_connected:
            await user_client.stop()

@bot.on_message(filters.command("uploadchannel") & filters.private)
async def uploadchannel_command(client, message):
    # Respond explicitly to non-admins instead of silently filtering the command.
    if not message.from_user or message.from_user.id not in Var.ADMINS:
        return await message.reply("⛔ This command is restricted to bot administrators.")
    if not getattr(Var, "USER_SESSION", None):
        return await message.reply(
            "⚠️ USER_SESSION is not configured. Add your Telegram user session to the deployment environment first."
        )
    status = await message.reply("Fetching currently airing anime and their release weekdays…")
    try:
        items = await _fetch_currently_airing()
        if not items:
            return await status.edit("AniList did not return any currently airing anime with a known next-episode day.")
        UPLOAD_CHANNEL_SESSIONS[message.from_user.id] = {
            "items": items,
            "selected": set(),
            "created": [],
        }
        keyboard = _selection_keyboard(items, set())
        await status.edit(
            "<b>Choose ongoing anime</b>\nSelect as many titles as you want. The weekday is based on the next scheduled episode in India time.",
            reply_markup=keyboard,
        )
    except Exception as exc:
        await rep.report(f"/uploadchannel schedule lookup failed: {type(exc).__name__}: {exc}", "warning")
        await status.edit(f"Could not load the AniList schedule: <code>{_escape(exc)}</code>")


@bot.on_callback_query(filters.regex(r"^uc:"))
async def uploadchannel_callbacks(client, callback):
    owner_id = callback.from_user.id
    state = UPLOAD_CHANNEL_SESSIONS.get(owner_id)
    if owner_id not in Var.ADMINS or not state:
        return await callback.answer("This selection has expired. Run /uploadchannel again.", show_alert=True)

    data = callback.data
    if data == "uc:noop":
        return await callback.answer()
    if data == "uc:cancel":
        UPLOAD_CHANNEL_SESSIONS.pop(owner_id, None)
        return await callback.message.edit("Channel creation cancelled.")
    if data.startswith("uc:toggle:"):
        index = int(data.rsplit(":", 1)[1])
        if index < 0 or index >= len(state["items"]):
            return await callback.answer("Invalid anime selection.", show_alert=True)
        if index in state["selected"]:
            state["selected"].remove(index)
        else:
            state["selected"].add(index)
        await callback.message.edit_reply_markup(
            _selection_keyboard(state["items"], state["selected"])
        )
        return await callback.answer(f"{len(state['selected'])} selected")

    if data != "uc:create":
        return await callback.answer()
    if not state["selected"]:
        return await callback.answer("Select at least one anime first.", show_alert=True)

    await callback.answer("Starting channel creation…")
    await callback.message.edit("Creating selected anime channels. Please keep the bot running…")
    user_client = None
    created = []
    archive_jobs = []
    channel_stage = "starting USER_SESSION"
    current_anime_title = "unknown"
    try:
        user_client = __import__("pyrogram").Client(
            "autonew_channel_creator",
            api_id=int(Var.API_ID),
            api_hash=Var.API_HASH,
            session_string=Var.USER_SESSION,
            in_memory=True,
            parse_mode=ParseMode.HTML,
        )
        channel_stage = "starting USER_SESSION client"
        await user_client.start()
        channel_stage = "resolving bot account in USER_SESSION"
        # Resolve the bot through its public username in the USER session.
        # A bot-session numeric ID is not necessarily an input peer known to this
        # separate MTProto session and can raise PEER_ID_INVALID.
        # The bot account authenticated by BOT_TOKEN is the only account we
        # are allowed to promote. Never let a stale BOT_USERNAME config promote
        # a different bot into every newly created channel.
        token_bot = await bot.get_me()
        token_bot_username = str(getattr(token_bot, "username", "") or "").strip().lstrip("@")
        configured_bot_username = str(getattr(Var, "BOT_USERNAME", "") or "").strip().lstrip("@")
        if not token_bot_username:
            raise RuntimeError("The account authenticated by BOT_TOKEN has no public username; cannot promote it by username.")
        # BOT_USERNAME may be stale from an older deployment. It is not an
        # authority for channel permissions: always use the identity returned
        # by the live BOT_TOKEN client. A mismatch is diagnostic only.
        if configured_bot_username and configured_bot_username.casefold() != token_bot_username.casefold():
            await rep.report(
                f"Ignoring stale BOT_USERNAME=@{configured_bot_username}; "
                f"BOT_TOKEN authenticates as @{token_bot_username}. "
                "Only the BOT_TOKEN account will be promoted in anime channels.",
                "warning",
            )
        bot_username = token_bot_username
        bot_user = await user_client.get_users(bot_username)
        if not bot_user or not getattr(bot_user, "id", None):
            raise RuntimeError(
                f"Could not resolve the actual BOT_TOKEN account @{bot_username} in USER_SESSION."
            )
        if int(bot_user.id) != int(token_bot.id):
            raise RuntimeError(
                f"Bot identity mismatch: BOT_TOKEN is user {token_bot.id}, but USER_SESSION resolved "
                f"@{bot_username} as {bot_user.id}. Refusing to promote the wrong account."
            )

        selected_items = [state["items"][i] for i in sorted(state["selected"])]
        for media in selected_items:
            title = _display_title(media)
            current_anime_title = title
            channel_stage = f"reading saved channel mapping for {title}"
            aliases = media.get("title") or {}
            names = []
            for key in ("english", "romaji", "native"):
                value = aliases.get(key)
                if value and value not in names:
                    names.append(value)
            names.append(title)
            names = list(dict.fromkeys(names))

            # Idempotency: check every known title alias before creating a channel.
            mapped_id = None
            for name in names:
                mapped_id = await db.get_anime_channel(name)
                if not mapped_id:
                    mapped_id = await db.get_selected_anime_channel(name)
                if mapped_id:
                    break

            chat = None
            reused = False
            persistent_invite = None
            if mapped_id:
                wanted_id = int(mapped_id)
                # The saved channels were created by USER_SESSION. Resolve them
                # through that same creator account, not the bot token: the bot
                # may not have been promoted yet if an earlier run stopped midway.
                # Do not scan get_dialogs(); stale dialog peers can trigger
                # channels.GetChannels / CHANNEL_INVALID.
                saved_invite = await db.get_anime_channel_invite_link(title)
                persistent_invite = saved_invite
                resolve_error = None
                try:
                    # First use the persisted invite if present; otherwise try the
                    # numeric peer directly (works when this session has its hash).
                    channel_stage = f"opening saved channel peer for {title}"
                    chat = (
                        await user_client.get_chat(saved_invite)
                        if saved_invite else await user_client.get_chat(wanted_id)
                    )
                except Exception as exc:
                    resolve_error = exc
                    chat = None

                if chat is None:
                    # Recover a legacy channel peer through raw messages.GetDialogs.
                    # Unlike Pyrogram's high-level iterator, this does not try to
                    # resolve each dialog's offset peer through channels.GetChannels.
                    try:
                        from pyrogram.raw.functions.messages import GetDialogs
                        from pyrogram.raw.types import InputPeerEmpty
                        channel_stage = f"recovering legacy peer from raw Telegram dialogs for {title}"
                        raw_dialogs = await user_client.invoke(
                            GetDialogs(
                                offset_date=0,
                                offset_id=0,
                                offset_peer=InputPeerEmpty(),
                                limit=100,
                                hash=0,
                            )
                        )
                        for raw_chat in getattr(raw_dialogs, "chats", []) or []:
                            raw_id = getattr(raw_chat, "id", None)
                            raw_hash = getattr(raw_chat, "access_hash", None)
                            if raw_id is None or raw_hash is None:
                                continue
                            # Pyrogram storage.update_peers expects normalized
                            # (marked_id, access_hash, type, username, phone) tuples.
                            marked_id = int(f"-100{int(raw_id)}")
                            peer_type = "channel" if getattr(raw_chat, "broadcast", False) else "supergroup"
                            await user_client.storage.update_peers([
                                (
                                    marked_id,
                                    int(raw_hash),
                                    peer_type,
                                    getattr(raw_chat, "username", None),
                                    None,
                                )
                            ])
                            if marked_id == wanted_id:
                                chat = await user_client.get_chat(wanted_id)
                                break
                    except Exception as raw_error:
                        resolve_error = raw_error

                if chat is None:
                    # The saved peer is irrecoverable for this USER_SESSION.
                    # Do not abort the whole selection or keep retrying the same
                    # invalid ID forever. Create a replacement channel and update
                    # every title alias below after Telegram confirms creation.
                    await rep.report(
                        f"Saved channel mapping for {title} ({wanted_id}) is not accessible "
                        f"({type(resolve_error).__name__}: {resolve_error}). Creating a replacement channel; "
                        "the old channel is left untouched.",
                        "warning",
                    )
                    channel_stage = f"creating replacement channel for {title}"
                    chat = await user_client.create_channel(
                        title, f"Official episode channel for {title}"
                    )
                    reused = False
                elif not chat or int(chat.id) != wanted_id:
                    raise RuntimeError(
                        f"Saved mapping for '{title}' points to {wanted_id}, but Telegram "
                        "returned a different channel. Refusing to overwrite the mapping."
                    )
                else:
                    reused = True
            else:
                chat = await user_client.create_channel(
                    title, f"Official episode channel for {title}"
                )

            # Persist a durable invite reference for private channels. Unlike a
            # numeric channel ID, the invite URL can repopulate Pyrogram's peer
            # cache after a fresh in-memory USER_SESSION on Heroku.
            if not persistent_invite:
                try:
                    channel_stage = f"creating persistent invite link for {title}"
                    invite_obj = await user_client.create_chat_invite_link(
                        chat.id, name="Anime uploader persistent peer"
                    )
                    persistent_invite = invite_obj.invite_link
                except Exception as invite_error:
                    raise RuntimeError(
                        f"Could not create a persistent invite reference for '{title}': {invite_error}"
                    ) from invite_error

            # Save aliases and invite reference only after the real channel is resolved.
            for name in names:
                await db.add_anime_channel_mapping(name, chat.id)
                await db.set_anime_channel_invite_link(name, chat.id, persistent_invite)
            await db.set_anime_channel_selected(title, chat.id)

            # USER_SESSION is the channel creator/admin. Verify the bot's status
            # through this session, and repair missing admin rights automatically.
            try:
                channel_stage = f"checking bot admin rights for {title}"
                current_member = await user_client.get_chat_member(chat.id, bot_user.id)
                current_status = str(getattr(current_member, "status", "")).lower()
            except Exception:
                current_status = ""

            if not any(role in current_status for role in ("administrator", "owner", "creator")):
                channel_stage = f"promoting bot administrator for {title}"
                await user_client.promote_chat_member(
                    chat.id,
                    bot_user.id,
                    privileges=ChatPrivileges(
                        can_manage_chat=True,
                        can_post_messages=True,
                        can_edit_messages=True,
                        can_delete_messages=True,
                        can_invite_users=True,
                        can_change_info=True,
                        can_pin_messages=True,
                    ),
                )
                # Confirm the promotion before any season/episode upload begins.
                channel_stage = f"verifying bot administrator rights for {title}"
                confirmed_member = await user_client.get_chat_member(chat.id, bot_user.id)
                confirmed_status = str(getattr(confirmed_member, "status", "")).lower()
                if not any(role in confirmed_status for role in ("administrator", "owner", "creator")):
                    raise RuntimeError(
                        f"Bot promotion could not be confirmed for '{title}' ({chat.id}). "
                        "No episode upload was started."
                    )
                if getattr(confirmed_member, "can_post_messages", True) is False:
                    raise RuntimeError(
                        f"@{bot_username} is an admin in '{title}' ({chat.id}) but Telegram denied "
                        "can_post_messages. Episode uploads cannot start until posting permission is granted."
                    )
            # Each archive season gets its own info -> sticker -> episode-batch sequence.
            created.append((title, chat.id, None, reused))
            archive_jobs.append((media, int(chat.id)))
            await asyncio.sleep(1)

        # Hold the scheduler lock for the entire archive backlog so a new
        # episode cannot interleave between Season 1/Season 2 or selected channels.
        channel_stage = "acquiring upload priority lock"
        await AUTO_UPLOAD_PRIORITY_LOCK.acquire()
        try:
            # Process one selected anime completely before starting the next.
            # Older seasons first, then the currently airing season.
            for selected_media, destination_id in archive_jobs:
                selected_title = _display_title(selected_media)
                prequels = _prequel_seasons(selected_media)
                season_jobs = [(index, item, int(item.get("episodes") or 0))
                               for index, item in enumerate(prequels, start=1)]
                current_episode_count = max(0, int((selected_media.get("nextAiringEpisode") or {}).get("episode") or 1) - 1)
                if current_episode_count:
                    season_jobs.append((len(season_jobs) + 1, selected_media, current_episode_count))
                if not season_jobs:
                    raise RuntimeError(f"No released episodes are known for {selected_title}; no upload was started.")
                for season_index, prequel, available_episodes in season_jobs:
                    season_title = _display_title(prequel)
                    archive_id = int(prequel.get("id") or 0)
                    if not archive_id:
                        raise RuntimeError(f"AniList ID is missing for archive season {season_title}.")
                    previous = await db.get_anime_archive_batch(
                        archive_id, season_index, destination_id
                    )
                    previous_status = (previous or {}).get("status")
                    # Resume safely from the channel history; a stale DB status must not
                    # suppress uploads when the destination channel is actually empty.
                    # Check the destination channel's history; existing episodes are skipped.
                    existing_episodes = set()
                    channel_stage = f"reading existing episode history in {destination_id} via USER_SESSION"
                    async for channel_message in user_client.get_chat_history(destination_id, limit=500):
                        body = " ".join(str(x or "") for x in (
                            getattr(channel_message, "text", ""),
                            getattr(channel_message, "caption", ""),
                        ))
                        match = re.search(
                            rf"(?i)Season\s*{season_index}\s*Episode\s*0*(\d+)", body
                        )
                        if match:
                            existing_episodes.add(int(match.group(1)))
                    season_year = prequel.get("seasonYear") or "N/A"
                    episode_total = available_episodes or "Unknown"
                    season_format = prequel.get("format") or "TV"
                    season_status = prequel.get("status") or "N/A"
                    season_info = (
                        f"<b>{_escape(selected_title)} — Season {season_index}</b>\n"
                        f"<b>Season title:</b> {_escape(season_title)}\n"
                        f"<b>Year:</b> {_escape(season_year)}\n"
                        f"<b>Episodes:</b> {_escape(episode_total)}\n"
                        f"<b>Format:</b> {_escape(season_format)}\n"
                        f"<b>Status:</b> {_escape(season_status)}\n"
                        f"<b>Qualities:</b> HDRip, 1080p, 720p, 480p"
                    )
                    channel_stage = f"posting Season {season_index} information to {destination_id} via USER_SESSION"
                    info_message = await user_client.send_message(
                        chat_id=destination_id,
                        text=season_info,
                        disable_web_page_preview=True,
                    )
                    if not info_message:
                        raise RuntimeError(
                            f"Could not publish Season {season_index} information for {selected_title}; "
                            "the season batch was not started."
                        )
                    channel_stage = f"posting Season {season_index} sticker to {destination_id} via USER_SESSION"
                    sticker_ok = await send_sticker_to_channel(
                        destination_id, getattr(Var, "STICKER_ID", None), client=user_client
                    )
                    if not sticker_ok:
                        # A decorative sticker failure must never cancel the
                        # actual episode backfill. The helper logs the Telegram
                        # exception; continue with the season information and uploads.
                        await rep.report(
                            f"Priority sticker could not be posted for {selected_title} "
                            f"Season {season_index} in {destination_id}; continuing episode uploads.",
                            "warning",
                            log=False,
                        )
                    if not available_episodes:
                        raise RuntimeError(
                            f"AniList has no released episode count for {season_title}; refusing to guess."
                        )
                    await db.set_anime_archive_batch(
                        archive_id, season_index, destination_id, "in_progress",
                        "", f"episode-by-episode S{season_index} 4-quality",
                    )
                    for episode_number in range(1, available_episodes + 1):
                        if episode_number in existing_episodes:
                            continue
                        await rep.report(
                            f"Uploading {selected_title} Season {season_index} Episode {episode_number} "
                            f"to dedicated channel {destination_id}.",
                            "info", log=False,
                        )
                        channel_stage = f"uploading Season {season_index} Episode {episode_number} to {destination_id}"
                        await _upload_single_episode(
                            prequel, destination_id, season_index, episode_number, user_client
                        )
                    await db.set_anime_archive_batch(
                        archive_id, season_index, destination_id, "complete",
                        "", f"episode-by-episode S{season_index} 4-quality",
                    )
    
        finally:
            AUTO_UPLOAD_PRIORITY_LOCK.release()

        UPLOAD_CHANNEL_SESSIONS.pop(owner_id, None)
        summary = "\n".join(f"• <b>{_escape(name)}</b> — <code>{chat_id}</code>" for name, chat_id, _, _ in created)
        await callback.message.edit(
            f"✅ Processed {len(created)} anime channel(s); existing channel mappings are reused to prevent duplicate creation.\n\n{summary}\n\nThe existing scheduled uploader can now route matching new releases through these mappings."
        )
    except Exception as exc:
        await rep.report(
            f"Automatic anime channel creation failed at stage '{channel_stage}' "
            f"for '{current_anime_title}': {type(exc).__name__}: {exc}",
            "error",
        )
        await callback.message.edit(
            f"⚠️ Channel setup stopped at <b>{_escape(channel_stage)}</b> for "
            f"<b>{_escape(current_anime_title)}</b>. {len(created)} channel(s) were completed before the error.\n"
            f"<code>{_escape(exc)}</code>"
        )
    finally:
        if user_client:
            try:
                await user_client.stop()
            except Exception:
                pass
