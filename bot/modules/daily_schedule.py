import asyncio
import logging
from datetime import datetime
from html import escape, unescape
import re
from pathlib import Path

import pytz
from aiohttp import ClientSession, ClientTimeout

from bot import bot, Var
from pyrogram.enums import ParseMode

LOGGER = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

SCHEDULE_URL = (
    "https://subsplease.org/api/"
    "?f=schedule&h=true&tz=Asia/Kolkata"
)

ANILIST_URL = "https://graphql.anilist.co"

ANILIST_QUERY = """
query ($search: String!) {
  Page(page: 1, perPage: 5) {
    media(search: $search, type: ANIME, isAdult: false) {
      id
      title {
        english
        romaji
        native
      }
    }
  }
}
"""

# Upload your original anime image to the repository root.
BANNER_PATH = Path("today_anime_schedule.JPG")

CHANNEL_URL = "https://t.me/Ongoing_Anime_Time"
CAPTION_LIMIT = 950


def parse_release_time(value):
    value = str(value or "").strip().upper()

    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue

    return None


async def get_english_title(session, original_title, cache):
    """Prefer AniList English title, then romanized title."""
    original_title = str(original_title or "").strip()

    if not original_title:
        return "Unknown anime"

    key = original_title.casefold()

    if key in cache:
        return cache[key]

    try:
        async with session.post(
            ANILIST_URL,
            json={
                "query": ANILIST_QUERY,
                "variables": {"search": original_title},
            },
            headers={"Accept": "application/json"},
        ) as response:
            if response.status != 200:
                LOGGER.warning(
                    "AniList returned HTTP %s for %s",
                    response.status,
                    original_title,
                )
                cache[key] = original_title
                return original_title

            data = await response.json(content_type=None)

        results = (
            data.get("data", {})
            .get("Page", {})
            .get("media", [])
        )

        if not results:
            cache[key] = original_title
            return original_title

        normalized = original_title.casefold().strip()
        match = next(
            (
                anime for anime in results
                if any(
                    (anime.get("title", {}).get(field) or "")
                    .casefold().strip() == normalized
                    for field in ("english", "romaji", "native")
                )
            ),
            results[0],
        )
        titles = match.get("title") or {}

        resolved = (
            titles.get("english")
            or titles.get("romaji")
            or original_title
        )

        cache[key] = resolved
        return resolved

    except Exception:
        LOGGER.warning(
            "English title lookup failed for %s",
            original_title,
            exc_info=True,
        )
        cache[key] = original_title
        return original_title


async def prepare_schedule(schedule, now, session):
    prepared = []

    for item in schedule:
        original_title = str(
            item.get("title") or "Unknown anime"
        ).strip()

        release_time = parse_release_time(item.get("time"))

        if release_time is None:
            LOGGER.warning(
                "Skipping invalid release time for %s",
                original_title,
            )
            continue

        release_datetime = IST.localize(
            datetime.combine(now.date(), release_time)
        )

        episode = (
            item.get("episode")
            or item.get("episodeNumber")
            or item.get("episode_number")
            or "TBA"
        )

        prepared.append({
            "original_title": original_title,
            "datetime": release_datetime,
            "episode": str(episode),
        })

    cache = {}
    semaphore = asyncio.Semaphore(4)

    async def resolve_title(item):
        async with semaphore:
            return await get_english_title(
                session,
                item["original_title"],
                cache,
            )

    titles = await asyncio.gather(
        *(resolve_title(item) for item in prepared),
        return_exceptions=True,
    )

    items = []

    for item, title in zip(prepared, titles):
        if isinstance(title, Exception):
            title = item["original_title"]

        release_datetime = item["datetime"]
        episode = item["episode"]

        if episode.isdigit():
            episode = episode.zfill(2)

        items.append({
            "title": str(title),
            # Display local clock time only: no IST, AM, or PM.
            "time": release_datetime.strftime("%I:%M %p").lstrip("0"),
            "datetime": release_datetime,
            "episode": episode,
            "status": (
                "Released"
                if now >= release_datetime
                else "Upcoming"
            ),
        })

    items.sort(key=lambda item: item["datetime"])
    return items


