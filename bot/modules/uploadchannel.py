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
from bot.core.anime_thumbnail import create_anime_thumbnail

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
    """Find a verified exact episode torrent, including common plain-number release formats."""
    titles = media.get("title") or {}
    aliases = [_plain_title(titles.get(k)) for k in ("english", "romaji", "native") if titles.get(k)]
    aliases = [a for a in aliases if a]
    if not aliases:
        return None

    season = int(season_number)
    episode = int(episode_number)
    ep = f"{episode:02d}"
    queries = []
    for key in ("english", "romaji", "native"):
        title = titles.get(key)
        if not title:
            continue
        queries.extend([
            f'"{title}" S{season:02d}E{ep} 720p',
            f'"{title}" Season {season} Episode {episode} 720p',
            f'"{title}" {season}x{ep} 720p',
            f'"{title}" - {ep} 720p',
            f'"{title}" {ep} 720p',
        ])

    feeds = [
        f"https://nyaa.si/?page=rss&q={quote_plus(q)}&c=1_2&f=0"
        for q in queries
    ]
    feeds.append((getattr(Var, "RSS_LOW_END", {}) or {}).get("720"))
    feeds.extend(list(getattr(Var, "RSS_ITEMS_ANIME", []) or []))

    # Collect candidates across feeds rather than stopping at the first feed.
    candidates = []
    seen_titles = set()
    for feed_url in dict.fromkeys(x for x in feeds if x):
        try:
            entries = await getfeeds(feed_url)
        except Exception as exc:
            await rep.report(f"Episode RSS scan failed: {exc}", "warning", log=False)
            continue
        for entry in entries or []:
            title = str(getattr(entry, "title", "") or "")
            if title in seen_titles:
                continue
            seen_titles.add(title)
            normalized = _plain_title(title)
            if not normalized or not any(alias == normalized or alias in normalized for alias in aliases):
                continue

            # Recognize standard fansub formats: S01E01, 1x01, Ep 01,
            # Episode 01, '#01', '- 01', and '[01]'. Plain-number forms
            # are only accepted when the number is a release episode token.
            explicit_patterns = (
                rf"(?i)\bS0*{season}[\s._-]*E0*{episode}\b",
                rf"(?i)\b{season}x0*{episode}\b",
                rf"(?i)\bEpisode[\s._-]*0*{episode}\b",
                rf"(?i)\bEp[\s._-]*0*{episode}\b",
                rf"(?i)(?:^|[\s._-])#0*{episode}(?:$|[\s._-])",
                rf"(?i)(?:^|[\s._-])-\s*0*{episode}(?:$|[\s._-])",
                rf"(?i)\[0*{episode}\](?:$|[\s._-])",
            )
            if not any(re.search(pattern, title) for pattern in explicit_patterns):
                continue

            detected_season = _season_number_from_release(title)
            if detected_season is not None and detected_season != season:
                continue
            # Season 2+ must be explicit to avoid accidentally selecting S1.
            if season > 1 and detected_season != season:
                continue

            link = None
            for candidate in (getattr(entry, "links", None) or []):
                href = candidate.get("href") if isinstance(candidate, dict) else None
                rel = str(candidate.get("rel") or "").lower() if isinstance(candidate, dict) else ""
                typ = str(candidate.get("type") or "").lower() if isinstance(candidate, dict) else ""
                if href and (rel == "enclosure" or "bittorrent" in typ or "x-bittorrent" in typ):
                    link = href
                    break
            link = link or getattr(entry, "link", None)
            if not link:
                continue
            match = re.match(r"^https?://(?:www\.)?nyaa\.si/view/(\d+)", str(link), re.I)
            if match:
                link = f"https://nyaa.si/download/{match.group(1)}.torrent"
            candidates.append({"title": title, "link": str(link)})

    # Prefer releases with an explicit episode marker and an actual torrent URL.
    if candidates:
        return candidates[0]
    return None

