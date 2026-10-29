from asyncio import gather, create_task, sleep as asleep, Event
from asyncio.subprocess import PIPE
from os import path as ospath, system, remove
from aiofiles import open as aiopen
from aiofiles.os import remove as aioremove
from traceback import format_exc
from base64 import urlsafe_b64encode
from time import time
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.errors import UserAlreadyParticipant, ChannelInvalid, PeerIdInvalid
import re
from bot import bot, bot_loop, Var, ani_cache, ffQueue, ffLock, ff_queued, LOGS
from .tordownload import TorDownloader
from .database import db
from .func_utils import getfeed, encode, editMessage, sendMessage, convertBytes
from .text_utils import TextEditor, MangaLister
from .ffencoder import FFEncoder
from .tguploader import TgUploader
from .text_utils import get_manga_filename, parse_manga_title, get_manga_caption, obfuscate_text_for_copyright
from .reporter import rep
import datetime
import asyncio
from pyrogram.errors import FloodWait
import requests
import os
import feedparser
from fpdf import FPDF
import httpx
import subprocess
from PIL import Image
import shutil
import time
import hashlib

MAX_ABOUT_LEN = 255

btn_formatter = {
    'HDRi':'𝗛𝗗𝗥𝗶𝗽',
    '1080':'𝟭𝟬𝟒𝟬𝗣', 
    '720':'𝟳𝟮𝟬𝗣',
    '480':'𝟰𝟴𝟬𝗣',
    '360':'𝟯𝟔𝟬𝗣',
    '240':'𝟮𝟒𝟬𝗣',
    '144':'𝟭𝟒𝟒𝗣'
}

#RAW_BTN = "𝗥𝗔𝗪"
RAW_BTN = "𝗛𝗗𝗥𝗶𝗽"
MANGA_BTN = "𝗥𝗲𝗮𝗱 𝗡𝗼𝘄"
MANGA_READ_BUTTON = "𝗥𝗲𝗮𝗱 𝗡𝗼𝘄"
MANGA_CHNL_BUTTON = "𝗝𝗼𝗶𝗻 𝗖𝗵𝗮𝗻𝗻𝗲𝗹"
DOWNLOAD_BTN = "𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 𝗡𝗼𝘄"
DOWNLOAD_ANIME_BTN = "𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 𝗡𝗼𝘄"
DOWNLAOD_CHNL_BTN = "𝗝𝗼𝗶𝗻 𝗖𝗵𝗮𝗻𝗻𝗲𝗹"

import hashlib
import time
import os
import shutil

def generate_unique_dir(name, torrent_url):
    safe_name = "".join(c for c in name if c.isalnum() or c in " _-").strip()
    safe_name = safe_name[:40]
    url_hash = hashlib.md5(torrent_url.encode()).hexdigest()[:8]
    timestamp = int(time.time())
    return f"./downloads/{safe_name}_{url_hash}_{timestamp}"

def find_video_files(folder):
    video_exts = ('.mp4', '.mkv', '.avi', '.webm', '.wmv')
    files = []
    for root, dirs, filenames in os.walk(folder):
        for f in filenames:
            if f.lower().endswith(video_exts):
                files.append(os.path.join(root, f))
    return sorted(files)

def patch_caption_episode(caption, episode_count):
    try:
        episode_count = int(episode_count)
    except Exception:
        episode_count = 1
    return re.sub(
        r"(❍\s*𝗘𝗽𝗶𝘀𝗼𝗱𝗲:\s*<i>)[^<]+(</i>)",
        f"\\1{episode_count}\\2",
        caption
    )

async def detect_audio_type(video_path):
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=language", "-of", "csv=p=0", video_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        audio_langs = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        unique_langs = set(audio_langs)
        if len(unique_langs) > 2:
            return "Multi"
        elif len(unique_langs) == 2:
            return "Dual"
        elif len(unique_langs) == 1:
            return "Dub" if "eng" in unique_langs else "Sub"
        else:
            return "Sub"
    except Exception as e:
        print(f"Audio detection failed: {e}")
        return "Sub"

