
import asyncio
import logging
from datetime import datetime
from html import escape
from pathlib import Path

import pytz
from aiohttp import ClientSession, ClientTimeout
from PIL import Image, ImageDraw, ImageFont, ImageOps

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
      nextAiringEpisode {
        episode
        airingAt
      }
    }
  }
}
"""

BANNER_PATH = Path("today_anime_schedule.JPG")
POSTER_PATH = Path("/tmp/today_anime_schedule_poster.jpg")
CHANNEL_URL = "https://t.me/Ongoing_Anime_Time"
MAX_POSTER_ITEMS = 8


def parse_release_time(value):
    value = str(value or "").strip().upper()

    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            pass

    return None


async def get_english_title(session, original_title, cache):
    """Prefer AniList English title, then romanized title."""
    original_title = str(original_title or "").strip()

    if not original_title:
        return "Unknown anime"

    cache_key = original_title.casefold()

    if cache_key in cache:
        return cache[cache_key]

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
                cache[cache_key] = original_title
                return original_title

            data = await response.json(content_type=None)

        results = (
            data.get("data", {})
            .get("Page", {})
            .get("media", [])
        )

        if not results:
            cache[cache_key] = original_title
            return original_title

        normalized = original_title.casefold().strip()

        # Prefer a matching romanized title when possible.
        exact_match = next(
            (
                anime for anime in results
                if (
                    anime.get("title", {}).get("romaji") or ""
                ).casefold().strip() == normalized
            ),
            None,
        )

        # If no exact match exists, use the first AniList result.
        match = exact_match or results[0]
        titles = match.get("title") or {}

        # English title is always the first preference.
        english_title = (
            titles.get("english")
            or titles.get("romaji")
            or original_title
        )

        cache[cache_key] = english_title
        return english_title

    except Exception:
        LOGGER.warning(
            "AniList title lookup failed for %s",
            original_title,
            exc_info=True,
        )
        cache[cache_key] = original_title
        return original_title


async def prepare_schedule(schedule, now, session):
    items = []
    title_cache = {}

    # Avoid sending too many simultaneous requests to AniList.
    semaphore = asyncio.Semaphore(4)

    async def resolve_title(original_title):
        async with semaphore:
            return await get_english_title(
                session, original_title, title_cache
            )

    prepared = []

    for item in schedule:
        original_title = str(
            item.get("title") or "Unknown anime"
        ).strip()

        release_time = parse_release_time(item.get("time"))

        if release_time is None:
            LOGGER.warning(
                "Skipping item with invalid release time: %s",
                original_title,
            )
            continue

        release_datetime = IST.localize(
            datetime.combine(now.date(), release_time)
        )

        prepared.append({
            "original_title": original_title,
            "datetime": release_datetime,
            "episode": (
                item.get("episode")
                or item.get("episodeNumber")
                or item.get("episode_number")
                or "TBA"
            ),
        })

    titles = await asyncio.gather(
        *[
            resolve_title(item["original_title"])
            for item in prepared
        ],
        return_exceptions=True,
    )

    for item, title in zip(prepared, titles):
        if isinstance(title, Exception):
            title = item["original_title"]

        episode = str(item["episode"])
        if episode.isdigit():
            episode = episode.zfill(2)

        release_datetime = item["datetime"]

        items.append({
            "title": str(title),
            "time": release_datetime.strftime(
                "%I:%M %p"
            ).lstrip("0"),
            "datetime": release_datetime,
            "episode": episode,
            "status": (
                "Aired" if now >= release_datetime else "Upcoming"
            ),
        })

    items.sort(key=lambda item: item["datetime"])
    return items


def build_linked_message(items, now):
    lines = [
        "✦ <b>TODAY | ANIME SCHEDULE</b> ✦",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📅 <b>{now.strftime('%A, %d %B %Y')}</b>",
        "🕒 <b>TIMEZONE:</b> Indian Standard Time (IST)",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if not items:
        lines.append("No valid anime releases were found today.")
        return "\n".join(lines)

    for item in items:
        title = escape(item["title"])
        linked_title = (
            f'<a href="{CHANNEL_URL}">{title}</a>'
        )

        status_icon = (
            "✅" if item["status"] == "Aired" else "⏳"
        )

        lines.extend([
            f"🌸 <b>»» {linked_title}</b>",
            f"»» <b>TIME:</b> {item['time']} IST",
            f"»» <b>EPISODE:</b> {escape(str(item['episode']))}",
            f"»» <b>STATUS:</b> {status_icon} {item['status']}",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ])

    lines.append(
        f'✦ <a href="{CHANNEL_URL}">'
        "@Ongoing_Anime_Time</a> ✦"
    )

    return "\n".join(lines)


def get_font(size, bold=False):
    candidates = (
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        ]
        if bold
        else [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
    )

    for filename in candidates:
        if Path(filename).is_file():
            return ImageFont.truetype(filename, size)

    return ImageFont.load_default()


def create_schedule_poster(items, now):
    if not BANNER_PATH.is_file():
        raise FileNotFoundError(
            f"Image missing: {BANNER_PATH}"
        )

    width = 1200
    visible_items = items[:MAX_POSTER_ITEMS]
    extra_count = max(0, len(items) - MAX_POSTER_ITEMS)

    header_height = 440
    row_height = 154
    footer_height = 155

    height = (
        header_height
        + len(visible_items) * row_height
        + footer_height
        + (65 if extra_count else 0)
    )

    background = Image.new(
        "RGB", (width, height), (15, 17, 29)
    )

    with Image.open(BANNER_PATH) as source:
        artwork = ImageOps.fit(
            source.convert("RGB"),
            (width, 440),
            method=Image.Resampling.LANCZOS,
        )
        background.paste(artwork, (0, 0))

    background = background.convert("RGBA")

    overlay = Image.new(
        "RGBA", (width, 440), (0, 0, 0, 0)
    )
    overlay_draw = ImageDraw.Draw(overlay)

    for y_pos in range(240, 440):
        alpha = int(190 * ((y_pos - 240) / 200))
        overlay_draw.line(
            (0, y_pos, width, y_pos),
            fill=(12, 14, 26, alpha),
        )

    background.alpha_composite(overlay, (0, 0))
    draw = ImageDraw.Draw(background)

    gold = (255, 202, 103)
    pink = (244, 91, 169)
    white = (246, 246, 252)
    muted = (174, 178, 199)
    card = (34, 34, 52, 255)

    title_font = get_font(39, bold=True)
    date_font = get_font(23, bold=True)
    anime_font = get_font(29, bold=True)
    detail_font = get_font(21)
    small_font = get_font(19, bold=True)

    y_pos = 462

    draw.text(
        (width // 2, y_pos),
        "✦ TODAY | ANIME SCHEDULE ✦",
        font=title_font,
        fill=white,
        anchor="mt",
    )

    y_pos += 62
    date_text = now.strftime("%A, %d %B %Y").upper()

    draw.line(
        (55, y_pos + 12, 300, y_pos + 12),
        fill=pink,
        width=3,
    )
    draw.line(
        (900, y_pos + 12, 1145, y_pos + 12),
        fill=pink,
        width=3,
    )
    draw.text(
        (width // 2, y_pos),
        date_text,
        font=date_font,
        fill=muted,
        anchor="mt",
    )

    y_pos += 48

    for item in visible_items:
        top = y_pos
        bottom = y_pos + row_height - 12

        draw.rounded_rectangle(
            (35, top, width - 35, bottom),
            radius=18,
            fill=card,
            outline=(65, 52, 79, 255),
            width=2,
        )

        draw.rounded_rectangle(
            (48, top + 17, 58, bottom - 17),
            radius=5,
            fill=pink,
        )

        # Wrap long English titles across at most two lines.
        words = item["title"].split()
        title_lines = []
        current = ""

        for word in words:
            candidate = f"{current} {word}".strip()

            if (
                draw.textlength(candidate, font=anime_font) > 1020
                and current
            ):
                title_lines.append(current)
                current = word
            else:
                current = candidate

        if current:
            title_lines.append(current)

        title_y = top + 14

        for title_line in title_lines[:2]:
            draw.text(
                (78, title_y),
                "»» " + title_line,
                font=anime_font,
                fill=gold,
            )
            title_y += 34

        detail_y = bottom - 43

        status_color = (
            (111, 222, 155)
            if item["status"] == "Aired"
            else (255, 204, 113)
        )

        draw.text(
            (78, detail_y),
            f"TIME: {item['time']}",
            font=detail_font,
            fill=white,
        )
        draw.text(
            (385, detail_y),
            f"EPISODE: {item['episode']}",
            font=detail_font,
            fill=white,
        )
        draw.text(
            (700, detail_y),
            f"STATUS: {item['status']}",
            font=small_font,
            fill=status_color,
        )

        draw.line(
            (75, bottom + 5, width - 75, bottom + 5),
            fill=pink,
            width=2,
        )

        y_pos += row_height

    if extra_count:
        draw.text(
            (width // 2, y_pos),
            f"+ {extra_count} MORE RELEASES — SEE MESSAGE BELOW",
            font=small_font,
            fill=gold,
            anchor="mt",
        )

    footer_y = height - footer_height + 15

    draw.line(
        (45, footer_y, width - 45, footer_y),
        fill=pink,
        width=3,
    )

    draw.text(
        (width // 2, footer_y + 24),
        "◷ TIMEZONE: INDIAN STANDARD TIME (IST)",
        font=date_font,
        fill=muted,
        anchor="mt",
    )

    draw.text(
        (width // 2, footer_y + 76),
        "✦ @Ongoing_Anime_Time ✦",
        font=date_font,
        fill=pink,
        anchor="mt",
    )

    background.convert("RGB").save(
        POSTER_PATH,
        "JPEG",
        quality=94,
        optimize=True,
    )

    return POSTER_PATH


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


async def send_daily_schedule(chat_id, reply_to_message_id=None):
    try:
        now = datetime.now(IST)
        raw_schedule = await fetch_today_schedule()

        timeout = ClientTimeout(total=20)

        async with ClientSession(timeout=timeout) as session:
            items = await prepare_schedule(
                raw_schedule, now, session
            )

        message_text = build_linked_message(items, now)
        poster_message = None

        # Send the designed image first.
        try:
            poster = create_schedule_poster(items, now)

            poster_message = await bot.send_photo(
                chat_id=chat_id,
                photo=str(poster),
                caption="✦ <b>TODAY | ANIME SCHEDULE</b> ✦",
                parse_mode=ParseMode.HTML,
                reply_to_message_id=reply_to_message_id,
            )

        except Exception:
            LOGGER.exception(
                "Poster failed; the text schedule will still be sent."
            )

        # Keep HTML links intact while splitting long messages.
        blocks = message_text.split(
            "━━━━━━━━━━━━━━━━━━━━"
        )

        chunks = []
        current = ""

        for block in blocks:
            block = block.strip()

            if not block:
                continue

            candidate = (
                current + "\n━━━━━━━━━━━━━━━━━━━━\n" + block
                if current
                else block
            )

            if len(candidate) > 3500 and current:
                chunks.append(current)
                current = block
            else:
                current = candidate

        if current:
            chunks.append(current)

        for index, chunk in enumerate(chunks):
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=(
                    poster_message.id
                    if index == 0 and poster_message
                    else reply_to_message_id
                ),
                disable_web_page_preview=True,
            )

        return True

    except Exception:
        LOGGER.exception(
            "Failed to build or send anime schedule."
        )

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ Could not fetch today's anime schedule.\n"
                    "Please try again later."
                ),
                reply_to_message_id=reply_to_message_id,
            )
        except Exception:
            LOGGER.exception(
                "Could not send schedule error message."
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