def style_e(value):
    """Style E: mathematical sans-serif bold italic."""
    output = []
    for char in str(value):
        code = ord(char)
        if "A" <= char <= "Z":
            output.append(chr(0x1D63C + code - ord("A")))
        elif "a" <= char <= "z":
            output.append(chr(0x1D656 + code - ord("a")))
        elif "0" <= char <= "9":
            output.append(chr(0x1D7EC + code - ord("0")))
        else:
            output.append(char)
    return "".join(output)


def telegram_utf16_length(value):
    return len(value.encode("utf-16-le")) // 2


def format_anime_entry(item):
    title = escape(style_e(item["title"]))
    time_text = escape(style_e(item["time"]))
    status = escape(style_e("Upcoming"))
    return f'<a href="{CHANNEL_URL}">{title}</a>\\n{time_text}  ⏳ {status}'


def build_schedule_message(items, now):
    header = "✦ 𝗧𝗢𝗗𝗔𝗬 | 𝗔𝗡𝗜𝗠𝗘 𝗦𝗖𝗛𝗘𝗗𝗨𝗟𝗘"
    divider = "────────────────────"
    if not items:
        return f"{header}\\n{divider}\\n›› No anime releases found today.\\n{divider}"

    # Always show Upcoming, regardless of the current clock time.
    blocks = [format_anime_entry(item) for item in items]
    message = header + "\\n" + divider + "\\n" + ("\\n" + divider + "\\n").join(blocks) + "\\n" + divider

    rendered_message = unescape(re.sub(r"<[^>]+>", "", message))
    if telegram_utf16_length(rendered_message) > 1024:
        raise ValueError(
            "The complete anime schedule exceeds Telegram's 1,024-character "
            "photo-caption limit. No anime were omitted."
        )
    return message


async def fetch_today_schedule():
    timeout = ClientTimeout(total=25)

    async with ClientSession(timeout=timeout) as session:
        async with session.get(SCHEDULE_URL) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)

    schedule = data.get("schedule", [])

    if not isinstance(schedule, list):
        raise ValueError(
            "Unexpected schedule format from SubsPlease."
        )

    return schedule


async def send_daily_schedule(
    chat_id,
    reply_to_message_id=None,
):
    """Send the banner and the complete compact schedule as one photo post."""
    try:
        now = datetime.now(IST)
        raw_schedule = await fetch_today_schedule()

        timeout = ClientTimeout(total=20)
        async with ClientSession(timeout=timeout) as session:
            items = await prepare_schedule(raw_schedule, now, session)

        # Remove accidental duplicate feed rows without dropping different episode times.
        unique = {}
        for item in items:
            key = (item["title"].casefold().strip(), item["datetime"])
            unique[key] = item
        items = sorted(unique.values(), key=lambda item: item["datetime"])
        message = build_schedule_message(items, now)

        photo = getattr(Var, "SCHEDULE_PHOTO", None)
        if not photo:
            try:
                photo = await __import__("bot.core.database", fromlist=["db"]).db.get_schedule_photo()
            except Exception:
                photo = None
        if not photo and BANNER_PATH.is_file():
            photo = str(BANNER_PATH)

        if not photo:
            LOGGER.error("Schedule banner is not configured; refusing to send a text-only or extra message.")
            return False

        await bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=message,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_message_id,
        )
        return True

    except Exception:
        LOGGER.exception("Failed to send today's anime schedule.")
        return False


async def upcoming_animes():
    if not getattr(Var, "SEND_SCHEDULE", False):
        LOGGER.info("Daily anime schedule is disabled.")
        return

    main_channel = getattr(Var, "MAIN_CHANNEL", None)
    if not main_channel:
        LOGGER.error("MAIN_CHANNEL is not configured.")
        return

    # One destination, one banner photo, one complete caption. No backup/extra post.
    await send_daily_schedule(main_channel)