async def _upload_single_episode(media, destination_id, season_number, episode_number):
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
    quality_order = ("480", "720", "1080", "HDRi")
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
        url = f"https://t.me/{Var.BOT_USERNAME}?start={await encode(start_payload)}"
        buttons.append(InlineKeyboardButton("HDRip" if quality == "HDRi" else f"{quality}p", url=url))
    caption = (
        f"<b>{_escape(title)} Season {season_number} Episode {int(episode_number):02d}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"➜ <b>Audio:</b> {_escape(info.pdata.get('audio_type') or 'Japanese [English Sub]')}\n"
        f"➤ <b>Quality:</b> 480p, 720p, 1080p &amp; HDRip\n"
        f"➜ <b>Episode:</b> {int(episode_number):02d}\n"
        f"━━━━━━━━━━━━━━\n"
        f"<blockquote>{_escape((info.adata.get('description') or '')[:450])}</blockquote>"
    )
    episode_thumb = await create_anime_thumbnail(
        info.adata.get("bannerImage") or
        (info.adata.get("coverImage") or {}).get("large") or
        (info.adata.get("coverImage") or {}).get("extraLarge"),
        title,
        f"{int(episode_number):02d}",
        output_name=f"uc_episode_thumb_{safe}.jpg",
        metadata={**(info.adata or {}), "season": str(season_number)},
    )
    if episode_thumb and os.path.exists(episode_thumb):
        await bot.send_photo(
            chat_id=destination_id,
            photo=episode_thumb,
            caption=caption,
            reply_markup=InlineKeyboardMarkup([buttons[:2], buttons[2:]]),
        )
        os.remove(episode_thumb)
    else:
        await bot.send_message(
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
    try:
        user_client = __import__("pyrogram").Client(
            "autonew_channel_creator",
            api_id=int(Var.API_ID),
            api_hash=Var.API_HASH,
            session_string=Var.USER_SESSION,
            in_memory=True,
        )
        await user_client.start()
        # Resolve the bot through its public username in the USER session.
        # A bot-session numeric ID is not necessarily an input peer known to this
        # separate MTProto session and can raise PEER_ID_INVALID.
        bot_username = str(getattr(Var, "BOT_USERNAME", "") or "").strip().lstrip("@")
        if not bot_username:
            bot_username = (await bot.get_me()).username or ""
        if not bot_username:
            raise RuntimeError("BOT_USERNAME is missing; set the public bot username in Heroku config.")
        bot_user = await user_client.get_users(bot_username)
        if not bot_user or not getattr(bot_user, "id", None):
            raise RuntimeError(f"Could not resolve public bot username @{bot_username} in USER_SESSION.")

        selected_items = [state["items"][i] for i in sorted(state["selected"])]
        for media in selected_items:
            title = _display_title(media)
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
            if mapped_id:
                wanted_id = int(mapped_id)
                # Load the saved channel peer from the USER_SESSION dialog cache.
                async for dialog in user_client.get_dialogs():
                    if int(dialog.chat.id) == wanted_id:
                        chat = dialog.chat
                        break
                if chat is None:
                    try:
                        chat = await user_client.get_chat(wanted_id)
                    except Exception:
                        chat = None
                if chat is None:
                    raise RuntimeError(
                        f"A channel mapping already exists for '{title}' ({wanted_id}), "
                        "but USER_SESSION cannot access it. No duplicate channel was created. "
                        "Check that this account still has access to that channel."
                    )
                reused = True
            else:
                chat = await user_client.create_channel(
                    title, f"Official episode channel for {title}"
                )

            # Refresh all aliases and selected mapping for either reused or new channel.
            for name in names:
                await db.add_anime_channel_mapping(name, chat.id)
            await db.set_anime_channel_selected(title, chat.id)

            # Telegram does not allow bots to be ordinary channel members.
            # A bot must be promoted directly by the channel creator/admin.
            # Calling promote_chat_member performs channels.editAdmin; do NOT
            # call add_chat_members (channels.inviteToChannel), which returns
            # USER_BOT for bot accounts.
            try:
                current_member = await user_client.get_chat_member(chat.id, bot_user.id)
                current_status = str(getattr(current_member, "status", "")).lower()
            except Exception:
                current_status = ""

            if not any(role in current_status for role in ("administrator", "owner", "creator")):
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
            # Each archive season gets its own info -> sticker -> episode-batch sequence.
            created.append((title, chat.id, None, reused))
            archive_jobs.append((media, int(chat.id)))
            await asyncio.sleep(1)

        # Hold the scheduler lock for the entire archive backlog so a new
        # episode cannot interleave between Season 1/Season 2 or selected channels.
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
                    # Bots cannot invoke messages.GetHistory. Read channel history through the authorized USER_SESSION.
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
                    # Publish the season header only once. Existing channel history is
                    # authoritative on retries, so a retry does not spam duplicate posters.
                    info_exists = False
                    async for history_message in user_client.get_chat_history(destination_id, limit=500):
                        history_body = " ".join(str(x or "") for x in (
                            getattr(history_message, "text", ""),
                            getattr(history_message, "caption", ""),
                        ))
                        if re.search(
                            rf"(?i)Season\s*{season_index}\b.*(?:Episodes|Format|Status)",
                            history_body,
                        ):
                            info_exists = True
                            break

                    # Use the season's own title, not the selected sequel title,
                    # to prevent captions like "Season 2 — Season 1".
                    season_info = (
                        f"<b>{_escape(season_title)} — Season {season_index}</b>\n"
                        f"<b>Year:</b> {_escape(season_year)}\n"
                        f"<b>Episodes:</b> {_escape(episode_total)}\n"
                        f"<b>Format:</b> {_escape(season_format)}\n"
                        f"<b>Status:</b> {_escape(season_status)}\n"
                        f"<b>Qualities:</b> HDRip, 1080p, 720p, 480p"
                    )
                    if not info_exists:
                        season_editor = TextEditor(season_title)
                        await season_editor.load_info()
                        poster_meta = dict(prequel)
                        poster_meta.update(season_editor.adata or {})
                        poster_meta.setdefault("title", prequel.get("title") or {})
                        poster_meta.setdefault("description", prequel.get("description"))
                        poster_meta.setdefault("bannerImage", prequel.get("bannerImage"))
                        poster_meta.setdefault("coverImage", prequel.get("coverImage") or {})
                        poster_meta.setdefault("season", prequel.get("season"))
                        poster_meta.setdefault("seasonYear", season_year)\n                        poster_meta["season"] = str(season_index)
                        poster_path = await create_anime_thumbnail(
                            poster_meta.get("bannerImage") or
                            (poster_meta.get("coverImage") or {}).get("extraLarge") or
                            (poster_meta.get("coverImage") or {}).get("large"),
                            season_title,
                            f"Season {season_index}",
                            output_name=f"uc_info_{archive_id}_{season_index}.jpg",
                            metadata=poster_meta,
                        )
                        if poster_path and os.path.exists(poster_path):
                            info_message = await bot.send_photo(
                                chat_id=destination_id,
                                photo=poster_path,
                                caption=season_info,
                            )
                            os.remove(poster_path)
                        else:
                            info_message = await bot.send_message(
                                chat_id=destination_id,
                                text=season_info,
                                disable_web_page_preview=True,
                            )
                        if not info_message:
                            raise RuntimeError(
                                f"Could not publish Season {season_index} information for {selected_title}; "
                                "the season batch was not started."
                            )
                        sticker_ok = await send_sticker_to_channel(
                            destination_id, getattr(Var, "STICKER_ID", None)
                        )
                        if not sticker_ok:
                            raise RuntimeError(
                                f"Season {season_index} sticker failed for {selected_title}; episodes were not started."
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
                        await _upload_single_episode(
                            prequel, destination_id, season_index, episode_number
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
        await rep.report(f"Automatic anime channel creation failed: {type(exc).__name__}: {exc}", "error")
        await callback.message.edit(
            f"⚠️ Channel creation stopped. {len(created)} channel(s) were created before the error.\n<code>{_escape(exc)}</code>"
        )
    finally:
        if user_client:
            try:
                await user_client.stop()
            except Exception:
                pass
