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

import httpx
from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ChatPrivileges

from bot import bot, Var, LOGS
from bot.core.database import db
from bot.core.auto_animes import send_sticker_to_channel
from bot.core.reporter import rep

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
            # Do not pass a database channel ID to get_chat(). Pyrogram needs
            # an access_hash for channels, and a bare ID from MongoDB is not
            # sufficient in a fresh USER_SESSION. That was the source of
            # CHANNEL_INVALID (channels.GetChannels) on every retry.
            # Create the channel in this live session and keep its returned
            # Chat object for all following operations in this run.
            chat = await user_client.create_channel(
                title, f"Official episode channel for {title}"
            )

            # Persist the new ID immediately so the existing uploader's
            # anime-to-channel routing can see the selected destination.
            aliases = media.get("title") or {}
            names = []
            for key in ("english", "romaji", "native"):
                value = aliases.get(key)
                if value and value not in names:
                    names.append(value)
            names.append(title)
            for name in dict.fromkeys(names):
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
            # Match the requested channel post layout: thumbnail first, then
            # one compact title quote and one expandable synopsis quote.
            # AniList descriptions sometimes contain literal HTML <br> tags even
            # when asHtml=false; normalize them before escaping for Telegram HTML.
            raw_synopsis = str(media.get("description") or "Synopsis unavailable.")
            raw_synopsis = re.sub(r"(?i)<br\s*/?>", "\n\n", raw_synopsis)
            raw_synopsis = re.sub(r"(?i)</(?:p|div|li)\s*>", "\\n\\n", raw_synopsis)
            synopsis = re.sub(r"<[^>]+>", "", raw_synopsis)
            synopsis = html.unescape(synopsis)
            synopsis = re.sub(r"[ \t]+", " ", synopsis)
            synopsis = re.sub(r"\n{3,}", "\\n\\n", synopsis).strip()
            titles = media.get("title") or {}
            english_title = titles.get("english") or title
            romaji_title = titles.get("romaji")
            title_line = (
                f"❝ <b>{_escape(english_title)}</b> | <i>{_escape(romaji_title)}</i> ❞"
                if romaji_title and romaji_title.casefold() != english_title.casefold()
                else f"❝ <b>{_escape(english_title)}</b> ❞"
            )
            details = (
                f"<blockquote>{title_line}</blockquote>\n\n"
                f"<blockquote expandable>▸ <b>Synopsis :</b> <i>{_escape(synopsis)}</i></blockquote>"
            )
            image_url = (media.get("coverImage") or {}).get("extraLarge") or (media.get("coverImage") or {}).get("large")
            if image_url:
                await user_client.send_photo(chat.id, image_url)
            await user_client.send_message(chat.id, details, disable_web_page_preview=True)
            sticker_ok = await send_sticker_to_channel(
                chat.id, getattr(Var, "STICKER_ID", None), client=user_client
            )
            created.append((title, chat.id, sticker_ok))
            await asyncio.sleep(1)

        UPLOAD_CHANNEL_SESSIONS.pop(owner_id, None)
        summary = "\n".join(f"• <b>{_escape(name)}</b> — <code>{chat_id}</code>" for name, chat_id, _ in created)
        await callback.message.edit(
            f"✅ Created {len(created)} anime channel(s), saved their anime mappings, and posted the metadata cards.\n\n{summary}\n\nThe existing scheduled uploader can now route matching new releases through these mappings."
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
