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
CAPTION_LIMIT = 1024


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
    """Count visible caption characters, excluding HTML tags and link URLs."""
    visible = re.sub(r"<[^>]+>", "", str(value))
    visible = unescape(visible)
    return len(visible.encode("utf-16-le")) // 2


def format_anime_entry(item):
    """One clean linked anime title followed by its local release time."""
    title = escape(style_e(item["title"]))
    time_text = escape(style_e(item["time"]))
    return f'<a href="{CHANNEL_URL}">{title}</a> - {time_text}'


def build_schedule_message(items, now):
    """Build the first poster caption chunk in the approved schedule style."""
    header = "✦ 𝗧𝗢𝗗𝗔𝗬 | 𝗔𝗡𝗜𝗠𝗘 𝗦𝗖𝗛𝗘𝗗𝗨𝗟𝗘"
    divider = "────────────────────"
    if not items:
        return f"{header}\n{divider}"

    blocks = [format_anime_entry(item) for item in items]
    return header + "\n" + divider + "\n" + ("\n" + divider + "\n").join(blocks) + "\n" + divider


def split_schedule_messages(items):
    """Split complete linked entries across Telegram caption/message limits."""
    divider = "────────────────────"
    if not items:
        return ["✦ 𝗧𝗢𝗗𝗔𝗬 | 𝗔𝗡𝗜𝗠𝗘 𝗦𝗖𝗛𝗘𝗗𝗨𝗟𝗘\n" + divider]
    caption_prefix = (
        "✦ 𝗧𝗢𝗗𝗔𝗬 | 𝗔𝗡𝗜𝗠𝗘 𝗦𝗖𝗛𝗘𝗗𝗨𝗟𝗘\n"
        + divider
        + "\n"
    )
    chunks = []
    current_entries = []
    current_prefix = caption_prefix
    current_limit = CAPTION_LIMIT

    for item in items:
        entry = format_anime_entry(item)
        candidate_entries = current_entries + [entry]
        candidate = current_prefix + ("\n" + divider + "\n").join(candidate_entries) + "\n" + divider

        if telegram_utf16_length(candidate) <= current_limit:
            current_entries.append(entry)
            continue

        if not current_entries:
            raise ValueError(
                "A single anime schedule entry exceeds Telegram's message limit."
            )

        chunks.append(
            current_prefix
            + ("\n" + divider + "\n").join(current_entries)
            + "\n" + divider
        )

        # Follow-up messages contain only anime names and times (plus the
        # existing visual divider); no repeated heading or explanatory text.
        current_prefix = ""
        current_limit = 4096
        current_entries = [entry]

        single = entry + "\n" + divider
        if telegram_utf16_length(single) > current_limit:
            raise ValueError(
                "A single anime schedule entry exceeds Telegram's message limit."
            )

    if current_entries:
        chunks.append(
            current_prefix
            + ("\n" + divider + "\n").join(current_entries)
            + "\n" + divider
        )

    return chunks


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
    """Send the poster and every schedule entry without caption truncation."""
    try:
        now = datetime.now(IST)
        raw_schedule = await fetch_today_schedule()

        timeout = ClientTimeout(total=20)
        async with ClientSession(timeout=timeout) as session:
            items = await prepare_schedule(raw_schedule, now, session)

        # Remove exact duplicate feed rows, preserving different airing times.
        unique = {}
        for item in items:
            key = (item["title"].casefold().strip(), item["datetime"])
            unique[key] = item
        items = sorted(unique.values(), key=lambda item: item["datetime"])

        chunks = split_schedule_messages(items)

        photo = getattr(Var, "SCHEDULE_PHOTO", None)
        if not photo:
            try:
                from bot.core.database import db
                photo = await db.get_schedule_photo()
            except Exception:
                photo = None
        if not photo and BANNER_PATH.is_file():
            photo = str(BANNER_PATH)

        if not photo:
            LOGGER.error("Schedule banner is not configured; refusing to send a text-only schedule.")
            return False

        sent_photo = await bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=chunks[0],
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_message_id,
        )

        # Overflow is sent as plain schedule rows only: no extra heading,
        # explanation, status, episode label, or TXT attachment.
        for chunk in chunks[1:]:
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=sent_photo.id,
            )

        LOGGER.info(
            "Daily anime schedule sent (%s anime, %s follow-up message(s)).",
            len(items),
            max(0, len(chunks) - 1),
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
