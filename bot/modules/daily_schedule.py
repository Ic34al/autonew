import asyncio
import logging
from datetime import datetime
from html import escape
from pathlib import Path

import pytz
from aiohttp import ClientSession, ClientTimeout

from bot import bot, Var
from bot.modules.schedule_image import create_schedule_image
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

        # First look for an exact English or romanized title match.
        match = next(
            (
                anime for anime in results
                if any(
                    (anime.get("title", {}).get(field) or "")
                    .casefold().strip() == normalized
                    for field in ("english", "romaji")
                )
            ),
            None,
        )

        # AniList search may return ambiguous matches.
if not match:
    cache[key] = original_title
    return original_title
        titles = match.get("title") or {}

        # English title has priority.
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
            "time": release_datetime.strftime(
                "%I:%M %p"
            ).lstrip("0"),
            "datetime": release_datetime,
            "episode": episode,
            "status": (
                "Aired"
                if now >= release_datetime
                else "Upcoming"
            ),
        })

    items.sort(key=lambda item: item["datetime"])
    return items


def format_anime_entry(item):
    title = escape(item["title"])
    linked_title = f'<a href="{CHANNEL_URL}">{title}</a>'

    status_icon = (
        "✅" if item["status"] == "Aired" else "⏳"
    )

    return (
        f'🌸 <b>»» {linked_title} ❞</b>\n'
        f'»» TIME : {item["time"]} IST\n'
        f'»» EPISODE : {escape(str(item["episode"]))}\n'
        f'»» STATUS : {status_icon} {item["status"]}'
    )


def build_schedule_message(items, now):
    """Build the complete daily anime schedule in one message."""

    header = (
        "✦ <b>TODAY | ANIME SCHEDULE</b> ✦\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📅 <b>{now.strftime('%A, %d %B %Y')}</b>\n"
        "🕒 <b>TIMEZONE:</b> Indian Standard Time (IST)\n\n"
    )

    footer = (
        f'\n\n✦ <a href="{CHANNEL_URL}">'
        '@Ongoing_Anime_Time</a> ✦'
    )

    if not items:
        return header + "No anime releases found for today." + footer

    body = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(
        format_anime_entry(item) for item in items
    )

    message = header + body + footer

    if len(message) > 4096:
        raise ValueError(
            f"Schedule exceeds Telegram's 4096-character limit: "
            f"{len(message)} characters."
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
    try:
        now = datetime.now(IST)
        raw_schedule = await fetch_today_schedule()

        timeout = ClientTimeout(total=20)

        async with ClientSession(timeout=timeout) as session:
            items = await prepare_schedule(
                raw_schedule,
                now,
                session,
            )

        message = build_schedule_message(items, now)

        image_path = None

        try:
            image_path = create_schedule_image(items, now)

            await bot.send_photo(
                chat_id=chat_id,
                photo=image_path,
                caption=message,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to_message_id,
            )

        except Exception as exc:
            logging.exception(
                "Schedule image sending failed: %s",
                exc,
            )

            await bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to_message_id,
                disable_web_page_preview=True,
            )

        finally:
            if image_path:
                from pathlib import Path
                Path(image_path).unlink(missing_ok=True)

        return True

    except Exception:
        LOGGER.exception(
            "Failed to send today's anime schedule."
        )

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ Could not fetch today's anime schedule. "
                    "Please try again later."
                ),
                reply_to_message_id=reply_to_message_id,
            )
        except Exception:
            LOGGER.exception(
                "Could not send the schedule error message."
            )

        return False


async def upcoming_animes():
    if not getattr(Var, "SEND_SCHEDULE", False):
        LOGGER.info("Daily anime schedule is disabled.")
        return

    main_channel = getattr(Var, "MAIN_CHANNEL", None)

    if not main_channel:
        LOGGER.error("MAIN_CHANNEL is not configured.")
        return

    await send_daily_schedule(main_channel)

    backup_channel = getattr(Var, "BACKUP_CHANNEL", None)

    if backup_channel:
        await send_daily_schedule(backup_channel)
