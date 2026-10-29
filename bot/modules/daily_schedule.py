import logging
from datetime import datetime
from html import escape
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

BANNER_PATH = Path("today_anime_schedule.JPG")


def parse_release_time(value):
    value = str(value or "").strip().upper()

    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue

    return None


def build_schedule_text(schedule, now):
    lines = [
        "✦ <b>TODAY | ANIME SCHEDULE</b> ✦",
        "━━━━━━━━━━━━━━━━━━",
        f"📅 <b>DATE:</b> {now.strftime('%A, %d %B %Y')}",
        "🕒 <b>TIMEZONE:</b> Indian Standard Time (IST)",
        "━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if not schedule:
        lines.append("No anime releases were found for today.")
        return "\n".join(lines)

    parsed_items = []

    for item in schedule:
        title = str(item.get("title") or "Unknown anime").strip()
        release_time = parse_release_time(item.get("time"))

        if release_time is None:
            LOGGER.warning(
                "Skipping anime with invalid release time: %s",
                title,
            )
            continue

        parsed_items.append((release_time, title))

    parsed_items.sort(key=lambda item: item[0])

    if not parsed_items:
        lines.append("No valid anime release times were returned.")
        return "\n".join(lines)

    for release_time, title in parsed_items:
        release_datetime = IST.localize(
            datetime.combine(now.date(), release_time)
        )

        status = (
            "✅ Aired"
            if now >= release_datetime
            else "⏳ Upcoming"
        )

        display_time = release_datetime.strftime(
            "%I:%M %p"
        ).lstrip("0")

        lines.extend([
            f"❯ <b>{escape(title)}</b>",
            f"» <b>TIME:</b> {display_time} IST",
            f"» <b>STATUS:</b> {status}",
            "━━━━━━━━━━━━━━━━━━",
            "",
        ])

    lines.append("✦ <i>Schedule times are shown in IST.</i>")
    return "\n".join(lines)


async def fetch_today_schedule():
    timeout = ClientTimeout(total=25)

    async with ClientSession(timeout=timeout) as session:
        async with session.get(SCHEDULE_URL) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)

    schedule = data.get("schedule", [])

    if not isinstance(schedule, list):
        raise ValueError("Unexpected schedule format from SubsPlease.")

    return schedule


async def send_daily_schedule(chat_id, reply_to_message_id=None):
    try:
        now = datetime.now(IST)
        schedule = await fetch_today_schedule()
        message_text = build_schedule_text(schedule, now)

        banner_message = None

        # Send photo with the schedule title.
        if BANNER_PATH.is_file():
            try:
                banner_message = await bot.send_photo(
                    chat_id=chat_id,
                    photo=str(BANNER_PATH),
                    caption="✦ <b>TODAY'S ANIME SCHEDULE</b> ✦",
                    parse_mode=ParseMode.HTML,
                    reply_to_message_id=reply_to_message_id,
                )
            except Exception:
                LOGGER.exception(
                    "Could not send schedule photo; sending text instead."
                )

        # Split long schedules into Telegram-sized messages.
        chunks = []
        current_chunk = ""

        for line in message_text.splitlines():
            candidate = f"{current_chunk}\n{line}".strip()

            if len(candidate) > 3900 and current_chunk:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk = candidate

        if current_chunk:
            chunks.append(current_chunk)

        for index, chunk in enumerate(chunks):
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                reply_to_message_id=(
                    banner_message.id
                    if index == 0 and banner_message
                    else reply_to_message_id
                ),
                disable_web_page_preview=True,
            )

        return True

    except Exception:
        LOGGER.exception("Failed to build or send anime schedule")

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
            LOGGER.exception("Could not send schedule error message")

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
