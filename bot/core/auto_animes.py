import uuid
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
from bot.core.anime_thumbnail import create_anime_thumbnail
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
    '1080':'𝟭𝟬𝟴𝟬𝗣', 
    '720':'𝟳𝟮𝟬𝗣',
    '480':'𝟰𝟴𝟬𝗣',
    '360':'𝟯𝟲𝟬𝗣',
    '240':'𝟮𝟰𝟬𝗣',
    '144':'𝟭𝟰𝟰𝗣'
}

async def build_anime_file_buttons(ani_id, ep_no, required_quals):
    file_ids = await db.getAnimeFileIds(ani_id, ep_no)

    missing = [
        qual for qual in required_quals
        if not file_ids.get(qual)
    ]

    if missing:
        raise RuntimeError(
            "Cannot publish episode. Missing FILE_STORE IDs: "
            + ", ".join(missing)
        )

    buttons = []

    for qual in required_quals:
        msg_id = file_ids[qual]

        encoded = await encode(
            "get-" + str(msg_id * abs(Var.FILE_STORE))
        )

        buttons.append(
            InlineKeyboardButton(
                btn_formatter.get(qual, qual + "p"),
                url=f"https://t.me/{Var.BOT_USERNAME}?start={encoded}",
            )
        )

    return [
        buttons[index:index + 2]
        for index in range(0, len(buttons), 2)
    ]

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