async def process_batch_anime(anime_name, torrent_url, audio):
    import re
    import time
    import os
    import shutil
    from os import path as ospath
    import asyncio

    cleaned_name = clean_anime_title(anime_name)
    safe_title = re.sub(r'[\\/*?:"<>|\'\n\r\t]', '_', cleaned_name)
    unique_dir = f"./downloads/{safe_title}"
    os.makedirs(unique_dir, exist_ok=True)

    aniInfo = TextEditor(cleaned_name)
    await aniInfo.load_info()
    all_banners = await db.list_anime_banners()
    banner_url = None
    for banner_key, url in all_banners:
        if banner_key.lower() in anime_name.lower():
            banner_url = url
            break
    if banner_url:
        poster_url = banner_url
    elif hasattr(Var, 'ANIME') and Var.ANIME in anime_name:
        poster_url = Var.CUSTOM_BANNER
    else:
        poster_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")

    channel_to_use = await get_or_create_anime_channel(anime_name, aniInfo)

    channel_valid = False
    for attempt in range(3):
        if await validate_channel_id(channel_to_use):
            channel_valid = True
            break
        await asyncio.sleep(2)
    if not channel_valid:
        await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
        channel_to_use = Var.MAIN_CHANNEL

    dl_path = await TorDownloader(unique_dir).download(torrent_url)
    if not dl_path or not ospath.exists(dl_path):
        await rep.report(f"Batch download failed for {anime_name}", "error")
        return

    batch_dir = dl_path if ospath.isdir(dl_path) else unique_dir
    video_files = find_video_files(batch_dir)
    episode_count = len(video_files)
    if not video_files:
        await rep.report(f"No video files found in batch for {anime_name}", "error")
        return

    aniInfo.pdata["audio_type"] = audio
    caption = await aniInfo.get_caption()

    caption = re.sub(
        r"(❍\s*𝗘𝗽𝗶𝘀𝗼𝗱𝗲:\s*<i>)[^<]+(</i>)",
        lambda m: f"{m.group(1)}1-{episode_count}{m.group(2)}",
        caption
    )

    try:
        fontchanger_status = await db.get_fontchanger()
        if fontchanger_status == "on":
            caption = obfuscate_text_for_copyright(caption)
    except Exception as e:
        await rep.report(f"Error applying font changer: {e}", "warning")

    await asleep(1.5)
    await rep.report(f"Using channel: {channel_to_use} for anime: {cleaned_name}", "info")
    await asleep(1.5)

    stat_msg = await bot.send_message(
        chat_id=channel_to_use,
        text=f"<blockquote>‣ <b>Anime Name :</b> <b><i>{anime_name}</i></b></blockquote>\n\n<pre><i>Downloading...</i></pre>"
    )

    required_qualities = ("480", "720", "1080")
    uploaded_by_quality = {}
    for qual in Var.QUALS:
        qual_msg_ids = []
        os.makedirs("encode", exist_ok=True)
        for idx, vfile in enumerate(video_files, 1):
            ep_num = str(idx).zfill(2)
            aniInfo.pdata["episode_number"] = ep_num
            ep_upname = await aniInfo.get_upname(qual)
            out_path = f"encode/{ep_upname}"

            encoded_path = await FFEncoder(stat_msg, vfile, ep_upname, qual).start_encode()
            if not encoded_path or not ospath.exists(encoded_path):
                await rep.report(f"Encoding failed for {vfile} ({qual}p)", "error")
                continue

            try:
                msg = await TgUploader(stat_msg).upload(encoded_path, qual)
            except Exception as e:
                await rep.report(f"Upload failed for {ep_upname} ({qual}p): {e}", "error")
                msg = None
            if msg:
                qual_msg_ids.append(msg.id)
            try:
                if ospath.exists(encoded_path):
                    os.remove(encoded_path)
            except Exception:
                pass

        if len(qual_msg_ids) == episode_count:
            first_id = min(qual_msg_ids)
            last_id = max(qual_msg_ids)
            batch_string = f"get-{first_id * abs(Var.FILE_STORE)}-{last_id * abs(Var.FILE_STORE)}"
            uploaded_by_quality[qual] = f"https://t.me/{Var.BOT_USERNAME}?start={await encode(batch_string)}"
        else:
            await rep.report(
                f"{qual}p incomplete for {cleaned_name}: "
                f"{len(qual_msg_ids)}/{episode_count} episodes uploaded",
                "error",
            )

    if all(q in uploaded_by_quality for q in required_qualities):
        buttons = [[
            InlineKeyboardButton(
                btn_formatter.get(q, q + "p"),
                url=uploaded_by_quality[q],
            )
            for q in required_qualities
        ]]
        # Publish the anime post
        try:
            post_msg = await bot.send_photo(
                chat_id=channel_to_use,
                photo=poster_url,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except Exception as e:
            await rep.report(f"Failed to send batch poster: {e}", "error")
            post_msg = None

        # Send animated Ongoing Anime Time banner beneath the anime post.
        if post_msg:
            try:
                banner_path = "ongoing_anime_time.gif"

                if os.path.isfile(banner_path):

                    await bot.send_animation(
                        chat_id=channel_to_use,
                        animation=banner_path,
                        reply_to_message_id=post_msg.id,
                    )

                else:

                    await rep.report(
                        f"Banner file not found: {banner_path}",
                        "warning",
                    )

            except Exception as e:

                await rep.report(
                    f"Failed to send Ongoing Anime Time GIF: {e}",
                    "warning",
                )

        # Preserve the existing main-channel mirroring
        await mirror_to_main_channel(
            post_msg=post_msg,
            photo_url=poster_url,
            caption=caption,
            channel_to_use=channel_to_use,
        )
    else:
        await rep.report(
            f"Final post withheld: one or more required qualities "
            f"are missing for {cleaned_name}",
            "error",
        )

    await stat_msg.delete()
    import logging
    if ospath.exists(batch_dir):
        logging.info(f"[CLEANUP] Removing batch_dir: {batch_dir}")
        shutil.rmtree(batch_dir, ignore_errors=True)
    await rep.report(f"Batch processing done and cleaned for {anime_name}", "info")

# rest of file unchanged below

async def send_sticker_to_channel(channel, sticker_id=None):

    """Send the animated Ongoing Anime Time GIF banner."""

    if not getattr(Var, "SEND_STICKER", False):

        return

    banner_path = "ongoing_anime_time.gif"

    try:

        if not os.path.isfile(banner_path):

            await rep.report(

                f"Banner file not found: {banner_path}",

                "warning",

            )

            return

        await bot.send_animation(

            chat_id=channel,

            animation=banner_path,

        )

    except Exception as e:

        await rep.report(

            f"Failed to send Ongoing Anime Time GIF: {e}",

            "warning",

        )

# Existing file continues unchanged after this patch.
