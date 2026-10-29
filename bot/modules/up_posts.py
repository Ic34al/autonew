from json import loads as jloads
from json import dumps as jdumps
from os import path as ospath, execl
from sys import executable
from bot import bot
from aiohttp import ClientSession
from bot import Var, bot, ffQueue
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait, MessageNotModified
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CallbackContext, CommandHandler
from pyrogram import filters
from bot.core.text_utils import TextEditor
from bot.core.reporter import rep
from bot.core.func_utils import decode, is_fsubbed, get_fsubs, editMessage, sendMessage, new_task, convertTime, getfeed
from asyncio import sleep as asleep, gather
from pyrogram.filters import command, private, user
from pyrogram import filters
import time
from datetime import datetime, timezone, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram.types import Message
from pyrogram.types import Message
import subprocess
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import FloodWait, MessageNotModified
from bot.core.database import db
from bot import bot, bot_loop, Var, ani_cache
import datetime as dt
import re
from lxml import html as lh
import logging
import pytz

DB_URI = Var.MONGO_URI
LOGGER = logging.getLogger(__name__)

TD_SCHR = None

def get_readable_time(seconds: int) -> str:
    count = 0
    up_time = ""
    time_list = []
    time_suffix_list = ["s", "m", "h", "days"]
    while count < 4:
        count += 1
        remainder, result = divmod(seconds, 60) if count < 3 else divmod(seconds, 24)
        if seconds == 0 and remainder == 0:
            break
        time_list.append(int(result))
        seconds = int(remainder)
    hmm = len(time_list)
    for x in range(hmm):
        time_list[x] = str(time_list[x]) + time_suffix_list[x]
    if len(time_list) == 4:
        up_time += f"{time_list.pop()}, "
    time_list.reverse()
    up_time += ":".join(time_list)
    return up_time

async def get_db_response_time() -> float:
    start = time.time()
    await db.command("ping")
    end = time.time()
    return round((end - start) * 1000, 2)

async def get_ping(bot: bot) -> float:
    start = time.time()
    await bot.get_me()
    end = time.time()
    return round((end - start) * 1000, 2)  

@bot.on_message(command('shell') & private & user(Var.ADMINS))
@new_task
async def shell(client, message):
    cmd = message.text.split(" ", 1)
    if len(cmd) == 1:
        await message.reply_text("<blockquote>No command to execute was given.</blockquote>")
        return
    cmd = cmd[1]
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True
    )
    stdout, stderr = process.communicate()
    reply = ""
    stderr = stderr.decode()
    stdout = stdout.decode()
    if stdout:
        reply += f"*ᴘᴀʀᴀᴅᴏx \n stdout*\n`{stdout}`\n"
        LOGGER.info(f"Shell - {cmd} - {stdout}")
    if stderr:
        reply += f"*ᴘᴀʀᴀᴅᴏx \n stderr*\n`{stderr}`\n"
        LOGGER.error(f"Shell - {cmd} - {stderr}")
    if len(reply) > 3000:
        with open("shell_output.txt", "w") as file:
            file.write(reply)
        with open("shell_output.txt", "rb") as doc:
            await message.reply_document(
                document=doc,
                file_name=doc.name,
            )
    else:
        await message.reply_text(reply, parse_mode=ParseMode.MARKDOWN)

@bot.on_message(filters.command("ongoing"))
@new_task
async def ongoing_animes(client, message):
    if not getattr(Var, "SEND_SCHEDULE", False):
        await message.reply_text(
            "The anime schedule feature is disabled."
        )
        return

    from bot.modules.daily_schedule import send_daily_schedule

    await send_daily_schedule(
        chat_id=message.chat.id,
        reply_to_message_id=message.id,
    )

