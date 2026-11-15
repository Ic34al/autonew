"""Interactive automatic Telegram channel creation for selected currently-airing anime.

This module deliberately reuses the existing USER_SESSION, AniList metadata,
anime-channel mapping collection, and sticker helper. Episode ingestion remains
owned by the existing scheduler/uploader pipeline.
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
from bot.core.auto_animes import send_sticker_to_channel, process_batch_anime, AUTO_UPLOAD_PRIORITY_LOCK
from bot.core.func_utils import getfeeds
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
            link = getattr(entry, "link", None)
            if not link:
                continue
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
            start = media.get("startDate") or {}
            end = media.get("endDate") or {}
            studios = (media.get("studios") or {}).get("nodes") or []

            # Keep the expandable synopsis format unchanged and restore all metadata.
            raw_synopsis = str(media.get("description") or "Synopsis unavailable.")
            raw_synopsis = re.sub(r"(?i)<br\s*/?>", "\n\n", raw_synopsis)
            raw_synopsis = re.sub(r"(?i)</(?:p|div|li)\s*>", "\n\n", raw_synopsis)
            synopsis = re.sub(r"<[^>]+>", "", raw_synopsis)
            synopsis = html.unescape(synopsis)
            synopsis = re.sub(r"[ \t]+", " ", synopsis)
            synopsis = re.sub(r"\n{3,}", "\n\n", synopsis).strip()
            titles = media.get("title") or {}
            english_title = titles.get("english") or title
            romaji_title = titles.get("romaji")
            title_line = (
                f"❝ <b>{_escape(english_title)}</b> | <i>{_escape(romaji_title)}</i> ❞"
                if romaji_title and romaji_title.casefold() != english_title.casefold()
                else f"❝ <b>{_escape(english_title)}</b> ❞"
            )
            score = media.get("averageScore")
            first_aired = "-".join(
                str(start.get(key, "")).zfill(2) for key in ("year", "month", "day")
            ) if start.get("year") else "N/A"
            last_aired = "-".join(
                str(end.get(key, "")).zfill(2) for key in ("year", "month", "day")
            ) if end.get("year") else "Ongoing"
            studios_text = ", ".join(
                studio.get("name", "") for studio in studios if studio.get("name")
            ) or "N/A"
            metadata_text = (
                f"<b>Genres:</b> {_escape(', '.join(media.get('genres') or []) or 'N/A')}\n"
                f"<b>Type:</b> {_escape(media.get('format') or media.get('type') or 'N/A')}\n"
                f"<b>Average Rating:</b> {_escape(f'{score / 10:.1f}/10' if score else 'N/A')}\n"
                f"<b>Status:</b> {_escape(media.get('status') or 'N/A')}\n"
                f"<b>First aired:</b> {_escape(first_aired)}\n"
                f"<b>Last aired:</b> {_escape(last_aired)}\n"
                f"<b>Runtime:</b> {_escape(str(media.get('duration') or 'N/A'))} minutes\n"
                f"<b>No. of episodes:</b> {_escape(media.get('episodes') or 'N/A')}\n"
                f"<b>Studio:</b> {_escape(studios_text)}"
            )
            # Telegram photo captions are limited to 1024 characters.
            # Keep the complete metadata list and expandable synopsis styling,
            # shortening only the synopsis text if necessary to fit one message.
            caption_synopsis = synopsis
            details = ""
            while True:
                details = (
                    f"<blockquote>{title_line}</blockquote>\n\n"
                    f"{metadata_text}\n\n"
                    f"<blockquote expandable>▸ <b>Synopsis :</b> <i>{_escape(caption_synopsis)}</i></blockquote>"
                )
                if len(details) <= 980 or len(caption_synopsis) <= 100:
                    break
                caption_synopsis = caption_synopsis[:max(100, len(caption_synopsis) - 40)].rsplit(" ", 1)[0].rstrip() + "…"

            # Generate the reference-matched split-screen ANIME SOUL thumbnail.
            image_url = (
                media.get("bannerImage")
                or (media.get("coverImage") or {}).get("extraLarge")
                or (media.get("coverImage") or {}).get("large")
            )
            from bot.core.anime_thumbnail import create_anime_thumbnail
            thumbnail_path = await create_anime_thumbnail(
                image_url, title, None,
                output_name=f"channel_{media.get('id') or chat.id}.jpg",
                metadata=media,
            )

            # Photo and caption are sent together as ONE Telegram message.
            photo_source = thumbnail_path or image_url
            if photo_source:
                await user_client.send_photo(
                    chat.id,
                    photo_source,
                    caption=details,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await user_client.send_message(
                    chat.id, details, disable_web_page_preview=True
                )
            sticker_ok = await send_sticker_to_channel(
                chat.id, getattr(Var, "STICKER_ID", None), client=user_client
            )
            created.append((title, chat.id, sticker_ok, reused))
            archive_jobs.append((media, int(chat.id)))
            await asyncio.sleep(1)

        # Process completed prequel seasons serially, one dedicated channel at a time.
        # The scheduled scanner uses the same lock, so new episodes wait until this
        # archive queue has finished rather than interleaving storage message IDs.
        for selected_media, destination_id in archive_jobs:
            selected_title = _display_title(selected_media)
            prequels = _prequel_seasons(selected_media)
            for season_index, prequel in enumerate(prequels, start=1):
                season_title = _display_title(prequel)
                pack = await _find_720p_season_pack(prequel, season_index)
                if not pack:
                    await rep.report(
                        f"No complete 720p season-pack RSS entry found for {season_title} "
                        f"(Season {season_index}) of {selected_title}; this channel batch is held. "
                        "Retry /uploadchannel after the complete 720p pack appears in RSS.",
                        "warning"
                    )
                    raise RuntimeError(
                        f"Season {season_index} of {selected_title} is not complete yet. "
                        "The next selected channel was not started."
                    )
                pack_title = str(pack["title"])
                archive_id = int(prequel.get("id") or 0)
                if not archive_id:
                    raise RuntimeError(f"AniList ID is missing for archive season {season_title}.")
                previous = await db.get_anime_archive_batch(
                    archive_id, season_index, destination_id
                )
                previous_status = (previous or {}).get("status")
                if previous_status == "complete":
                    await rep.report(
                        f"Archive already completed; skipping duplicate: {selected_title} / Season {season_index}.",
                        "info",
                        log=False,
                    )
                    continue
                if previous_status == "in_progress":
                    raise RuntimeError(
                        f"Season {season_index} of {selected_title} has an unfinished previous batch run. "
                        "It was not restarted to avoid duplicate file uploads; inspect Anime Logs before retrying."
                    )

                audio = (
                    "Dual" if re.search(r"(?i)\bdual\b", pack_title)
                    else "Dub" if re.search(r"(?i)\b(dub|english)\b", pack_title)
                    else "Sub"
                )
                await db.set_anime_archive_batch(
                    archive_id, season_index, destination_id, "in_progress",
                    pack["link"], pack_title,
                )
                await rep.report(
                    f"Starting completed-season archive: {selected_title} / Season {season_index} "
                    f"using the 720p RSS pack in channel {destination_id}.",
                    "info"
                )
                async with AUTO_UPLOAD_PRIORITY_LOCK:
                    result = await process_batch_anime(
                        pack_title, pack["link"], audio,
                        destination_channel=destination_id,
                    )
                if not result:
                    await db.set_anime_archive_batch(
                        archive_id, season_index, destination_id, "failed",
                        pack["link"], pack_title,
                    )
                    raise RuntimeError(
                        f"720p archive download did not complete for {season_title}; "
                        "the next selected channel was not started."
                    )
                await db.set_anime_archive_batch(
                    archive_id, season_index, destination_id, "complete",
                    pack["link"], pack_title,
                )

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