async def upcoming_animes():
    global TD_SCHR
    if not getattr(Var, "SEND_SCHEDULE", True):
        return

    schedule_type = getattr(Var, "SCHEDULE_TYPE", "sub")

    async def _send_schedule(test: bool = False, reply_message=None):
        try:
            sched_photo = getattr(Var, 'SCHEDULE_PHOTO', None)
            
            ist = pytz.timezone('Asia/Kolkata')
            current_time_ist = datetime.now(ist)
            current_day = current_time_ist.strftime("%A")

            if schedule_type == "dub":
                async with ClientSession() as ses:
                    res = await ses.get("https://myanimelist.net/forum/?topicid=1692966")
                    if res.status != 200:
                        await rep.report("Failed to fetch dub schedule from MAL", "error")
                        if reply_message:
                            await reply_message.reply_text("Failed to fetch dub schedule from MAL.")
                        return False

                    html_content = await res.text()
                    tree = lh.fromstring(html_content)
                    text = "<b><blockquote>Today's Anime Schedule</blockquote></b>\n\n"

                    day_sections = tree.xpath(f"//li[contains(text(), '{current_day}')]")
                    if day_sections:
                        anime_list = day_sections[0].xpath(".//following-sibling::ul[1]/li")
                        for anime in anime_list:
                            title = anime.xpath(".//a/text()")
                            if title:
                                info = anime.xpath(".//text()")
                                episodes = next((i for i in info if "Episodes:" in i), "")
                                ep_nums = re.search(r'Episodes:\s*(\d+)\s*/\s*(\d+|\?+)', episodes)
                                if ep_nums:
                                    current_ep = ep_nums.group(1).zfill(2)
                                    total_ep = ep_nums.group(2)
                                    ep_str = f"{current_ep}/{total_ep}"
                                else:
                                    ep_str = "??/??"
                                CHANNEL = Var.BRAND_UNAME.strip('@')
                                text += f'<b><blockquote>EP: {ep_str} — <a href="https://t.me/{CHANNEL}/">{title[0]}</a></blockquote></b>\n'
                    else:
                        await rep.report(f"No dub schedule entries found for {current_day}", "warning")
                        if reply_message:
                            await reply_message.reply_text(f"No dub schedule entries found for {current_day}.")
                        return False

            else:
                async with ClientSession() as ses:
                    res = await ses.get("https://subsplease.org/api/?f=schedule&h=true&tz=Asia/Kolkata")
                    if res.status != 200:
                        await rep.report("Failed to fetch schedule from SubsPlease", "error")
                        if reply_message:
                            await reply_message.reply_text("Failed to fetch schedule from SubsPlease.")
                        return False
                    aniContent = jloads(await res.text()).get("schedule", [])

                if not aniContent:
                    await rep.report("No anime schedule found for today", "warning")
                    if reply_message:
                        await reply_message.reply_text("No anime schedule found for today.")
                    return False

                text = "<blockquote><b>Today's Anime Releases Schedule:</b></blockquote>\n\n"
                for i in aniContent:
                    try:
                        aname = TextEditor(i["title"])
                        await aname.load_anilist()
                        title = aname.adata.get('title', {}).get('english') or i['title']
                    except Exception as e:
                        LOGGER.warning(f"Failed to load AniList data for {i['title']}: {e}")
                        title = i['title']
                    CHANNEL = Var.BRAND_UNAME.strip('@')
                    text += (
                        f'<b><blockquote>{i["time"]}  ── <a href="https://t.me/{CHANNEL}/">{title}</a></blockquote></b>\n'
                    )

            if not sched_photo:
                try:
                    sched_photo = await db.get_schedule_photo()
                except Exception as e:
                    LOGGER.warning(f"Failed to get schedule photo from DB: {e}")
                    sched_photo = None

            try:
                if sched_photo and str(sched_photo).strip() and str(sched_photo) not in ['0', 'None', '']:
                    caption = text
                    TD_SCHR = await bot.send_photo(
                        Var.MAIN_CHANNEL,
                        sched_photo,
                        caption=caption
                    )
                else:
                    TD_SCHR = await bot.send_message(Var.MAIN_CHANNEL, text)
                try:
                    await TD_SCHR.pin(disable_notification=True)
                except Exception as e:
                    LOGGER.warning(f"Failed to pin schedule message in main channel: {e}")
            except Exception as e:
                await rep.report(f"Failed to send schedule to main channel: {e}", "error")
                if reply_message:
                    await reply_message.reply_text(f"Failed to send schedule to main channel: {e}")
                return False

            backup_id = getattr(Var, 'BACKUP_CHANNEL', 0)
            if backup_id:
                try:
                    if sched_photo and str(sched_photo).strip() and str(sched_photo) not in ['0', 'None', '']:
                        caption = text
                        backup_msg = await bot.send_photo(
                            backup_id,
                            sched_photo,
                            caption=caption
                        )
                    else:
                        backup_msg = await bot.send_message(backup_id, text)
                    try:
                        await backup_msg.pin(disable_notification=True)
                    except Exception as e:
                        LOGGER.warning(f"Failed to pin schedule message in backup channel: {e}")
                except Exception as e:
                    await rep.report(f"Failed to send schedule to backup channel {backup_id}: {e}", "warning")

            if reply_message:
                await reply_message.reply_text("Schedule sent successfully.")
            return True

        except Exception as err:
            await rep.report(f"Error in _send_schedule: {str(err)}", "error")
            if reply_message:
                await reply_message.reply_text(f"Error sending schedule: {err}")
            return False

    try:
        await _send_schedule()
    except Exception as err:
        await rep.report(f"Error in upcoming_animes: {str(err)}", "error")
    
    if not ffQueue.empty():
        await ffQueue.join()
    await rep.report("Auto Restarting..!!", "info")
    execl(executable, executable, "-m", "bot")

async def update_shdr(name, link):
    global TD_SCHR
    if TD_SCHR is not None:
        try:
            TD_lines = TD_SCHR.text.split('\n') if TD_SCHR.text else []
            for i, line in enumerate(TD_lines):
                if line.startswith(f"📌 {name}"):
                    if i + 2 < len(TD_lines):
                        TD_lines[i+2] = f"    • <b>Status :</b> ✅ __Uploaded__\n    • <b>Link :</b> {link}"
            await TD_SCHR.edit_text("\n".join(TD_lines))
        except Exception as e:
            LOGGER.error(f"Failed to update schedule: {e}")
