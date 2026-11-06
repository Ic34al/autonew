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
from .func_utils import getfeed, getfeeds, encode, editMessage, sendMessage, convertBytes
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

# Scheduled releases take priority over manual /search queues.
AUTO_UPLOAD_PRIORITY_LOCK = asyncio.Lock()
AUTO_UPLOAD_PRIORITY_PENDING = asyncio.Event()

MAX_ABOUT_LEN = 255

btn_formatter = {
    "HDRi": "HDRip",
    "1080": "1080p",
    "720": "720p",
    "480": "480p",
    "240": "240p",
    "144": "144p"
}

RAW_BTN = "HDRip"
MANGA_BTN = "Read Now"
MANGA_READ_BUTTON = "Read Now"
MANGA_CHNL_BUTTON = "Join Channel"

DOWNLOAD_BTN = "Download Now"
DOWNLOAD_ANIME_BTN = "• ᴄʟɪᴄᴋ ʜᴇʀᴇ ᴛᴏ ᴡᴀᴛᴄʜ •"
DOWNLAOD_CHNL_BTN = "Join Channel"

async def build_anime_watch_markup(channel_id):
    """Create one join-request button for the anime's dedicated channel."""
    invite = await bot.create_chat_invite_link(
        channel_id,
        creates_join_request=True
    )
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(DOWNLOAD_ANIME_BTN, url=invite.invite_link)
    ]])


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
        r"(â\s*ðð½ð¶ðð¼ð±ð²:\s*<i>)[^<]+(</i>)",
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
    aniInfo.pdata["quality_label"] = "720p"
    aniInfo.pdata["episode_number"] = "01"

    # Use the same ANIME SOUL 16:9 dashboard thumbnail as regular episode posts.
    batch_poster_path = (
        f"thumbs/anime_soul_batch_{aniInfo.adata.get('id') or hashlib.md5(cleaned_name.encode()).hexdigest()}.jpg"
    )
    batch_poster = await build_anime_soul_poster(
        aniInfo, anime_name, batch_poster_path
    )
    batch_photo = batch_poster if batch_poster and ospath.exists(batch_poster) else poster_url

    caption = await aniInfo.get_caption()

    caption = re.sub(
        r"(â\s*ðð½ð¶ðð¼ð±ð²:\s*<i>)[^<]+(</i>)",
        lambda m: f"{m.group(1)}1-{episode_count}{m.group(2)}",
        caption
    )

    try:
        fontchanger_status = await db.get_fontchanger()
        if fontchanger_status == "on":
            caption = obfuscate_text_for_copyright(caption)
    except Exception as e:
        await rep.report(f"Error applying font changer: {e}", "warning")

    post_msg = None
    stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing batch:</b> {anime_name}\nDownloading...")

    batch_buttons = []
    for qual in ("720",):
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

            msg = await TgUploader(stat_msg).upload(encoded_path, qual)
            if msg:
                qual_msg_ids.append(msg.id)
            try:
                if ospath.exists(encoded_path):
                    os.remove(encoded_path)
            except Exception:
                pass

        if qual_msg_ids:
            first_id = min(qual_msg_ids)
            last_id = max(qual_msg_ids)
            batch_string = f"get-{first_id * abs(Var.FILE_STORE)}-{last_id * abs(Var.FILE_STORE)}"
            batch_link = f"https://t.me/{Var.BOT_USERNAME}?start={await encode(batch_string)}"
            btn_text = f"Download Now Ep 1 to {episode_count}"
            batch_buttons.append(InlineKeyboardButton(btn_text, url=batch_link))
            if batch_buttons:
                btns = [batch_buttons[i:i+2] for i in range(0, len(batch_buttons), 2)]

    await stat_msg.delete()
    if batch_buttons:
        batch_markup = InlineKeyboardMarkup([batch_buttons[i:i+2] for i in range(0, len(batch_buttons), 2)])
        if channel_to_use != Var.MAIN_CHANNEL:
            batch_post = await bot.send_photo(channel_to_use, photo=batch_photo, caption=caption, reply_markup=batch_markup)
            main_markup = await build_anime_watch_markup(channel_to_use)
            await bot.send_photo(Var.MAIN_CHANNEL, photo=batch_photo, caption=caption, reply_markup=main_markup)
        else:
            batch_post = await bot.send_photo(Var.MAIN_CHANNEL, photo=batch_photo, caption=caption, reply_markup=batch_markup)
        if batch_post:
            await asleep(Var.STICKER_INTERVAL)
            if channel_to_use != Var.MAIN_CHANNEL:
                await send_sticker_to_channel(channel_to_use, Var.STICKER_ID)
    else:
        await rep.report(f"No batch qualities uploaded for {anime_name}; no Main Channel post sent", "warning")
    import logging
    if ospath.exists(batch_dir):
        logging.info(f"[CLEANUP] Removing batch_dir: {batch_dir}")
        shutil.rmtree(batch_dir, ignore_errors=True)
    await rep.report(f"Batch processing done and cleaned for {anime_name}", "info")

def download_image(url, filename):
    try:
        if not url:
            print("No URL provided for image download.")
            return None
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            with open(filename, "wb") as f:
                f.write(r.content)
            print(f"Image downloaded: {filename}")
            return filename
        else:
            print(f"Failed to download image. Status code: {r.status_code}")
    except Exception as e:
        print(f"Exception during image download: {e}")
    return None

async def get_anilist_titles_from_cleaned(name: str):
    cleaned_name = extract_base_anime_name(name)
    query = '''
    query ($search: String) {
      Media(search: $search, type: ANIME) {
        title {
          romaji
          english
          native
        }
        synonyms
      }
    }
    '''
    variables = {"search": cleaned_name}
    url = "https://graphql.anilist.co"
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json={"query": query, "variables": variables})
            resp.raise_for_status()
            data = resp.json()
            
            media = data.get("data", {}).get("Media", None)
            if not media:
                return []

            titles = set()
            for key in ["romaji", "english", "native"]:
                val = media.get("title", {}).get(key)
                if val:
                    titles.add(val)
            
            for syn in media.get("synonyms", []):
                titles.add(syn)

            return [normalize_anime_name_for_search(t) for t in titles if t]
    
    except Exception as e:
        print(f"Error in get_anilist_titles_from_cleaned: {e}")
        return []

def safe_filename(name):
    return re.sub(r'[\\/*?:"<>|\'\n\r\t]', '_', name)

def clean_anime_title(title: str) -> str:
    cleaned_title = re.sub(r'^\[.*?\]\s*', '', title)
    cleaned_title = re.sub(r'\s*-\s*\d+\s*\(.*?\)', '', cleaned_title)
    cleaned_title = re.sub(r'\[.*?\]', '', cleaned_title)
    cleaned_title = cleaned_title.replace('.mkv', '').strip()
    return cleaned_title

def extract_base_anime_name(name: str) -> str:
    cleaned = name
    # Remove special CJK brackets and punctuation marks early
    cleaned = re.sub(r'[ãã\(\)\[\]{}ãããã<>ãã\u3010\u3011]', '', cleaned)
    cleaned = re.sub(r'^\[[^\]]+\]\s*', '', cleaned)
    cleaned = re.sub(r'\s*-\s*\d+\s*(\([^\)]*\))?', '', cleaned)
    cleaned = re.sub(r'\s*S\d{1,2}E\d{1,3}', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*Season\s*\d+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*Ep(isode)?\s*\d+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(HEVC|x265|x264|AAC|WEB[- ]?DL|WEB[- ]?Rip|BluRay|HDTV|MultiSub|Multi-Subs|CR|RAW|SUB|Dub|Dual[- ]?Audio|Dual[- ]?Subs|VOSTFR|AMZN|NF|BILI|WeTV|iQ|AVC|EAC3|DDP2\.0|DDP5\.1|H\.?264|H\.?265|10bit|8bit|OV|REPACK|weekly|English Audio|VARYG|YURASUKA|Tsundere-Raws|Aniplus|YT|Disney\+|Opus|CA|Hero|Finals Arc|Uncensored|Remastered|A Multi-Language Release|Dual Audio|Dual Subs|Multi-Audio|Multi Subs|Dual|Eng|Jap|EN|JP)\b', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\b(\d{3,4}p)\b', '', cleaned)
    cleaned = re.sub(r'\.(mkv|mp4|avi|webm|wmv|mov|flv|ts)$', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\[[A-Fa-f0-9]{6,}\]', '', cleaned)
    cleaned = re.sub(r'\([^)]+\)', '', cleaned)
    cleaned = re.sub(r'\[[^\]]+\]', '', cleaned)
    cleaned = re.sub(r'[._\-:,/]+', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()

def normalize_anime_name_for_search(name: str) -> str:
    # Remove special CJK brackets and punctuation early
    cleaned = re.sub(r'[ãã\(\)\[\]{}"ãããã<>ãã\u3010\u3011]', '', name)
    base_name = extract_base_anime_name(cleaned)
    normalized = re.sub(r'\s+', ' ', base_name.lower().strip())
    return normalized

async def get_all_possible_anime_names(name: str):
    base_name = extract_base_anime_name(name)
    anilist_titles = await get_anilist_titles_from_cleaned(base_name)
    if not anilist_titles:
        return [base_name]
    all_names = set([normalize_anime_name_for_search(base_name)] + anilist_titles)
    return list(all_names)

def parse_torrent_size(size_str: str) -> float:
    if not size_str or not isinstance(size_str, str):
        return 0.0
    
    size_str = size_str.strip().upper()
 
    match = re.match(r'([\d.]+)\s*([A-Z]+)', size_str)
    if not match:
        return 0.0
    
    try:
        size = float(match.group(1))
        unit = match.group(2).replace(' ', '')
        
        conversions = {
            'B': 1 / (1024**3),
            'KB': 1 / (1024**2),
            'MB': 1 / 1024,
            'GB': 1,
            'TB': 1024,
            'KIB': 1 / (1024**2),
            'MIB': 1 / 1024,
            'GIB': 1,
            'TIB': 1024,
        }
        
        multiplier = conversions.get(unit, 1)
        return size * multiplier
    except (ValueError, AttributeError):
        return 0.0

def is_torrent_oversized(size_str: str, max_size_gb: float = 4.5) -> bool:
    size_in_gb = parse_torrent_size(size_str)
    return size_in_gb > max_size_gb

def is_manga_entry(title: str) -> bool:
    title_lower = title.lower()
    manga_keywords = [
        "ch.", "chapter", "ch ", "vol.", "vol ", "volume",
        " ch:", " chapter:", " vol:", " volume:"
    ]
    return any(keyword in title_lower for keyword in manga_keywords)

async def is_valid_torrent_url(url):
    try:
        if not url or not url.startswith(('http://', 'https://', 'magnet:')):
            return False
        
        if url.startswith('magnet:'):
            return True
        
        if url.endswith('.torrent'):
            return True

        async with httpx.AsyncClient() as client:
            try:
                response = await client.head(url, timeout=10)
                content_type = response.headers.get('content-type', '').lower()
                return 'torrent' in content_type or 'bittorrent' in content_type
            except:
                return False
                
    except Exception:
        return False

async def find_existing_anime_channel(anime_name: str):
    normalized_input = normalize_anime_name_for_search(anime_name)
    all_channels = await db.get_all_anime_channels()

    if normalized_input in all_channels:
        return all_channels[normalized_input]
        
    for stored_name, channel_id in all_channels.items():
        if normalized_input in stored_name or stored_name in normalized_input:
            return channel_id
    return None

async def send_sticker_to_channel(channel, sticker_id=None):
    """Send the configured Ongoing Anime Time sticker after a published post."""
    if not getattr(Var, "SEND_STICKER", True):
        await rep.report("Sticker sending is disabled; enable SEND_STICKER in Heroku config.", "warning")
        return False

    try:
        # Prefer the explicit configured/database sticker ID. The old helper
        # ignored this argument and always selected the first sticker in a pack.
        configured_id = sticker_id or getattr(Var, "STICKER_ID", None)
        if configured_id:
            sent_message = await bot.send_sticker(chat_id=channel, sticker=configured_id)
        else:
            sticker_set_name = "Ongoing_Anime_stickers_by_fStikBot"
            endpoint = f"https://api.telegram.org/bot{Var.BOT_TOKEN}/getStickerSet"
            async with httpx.AsyncClient(timeout=25.0) as session:
                response = await session.get(endpoint, params={"name": sticker_set_name})
                response.raise_for_status()
                payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(payload.get("description", "Telegram getStickerSet failed"))
            stickers = payload.get("result", {}).get("stickers") or []
            if not stickers or not stickers[0].get("file_id"):
                raise RuntimeError(f"Sticker pack {sticker_set_name} is empty")
            sent_message = await bot.send_sticker(chat_id=channel, sticker=stickers[0]["file_id"])

        if not sent_message:
            raise RuntimeError("Telegram did not confirm the sticker message")
        await rep.report(
            f"Successfully sent Ongoing Anime Time sticker after episode post to {channel} "
            f"(message_id={sent_message.id})",
            "info"
        )
        return True
    except Exception as error:
        await rep.report(
            f"Sticker send failed after episode post in {channel}: {type(error).__name__}: {error}",
            "error"
        )
        return False

async def validate_channel_id(channel_id):
    if not channel_id:
        return None
        
    try:
        await asleep(1)
        chat_info = await bot.get_chat(channel_id)
        return chat_info
    except (ChannelInvalid, PeerIdInvalid) as e:
        await rep.report(f"Channel validation failed for {channel_id}: {e}", "error")
        return None
    except Exception as e:
        await rep.report(f"Unexpected error validating channel {channel_id}: {e}", "error")
        return None

async def mirror_to_main_channel(post_msg, photo_url, caption, channel_to_use):
    try:
        main_post = None
        backup_msg = None
        chat_info = await validate_channel_id(channel_to_use)
        if not chat_info:
            await rep.report(f"Cannot mirror from invalid channel: {channel_to_use}", "error")
            return None, None

        buttons = None
        try:
            if post_msg:
                buttons = await build_anime_watch_markup(channel_to_use)
        except Exception as e:
            await rep.report(f"Failed to construct buttons for mirror: {e}", "warning")
            buttons = None

        try:
            if channel_to_use != Var.MAIN_CHANNEL:
                try:
                    main_post = await bot.send_photo(
                        Var.MAIN_CHANNEL,
                        photo=post_msg.photo.file_id if post_msg and post_msg.photo else photo_url,
                        caption=caption,
                        reply_markup=buttons
                    )
                    # Dedicated-channel episode mirrors do not get an
                    # automatic Main Channel sticker here. The batch/search
                    # announcement flow adds exactly one sticker after its
                    # combined Main Channel post.
                except Exception as e:
                    await rep.report(f"Failed to mirror to main channel {Var.MAIN_CHANNEL}: {e}", "error")
            else:
                main_post = post_msg
                # Main Channel sticker is sent only by the completed
                # announcement workflow, after its post is confirmed.

            try:
                db_backup = await db.get_backup_channel()
            except Exception:
                db_backup = None
            backup_id = db_backup or getattr(Var, 'BACKUP_CHANNEL', 0)

            if backup_id:
                backup_valid = await validate_channel_id(backup_id)
                if not backup_valid:
                    await rep.report(f"Backup channel {backup_id} is invalid", "warning")
                else:
                    try:
                        try:
                            backup_mode = await db.get_backup_mode()
                        except Exception:
                            backup_mode = getattr(Var, 'BACKUP_MODE', 'archive')

                        try:
                            norm_backup_mode = (backup_mode or '').strip().lower()
                        except Exception:
                            norm_backup_mode = 'archive'

                        if norm_backup_mode in ('archive', 'archieve', 'archived'):
                            backup_msg = await bot.send_photo(
                                backup_id,
                                photo=post_msg.photo.file_id if post_msg and post_msg.photo else photo_url,
                                caption=caption,
                                reply_markup=buttons
                            )
                            await asleep(Var.STICKER_INTERVAL)
                            await send_sticker_to_channel(backup_id, Var.STICKER_ID)
                        else:
                            try:
                                await rep.report(f"Backup mode set to '{backup_mode}' â not recognized as archive; marking pending backup.", "warning")
                                if post_msg:
                                    await db.add_pending_backup(Var.MAIN_CHANNEL, post_msg.id)
                                elif main_post:
                                    await db.add_pending_backup(Var.MAIN_CHANNEL, main_post.id)
                            except Exception as e:
                                await rep.report(f"Failed to mark pending backup: {e}", "warning")

                    except Exception as e:
                        await rep.report(f"Failed to mirror to backup channel {backup_id}: {e}", "warning")
        except Exception as e:
            await rep.report(f"Error in mirror_to_main_channel backup flow: {e}", "error")

        return main_post, backup_msg

    except Exception as e:
        await rep.report(f"Mirror Error: {e}", "error")
        return main_post, backup_msg

async def fetch_manga():
    await rep.report("Fetching Manga Started !!!", "info")
    processed_urls = set()
    manga_queue = []
    last_checked = set()

    while True:
        await asleep(10)
        if len(processed_urls) > 1000:
            processed_urls.clear()
        mode = await db.get_mode()
        if ani_cache.get('global_pause') and not ani_cache.get('manual_task'):
            continue
        if mode == "manga" and ani_cache['fetch_manga']:
            for rss_url in Var.RSS_ITEMS_MANGA:
                try:
                    
                    feed = feedparser.parse(rss_url)
                    manga_check_mode = await db.get_manga_check_mode()
                    for entry in feed.entries:
                        title = entry.title
                        link = entry.link
                        if link in processed_urls or link in last_checked:
                            continue
                        if not is_manga_entry(title):
                            continue

                        if await db.getAnime(link):
                            continue

                        if manga_check_mode == "specific_manga":
                            manga_name = title.split(":")[0].strip()
                            possible_names = await get_all_possible_anime_names(manga_name)
                            matched = False
                            for check_name in possible_names:
                                if await db.get_specific_manga(check_name):
                                    matched = True
                                    break
                            if not matched:
                                continue

                        manga_queue.append((title, link))
                        last_checked.add(link)
                except Exception as e:
                    await rep.report(f"Error in fetch_manga: {e}", "error")
            while manga_queue:
                title, url = manga_queue.pop(0)
                try:
                    await process_manga_chapter(title, url)
                    processed_urls.add(url)
                except Exception as e:
                    await rep.report(f"Error processing manga: {e}", "error")
                await asleep(2)

async def get_or_create_manga_channel(name: str, manga_info):
    try:
        manga_name = name.split(":")[0].strip()
        normalized_name = manga_name.lower()

        print(f"Searching for existing manga channel for: {manga_name}")
        await asleep(1.5)
        custom_channel = await db.get_manga_channel(normalized_name)
        if custom_channel:
            await rep.report(f"Found custom manga channel mapping: {custom_channel}", "info")
            return custom_channel

        channel_creation = await db.get_channel_creation()
        if not channel_creation or not Var.USER_SESSION:
            LOGS.info("Channel creation disabled or no user session, using main channel")
            return Var.MAIN_CHANNEL

        await rep.report(f"Creating new channel for manga: {manga_name}", "info")
        new_channel = await create_manga_channel(name, manga_info)
        
        if new_channel:
            await db.add_manga_channel_mapping(normalized_name, new_channel)
            LOGS.info(f"Successfully created and saved manga channel mapping for {manga_name}")
            return new_channel
        else:
            await rep.report("Failed to create manga channel, using main channel", "error")
            return Var.MAIN_CHANNEL
            
    except Exception as e:
        await rep.report(f"Error in get_or_create_manga_channel: {e}", "error")
        return Var.MAIN_CHANNEL

async def create_manga_channel(name: str, manga_info):
    try:
        if not Var.USER_SESSION:
            await rep.report("User session not available for channel creation", "error")
            return None
            
        from pyrogram import Client
        user_client = Client("user_session", session_string=Var.USER_SESSION, 
                           api_id=Var.API_ID, api_hash=Var.API_HASH)
        
        await user_client.start()

        manga_name = name.split(":")[0].strip()
        channel_name = manga_name
        description = f"Manga Channel | Created by AutoAnime @{Var.BOT_USERNAME}"

        created_chat = await user_client.create_channel(channel_name, description)
        channel_id = created_chat.id

        poster_url = (manga_info.get("coverImage", {}).get("large") or 
                     manga_info.get("coverImage", {}).get("medium") or 
                     "https://encrypted-tbn0.gstatic.com/images?q=tbn:ANd9GcSvJiVgwS6MM3Gv0riNlqmCsxqHu5Y9_ScrFQ&s")
        
        if poster_url:
            filename = f"/tmp/{channel_id}_logo.jpg"
            filename = download_image(poster_url, filename)
            if filename:
                try:
                    await user_client.set_chat_photo(channel_id, photo=filename)
                    remove(filename)
                except Exception as e:
                    await rep.report(f"Failed to set manga channel photo: {e}", "warning")

        try:
            from pyrogram.types import ChatPrivileges
            await user_client.promote_chat_member(
                channel_id,
                Var.CREATION_USERNAME,
                privileges=ChatPrivileges(
                    can_post_messages=True,
                    can_edit_messages=True,
                    can_delete_messages=True,
                    can_invite_users=True,
                    can_pin_messages=True,
                    can_manage_chat=True
                )
            )
        except Exception as e:
            await rep.report(f"Failed to add/promote bot in manga channel: {e}", "error")

        await user_client.stop()
        return channel_id
        
    except Exception as e:
        await rep.report(f"Failed to create manga channel: {e}", "error")
        return None

async def process_manga_chapter(title, chapter_url, manual=False):
    try:
        if ani_cache.get('global_pause') and not manual:
            return

        if await db.getAnime(chapter_url):
            await rep.report(f"[DEBUG] Skipping duplicate manga: {title}", "info")
            return

        safe_title = safe_filename(title)
        manga_dir = os.path.join(os.getcwd(), "downloads", "manga")
        os.makedirs(manga_dir, exist_ok=True)

        status_msg = await bot.send_message(
            Var.MAIN_CHANNEL,
            f"<blockquote><b>â£ Manga Name:</b> <i>{title}</i></blockquote>\n\n<pre>Processing...</pre>"
        )

        manga_name = title.split(":")[0].strip()
        try:
            manga_lister = MangaLister(manga_name, datetime.datetime.now().year)
            manga_info = await manga_lister.get_mangadata() or {}
        except Exception as e:
            await rep.report(f"Failed to get manga metadata: {e}", "warning")
            manga_info = {}

        file_store_msg = None
        try:
            await status_msg.edit_text(
                f"<blockquote><b>â£ Manga Name :</b> <i>{title}</i></blockquote>\n\n<pre>Downloading images...</pre>"
            )
            images = await get_mangadex_images(chapter_url)
            if not isinstance(images, list) or not images or not all(isinstance(i, str) and i.strip() for i in images):
                await rep.report(f"Upload failed: No images found or bad image data for {title}", "error")
                await status_msg.edit_text(
                    f"<blockquote><b>â£ Manga Name :</b> <i>{title}</i></blockquote>\n\n<pre>No images found. Skipping.</pre>"
                )
                return

            await status_msg.edit_text(
                f"<blockquote><b>â£ Manga Name :</b> <i>{title}</i></blockquote>\n\n<pre>Creating PDF...</pre>"
            )

            pdf_path = await images_to_pdf(safe_title, images, manga_dir)
            if not pdf_path or not ospath.exists(pdf_path):
                await rep.report(f"PDF creation failed for {title}", "error")
                await status_msg.edit_text(
                    f"<blockquote><b>â£ Manga Name :</b> <i>{title}</i></blockquote>\n\n<pre>PDF creation failed.</pre>"
                )
                return

            await status_msg.edit_text(
                f"<blockquote><b>â£ Manga Name :</b> <i>{title}</i></blockquote>\n\n<pre>Uploading...</pre>"
            )

            file_caption = get_manga_filename(title, Var.BRAND_UNAME)
            if not isinstance(file_caption, str) or not file_caption.strip():
                file_caption = "<b>Manga</b>"
            else:
                file_caption = f"<b>{file_caption}</b>"

            thumb_path = "thumb.jpg" if os.path.exists("thumb.jpg") else None
            if thumb_path is not None and not isinstance(thumb_path, str):
                thumb_path = None

            debug_msg = (
                f"[DEBUG] Manga upload vars: pdf_path={pdf_path!r} (exists={ospath.exists(pdf_path) if isinstance(pdf_path, str) else 'N/A'}), "
                f"file_caption={file_caption!r} (type={type(file_caption)}), thumb_path={thumb_path!r} (type={type(thumb_path)})"
            )
            await rep.report(debug_msg, "info")

            if not isinstance(pdf_path, str) or not ospath.exists(pdf_path):
                await rep.report(f"Upload failed: pdf_path is not a valid string or file does not exist: {pdf_path}", "error")
                return

            if not isinstance(file_caption, str) or not file_caption.strip():
                await rep.report(f"Upload failed: file_caption is not a valid string: {file_caption}", "error")
                file_caption = "<b>Manga</b>"

            if thumb_path is not None and not isinstance(thumb_path, str):
                await rep.report(f"Upload warning: thumb_path is not a string: {thumb_path}", "warning")
                thumb_path = None

            file_store_msg = await bot.send_document(
                Var.FILE_STORE,
                pdf_path,
                caption=file_caption,
                thumb=thumb_path,
                force_document=True
            )

            bot_link = f"https://telegram.me/{Var.BOT_USERNAME}?start={await encode('get-'+str(file_store_msg.id * abs(Var.FILE_STORE)))}"

            manga_banner = await db.get_manga_banner(manga_name)
            poster_url = (manga_banner or 
                        manga_info.get("coverImage", {}).get("large") or 
                        manga_info.get("coverImage", {}).get("medium") or 
                        "https://i.ibb.co/NdG0PXdx/not-found.png")

            main_buttons = [[InlineKeyboardButton(f"{MANGA_BTN}", url=bot_link)]]

            channel_creation = await db.get_channel_creation()
            if channel_creation:
                custom_channel = await get_or_create_manga_channel(title, manga_info)
                if custom_channel and custom_channel != Var.FILE_STORE:
                    await status_msg.edit_text(
                        f"<blockquote><b>â£ Manga Name :</b> <i>{title}</i></blockquote>\n\n<pre>Mirroring to custom channel...</pre>"
                    )

                    custom_msg = await bot.send_document(
                        custom_channel,
                        pdf_path,
                        caption=file_caption,
                        thumb="thumb.jpg" if os.path.exists("thumb.jpg") else None,
                        force_document=True
                    )
                    

                    chat_info = await bot.get_chat(custom_channel)
                    if chat_info.username:
                        custom_link = f"https://t.me/{chat_info.username}/{custom_msg.id}"
                        main_buttons = [[InlineKeyboardButton(f"{MANGA_BUTTON}", url=custom_link)]]
                    else:
                        try:
                            invite_link = await bot.export_chat_invite_link(custom_channel)
                            raw_channel_id = str(custom_channel).replace("-100", "")
                            post_link = f"https://t.me/c/{raw_channel_id}/{custom_msg.id}"
                            main_buttons = [
                                [InlineKeyboardButton(f"{MANGA_READ_BUTTON}", url=post_link),
                                InlineKeyboardButton(f"{MANGA_CHNL_BUTTON}", url=invite_link)]
                            ]
                        except Exception as e:
                            await rep.report(f"Failed to create custom channel links: {e}", "warning")

            await status_msg.edit_text(
                f"<blockquote><b>â£ Manga Name:</b> <i>{title}</i></blockquote>\n\n<pre>Sending main post...</pre>"
            )

            caption = await get_manga_caption(manga_info, title)
            main_post = await bot.send_photo(
                Var.MAIN_CHANNEL,
                photo=poster_url,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(main_buttons)
            )


            backup_id = getattr(Var, 'BACKUP_CHANNEL', 0)
            try:
                db_backup = await db.get_backup_channel()
            except Exception:
                db_backup = None
            backup_id = db_backup or getattr(Var, 'BACKUP_CHANNEL', 0)

            if backup_id:
                backup_valid = await validate_channel_id(backup_id)
                if not backup_valid:
                    await rep.report(f"Backup channel {backup_id} is invalid", "warning")
                else:
                    try:
                        try:
                            backup_mode = await db.get_backup_mode()
                        except Exception:
                            backup_mode = getattr(Var, 'BACKUP_MODE', 'archive')

                        if backup_mode == 'archive':
                            try:
                                await bot.send_photo(
                                    backup_id,
                                    photo=poster_url,
                                    caption=caption,
                                    reply_markup=InlineKeyboardMarkup(main_buttons)
                                )
                                await asleep(Var.STICKER_INTERVAL)
                                await send_sticker_to_channel(backup_id, Var.STICKER_ID)
                            except Exception as e:
                                await rep.report(f"Failed to mirror manga post to backup channel {backup_id}: {e}", "warning")
                        else:
                            try:
                                backup_msg = await bot.send_photo(
                                    backup_id,
                                    photo=poster_url,
                                    caption=caption,
                                    reply_markup=InlineKeyboardMarkup(main_buttons)
                                )
                                await asleep(Var.STICKER_INTERVAL)
                                await send_sticker_to_channel(backup_id, Var.STICKER_ID)
                                try:
                                    await db.add_backup_mapping(Var.MAIN_CHANNEL, main_post.id if 'main_post' in locals() else post_msg.id, backup_id, backup_msg.id)
                                except Exception as e:
                                    await rep.report(f"Failed to save manga backup mapping: {e}", "warning")
                            except Exception as e:
                                await rep.report(f"Failed to create manga backup post in sep mode: {e}", "warning")
                    except Exception as e:
                        await rep.report(f"Failed to mirror to backup channel {backup_id}: {e}", "warning")

            if os.path.exists(pdf_path):
                os.remove(pdf_path)

            await db.saveAnime(chapter_url, "1", "pdf", file_store_msg.id)

            if status_msg:
                await status_msg.delete()

            await rep.report(f"Successfully uploaded manga: {title}", "info")

        except Exception as e:
            raise Exception(f"Upload failed: {e}")

    except Exception as e:
        await rep.report(f"Error processing manga: {e}", "error")
        if 'pdf_path' in locals() and os.path.exists(pdf_path):
            os.remove(pdf_path)
        if 'status_msg' in locals() and status_msg:
            await status_msg.delete()

async def get_mangadex_images(chapter_url):
    try:
        import re
        match = re.search(r'/chapter/([a-f0-9\-]+)', chapter_url)
        if not match:
            return []
        chapter_id = match.group(1)
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://api.mangadex.org/at-home/server/{chapter_id}")
            if resp.status_code != 200:
                return []
            data = resp.json()
            base_url = data["baseUrl"]
            chapter = data["chapter"]
            hash_ = chapter["hash"]
            images = chapter["data"]
 
            return [f"{base_url}/data/{hash_}/{img}" for img in images]
    except Exception as e:
        await rep.report(f"Failed to fetch MangaDx images: {e}", "error")
        return []

async def images_to_pdf(title, images, manga_dir):
    try:
        if not isinstance(title, str):
            await rep.report(f"PDF creation failed: title is not a string: {title}", "error")
            return None
        if not isinstance(images, list) or not all(isinstance(img, str) for img in images):
            await rep.report(f"PDF creation failed: images list contains non-string items: {images}", "error")
            return None

            
        pdf = FPDF()
        img_paths = []
        error_count = 0
        max_errors = 3

        async with httpx.AsyncClient(timeout=30) as client:
            tasks = []
            for idx, img_url in enumerate(images):
                if not isinstance(img_url, str):
                    await rep.report(f"Skipping non-string image URL: {img_url}", "warning")
                    error_count += 1
                    continue
                img_path = os.path.join(manga_dir, f"{title}_{idx}.jpg")
                tasks.append(download_image_async(client, img_url, img_path))
                img_paths.append(img_path)
            
            await asyncio.gather(*tasks)

        for img_path in img_paths:
            if not ospath.exists(img_path):
                error_count += 1
                if error_count > max_errors:
                    await rep.report(f"Too many image failures for {title}, aborting", "error")
                    break
                continue
                
            try:
                with Image.open(img_path) as im:
                    if im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info):
                        bg = Image.new('RGB', im.size, (255, 255, 255))
                        if im.mode == 'RGBA':
                            bg.paste(im, mask=im.split()[3])
                        elif im.mode == 'LA':
                            bg.paste(im, mask=im.split()[1])
                        else:
                            bg.paste(im)
                        im = bg
                    elif im.mode != 'RGB':
                        im = im.convert('RGB')

                    im.save(img_path, "JPEG", quality=75)

                width, height = Image.open(img_path).size
                pdf_width = 210
                pdf_height = height * (pdf_width / width)
                
                pdf.add_page(orientation='P' if width <= height else 'L')
                pdf.image(img_path, x=0, y=0, w=pdf_width)
            
            except Exception as e:
                error_count += 1
                await rep.report(f"Failed to process image {img_path}: {e}", "warning")
                if error_count > max_errors:
                    await rep.report(f"Too many image failures for {title}, aborting", "error")
                    break
            finally:
                try:
                    if ospath.exists(img_path):
                        os.remove(img_path)
                except Exception:
                    pass

        if error_count > max_errors:
            return None

        pdf_path = os.path.join(manga_dir, f"{title}.pdf")
        pdf.output(pdf_path)
        
        if not ospath.exists(pdf_path) or ospath.getsize(pdf_path) < 1024:
            await rep.report(f"Generated PDF appears invalid for {title}", "error")
            return None
            
        return pdf_path
        
    except Exception as e:
        await rep.report(f"PDF creation failed: {e}", "error")
        return None
    finally:
        for img_path in img_paths:
            try:
                if ospath.exists(img_path):
                    os.remove(img_path)
            except Exception:
                pass
async def download_image_async(client, url, filename):
    try:
        resp = await client.get(url, timeout=30)
        if resp.status_code == 200:
            with open(filename, "wb") as f:
                f.write(resp.content)
    except Exception as e:
        print(f"Failed to download {url}: {e}")

def normalize_title_for_comparison(title):
    """Normalize release titles so quality feeds can be matched to the same episode."""
    value = str(title or "").casefold()
    value = re.sub(r"^\s*\[[^\]]+\]\s*", " ", value)
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", value)
    value = re.sub(r"\bs\d{1,2}\s*[-.]?\s*e?\s*\d{1,3}\b", " ", value)
    value = re.sub(r"\b(?:episode|ep)\s*[-._ ]?\d{1,3}\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())

async def process_low_end(name, torrent_url, force=False, destination_channel=None):
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        encoding_enabled = await db.get_encoding()
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        # Resolve the mapped anime channel for automatic releases too.
        # Previously low-end mode always defaulted to MAIN_CHANNEL and bypassed /addanime.
        if destination_channel is not None:
            channel_to_use = destination_channel
        else:
            channel_to_use = await get_or_create_anime_channel(name, aniInfo)

        channel_info = await validate_channel_id(channel_to_use)
        if not channel_info:
            await rep.report(
                f"Anime destination {channel_to_use} is unavailable; falling back to Main Channel.",
                "error"
            )
            channel_to_use = Var.MAIN_CHANNEL
            channel_info = await validate_channel_id(channel_to_use)

        target_channel = int(channel_info.id) if channel_info else int(Var.MAIN_CHANNEL)
        existing_target_post = (
            await db.getAnimeMainPost(ani_id, ep_no)
            if target_channel == int(Var.MAIN_CHANNEL)
            else await db.getAnimeDestinationPost(ani_id, ep_no, target_channel)
        )
        if existing_target_post:
            ani_cache['ongoing'].discard(episode_key)
            return "already_published"

        await db.saveAnimeResume(ani_id, ep_no, name, torrent_url)

        if force or (not (ani_data := await db.getAnime(ani_id)) \
            or (ani_data and not (qual_data := ani_data.get(ep_no))) \
            or (ani_data and qual_data and not all(qual for qual in qual_data.values()))):

            if "[Batch]" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if "(Batch)" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if hasattr(torrent_url, 'size') and torrent_url.size:
                if is_torrent_oversized(str(torrent_url.size), max_size_gb=4.5):
                    await rep.report(f"Torrent Skipped! - Size exceeds 4.5GB limit\n\n{name}", "warning")
                    return

            await rep.report(f"New Anime Torrent Found!\n\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            channel_to_use = target_channel if destination_channel is not None else await get_or_create_anime_channel(name, aniInfo)

            channel_valid = False
            for attempt in range(3):
                if await validate_channel_id(channel_to_use):
                    channel_valid = True
                    break
                await asyncio.sleep(2)

            if not channel_valid:
                await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
                channel_to_use = Var.MAIN_CHANNEL

            all_banners = await db.list_anime_banners()
            banner_url = None
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break

            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")

            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")

            post_msg = None
            main_post = backup_post = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")

            file_store_links = {}
            saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)
            buttons = []
            btns = []
            post_id = None
            for qual in ["480", "720", "1080"]:
                saved_msg_id = saved_file_ids.get(qual)
                if saved_msg_id:
                    try:
                        stored_msg = await bot.get_messages(
                            Var.FILE_STORE, message_ids=int(saved_msg_id)
                        )
                        if stored_msg and not getattr(stored_msg, "empty", False):
                            file_store_links[qual] = int(saved_msg_id)
                            await rep.report(
                                f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                "info"
                            )
                            continue
                    except Exception as verify_error:
                        await rep.report(
                            f"Saved {qual} upload is unavailable; retrying this quality: {verify_error}",
                            "warning"
                        )

                rss_link = Var.RSS_LOW_END.get(qual)
                if not rss_link:
                    continue
                feed = await find_matching_quality_release(rss_link, name)
                if not feed or not feed.get("link"):
                    await rep.report(
                        f"No matching {qual}p release found for this exact episode yet: {name}",
                        "warning"
                    )
                    continue

                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Downloading matching {qual}p release...</i></pre>")
                dl_path = await download_torrent(feed["link"])
                if not dl_path:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to download {qual}p</i></pre>")
                    continue

                if Var.LOW_END_RENAME:
                        editor = TextEditor(name)
                        filename = await aniInfo.get_upname(qual)
                        anime_format = aniInfo.adata.get("format", "").lower()
                        is_movie = anime_format == "movie"
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Encode...</i></pre>")
                        await asleep(1.5)
                        await rep.report("Starting Encode...", "info")
                        try:
                            out_path = await FFEncoder(stat_msg, dl_path, filename, qual, is_movie=is_movie).start_encode()
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Compressed Now Going To Upload...", "info")
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Upload...</i></pre>")
                        await asleep(1.5)
                        try:
                            msg = await TgUploader(stat_msg).upload(out_path, qual)
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Uploaded File into Channel...", "info")
                        msg_id = msg.id
                        file_store_links[qual] = msg_id
                        await db.saveAnimeFileId(ani_id, ep_no, qual, msg_id)
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        saved_file_ids[qual] = msg_id
                        link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(await encode('get-'+str(msg_id * abs(Var.FILE_STORE))))
                        if len(btns) != 0 and len(btns[-1]) == 1:
                            btns[-1].insert(1, InlineKeyboardButton(f"{btn_formatter[qual]}", url=link))
                        else:
                            btns.append([InlineKeyboardButton(f"{btn_formatter[qual]}", url=link)])
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        bot_loop.create_task(extra_utils(msg_id, out_path))

                        gdrive_status = await db.get_gdrive_upload()
                        if gdrive_status == "on":
                            possible_names = await get_all_possible_anime_names(cleaned_name)
                            gdrive_link = None
                            for check_name in possible_names:
                                gdrive_link = await db.get_gdrive_mapping(check_name, qual)
                                if gdrive_link:
                                    try:
                                        from bot.core.gdrive import GdriveUploader
                                        await rep.report(f"Uploading to GDrive for {check_name} - {qual}p", "info")
                                        gdrive_uploader = GdriveUploader()
                                        await gdrive_uploader.upload_file(out_path, gdrive_link)
                                        await rep.report(f"Uploaded to GDrive for {check_name} - {qual}p", "info")
                                    except Exception as e:
                                        await rep.report(f"GDrive upload failed for {check_name} - {qual}p: {e}", "error")
                                    break
                else:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to upload {qual}p</i></pre>")

            quality_order = ["480", "720", "1080", "HDRi"]
            all_buttons = []

            for qual in quality_order:
                msg_id = file_store_links.get(qual)
                if not msg_id:
                    continue

                encoded = await encode(
                    "get-" + str(msg_id * abs(Var.FILE_STORE))
                )
                all_buttons.append(
                    InlineKeyboardButton(
                        text=btn_formatter[qual],
                        url=f"https://t.me/{Var.BOT_USERNAME}?start={encoded}"
                    )
                )

            grouped_buttons = [
                all_buttons[i:i + 2]
                for i in range(0, len(all_buttons), 2)
            ]

            final_caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    final_caption = obfuscate_text_for_copyright(final_caption)
            except Exception as e:
                await rep.report(
                    f"Error applying font changer: {e}",
                    "warning"
                )

            final_markup = (
                InlineKeyboardMarkup(grouped_buttons)
                if grouped_buttons else None
            )

            required_low_end = ["480", "720", "1080", "HDRi"]
            missing_low_end = [q for q in required_low_end if q not in file_store_links]
            if missing_low_end:
                raise RuntimeError(
                    f"Low-end episode {name} is not ready to publish; missing qualities: {', '.join(missing_low_end)}"
                )

            # Publish the quality/download buttons in the anime's own channel.
            episode_post = await bot.send_photo(
                target_channel, photo=photo_url,
                caption=final_caption, reply_markup=final_markup
            )
            if not episode_post or not getattr(episode_post, "id", None):
                raise RuntimeError(f"Anime channel did not confirm post for {name}.")

            if target_channel != int(Var.MAIN_CHANNEL):
                # Main Channel announcement has one button only; it creates a
                # join-request invite for this anime-specific channel.
                watch_markup = await build_anime_watch_markup(target_channel)
                final_post = await bot.send_photo(
                    Var.MAIN_CHANNEL, photo=photo_url,
                    caption=final_caption, reply_markup=watch_markup
                )
                if not final_post or not getattr(final_post, "id", None):
                    raise RuntimeError(f"Main Channel announcement failed for {name}.")
                await db.saveAnimeDestinationPost(ani_id, ep_no, target_channel, episode_post.id)
                await db.saveAnimeDedicatedPost(ani_id, ep_no, episode_post.id)
            else:
                final_post = episode_post

            await db.saveAnimeMainPost(ani_id, ep_no, final_post.id)
            for uploaded_qual in file_store_links:
                await db.saveAnime(ani_id, ep_no, uploaded_qual, final_post.id)
            await db.markAnimeResumeComplete(ani_id, ep_no)
            if ani_id and ep_no:
                await db.mark_episode_completed(ani_id, ep_no)

            # Do not send episode stickers to either the selected channel or
            # Main Channel. The selected-channel workflow is episode-only;
            # Main Channel receives only its configured joining announcement.


            if stat_msg:
                await stat_msg.delete()
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)

    except Exception as e:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(f"Low-end processing error: {str(e)}", "error")

async def fetch_animes():
    """Upload only today's scheduled anime releases; never auto-resume old backlog."""
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone
    from bot.modules.daily_schedule import fetch_today_schedule

    await rep.report(
        "Scheduled-only Anime Fetcher Started. Old backlog and unscheduled RSS uploads are disabled.",
        "info"
    )
    seen_rss_links = {}
    ist = ZoneInfo("Asia/Kolkata")

    def normalize_schedule_title(value):
        value = re.sub(r"^\s*\[[^\]]+\]\s*", "", str(value or ""))
        value = re.sub(r"\([^)]*(?:480p|720p|1080p|WEB[- ]?DL|BluRay|Batch)[^)]*\)", " ", value, flags=re.I)
        value = re.sub(r"\[[^\]]*(?:480p|720p|1080p|WEB[- ]?DL|BluRay|Batch)[^\]]*\]", " ", value, flags=re.I)
        value = re.sub(r"\s*-\s*(?:\d{1,3})(?:v\d+)?\s*$", "", value)
        return re.sub(r"[^a-z0-9]+", "", value.casefold())

    def published_today(entry, today):
        published = (
            getattr(entry, "published_parsed", None)
            or getattr(entry, "updated_parsed", None)
        )
        if not published:
            return False
        try:
            published_at = datetime(*published[:6], tzinfo=timezone.utc)
            return published_at.astimezone(ist).date() == today
        except Exception:
            return False

    while True:
        await asleep(30)
        mode = await db.get_mode()
        if ani_cache.get("global_pause") or mode != "anime" or not ani_cache.get("fetch_animes"):
            continue

        try:
            now_ist = datetime.now(ist)
            today = now_ist.date()

            # The daily SubsPlease schedule is the allow-list. If it cannot
            # be fetched, fail closed: do not upload arbitrary RSS entries.
            schedule_items = await fetch_today_schedule()
            scheduled_titles = {
                normalize_schedule_title(item.get("title"))
                for item in schedule_items
                if isinstance(item, dict) and item.get("title")
            }
            scheduled_titles.discard("")
            if not scheduled_titles:
                await rep.report(
                    "No valid anime titles found in today's schedule. Automatic uploads held.",
                    "warning"
                )
                continue

            configured_feeds = list(Var.RSS_ITEMS_ANIME or [])
            quality_feeds = [
                Var.RSS_LOW_END.get(q)
                for q in ("480", "720", "1080")
                if Var.RSS_LOW_END.get(q)
            ]
            rss_urls = list(dict.fromkeys(
                url for url in configured_feeds + quality_feeds if url
            ))
            if not rss_urls:
                await rep.report("No RSS feeds configured; scheduled uploads held.", "warning")
                continue

            feeds_by_url = await asyncio.gather(*(getfeeds(url) for url in rss_urls))
            candidates = []
            for rss_url, entries in zip(rss_urls, feeds_by_url):
                if not entries:
                    continue
                seen = seen_rss_links.setdefault(rss_url, set())
                for entry in entries:
                    link = getattr(entry, "link", None)
                    title = getattr(entry, "title", None)
                    if not link or not title or link in seen:
                        continue

                    # Only RSS releases published today in India are eligible.
                    if not published_today(entry, today):
                        continue

                    normalized_feed_title = normalize_schedule_title(title)
                    if not normalized_feed_title:
                        continue

                    # Require the scheduled series title to appear in the
                    # normalized RSS title. No audio-type preference is used.
                    if not any(
                        scheduled in normalized_feed_title
                        or normalized_feed_title in scheduled
                        for scheduled in scheduled_titles
                        if len(scheduled) >= 5
                    ):
                        continue

                    candidates.append((title, link, rss_url))

            if not candidates:
                continue

            # Process only one eligible scheduled release per scan. Re-scan
            # schedule/RSS before the next upload so no stale backlog is drained.
            candidates.sort(key=lambda item: item[0].casefold())
            title, link, source_feed = candidates[0]
            await rep.report(
                f"Today's scheduled release selected: {title}. "
                f"Unscheduled and older releases remain blocked.",
                "info"
            )
            try:
                await get_animes(title, link)
                seen_rss_links.setdefault(source_feed, set()).add(link)
            except asyncio.CancelledError:
                raise
            except Exception:
                await rep.report(
                    f"Scheduled release failed; it will be checked again: {title}\n{format_exc()}",
                    "error"
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await rep.report(
                f"Scheduled-only fetch check failed closed; no RSS upload started: {e}",
                "error"
            )

async def get_or_create_anime_channel(name: str, aniInfo):
    """Resolve a manually mapped anime channel across AniList/release-title aliases."""
    try:
        base_anime_name = extract_base_anime_name(name)
        normalized_name = normalize_anime_name_for_search(base_anime_name)

        # /addanime stores every known title variant. Resolve those variants
        # before falling back to auto-created channels, so today's scheduled
        # RSS release is routed to the channel the admin actually configured.
        candidates = [base_anime_name, name, normalized_name]
        try:
            candidates.extend(await get_all_possible_anime_names(base_anime_name))
        except Exception as alias_error:
            await rep.report(
                f"Could not expand anime channel aliases for {base_anime_name}: {alias_error}",
                "warning"
            )

        seen_candidates = set()
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if not candidate:
                continue
            candidate_key = normalize_anime_name_for_search(candidate)
            if not candidate_key or candidate_key in seen_candidates:
                continue
            seen_candidates.add(candidate_key)

            try:
                mapped_channel = await db.get_anime_channel(candidate_key)
                if not mapped_channel:
                    mapped_channel = await db.get_anime_channel(candidate.lower())
                if mapped_channel:
                    channel_info = await validate_channel_id(mapped_channel)
                    if channel_info:
                        # Keep the release's canonical name mapped as well,
                        # avoiding repeated alias resolution on later episodes.
                        await db.add_anime_channel_mapping(normalized_name, channel_info.id)
                        await rep.report(
                            f"Automatic release routed to configured anime channel: "
                            f"{base_anime_name} -> {channel_info.id} (matched '{candidate}')",
                            "info"
                        )
                        return channel_info.id
                    await rep.report(
                        f"Configured anime channel {mapped_channel} for '{candidate}' is unavailable; "
                        "checking other aliases.",
                        "warning"
                    )
            except Exception as mapping_error:
                await rep.report(
                    f"Anime channel mapping lookup failed for '{candidate}': {mapping_error}",
                    "warning"
                )

        existing_channel = await find_existing_anime_channel(base_anime_name)
        if existing_channel:
            channel_info = await validate_channel_id(existing_channel)
            if channel_info:
                await db.add_anime_channel_mapping(normalized_name, channel_info.id)
                await rep.report(
                    f"Found existing anime channel for {base_anime_name}: {channel_info.id}",
                    "info"
                )
                return channel_info.id

        channel_creation = await db.get_channel_creation()
        if not channel_creation or not Var.USER_SESSION:
            await rep.report(
                f"No dedicated channel mapping for {base_anime_name}; using Main Channel.",
                "info"
            )
            return Var.MAIN_CHANNEL

        await rep.report(f"Creating new channel for: {base_anime_name}", "info")
        new_channel = await create_anime_channel(name, aniInfo)
        if new_channel:
            await db.add_anime_channel_mapping(normalized_name, new_channel)
            await rep.report(
                f"Created and saved anime channel mapping for {base_anime_name}: {new_channel}",
                "info"
            )
            return new_channel

        await rep.report(
            f"Could not create dedicated channel for {base_anime_name}; using Main Channel.",
            "error"
        )
        return Var.MAIN_CHANNEL
    except Exception as e:
        await rep.report(f"Error in get_or_create_anime_channel: {e}", "error")
        return Var.MAIN_CHANNEL

async def create_anime_channel(name, aniInfo):
    try:
        if not Var.USER_SESSION:
            await rep.report("User session not available for channel creation", "error")
            return None

        from pyrogram import Client
        user_client = Client("user_session", session_string=Var.USER_SESSION, 
                           api_id=Var.API_ID, api_hash=Var.API_HASH)

        await user_client.start()

        anime_format = (aniInfo.adata.get("format", "") or "").lower()
        is_movie = anime_format == "movie" or (
            hasattr(aniInfo, "is_movie_title") and aniInfo.is_movie_title(name)
        )

        titles = aniInfo.adata.get("title", {})
        channel_name = titles.get("english") or titles.get("romaji") or clean_anime_title(name)
        title = channel_name
        status = aniInfo.adata.get("status", "Unknown").capitalize()
        genres = aniInfo.adata.get("genres", [])[:3]
        genres_formatted = ", ".join(genres) if genres else "Unknown"

        if is_movie:
            description = (
                f"â¢ Movie: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )
        else:
            description = (
                f"â¢ Anime: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )

        created = await user_client.create_channel(channel_name, description)
        channel_id = created.id

        poster_url = (
            aniInfo.adata.get("coverImage", {}).get("large")
            or aniInfo.adata.get("coverImage", {}).get("medium")
            or aniInfo.adata.get("coverImage", {}).get("small")
        )
        if poster_url:
            filename = f"/tmp/{channel_id}_logo.jpg"
            filename = download_image(poster_url, filename)
            if filename:
                try:
                    await user_client.set_chat_photo(channel_id, photo=filename)
                    print(f"Set channel photo for {channel_id} successfully!")
                    remove(filename)
                except Exception as e:
                    print(f"Failed to set channel photo: {e}")
                    await rep.report(f"Failed to set channel photo: {e}", "warning")
            else:
                print("Image file was not downloaded or does not exist.")
                await rep.report(f"Image file was not downloaded or does not exist.", "warning")
        else:
            print("No poster URL found in AniList data.")
            await rep.report(f"No poster URL found in AniList data.", "warning")

        try:
            from pyrogram.types import ChatPrivileges
            await user_client.promote_chat_member(
                channel_id,
                Var.CREATION_USERNAME,
                privileges=ChatPrivileges(
                    can_post_messages=True,
                    can_edit_messages=True,
                    can_delete_messages=True,
                    can_invite_users=True,
                    can_change_info=True,
                    can_pin_messages=True,
                    can_manage_chat=True,
                    can_manage_video_chats=False,
                    is_anonymous=False
                )
            )
            await rep.report(f"Successfully added and promoted bot in channel", "info")
        except Exception as e:
            await rep.report(f"Failed to add/promote bot in channel: {e}", "error")

        try:
            titles = aniInfo.adata.get("title", {})
            genres = ', '.join(aniInfo.adata.get("genres", [])[:3])
            anime_type = aniInfo.adata.get("format", "Unknown")
            avg_rating = aniInfo.adata.get("averageScore", "N/A")
            status = aniInfo.adata.get("status", "Unknown")
            start_date = aniInfo.adata.get("startDate", {})
            end_date = aniInfo.adata.get("endDate", {})
            runtime = aniInfo.adata.get("duration", "Unknown")
            episodes = aniInfo.adata.get("episodes", "Unknown")
            desc = aniInfo.adata.get("description", "No description available").replace("<br>", "").replace("<i>", "").replace("</i>", "").strip()
            desc = (desc[:200] + "...") if len(desc) > 200 else desc 
            site_url = aniInfo.adata.get("siteUrl", "https://anilist.co")
            media_id = aniInfo.adata.get("id")

            english = titles.get("english", "Unknown")
            romaji = titles.get("romaji", "Unknown")
            display_title = english if english != "Unknown" else romaji

            graphic_poster_url = f"https://img.anili.st/media/{media_id}"

            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.head(graphic_poster_url) as resp:
                    if resp.status != 200:
                        graphic_poster_url = poster_url or "https://imgs.search.brave.com/33oSQBqhBoRB6akjKPud5oFd-PxnMFEm1AjnBTS2ZhE/rs:fit:860:0:0:0/g:ce/aHR0cHM6Ly9pbWcu/ZnJlZXBpay5jb20v/dmVjdG9yLXByZW1p/dW0vaWx1c3RyYWNp/b24tYW5pbWUtZXJy/b3ItNDA0LXBhZ2lu/YS1uby1lbmNvbnRy/YWRhXzE1MDk3Mi0y/NTY0LmpwZw"

            if is_movie:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ Release Date : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Duration : {runtime} minutes</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""
            else:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ First aired : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Last aired : {end_date.get("year", '')}-{end_date.get("month", '')}-{end_date.get("day", '')}
â£ Runtime : {runtime} minutes
â£ No of episodes : {episodes}</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""

            await user_client.send_photo(
                chat_id=channel_id,
                photo=graphic_poster_url,
                caption=caption
            )

            STICKER_ID = "CAACAgUAAxkBAAIERmhruO6M5RpLtZjb2yrvPJxTuw0IAAIzEwACTrLwVSdrhgIPuK1TNgQ"
            await user_client.send_sticker(channel_id, STICKER_ID)

            await rep.report("Intro poster, caption, and sticker sent to channel", "info")

        except Exception as e:
            await rep.report(f"Failed to send intro content: {e}", "warning")

        await db.add_anime_channel_mapping(channel_name, channel_id)

        await asleep(2)

        await user_client.stop()

        await rep.report(f"Successfully created channel: {channel_name} (ID: {channel_id})", "info")
        return channel_id

    except Exception as e:
        await rep.report(f"Failed to create anime channel: {e}", "error")
        return None


async def build_anime_soul_poster(anime_info, anime_name, output_path, episode_label=None):
    """Create the ANIME SOUL split-screen streaming thumbnail (16:9)."""
    import io
    import textwrap
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter, ImageEnhance

    data = anime_info.adata or {}
    titles = data.get("title") or {}
    title = titles.get("english") or titles.get("romaji") or anime_name

    def clean_value(value, default=""):
        if isinstance(value, list):
            value = value[0] if value else default
        return str(value or default).strip()

    season = clean_value(anime_info.pdata.get("anime_season"), "01")
    episode = clean_value(episode_label or anime_info.pdata.get("episode_number"), "01")
    try:
        season = str(int(season))
    except Exception:
        pass
    try:
        episode = str(int(episode)).zfill(2)
    except Exception:
        pass

    cover_data = data.get("coverImage") or {}
    # Try every artwork source, with retries. Never silently substitute a
    # previous anime's cached/local thumbnail.
    artwork_urls = []
    for candidate in (
        cover_data.get("extraLarge"),
        cover_data.get("large"),
        data.get("poster_url"),
        data.get("bannerImage"),
    ):
        if candidate and candidate not in artwork_urls:
            artwork_urls.append(candidate)

    if not artwork_urls:
        await rep.report(
            f"ANIME SOUL thumbnail unavailable for '{title}': AniList returned no artwork URL.",
            "warning"
        )
        return None

    art = None
    last_art_error = None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            for artwork_url in artwork_urls:
                for attempt in range(1, 4):
                    try:
                        response = await client.get(artwork_url)
                        response.raise_for_status()
                        candidate_art = Image.open(
                            io.BytesIO(response.content)
                        ).convert("RGB")
                        candidate_art.load()
                        art = candidate_art
                        break
                    except Exception as exc:
                        last_art_error = exc
                        await rep.report(
                            f"Thumbnail attempt {attempt}/3 failed for '{title}' "
                            f"({artwork_url}): {exc}",
                            "warning"
                        )
                        if attempt < 3:
                            await asyncio.sleep(attempt * 2)
                if art is not None:
                    break
    except Exception as exc:
        last_art_error = exc

    if art is None:
        await rep.report(
            f"ANIME SOUL thumbnail unavailable after retries for '{title}': {last_art_error}",
            "warning"
        )
        return None

    W, H = 1600, 900
    split_x = 690
    art = ImageEnhance.Color(art).enhance(1.08)
    art = ImageEnhance.Contrast(art).enhance(1.04)

    # Left: full-height poster artwork, cropped to fill without stretching.
    left_art = ImageOps.fit(
        art, (split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.45),
    )

    # Right: same anime artwork, blurred and washed out under a translucent light panel.
    right_art = ImageOps.fit(
        art, (W - split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.35),
    ).filter(ImageFilter.GaussianBlur(16))
    right_art = ImageEnhance.Brightness(right_art).enhance(1.8)
    right_art = ImageEnhance.Color(right_art).enhance(0.45)

    canvas = Image.new("RGB", (W, H), (245, 246, 250))
    canvas.paste(left_art, (0, 0))
    canvas.paste(right_art, (split_x, 0))
    overlay = Image.new("RGBA", (W - split_x, H), (250, 250, 253, 222))
    canvas.paste(overlay, (split_x, 0), overlay)
    draw = ImageDraw.Draw(canvas)

    regular_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    bold_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    def font(size, bold=False):
        for path in (bold_candidates if bold else regular_candidates):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def fit_line(text, fnt, max_width):
        text = str(text)
        while text and draw.textbbox((0, 0), text, font=fnt)[2] > max_width:
            text = text[:-2].rstrip() + "…"
        return text

    def wrapped(text, fnt, max_width, max_lines):
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
            if len(lines) >= max_lines:
                break
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) == max_lines and len(words) > sum(len(x.split()) for x in lines):
            lines[-1] = fit_line(lines[-1] + "…", fnt, max_width)
        return lines

    def wrap_complete(text, fnt, max_width):
        """Wrap every title word; never drop the tail of a long AniList title."""
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    # Left poster branding and title overlay.
    left_overlay = Image.new("RGBA", (split_x, H), (0, 0, 0, 0))
    left_draw = ImageDraw.Draw(left_overlay)
    for y in range(560, H):
        alpha = int(205 * ((y - 560) / (H - 560)))
        left_draw.line((0, y, split_x, y), fill=(0, 0, 0, alpha))
    canvas.paste(left_overlay, (0, 0), left_overlay)
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle((34, 30, 330, 90), radius=15, fill=(8, 12, 22), outline=(255,255,255), width=2)
    draw.text((55, 45), "ANIME SOUL", font=font(29, True), fill=(255,255,255))
    draw.text((235, 51), "/ STREAM", font=font(16, True), fill=(235,55,70))
    # Show the complete AniList title across lines instead of truncating it.
    left_title_font = font(38, True)
    left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    if len(left_title_lines) == 3:
        left_title_font = font(32, True)
        left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    left_title_y = 720 if len(left_title_lines) <= 2 else 690
    for title_line in left_title_lines:
        draw.text((42, left_title_y), title_line, font=left_title_font,
                  fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0))
        left_title_y += left_title_font.size + 8
    draw.text((48, 850), f"SEASON {season}   •   EPISODE {episode}",
              font=font(23, True), fill=(255,220,70))

    # Right-side streaming dashboard header and content geometry.
    rx = split_x + 58
    right_w = W - rx - 58
    draw.text((rx, 40), "ANIME SOUL", font=font(25, True), fill=(15,18,27))
    draw.text((rx+210, 45), "/ STREAM", font=font(18), fill=(210,35,55))
    nav = "HOME     ANIME     SCHEDULE     LIBRARY"
    nav_font = font(17, True)
    nav_width = draw.textbbox((0,0), nav, font=nav_font)[2]
    if nav_width < right_w:
        draw.text((W-58-nav_width, 48), nav, font=nav_font, fill=(35,38,48))

    # Wrap the complete AniList title instead of cutting it off with an ellipsis.
    title_font = font(50, True)
    title_lines = wrap_complete(str(title), title_font, right_w)
    if len(title_lines) == 3:
        title_font = font(42, True)
        title_lines = wrap_complete(str(title), title_font, right_w)
    title_y = 150
    for title_line in title_lines:
        draw.text((rx, title_y), title_line, font=title_font, fill=(8,10,18))
        title_y += title_font.size + 10

    rule_y = title_y + 8
    draw.rounded_rectangle((rx, rule_y, rx+right_w, rule_y+8), radius=4, fill=(25,92,190))
    pill_y = rule_y + 32
    draw.rounded_rectangle((rx, pill_y, rx+195, pill_y+56), radius=26, fill=(20,91,190))
    draw.text((rx+25, pill_y+13), f"SEASON {season}", font=font(23, True), fill=(255,255,255))
    draw.rounded_rectangle((rx+215, pill_y, rx+440, pill_y+56), radius=26, outline=(65,70,85), width=2, fill=(255,255,255))
    draw.text((rx+238, pill_y+13), f"EPISODE {episode}", font=font(23, True), fill=(18,20,28))
    draw.rounded_rectangle((rx+right_w-112, pill_y, rx+right_w, pill_y+56), radius=12, outline=(20,24,35), width=2)
    draw.text((rx+right_w-92, pill_y+13), "HD", font=font(25, True), fill=(18,20,28))

    # Place the AniList synopsis below the variable-height title block.
    synopsis = re.sub(r"<[^>]+>", " ", str(data.get("description") or "Discover the story in this anime."))
    synopsis = re.sub(r"\\s+", " ", synopsis).strip()
    syn_font = font(19)
    sy = pill_y + 82
    synopsis_lines = max(2, min(6, (735 - sy) // 29))
    for line in wrapped(synopsis, syn_font, right_w, synopsis_lines):
        draw.text((rx, sy), line, font=syn_font, fill=(35,38,48))
        sy += 29

    # Watch button.
    button_y = min(650, max(sy + 18, pill_y + 145))
    draw.rounded_rectangle((rx, button_y, rx+310, button_y+78), radius=38, fill=(12,14,20))
    draw.polygon([(rx+38,button_y+24),(rx+38,button_y+54),(rx+64,button_y+39)], fill=(255,255,255))
    draw.text((rx+83, button_y+22), "WATCH NOW", font=font(25, True), fill=(255,255,255))
    draw.ellipse((rx+340, button_y+2, rx+416, button_y+78), outline=(75,80,92), width=2)
    draw.text((rx+363, button_y+19), "☆", font=font(38), fill=(20,24,34))

    # Metadata row at the bottom.
    line_y = 770
    draw.line((rx, line_y, W-58, line_y), fill=(130,135,145), width=2)
    studio_nodes = (data.get("studios") or {}).get("nodes") or []
    studio = studio_nodes[0].get("name") if studio_nodes else "Not listed"
    year = (data.get("startDate") or {}).get("year") or data.get("seasonYear") or "—"
    genres = ", ".join((data.get("genres") or [])[:2]) or "Anime"
    status = str(data.get("status") or "—").replace("_", " ").title()
    metadata = [
        ("YEAR", str(year)),
        ("STUDIO", str(studio)),
        ("GENRES", genres),
        ("STATUS", status),
    ]
    col_w = right_w // 4
    for i, (label, value) in enumerate(metadata):
        x = rx + i * col_w
        draw.text((x, 795), label, font=font(14, True), fill=(100,105,115))
        draw.text((x, 823), fit_line(value, font(17, True), col_w-14), font=font(17, True), fill=(15,18,27))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    canvas.save(output_path, "JPEG", quality=94, optimize=True, progressive=True)
    return output_path


async def get_animes(name, torrent, force=False, qualities_override=None, destination_channel=None, manual_search=False):
    mode = await db.get_mode()
    if mode != "anime":
        await rep.report(f"Skipping {name} because mode is not anime (current mode: {mode})", "info")
        return

    try:
        cleaned_name = clean_anime_title(name)
        base_name = extract_base_anime_name(cleaned_name)
        normalized_name = normalize_anime_name_for_search(base_name)
        
        gdrive_status = await db.get_gdrive_upload()
        if gdrive_status == "on":
            print(f"Checking GDrive mapping for: {normalized_name}")
            await asleep(1.5)
            possible_names = await get_all_possible_anime_names(normalized_name)
            
            for check_name in possible_names:
                for quality in Var.QUALS:
                    gdrive_link = await db.get_gdrive_mapping(check_name, quality)
                    if gdrive_link:
                        await rep.report(f"Found GDrive mapping for {check_name} - {quality}p", "info")
                        await db.saveAnime(torrent, "1", quality, None, gdrive_link)
                        break
                    
        upload_mode = await db.get_upload_mode()
        print(f"get_animes: upload_mode={upload_mode}")
        if upload_mode == "low_end":
            print(f"get_animes: calling process_low_end for {name}")
            await process_low_end(name, torrent, destination_channel=destination_channel)
            return
    except Exception as e:
        await rep.report(f"Error checking upload mode: {str(e)}", "error")
        return

    if not await is_valid_torrent_url(torrent):
        await rep.report(f"Invalid or non-torrent URL for {name}: {torrent}", "error")
        return
    
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        # Cache state per anime episode, not per series. A series can have
        # many episodes; using ani_id alone incorrectly blocks later episodes.
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        encoding_enabled = await db.get_encoding()
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        # Resolve the real destination before duplicate checks. Scheduled
        # automatic releases must use the anime's mapped channel; explicit
        # /search or /addtask destinations must be respected.
        if destination_channel is not None:
            channel_to_use = int(destination_channel)
        else:
            channel_to_use = await get_or_create_anime_channel(name, aniInfo)

        channel_info = await validate_channel_id(channel_to_use)
        if not channel_info:
            await rep.report(
                f"Anime destination {channel_to_use} is unavailable; falling back to Main Channel.",
                "error"
            )
            channel_to_use = int(Var.MAIN_CHANNEL)
        else:
            channel_to_use = int(channel_info.id)

        existing_target_post = (
            await db.getAnimeMainPost(ani_id, ep_no)
            if channel_to_use == int(Var.MAIN_CHANNEL)
            else await db.getAnimeDestinationPost(ani_id, ep_no, channel_to_use)
        )
        if existing_target_post:
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)
            return "already_published"

        # Keep the torrent source in MongoDB so a restart can rediscover this
        # episode even after it falls out of the RSS feed window.
        await db.saveAnimeResume(ani_id, ep_no, name, torrent)

        if force or not existing_target_post:

            if "[Batch]" in name:
                await rep.report(f"Torrent Skipped!\n{name}", "warning")
                return
            
            await rep.report(f"New Anime Torrent Found!\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            # channel_to_use was resolved and validated before duplicate checks.
            channel_valid = bool(await validate_channel_id(channel_to_use))
            if not channel_valid:
                channel_to_use = int(Var.MAIN_CHANNEL)
            
            post_msg = None
            photo_path = None
            all_banners = await db.list_anime_banners()
            banner_url = None
            
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break
            
            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")
            
            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")
            
            try:
                poster_path = f"thumbs/anime_soul_{aniInfo.adata.get('id') or hashlib.md5(cleaned_name.encode()).hexdigest()}.jpg"
                photo_path = await build_anime_soul_poster(aniInfo, cleaned_name, poster_path)
            except Exception as poster_error:
                await rep.report(f"ANIME SOUL poster builder failed; using original artwork: {poster_error}", "warning")
                photo_path = None
            post_msg = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")
            
            dl_path = await download_torrent(torrent, name)
            if not dl_path or not ospath.exists(dl_path):
                await rep.report(f"File Download Incomplete for {name}, Try Again", "error")
                await stat_msg.delete()
                return
            
            post_id = int(datetime.datetime.now().timestamp() * 1000000)
            ffEvent = Event()
            ff_queued[post_id] = ffEvent
            
            if ffLock.locked():
                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Queued to Encode...</i></pre>")
                await rep.report("Added Task to Queue...", "info")
            
            await ffQueue.put(post_id)
            await ffEvent.wait()
            await ffLock.acquire()
            try:
                btns = []
                uploaded_qualities = []
                required_qualities = (list(qualities_override) if qualities_override else list(Var.QUALS)) if encoding_enabled else ["HDRi"]
                saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)

                async def add_quality_button(qual, msg_id):
                    link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(
                        await encode('get-' + str(int(msg_id) * abs(Var.FILE_STORE)))
                    )
                    label = RAW_BTN if qual == "HDRi" else btn_formatter.get(qual, qual + "p")
                    button = InlineKeyboardButton(label, url=link)
                    if btns and len(btns[-1]) == 1:
                        btns[-1].append(button)
                    else:
                        btns.append([button])

                for qual in required_qualities:
                    # Reuse a quality that was already uploaded before the last
                    # crash. Verify the message still exists in FILE_STORE.
                    saved_msg_id = saved_file_ids.get(qual)
                    if saved_msg_id:
                        try:
                            stored_msg = await bot.get_messages(
                                Var.FILE_STORE, message_ids=int(saved_msg_id)
                            )
                            if stored_msg and not getattr(stored_msg, "empty", False):
                                await add_quality_button(qual, saved_msg_id)
                                uploaded_qualities.append(qual)
                                await rep.report(
                                    f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                    "info"
                                )
                                continue
                        except Exception as verify_error:
                            await rep.report(
                                f"Saved {qual} message {saved_msg_id} is unavailable; re-uploading this quality: {verify_error}",
                                "warning"
                            )

                    filename = await aniInfo.get_upname(qual)
                    anime_format = aniInfo.adata.get("format", "").lower()
                    is_movie = anime_format == "movie"
                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n"
                        f"<pre><i>Encoding {qual}...</i></pre>"
                    )
                    await rep.report(f"Starting Encode for {qual}...", "info")

                    out_path = await FFEncoder(
                        stat_msg, dl_path, filename, qual, is_movie=is_movie
                    ).start_encode()
                    if not out_path or not ospath.exists(out_path):
                        raise RuntimeError(
                            f"{qual} encoding failed for {name}; earlier uploaded qualities are saved and will be reused on retry."
                        )

                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{filename}</i></b></blockquote>\n\n"
                        f"<pre><i>Uploading {qual}...</i></pre>"
                    )
                    msg = await TgUploader(stat_msg).upload(out_path, qual)
                    if not msg or not getattr(msg, "id", None):
                        raise RuntimeError(f"Telegram did not confirm {qual} upload for {name}.")

                    # Persist immediately after Telegram confirms the upload.
                    await db.saveAnimeFileId(ani_id, ep_no, qual, msg.id)
                    await db.saveAnime(ani_id, ep_no, qual, None)
                    saved_file_ids[qual] = msg.id
                    await add_quality_button(qual, msg.id)
                    uploaded_qualities.append(qual)
                    await rep.report(f"Successfully uploaded and checkpointed {qual} for {name}.", "info")
                    bot_loop.create_task(extra_utils(msg.id, out_path))

            finally:
                ffLock.release()
            
            await stat_msg.delete()
            if btns:
                markup = InlineKeyboardMarkup(btns)
                # Telegram may temporarily rate-limit the Main Channel post.
                # Retry the publish step without re-uploading the already stored files.
                last_publish_error = None
                for publish_attempt in range(5):
                    try:
                        target_channel = destination_channel if destination_channel is not None else Var.MAIN_CHANNEL
                        if photo_path and ospath.exists(photo_path):
                            with open(photo_path, 'rb') as photo_file:
                                post_msg = await bot.send_photo(
                                    target_channel, photo=photo_file,
                                    caption=caption, reply_markup=markup
                                )
                        else:
                            post_msg = await bot.send_photo(
                                target_channel, photo=photo_url,
                                caption=caption, reply_markup=markup
                            )
                        last_publish_error = None
                        break
                    except FloodWait as flood:
                        wait_seconds = max(1, int(flood.value) + 1)
                        await rep.report(
                            f"Main Channel FLOOD_WAIT: retrying publish in {wait_seconds}s "
                            f"(attempt {publish_attempt + 1}/5).", "warning"
                        )
                        await asyncio.sleep(wait_seconds)
                        last_publish_error = flood
                    except Exception as publish_error:
                        last_publish_error = publish_error
                        break

                if last_publish_error is not None or post_msg is None:
                    raise RuntimeError(
                        f"Failed to publish episode to destination channel: {last_publish_error}"
                    )

                # Only mark the canonical Main Channel post when that was the destination.
                target_channel = destination_channel if destination_channel is not None else Var.MAIN_CHANNEL
                if int(target_channel) == int(Var.MAIN_CHANNEL):
                    await db.saveAnimeMainPost(ani_id, ep_no, post_msg.id)
                else:
                    await db.saveAnimeDestinationPost(ani_id, ep_no, target_channel, post_msg.id)
                for uploaded_qual in uploaded_qualities:
                    await db.saveAnime(ani_id, ep_no, uploaded_qual, post_msg.id)

                # Post sticker only after Telegram confirms the episode announcement.
                # Dedicated automatic routing posts to the anime channel and main channel.
                sticker_targets = []
                if destination_channel is not None:
                    sticker_targets = [] if manual_search else [int(destination_channel)]
                elif channel_to_use != int(Var.MAIN_CHANNEL):
                    sticker_targets = [int(channel_to_use), int(Var.MAIN_CHANNEL)]
                else:
                    sticker_targets = [int(Var.MAIN_CHANNEL)]

                for sticker_target in dict.fromkeys(sticker_targets):
                    await asleep(max(0, getattr(Var, "STICKER_INTERVAL", 1)))
                    await send_sticker_to_channel(
                        sticker_target, getattr(Var, "STICKER_ID", None)
                    )

                await db.markAnimeResumeComplete(ani_id, ep_no)
                # Manual /search uploads send their sticker from the selection
                # workflow, using the exact chosen destination. This prevents
                # the generic uploader path from placing it in Main Channel.
                if not manual_search:
                    try:
                        await asleep(Var.STICKER_INTERVAL)
                        await send_sticker_to_channel(target_channel, Var.STICKER_ID)
                    except FloodWait as flood:
                        await asyncio.sleep(max(1, int(flood.value) + 1))
                        await send_sticker_to_channel(target_channel, Var.STICKER_ID)
                    except Exception as sticker_error:
                        await rep.report(f"Episode published, but sticker send failed: {sticker_error}", "warning")

                # Mark complete only after the Main Channel post exists.
                ani_cache['completed'].add(episode_key)
                ani_cache['ongoing'].discard(episode_key)
            else:
                await rep.report(f"No qualities uploaded for {name}; no Main Channel post sent", "warning")
                raise RuntimeError(f"No qualities uploaded for {name}; episode remains retryable")

            if encoding_enabled:
                await aioremove(dl_path)
        else:
            # Existing Main Channel post is already recorded.
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
    except asyncio.CancelledError:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        try:
            await rep.report(f"Manual task cancelled: {name}", "warning")
        except Exception:
            pass
        raise
    except Exception as error:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(format_exc(), "error")
        # Propagate the failure to fetch_animes so it does not mark this
        # episode as handled and advance to the next RSS item.
        raise

async def extra_utils(msg_id, out_path):
    try:
        msg = await bot.get_messages(Var.FILE_STORE, message_ids=msg_id)

        if hasattr(Var, 'BACKUP_FILE_STORE') and Var.BACKUP_FILE_STORE != 0:
            for chat_id in str(Var.BACKUP_FILE_STORE).split():
                try:
                    await msg.copy(int(chat_id))
                except Exception as e:
                    await rep.report(f"Failed to backup to {chat_id}: {e}", "error")
    except Exception as e:
        await rep.report(f"Error in extra_utils: {e}", "error")

def safe_dirname(name):
    return re.sub(r'[\\/*?:"<>|\'\n\r\t]', '_', name)

async def download_torrent(torrent_url, name=""):
    try:
        unique_dir = generate_unique_dir(name, torrent_url)
        
        if os.path.exists(unique_dir):
            shutil.rmtree(unique_dir)
        os.makedirs(unique_dir, exist_ok=True)
        
        downloader = TorDownloader(unique_dir)
        dl_path = await downloader.download(torrent_url)
        
        if not dl_path or not os.path.exists(dl_path):
            await rep.report(f"Failed to download torrent: {torrent_url}", "error")
            return None
        
        print(f"Download directory contents: {os.listdir(unique_dir)}")
        print(f"Successfully downloaded to: {dl_path}")
        return dl_path
    except Exception as e:
        await rep.report(f"Download error: {str(e)}", "error")
        return None

async def upload_file(file_path, quality):
    try:
        uploader = TgUploader() 
        msg = await uploader.upload(file_path, quality)
        return msg
    except Exception as e:
        await rep.report(f"Upload error: {str(e)}", "error")
        return None
async def process_low_end(name, torrent_url, force=False):
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        encoding_enabled = await db.get_encoding()
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        existing_main_post = await db.getAnimeMainPost(ani_id, ep_no)
        if existing_main_post:
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)
            return

        await db.saveAnimeResume(ani_id, ep_no, name, torrent_url)

        if force or (not (ani_data := await db.getAnime(ani_id)) \
            or (ani_data and not (qual_data := ani_data.get(ep_no))) \
            or (ani_data and qual_data and not all(qual for qual in qual_data.values()))):

            if "[Batch]" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if "(Batch)" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if hasattr(torrent_url, 'size') and torrent_url.size:
                if is_torrent_oversized(str(torrent_url.size), max_size_gb=4.5):
                    await rep.report(f"Torrent Skipped! - Size exceeds 4.5GB limit\n\n{name}", "warning")
                    return

            await rep.report(f"New Anime Torrent Found!\n\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            channel_to_use = target_channel if destination_channel is not None else await get_or_create_anime_channel(name, aniInfo)

            channel_valid = False
            for attempt in range(3):
                if await validate_channel_id(channel_to_use):
                    channel_valid = True
                    break
                await asyncio.sleep(2)

            if not channel_valid:
                await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
                channel_to_use = Var.MAIN_CHANNEL

            all_banners = await db.list_anime_banners()
            banner_url = None
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break

            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")

            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")

            post_msg = None
            main_post = backup_post = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")

            file_store_links = {}
            saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)
            buttons = []
            btns = []
            post_id = None
            for qual in ["480", "720", "1080"]:
                saved_msg_id = saved_file_ids.get(qual)
                if saved_msg_id:
                    try:
                        stored_msg = await bot.get_messages(
                            Var.FILE_STORE, message_ids=int(saved_msg_id)
                        )
                        if stored_msg and not getattr(stored_msg, "empty", False):
                            file_store_links[qual] = int(saved_msg_id)
                            await rep.report(
                                f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                "info"
                            )
                            continue
                    except Exception as verify_error:
                        await rep.report(
                            f"Saved {qual} upload is unavailable; retrying this quality: {verify_error}",
                            "warning"
                        )

                rss_link = Var.RSS_LOW_END.get(qual)
                if not rss_link:
                    continue
                feed = await getfeed(rss_link)
                if not feed or not feed.get("link"):
                    continue

                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Downloading...</i></pre>")
                dl_path = await download_torrent(feed["link"])
                if not dl_path:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to download {qual}p</i></pre>")
                    continue

                if Var.LOW_END_RENAME:
                        editor = TextEditor(name)
                        filename = await aniInfo.get_upname(qual)
                        anime_format = aniInfo.adata.get("format", "").lower()
                        is_movie = anime_format == "movie"
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Encode...</i></pre>")
                        await asleep(1.5)
                        await rep.report("Starting Encode...", "info")
                        try:
                            out_path = await FFEncoder(stat_msg, dl_path, filename, qual, is_movie=is_movie).start_encode()
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Compressed Now Going To Upload...", "info")
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Upload...</i></pre>")
                        await asleep(1.5)
                        try:
                            msg = await TgUploader(stat_msg).upload(out_path, qual)
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Uploaded File into Channel...", "info")
                        msg_id = msg.id
                        file_store_links[qual] = msg_id
                        await db.saveAnimeFileId(ani_id, ep_no, qual, msg_id)
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        saved_file_ids[qual] = msg_id
                        link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(await encode('get-'+str(msg_id * abs(Var.FILE_STORE))))
                        if len(btns) != 0 and len(btns[-1]) == 1:
                            btns[-1].insert(1, InlineKeyboardButton(f"{btn_formatter[qual]}", url=link))
                        else:
                            btns.append([InlineKeyboardButton(f"{btn_formatter[qual]}", url=link)])
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        bot_loop.create_task(extra_utils(msg_id, out_path))

                        gdrive_status = await db.get_gdrive_upload()
                        if gdrive_status == "on":
                            possible_names = await get_all_possible_anime_names(cleaned_name)
                            gdrive_link = None
                            for check_name in possible_names:
                                gdrive_link = await db.get_gdrive_mapping(check_name, qual)
                                if gdrive_link:
                                    try:
                                        from bot.core.gdrive import GdriveUploader
                                        await rep.report(f"Uploading to GDrive for {check_name} - {qual}p", "info")
                                        gdrive_uploader = GdriveUploader()
                                        await gdrive_uploader.upload_file(out_path, gdrive_link)
                                        await rep.report(f"Uploaded to GDrive for {check_name} - {qual}p", "info")
                                    except Exception as e:
                                        await rep.report(f"GDrive upload failed for {check_name} - {qual}p: {e}", "error")
                                    break
                else:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to upload {qual}p</i></pre>")

            quality_order = ["480", "720", "1080", "HDRi"]
            all_buttons = []

            for qual in quality_order:
                msg_id = file_store_links.get(qual)
                if not msg_id:
                    continue

                encoded = await encode(
                    "get-" + str(msg_id * abs(Var.FILE_STORE))
                )
                all_buttons.append(
                    InlineKeyboardButton(
                        text=btn_formatter[qual],
                        url=f"https://t.me/{Var.BOT_USERNAME}?start={encoded}"
                    )
                )

            grouped_buttons = [
                all_buttons[i:i + 2]
                for i in range(0, len(all_buttons), 2)
            ]

            final_caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    final_caption = obfuscate_text_for_copyright(final_caption)
            except Exception as e:
                await rep.report(
                    f"Error applying font changer: {e}",
                    "warning"
                )

            final_markup = (
                InlineKeyboardMarkup(grouped_buttons)
                if grouped_buttons else None
            )

            required_low_end = ["480", "720", "1080", "HDRi"]
            missing_low_end = [q for q in required_low_end if q not in file_store_links]
            if missing_low_end:
                raise RuntimeError(
                    f"Low-end episode {name} is not ready to publish; missing qualities: {', '.join(missing_low_end)}"
                )

            final_post = await bot.send_photo(
                Var.MAIN_CHANNEL, photo=photo_url,
                caption=final_caption, reply_markup=final_markup
            )
            if not final_post or not getattr(final_post, "id", None):
                raise RuntimeError(f"Main Channel did not confirm post for {name}.")

            await db.saveAnimeMainPost(ani_id, ep_no, final_post.id)
            for uploaded_qual in file_store_links:
                await db.saveAnime(ani_id, ep_no, uploaded_qual, final_post.id)
            await db.markAnimeResumeComplete(ani_id, ep_no)
            if ani_id and ep_no:
                await db.mark_episode_completed(ani_id, ep_no)


            if stat_msg:
                await stat_msg.delete()
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)

    except Exception as e:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(f"Low-end processing error: {str(e)}", "error")

async def fetch_animes():
    await rep.report("Fetching Anime Started !!!", "info")
    seen_rss_links = {}
    while True:
        await asleep(15)
        mode, upload_mode = await asyncio.gather(db.get_mode(), db.get_upload_mode())
        if ani_cache.get('global_pause') and not ani_cache.get('manual_task'):
            continue
        if mode != "anime" or not ani_cache['fetch_animes']:
            continue
        try:
            # Scan every configured quality feed in both upload modes.
            # The main RSS list may contain only 1080p; omitting the 480p/720p
            # feeds caused scheduled releases and Sub/Dual variants to be missed.
            configured_feeds = list(Var.RSS_ITEMS_ANIME or [])
            quality_feeds = [
                Var.RSS_LOW_END.get(q)
                for q in ("480", "720", "1080")
                if Var.RSS_LOW_END.get(q)
            ]
            rss_urls = list(dict.fromkeys(
                url for url in configured_feeds + quality_feeds if url
            ))

            # Recover unfinished episodes from MongoDB before scanning fresh RSS.
            # This works even if the source entry has fallen out of the RSS window.
            recovered_links = set()
            resume_failed = False
            try:
                resume_tasks = await db.getAnimeResumeTasks()
                for resume_task in resume_tasks:
                    resume_name = resume_task.get("name")
                    resume_link = resume_task.get("torrent_url")
                    if not resume_name or not resume_link:
                        continue
                    recovered_links.add(resume_link)
                    await rep.report(
                        f"Resuming unfinished episode from saved checkpoint: {resume_name}",
                        "info"
                    )
                    # Do not advance to another episode until this saved episode
                    # completes. get_animes re-raises failures after preserving
                    # successful per-quality checkpoints.
                    await get_animes(resume_name, resume_link)
            except Exception as resume_error:
                resume_failed = True
                await rep.report(
                    f"Episode resume failed; holding the queue on this unfinished episode. "
                    f"It will retry on the next scan: {resume_error}",
                    "error"
                )

            if resume_failed:
                # Never start fresh RSS jobs while an earlier episode is unfinished.
                continue

            feeds_by_url = await asyncio.gather(*(getfeeds(url) for url in rss_urls))
            pending = []
            for rss_url, entries in zip(rss_urls, feeds_by_url):
                if not entries:
                    continue
                seen = seen_rss_links.setdefault(rss_url, set())
                # Process oldest unseen entry first so a burst of releases is not skipped.
                for entry in entries:
                    link = getattr(entry, "link", None)
                    title = getattr(entry, "title", None)
                    if not link or not title or link in seen or link in recovered_links:
                        continue
                    seen.add(link)
                    pending.append((title, link))
                # Bound memory while retaining a recent de-duplication window.
                if len(seen) > 500:
                    seen_rss_links[rss_url] = set(list(seen)[-250:])

            # One episode at a time: finish/retry the current item before advancing.
            for title, link in pending:
                try:
                    await get_animes(title, link)
                except Exception:
                    await rep.report(f"Episode processing failed; will retry on next RSS scan: {title}\\n{format_exc()}", "error")
                    # Make it eligible for the next scan instead of permanently consuming it.
                    for rss_url in rss_urls:
                        seen_rss_links.get(rss_url, set()).discard(link)
                    break
        except Exception as e:
            await rep.report(f"Anime fetch error: {str(e)}", "error")

async def get_or_create_anime_channel(name: str, aniInfo):
    try:
        base_anime_name = extract_base_anime_name(name)
        normalized_name = normalize_anime_name_for_search(name)
        
        await rep.report(f"Searching for existing channel for: {base_anime_name}", "info")
        await asleep(1.5)
        custom_channel = await db.get_anime_channel(normalized_name)
        if custom_channel:
            await rep.report(f"Found custom channel mapping: {custom_channel}", "info")
            return custom_channel

        raw_entry = await db.get_raw_anime_channel(normalized_name)
        if raw_entry is not None and 'channel_id' not in raw_entry:
            await rep.report(f"Found banner-only mapping for {normalized_name} (no channel_id)", "info")
            channel_creation = await db.get_channel_creation()
            if channel_creation and Var.USER_SESSION:
                await rep.report("Banner-only mapping found â creating channel because channel creation is enabled", "info")
                new_channel = await create_anime_channel(name, aniInfo)
                if new_channel:
                    await db.add_anime_channel_mapping(normalized_name, new_channel)
                    await rep.report(f"Created channel for banner-only mapping and updated DB: {new_channel}", "info")
                    return new_channel
                else:
                    await rep.report("Failed to create channel for banner-only mapping, falling back to main channel", "error")
                    return Var.MAIN_CHANNEL
            else:
                await rep.report("Banner-only mapping present but channel creation is disabled or no user session; using main channel", "info")
                return Var.MAIN_CHANNEL

        existing_channel = await find_existing_anime_channel(name)
        if existing_channel:
            await rep.report(f"Found existing channel for {base_anime_name}: {existing_channel}", "info")
            await db.add_anime_channel_mapping(normalized_name, existing_channel)
            return existing_channel
        
        channel_creation = await db.get_channel_creation()
        if not channel_creation or not Var.USER_SESSION:
            await rep.report("Channel creation disabled or no user session, using main channel", "info")
            return Var.MAIN_CHANNEL

        await rep.report(f"Creating new channel for: {base_anime_name}", "info")
        new_channel = await create_anime_channel(name, aniInfo)
        
        if new_channel:
            await db.add_anime_channel_mapping(normalized_name, new_channel)
            await rep.report(f"Successfully created and saved channel mapping for {base_anime_name}", "info")
            return new_channel
        else:
            await rep.report("Failed to create channel, using main channel", "error")
            return Var.MAIN_CHANNEL
            
    except Exception as e:
        await rep.report(f"Error in get_or_create_anime_channel: {e}", "error")
        return Var.MAIN_CHANNEL

async def create_anime_channel(name, aniInfo):
    try:
        if not Var.USER_SESSION:
            await rep.report("User session not available for channel creation", "error")
            return None

        from pyrogram import Client
        user_client = Client("user_session", session_string=Var.USER_SESSION, 
                           api_id=Var.API_ID, api_hash=Var.API_HASH)

        await user_client.start()

        anime_format = (aniInfo.adata.get("format", "") or "").lower()
        is_movie = anime_format == "movie" or (
            hasattr(aniInfo, "is_movie_title") and aniInfo.is_movie_title(name)
        )

        titles = aniInfo.adata.get("title", {})
        channel_name = titles.get("english") or titles.get("romaji") or clean_anime_title(name)
        title = channel_name
        status = aniInfo.adata.get("status", "Unknown").capitalize()
        genres = aniInfo.adata.get("genres", [])[:3]
        genres_formatted = ", ".join(genres) if genres else "Unknown"

        if is_movie:
            description = (
                f"â¢ Movie: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )
        else:
            description = (
                f"â¢ Anime: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )

        created = await user_client.create_channel(channel_name, description)
        channel_id = created.id

        poster_url = (
            aniInfo.adata.get("coverImage", {}).get("large")
            or aniInfo.adata.get("coverImage", {}).get("medium")
            or aniInfo.adata.get("coverImage", {}).get("small")
        )
        if poster_url:
            filename = f"/tmp/{channel_id}_logo.jpg"
            filename = download_image(poster_url, filename)
            if filename:
                try:
                    await user_client.set_chat_photo(channel_id, photo=filename)
                    print(f"Set channel photo for {channel_id} successfully!")
                    remove(filename)
                except Exception as e:
                    print(f"Failed to set channel photo: {e}")
                    await rep.report(f"Failed to set channel photo: {e}", "warning")
            else:
                print("Image file was not downloaded or does not exist.")
                await rep.report(f"Image file was not downloaded or does not exist.", "warning")
        else:
            print("No poster URL found in AniList data.")
            await rep.report(f"No poster URL found in AniList data.", "warning")

        try:
            from pyrogram.types import ChatPrivileges
            await user_client.promote_chat_member(
                channel_id,
                Var.CREATION_USERNAME,
                privileges=ChatPrivileges(
                    can_post_messages=True,
                    can_edit_messages=True,
                    can_delete_messages=True,
                    can_invite_users=True,
                    can_change_info=True,
                    can_pin_messages=True,
                    can_manage_chat=True,
                    can_manage_video_chats=False,
                    is_anonymous=False
                )
            )
            await rep.report(f"Successfully added and promoted bot in channel", "info")
        except Exception as e:
            await rep.report(f"Failed to add/promote bot in channel: {e}", "error")

        try:
            titles = aniInfo.adata.get("title", {})
            genres = ', '.join(aniInfo.adata.get("genres", [])[:3])
            anime_type = aniInfo.adata.get("format", "Unknown")
            avg_rating = aniInfo.adata.get("averageScore", "N/A")
            status = aniInfo.adata.get("status", "Unknown")
            start_date = aniInfo.adata.get("startDate", {})
            end_date = aniInfo.adata.get("endDate", {})
            runtime = aniInfo.adata.get("duration", "Unknown")
            episodes = aniInfo.adata.get("episodes", "Unknown")
            desc = aniInfo.adata.get("description", "No description available").replace("<br>", "").replace("<i>", "").replace("</i>", "").strip()
            desc = (desc[:200] + "...") if len(desc) > 200 else desc 
            site_url = aniInfo.adata.get("siteUrl", "https://anilist.co")
            media_id = aniInfo.adata.get("id")

            english = titles.get("english", "Unknown")
            romaji = titles.get("romaji", "Unknown")
            display_title = english if english != "Unknown" else romaji

            graphic_poster_url = f"https://img.anili.st/media/{media_id}"

            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.head(graphic_poster_url) as resp:
                    if resp.status != 200:
                        graphic_poster_url = poster_url or "https://imgs.search.brave.com/33oSQBqhBoRB6akjKPud5oFd-PxnMFEm1AjnBTS2ZhE/rs:fit:860:0:0:0/g:ce/aHR0cHM6Ly9pbWcu/ZnJlZXBpay5jb20v/dmVjdG9yLXByZW1p/dW0vaWx1c3RyYWNp/b24tYW5pbWUtZXJy/b3ItNDA0LXBhZ2lu/YS1uby1lbmNvbnRy/YWRhXzE1MDk3Mi0y/NTY0LmpwZw"

            if is_movie:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ Release Date : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Duration : {runtime} minutes</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""
            else:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ First aired : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Last aired : {end_date.get("year", '')}-{end_date.get("month", '')}-{end_date.get("day", '')}
â£ Runtime : {runtime} minutes
â£ No of episodes : {episodes}</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""

            await user_client.send_photo(
                chat_id=channel_id,
                photo=graphic_poster_url,
                caption=caption
            )

            STICKER_ID = "CAACAgUAAxkBAAIERmhruO6M5RpLtZjb2yrvPJxTuw0IAAIzEwACTrLwVSdrhgIPuK1TNgQ"
            await user_client.send_sticker(channel_id, STICKER_ID)

            await rep.report("Intro poster, caption, and sticker sent to channel", "info")

        except Exception as e:
            await rep.report(f"Failed to send intro content: {e}", "warning")

        await db.add_anime_channel_mapping(channel_name, channel_id)

        await asleep(2)

        await user_client.stop()

        await rep.report(f"Successfully created channel: {channel_name} (ID: {channel_id})", "info")
        return channel_id

    except Exception as e:
        await rep.report(f"Failed to create anime channel: {e}", "error")
        return None


async def build_anime_soul_poster(anime_info, anime_name, output_path, episode_label=None):
    """Create the ANIME SOUL split-screen streaming thumbnail (16:9)."""
    import io
    import textwrap
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter, ImageEnhance

    data = anime_info.adata or {}
    titles = data.get("title") or {}
    title = titles.get("english") or titles.get("romaji") or anime_name

    def clean_value(value, default=""):
        if isinstance(value, list):
            value = value[0] if value else default
        return str(value or default).strip()

    season = clean_value(anime_info.pdata.get("anime_season"), "01")
    episode = clean_value(episode_label or anime_info.pdata.get("episode_number"), "01")
    try:
        season = str(int(season))
    except Exception:
        pass
    try:
        episode = str(int(episode)).zfill(2)
    except Exception:
        pass

    cover_data = data.get("coverImage") or {}
    cover_url = cover_data.get("extraLarge") or cover_data.get("large") or data.get("poster_url")
    banner_url = data.get("bannerImage")
    # Prefer the portrait cover for the large poster panel; use banner if cover is unavailable.
    artwork_url = cover_url or banner_url
    if not artwork_url:
        await rep.report("ANIME SOUL split-screen thumbnail skipped: AniList has no artwork URL.", "warning")
        return None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(artwork_url)
            response.raise_for_status()
            art = Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception as exc:
        await rep.report(f"ANIME SOUL artwork download failed: {exc}", "warning")
        return None

    W, H = 1600, 900
    split_x = 690
    art = ImageEnhance.Color(art).enhance(1.08)
    art = ImageEnhance.Contrast(art).enhance(1.04)

    # Left: full-height poster artwork, cropped to fill without stretching.
    left_art = ImageOps.fit(
        art, (split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.45),
    )

    # Right: same anime artwork, blurred and washed out under a translucent light panel.
    right_art = ImageOps.fit(
        art, (W - split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.35),
    ).filter(ImageFilter.GaussianBlur(16))
    right_art = ImageEnhance.Brightness(right_art).enhance(1.8)
    right_art = ImageEnhance.Color(right_art).enhance(0.45)

    canvas = Image.new("RGB", (W, H), (245, 246, 250))
    canvas.paste(left_art, (0, 0))
    canvas.paste(right_art, (split_x, 0))
    overlay = Image.new("RGBA", (W - split_x, H), (250, 250, 253, 222))
    canvas.paste(overlay, (split_x, 0), overlay)
    draw = ImageDraw.Draw(canvas)

    regular_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    bold_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    def font(size, bold=False):
        for path in (bold_candidates if bold else regular_candidates):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def fit_line(text, fnt, max_width):
        text = str(text)
        while text and draw.textbbox((0, 0), text, font=fnt)[2] > max_width:
            text = text[:-2].rstrip() + "…"
        return text

    def wrapped(text, fnt, max_width, max_lines):
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
            if len(lines) >= max_lines:
                break
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) == max_lines and len(words) > sum(len(x.split()) for x in lines):
            lines[-1] = fit_line(lines[-1] + "…", fnt, max_width)
        return lines

    def wrap_complete(text, fnt, max_width):
        """Wrap every title word; never drop the tail of a long AniList title."""
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    # Left poster branding and title overlay.
    left_overlay = Image.new("RGBA", (split_x, H), (0, 0, 0, 0))
    left_draw = ImageDraw.Draw(left_overlay)
    for y in range(560, H):
        alpha = int(205 * ((y - 560) / (H - 560)))
        left_draw.line((0, y, split_x, y), fill=(0, 0, 0, alpha))
    canvas.paste(left_overlay, (0, 0), left_overlay)
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle((34, 30, 330, 90), radius=15, fill=(8, 12, 22), outline=(255,255,255), width=2)
    draw.text((55, 45), "ANIME SOUL", font=font(29, True), fill=(255,255,255))
    draw.text((235, 51), "/ STREAM", font=font(16, True), fill=(235,55,70))
    # Show the complete AniList title across lines instead of truncating it.
    left_title_font = font(38, True)
    left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    if len(left_title_lines) == 3:
        left_title_font = font(32, True)
        left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    left_title_y = 720 if len(left_title_lines) <= 2 else 690
    for title_line in left_title_lines:
        draw.text((42, left_title_y), title_line, font=left_title_font,
                  fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0))
        left_title_y += left_title_font.size + 8
    draw.text((48, 850), f"SEASON {season}   •   EPISODE {episode}",
              font=font(23, True), fill=(255,220,70))

    # Right-side streaming dashboard header and content geometry.
    rx = split_x + 58
    right_w = W - rx - 58
    draw.text((rx, 40), "ANIME SOUL", font=font(25, True), fill=(15,18,27))
    draw.text((rx+210, 45), "/ STREAM", font=font(18), fill=(210,35,55))
    nav = "HOME     ANIME     SCHEDULE     LIBRARY"
    nav_font = font(17, True)
    nav_width = draw.textbbox((0,0), nav, font=nav_font)[2]
    if nav_width < right_w:
        draw.text((W-58-nav_width, 48), nav, font=nav_font, fill=(35,38,48))

    # Wrap the complete AniList title instead of cutting it off with an ellipsis.
    title_font = font(50, True)
    title_lines = wrap_complete(str(title), title_font, right_w)
    if len(title_lines) == 3:
        title_font = font(42, True)
        title_lines = wrap_complete(str(title), title_font, right_w)
    title_y = 150
    for title_line in title_lines:
        draw.text((rx, title_y), title_line, font=title_font, fill=(8,10,18))
        title_y += title_font.size + 10

    rule_y = title_y + 8
    draw.rounded_rectangle((rx, rule_y, rx+right_w, rule_y+8), radius=4, fill=(25,92,190))
    pill_y = rule_y + 32
    draw.rounded_rectangle((rx, pill_y, rx+195, pill_y+56), radius=26, fill=(20,91,190))
    draw.text((rx+25, pill_y+13), f"SEASON {season}", font=font(23, True), fill=(255,255,255))
    draw.rounded_rectangle((rx+215, pill_y, rx+440, pill_y+56), radius=26, outline=(65,70,85), width=2, fill=(255,255,255))
    draw.text((rx+238, pill_y+13), f"EPISODE {episode}", font=font(23, True), fill=(18,20,28))
    draw.rounded_rectangle((rx+right_w-112, pill_y, rx+right_w, pill_y+56), radius=12, outline=(20,24,35), width=2)
    draw.text((rx+right_w-92, pill_y+13), "HD", font=font(25, True), fill=(18,20,28))

    # Place the AniList synopsis below the variable-height title block.
    synopsis = re.sub(r"<[^>]+>", " ", str(data.get("description") or "Discover the story in this anime."))
    synopsis = re.sub(r"\\s+", " ", synopsis).strip()
    syn_font = font(19)
    sy = pill_y + 82
    synopsis_lines = max(2, min(6, (735 - sy) // 29))
    for line in wrapped(synopsis, syn_font, right_w, synopsis_lines):
        draw.text((rx, sy), line, font=syn_font, fill=(35,38,48))
        sy += 29

    # Watch button.
    button_y = min(650, max(sy + 18, pill_y + 145))
    draw.rounded_rectangle((rx, button_y, rx+310, button_y+78), radius=38, fill=(12,14,20))
    draw.polygon([(rx+38,button_y+24),(rx+38,button_y+54),(rx+64,button_y+39)], fill=(255,255,255))
    draw.text((rx+83, button_y+22), "WATCH NOW", font=font(25, True), fill=(255,255,255))
    draw.ellipse((rx+340, button_y+2, rx+416, button_y+78), outline=(75,80,92), width=2)
    draw.text((rx+363, button_y+19), "☆", font=font(38), fill=(20,24,34))

    # Metadata row at the bottom.
    line_y = 770
    draw.line((rx, line_y, W-58, line_y), fill=(130,135,145), width=2)
    studio_nodes = (data.get("studios") or {}).get("nodes") or []
    studio = studio_nodes[0].get("name") if studio_nodes else "Not listed"
    year = (data.get("startDate") or {}).get("year") or data.get("seasonYear") or "—"
    genres = ", ".join((data.get("genres") or [])[:2]) or "Anime"
    status = str(data.get("status") or "—").replace("_", " ").title()
    metadata = [
        ("YEAR", str(year)),
        ("STUDIO", str(studio)),
        ("GENRES", genres),
        ("STATUS", status),
    ]
    col_w = right_w // 4
    for i, (label, value) in enumerate(metadata):
        x = rx + i * col_w
        draw.text((x, 795), label, font=font(14, True), fill=(100,105,115))
        draw.text((x, 823), fit_line(value, font(17, True), col_w-14), font=font(17, True), fill=(15,18,27))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    canvas.save(output_path, "JPEG", quality=94, optimize=True, progressive=True)
    return output_path


async def get_animes(name, torrent, force=False):
    mode = await db.get_mode()
    if mode != "anime":
        await rep.report(f"Skipping {name} because mode is not anime (current mode: {mode})", "info")
        return

    try:
        cleaned_name = clean_anime_title(name)
        base_name = extract_base_anime_name(cleaned_name)
        normalized_name = normalize_anime_name_for_search(base_name)
        
        gdrive_status = await db.get_gdrive_upload()
        if gdrive_status == "on":
            print(f"Checking GDrive mapping for: {normalized_name}")
            await asleep(1.5)
            possible_names = await get_all_possible_anime_names(normalized_name)
            
            for check_name in possible_names:
                for quality in Var.QUALS:
                    gdrive_link = await db.get_gdrive_mapping(check_name, quality)
                    if gdrive_link:
                        await rep.report(f"Found GDrive mapping for {check_name} - {quality}p", "info")
                        await db.saveAnime(torrent, "1", quality, None, gdrive_link)
                        break
                    
        upload_mode = await db.get_upload_mode()
        print(f"get_animes: upload_mode={upload_mode}")
        if upload_mode == "low_end":
            print(f"get_animes: calling process_low_end for {name}")
            await process_low_end(name, torrent)
            return
    except Exception as e:
        await rep.report(f"Error checking upload mode: {str(e)}", "error")
        return

    if not await is_valid_torrent_url(torrent):
        await rep.report(f"Invalid or non-torrent URL for {name}: {torrent}", "error")
        return
    
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        # Cache state per anime episode, not per series. A series can have
        # many episodes; using ani_id alone incorrectly blocks later episodes.
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        encoding_enabled = await db.get_encoding()
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        # If the Main Channel post already exists, never publish it twice.
        existing_main_post = await db.getAnimeMainPost(ani_id, ep_no)
        if existing_main_post:
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)
            return

        # Keep the torrent source in MongoDB so a restart can rediscover this
        # episode even after it falls out of the RSS feed window.
        await db.saveAnimeResume(ani_id, ep_no, name, torrent)
        
        if force or not existing_main_post:
            
            if "[Batch]" in name:
                await rep.report(f"Torrent Skipped!\n{name}", "warning")
                return
            
            await rep.report(f"New Anime Torrent Found!\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            channel_to_use = target_channel if destination_channel is not None else await get_or_create_anime_channel(name, aniInfo)
            
            channel_valid = False
            for attempt in range(3):
                if await validate_channel_id(channel_to_use):
                    channel_valid = True
                    break
                await asyncio.sleep(2)
            
            if not channel_valid:
                await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
                channel_to_use = Var.MAIN_CHANNEL
            
            post_msg = None
            photo_path = None
            all_banners = await db.list_anime_banners()
            banner_url = None
            
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break
            
            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")
            
            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")
            
            try:
                poster_path = f"thumbs/anime_soul_{aniInfo.adata.get('id') or hashlib.md5(cleaned_name.encode()).hexdigest()}.jpg"
                photo_path = await build_anime_soul_poster(aniInfo, cleaned_name, poster_path)
            except Exception as poster_error:
                await rep.report(f"ANIME SOUL poster builder failed; using original artwork: {poster_error}", "warning")
                photo_path = None
            post_msg = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")
            
            dl_path = await download_torrent(torrent, name)
            if not dl_path or not ospath.exists(dl_path):
                await rep.report(f"File Download Incomplete for {name}, Try Again", "error")
                await stat_msg.delete()
                return
            
            post_id = int(datetime.datetime.now().timestamp() * 1000000)
            ffEvent = Event()
            ff_queued[post_id] = ffEvent
            
            if ffLock.locked():
                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Queued to Encode...</i></pre>")
                await rep.report("Added Task to Queue...", "info")
            
            await ffQueue.put(post_id)
            await ffEvent.wait()
            await ffLock.acquire()
            try:
                btns = []
                uploaded_qualities = []
                required_qualities = list(Var.QUALS) if encoding_enabled else ["HDRi"]
                saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)

                async def add_quality_button(qual, msg_id):
                    link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(
                        await encode('get-' + str(int(msg_id) * abs(Var.FILE_STORE)))
                    )
                    label = RAW_BTN if qual == "HDRi" else btn_formatter.get(qual, qual + "p")
                    button = InlineKeyboardButton(label, url=link)
                    if btns and len(btns[-1]) == 1:
                        btns[-1].append(button)
                    else:
                        btns.append([button])

                for qual in required_qualities:
                    # Reuse a quality that was already uploaded before the last
                    # crash. Verify the message still exists in FILE_STORE.
                    saved_msg_id = saved_file_ids.get(qual)
                    if saved_msg_id:
                        try:
                            stored_msg = await bot.get_messages(
                                Var.FILE_STORE, message_ids=int(saved_msg_id)
                            )
                            if stored_msg and not getattr(stored_msg, "empty", False):
                                await add_quality_button(qual, saved_msg_id)
                                uploaded_qualities.append(qual)
                                await rep.report(
                                    f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                    "info"
                                )
                                continue
                        except Exception as verify_error:
                            await rep.report(
                                f"Saved {qual} message {saved_msg_id} is unavailable; re-uploading this quality: {verify_error}",
                                "warning"
                            )

                    filename = await aniInfo.get_upname(qual)
                    anime_format = aniInfo.adata.get("format", "").lower()
                    is_movie = anime_format == "movie"
                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n"
                        f"<pre><i>Encoding {qual}...</i></pre>"
                    )
                    await rep.report(f"Starting Encode for {qual}...", "info")

                    out_path = await FFEncoder(
                        stat_msg, dl_path, filename, qual, is_movie=is_movie
                    ).start_encode()
                    if not out_path or not ospath.exists(out_path):
                        raise RuntimeError(
                            f"{qual} encoding failed for {name}; earlier uploaded qualities are saved and will be reused on retry."
                        )

                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{filename}</i></b></blockquote>\n\n"
                        f"<pre><i>Uploading {qual}...</i></pre>"
                    )
                    msg = await TgUploader(stat_msg).upload(out_path, qual)
                    if not msg or not getattr(msg, "id", None):
                        raise RuntimeError(f"Telegram did not confirm {qual} upload for {name}.")

                    # Persist immediately after Telegram confirms the upload.
                    await db.saveAnimeFileId(ani_id, ep_no, qual, msg.id)
                    await db.saveAnime(ani_id, ep_no, qual, None)
                    saved_file_ids[qual] = msg.id
                    await add_quality_button(qual, msg.id)
                    uploaded_qualities.append(qual)
                    await rep.report(f"Successfully uploaded and checkpointed {qual} for {name}.", "info")
                    bot_loop.create_task(extra_utils(msg.id, out_path))

            finally:
                ffLock.release()
            
            await stat_msg.delete()
            if btns:
                markup = InlineKeyboardMarkup(btns)
                # Telegram may temporarily rate-limit the Main Channel post.
                # Retry the publish step without re-uploading the already stored files.
                last_publish_error = None
                for publish_attempt in range(5):
                    try:
                        if photo_path and ospath.exists(photo_path):
                            with open(photo_path, 'rb') as photo_file:
                                post_msg = await bot.send_photo(
                                    Var.MAIN_CHANNEL, photo=photo_file,
                                    caption=caption, reply_markup=markup
                                )
                        else:
                            post_msg = await bot.send_photo(
                                Var.MAIN_CHANNEL, photo=photo_url,
                                caption=caption, reply_markup=markup
                            )
                        last_publish_error = None
                        break
                    except FloodWait as flood:
                        wait_seconds = max(1, int(flood.value) + 1)
                        await rep.report(
                            f"Main Channel FLOOD_WAIT: retrying publish in {wait_seconds}s "
                            f"(attempt {publish_attempt + 1}/5).", "warning"
                        )
                        await asyncio.sleep(wait_seconds)
                        last_publish_error = flood
                    except Exception as publish_error:
                        last_publish_error = publish_error
                        break

                if last_publish_error is not None or post_msg is None:
                    raise RuntimeError(
                        f"Failed to publish completed episode to Main Channel: {last_publish_error}"
                    )

                # Persist the Main Channel post ID immediately after send succeeds.
                await db.saveAnimeMainPost(ani_id, ep_no, post_msg.id)
                for uploaded_qual in uploaded_qualities:
                    await db.saveAnime(ani_id, ep_no, uploaded_qual, post_msg.id)

                await db.markAnimeResumeComplete(ani_id, ep_no)
                # Keep the Ongoing Anime Time sticker beside the episode in
                # its actual destination. Never send an extra sticker to Main
                # Channel when this episode was uploaded to a dedicated channel.
                sticker_channel = (
                    int(destination_channel)
                    if destination_channel is not None
                    else int(channel_to_use)
                )
                try:
                    await asleep(Var.STICKER_INTERVAL)
                    await send_sticker_to_channel(sticker_channel, Var.STICKER_ID)
                except FloodWait as flood:
                    await asyncio.sleep(max(1, int(flood.value) + 1))
                    await send_sticker_to_channel(sticker_channel, Var.STICKER_ID)
                except Exception as sticker_error:
                    await rep.report(
                        f"Episode published to {sticker_channel}, but sticker send failed: {sticker_error}",
                        "warning"
                    )

                # Mark complete only after the Main Channel post exists.
                ani_cache['completed'].add(episode_key)
                ani_cache['ongoing'].discard(episode_key)
            else:
                await rep.report(f"No qualities uploaded for {name}; no Main Channel post sent", "warning")
                raise RuntimeError(f"No qualities uploaded for {name}; episode remains retryable")

            if encoding_enabled:
                await aioremove(dl_path)
        else:
            # Existing Main Channel post is already recorded.
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
        return "uploaded"
    except Exception as error:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(format_exc(), "error")
        # Propagate the failure to fetch_animes so it does not mark this
        # episode as handled and advance to the next RSS item.
        raise

async def extra_utils(msg_id, out_path):
    try:
        msg = await bot.get_messages(Var.FILE_STORE, message_ids=msg_id)

        if hasattr(Var, 'BACKUP_FILE_STORE') and Var.BACKUP_FILE_STORE != 0:
            for chat_id in str(Var.BACKUP_FILE_STORE).split():
                try:
                    await msg.copy(int(chat_id))
                except Exception as e:
                    await rep.report(f"Failed to backup to {chat_id}: {e}", "error")
    except Exception as e:
        await rep.report(f"Error in extra_utils: {e}", "error")

def safe_dirname(name):
    return re.sub(r'[\\/*?:"<>|\'\n\r\t]', '_', name)

async def download_torrent(torrent_url, name=""):
    try:
        unique_dir = generate_unique_dir(name, torrent_url)
        
        if os.path.exists(unique_dir):
            shutil.rmtree(unique_dir)
        os.makedirs(unique_dir, exist_ok=True)
        
        downloader = TorDownloader(unique_dir)
        dl_path = await downloader.download(torrent_url)
        
        if not dl_path or not os.path.exists(dl_path):
            await rep.report(f"Failed to download torrent: {torrent_url}", "error")
            return None
        
        print(f"Download directory contents: {os.listdir(unique_dir)}")
        print(f"Successfully downloaded to: {dl_path}")
        return dl_path
    except Exception as e:
        await rep.report(f"Download error: {str(e)}", "error")
        return None

async def upload_file(file_path, quality):
    try:
        uploader = TgUploader() 
        msg = await uploader.upload(file_path, quality)
        return msg
    except Exception as e:
        await rep.report(f"Upload error: {str(e)}", "error")
        return None

async def process_low_end(name, torrent_url, force=False):
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        encoding_enabled = await db.get_encoding()
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        existing_main_post = await db.getAnimeMainPost(ani_id, ep_no)
        if existing_main_post:
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)
            return

        await db.saveAnimeResume(ani_id, ep_no, name, torrent_url)

        if force or (not (ani_data := await db.getAnime(ani_id)) \
            or (ani_data and not (qual_data := ani_data.get(ep_no))) \
            or (ani_data and qual_data and not all(qual for qual in qual_data.values()))):

            if "[Batch]" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if "(Batch)" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if hasattr(torrent_url, 'size') and torrent_url.size:
                if is_torrent_oversized(str(torrent_url.size), max_size_gb=4.5):
                    await rep.report(f"Torrent Skipped! - Size exceeds 4.5GB limit\n\n{name}", "warning")
                    return

            await rep.report(f"New Anime Torrent Found!\n\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            channel_to_use = target_channel if destination_channel is not None else await get_or_create_anime_channel(name, aniInfo)

            channel_valid = False
            for attempt in range(3):
                if await validate_channel_id(channel_to_use):
                    channel_valid = True
                    break
                await asyncio.sleep(2)

            if not channel_valid:
                await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
                channel_to_use = Var.MAIN_CHANNEL

            all_banners = await db.list_anime_banners()
            banner_url = None
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break

            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")

            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")

            post_msg = None
            main_post = backup_post = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")

            file_store_links = {}
            saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)
            buttons = []
            btns = []
            post_id = None
            for qual in ["480", "720", "1080"]:
                saved_msg_id = saved_file_ids.get(qual)
                if saved_msg_id:
                    try:
                        stored_msg = await bot.get_messages(
                            Var.FILE_STORE, message_ids=int(saved_msg_id)
                        )
                        if stored_msg and not getattr(stored_msg, "empty", False):
                            file_store_links[qual] = int(saved_msg_id)
                            await rep.report(
                                f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                "info"
                            )
                            continue
                    except Exception as verify_error:
                        await rep.report(
                            f"Saved {qual} upload is unavailable; retrying this quality: {verify_error}",
                            "warning"
                        )

                rss_link = Var.RSS_LOW_END.get(qual)
                if not rss_link:
                    continue
                feed = await getfeed(rss_link)
                if not feed or not feed.get("link"):
                    continue

                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Downloading...</i></pre>")
                dl_path = await download_torrent(feed["link"])
                if not dl_path:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to download {qual}p</i></pre>")
                    continue

                if Var.LOW_END_RENAME:
                        editor = TextEditor(name)
                        filename = await aniInfo.get_upname(qual)
                        anime_format = aniInfo.adata.get("format", "").lower()
                        is_movie = anime_format == "movie"
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Encode...</i></pre>")
                        await asleep(1.5)
                        await rep.report("Starting Encode...", "info")
                        try:
                            out_path = await FFEncoder(stat_msg, dl_path, filename, qual, is_movie=is_movie).start_encode()
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Compressed Now Going To Upload...", "info")
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Upload...</i></pre>")
                        await asleep(1.5)
                        try:
                            msg = await TgUploader(stat_msg).upload(out_path, qual)
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Uploaded File into Channel...", "info")
                        msg_id = msg.id
                        file_store_links[qual] = msg_id
                        await db.saveAnimeFileId(ani_id, ep_no, qual, msg_id)
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        saved_file_ids[qual] = msg_id
                        link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(await encode('get-'+str(msg_id * abs(Var.FILE_STORE))))
                        if len(btns) != 0 and len(btns[-1]) == 1:
                            btns[-1].insert(1, InlineKeyboardButton(f"{btn_formatter[qual]}", url=link))
                        else:
                            btns.append([InlineKeyboardButton(f"{btn_formatter[qual]}", url=link)])
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        bot_loop.create_task(extra_utils(msg_id, out_path))

                        gdrive_status = await db.get_gdrive_upload()
                        if gdrive_status == "on":
                            possible_names = await get_all_possible_anime_names(cleaned_name)
                            gdrive_link = None
                            for check_name in possible_names:
                                gdrive_link = await db.get_gdrive_mapping(check_name, qual)
                                if gdrive_link:
                                    try:
                                        from bot.core.gdrive import GdriveUploader
                                        await rep.report(f"Uploading to GDrive for {check_name} - {qual}p", "info")
                                        gdrive_uploader = GdriveUploader()
                                        await gdrive_uploader.upload_file(out_path, gdrive_link)
                                        await rep.report(f"Uploaded to GDrive for {check_name} - {qual}p", "info")
                                    except Exception as e:
                                        await rep.report(f"GDrive upload failed for {check_name} - {qual}p: {e}", "error")
                                    break
                else:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to upload {qual}p</i></pre>")

            quality_order = ["480", "720", "1080", "HDRi"]
            all_buttons = []

            for qual in quality_order:
                msg_id = file_store_links.get(qual)
                if not msg_id:
                    continue

                encoded = await encode(
                    "get-" + str(msg_id * abs(Var.FILE_STORE))
                )
                all_buttons.append(
                    InlineKeyboardButton(
                        text=btn_formatter[qual],
                        url=f"https://t.me/{Var.BOT_USERNAME}?start={encoded}"
                    )
                )

            grouped_buttons = [
                all_buttons[i:i + 2]
                for i in range(0, len(all_buttons), 2)
            ]

            final_caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    final_caption = obfuscate_text_for_copyright(final_caption)
            except Exception as e:
                await rep.report(
                    f"Error applying font changer: {e}",
                    "warning"
                )

            final_markup = (
                InlineKeyboardMarkup(grouped_buttons)
                if grouped_buttons else None
            )

            required_low_end = ["480", "720", "1080", "HDRi"]
            missing_low_end = [q for q in required_low_end if q not in file_store_links]
            if missing_low_end:
                raise RuntimeError(
                    f"Low-end episode {name} is not ready to publish; missing qualities: {', '.join(missing_low_end)}"
                )

            final_post = await bot.send_photo(
                Var.MAIN_CHANNEL, photo=photo_url,
                caption=final_caption, reply_markup=final_markup
            )
            if not final_post or not getattr(final_post, "id", None):
                raise RuntimeError(f"Main Channel did not confirm post for {name}.")

            await db.saveAnimeMainPost(ani_id, ep_no, final_post.id)
            for uploaded_qual in file_store_links:
                await db.saveAnime(ani_id, ep_no, uploaded_qual, final_post.id)
            await db.markAnimeResumeComplete(ani_id, ep_no)
            if ani_id and ep_no:
                await db.mark_episode_completed(ani_id, ep_no)


            if stat_msg:
                await stat_msg.delete()
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)

    except Exception as e:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(f"Low-end processing error: {str(e)}", "error")

async def fetch_animes():
    await rep.report("Fetching Anime Started !!!", "info")
    seen_rss_links = {}
    while True:
        await asleep(15)
        mode, upload_mode = await asyncio.gather(db.get_mode(), db.get_upload_mode())
        if ani_cache.get('global_pause') and not ani_cache.get('manual_task'):
            continue
        if mode != "anime" or not ani_cache['fetch_animes']:
            continue
        try:
            # Scan every configured quality feed in both upload modes.
            # The main RSS list may contain only 1080p; omitting the 480p/720p
            # feeds caused scheduled releases and Sub/Dual variants to be missed.
            configured_feeds = list(Var.RSS_ITEMS_ANIME or [])
            quality_feeds = [
                Var.RSS_LOW_END.get(q)
                for q in ("480", "720", "1080")
                if Var.RSS_LOW_END.get(q)
            ]
            rss_urls = list(dict.fromkeys(
                url for url in configured_feeds + quality_feeds if url
            ))

            # Recover unfinished episodes from MongoDB before scanning fresh RSS.
            # This works even if the source entry has fallen out of the RSS window.
            recovered_links = set()
            resume_failed = False
            try:
                resume_tasks = await db.getAnimeResumeTasks()
                for resume_task in resume_tasks:
                    resume_name = resume_task.get("name")
                    resume_link = resume_task.get("torrent_url")
                    if not resume_name or not resume_link:
                        continue
                    recovered_links.add(resume_link)
                    await rep.report(
                        f"Resuming unfinished episode from saved checkpoint: {resume_name}",
                        "info"
                    )
                    # Do not advance to another episode until this saved episode
                    # completes. get_animes re-raises failures after preserving
                    # successful per-quality checkpoints.
                    await get_animes(resume_name, resume_link)
            except Exception as resume_error:
                resume_failed = True
                await rep.report(
                    f"Episode resume failed; holding the queue on this unfinished episode. "
                    f"It will retry on the next scan: {resume_error}",
                    "error"
                )

            if resume_failed:
                # Never start fresh RSS jobs while an earlier episode is unfinished.
                continue

            feeds_by_url = await asyncio.gather(*(getfeeds(url) for url in rss_urls))
            pending = []
            for rss_url, entries in zip(rss_urls, feeds_by_url):
                if not entries:
                    continue
                seen = seen_rss_links.setdefault(rss_url, set())
                # Process oldest unseen entry first so a burst of releases is not skipped.
                for entry in entries:
                    link = getattr(entry, "link", None)
                    title = getattr(entry, "title", None)
                    if not link or not title or link in seen or link in recovered_links:
                        continue
                    seen.add(link)
                    pending.append((title, link))
                # Bound memory while retaining a recent de-duplication window.
                if len(seen) > 500:
                    seen_rss_links[rss_url] = set(list(seen)[-250:])

            # One episode at a time: finish/retry the current item before advancing.
            for title, link in pending:
                try:
                    await get_animes(title, link)
                except Exception:
                    await rep.report(f"Episode processing failed; will retry on next RSS scan: {title}\\n{format_exc()}", "error")
                    # Make it eligible for the next scan instead of permanently consuming it.
                    for rss_url in rss_urls:
                        seen_rss_links.get(rss_url, set()).discard(link)
                    break
        except Exception as e:
            await rep.report(f"Anime fetch error: {str(e)}", "error")

async def get_or_create_anime_channel(name: str, aniInfo):
    try:
        base_anime_name = extract_base_anime_name(name)
        normalized_name = normalize_anime_name_for_search(name)
        
        await rep.report(f"Searching for existing channel for: {base_anime_name}", "info")
        await asleep(1.5)
        custom_channel = await db.get_anime_channel(normalized_name)
        if custom_channel:
            await rep.report(f"Found custom channel mapping: {custom_channel}", "info")
            return custom_channel

        raw_entry = await db.get_raw_anime_channel(normalized_name)
        if raw_entry is not None and 'channel_id' not in raw_entry:
            await rep.report(f"Found banner-only mapping for {normalized_name} (no channel_id)", "info")
            channel_creation = await db.get_channel_creation()
            if channel_creation and Var.USER_SESSION:
                await rep.report("Banner-only mapping found â creating channel because channel creation is enabled", "info")
                new_channel = await create_anime_channel(name, aniInfo)
                if new_channel:
                    await db.add_anime_channel_mapping(normalized_name, new_channel)
                    await rep.report(f"Created channel for banner-only mapping and updated DB: {new_channel}", "info")
                    return new_channel
                else:
                    await rep.report("Failed to create channel for banner-only mapping, falling back to main channel", "error")
                    return Var.MAIN_CHANNEL
            else:
                await rep.report("Banner-only mapping present but channel creation is disabled or no user session; using main channel", "info")
                return Var.MAIN_CHANNEL

        existing_channel = await find_existing_anime_channel(name)
        if existing_channel:
            await rep.report(f"Found existing channel for {base_anime_name}: {existing_channel}", "info")
            await db.add_anime_channel_mapping(normalized_name, existing_channel)
            return existing_channel
        
        channel_creation = await db.get_channel_creation()
        if not channel_creation or not Var.USER_SESSION:
            await rep.report("Channel creation disabled or no user session, using main channel", "info")
            return Var.MAIN_CHANNEL

        await rep.report(f"Creating new channel for: {base_anime_name}", "info")
        new_channel = await create_anime_channel(name, aniInfo)
        
        if new_channel:
            await db.add_anime_channel_mapping(normalized_name, new_channel)
            await rep.report(f"Successfully created and saved channel mapping for {base_anime_name}", "info")
            return new_channel
        else:
            await rep.report("Failed to create channel, using main channel", "error")
            return Var.MAIN_CHANNEL
            
    except Exception as e:
        await rep.report(f"Error in get_or_create_anime_channel: {e}", "error")
        return Var.MAIN_CHANNEL

async def create_anime_channel(name, aniInfo):
    try:
        if not Var.USER_SESSION:
            await rep.report("User session not available for channel creation", "error")
            return None

        from pyrogram import Client
        user_client = Client("user_session", session_string=Var.USER_SESSION, 
                           api_id=Var.API_ID, api_hash=Var.API_HASH)

        await user_client.start()

        anime_format = (aniInfo.adata.get("format", "") or "").lower()
        is_movie = anime_format == "movie" or (
            hasattr(aniInfo, "is_movie_title") and aniInfo.is_movie_title(name)
        )

        titles = aniInfo.adata.get("title", {})
        channel_name = titles.get("english") or titles.get("romaji") or clean_anime_title(name)
        title = channel_name
        status = aniInfo.adata.get("status", "Unknown").capitalize()
        genres = aniInfo.adata.get("genres", [])[:3]
        genres_formatted = ", ".join(genres) if genres else "Unknown"

        if is_movie:
            description = (
                f"â¢ Movie: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )
        else:
            description = (
                f"â¢ Anime: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )

        created = await user_client.create_channel(channel_name, description)
        channel_id = created.id

        poster_url = (
            aniInfo.adata.get("coverImage", {}).get("large")
            or aniInfo.adata.get("coverImage", {}).get("medium")
            or aniInfo.adata.get("coverImage", {}).get("small")
        )
        if poster_url:
            filename = f"/tmp/{channel_id}_logo.jpg"
            filename = download_image(poster_url, filename)
            if filename:
                try:
                    await user_client.set_chat_photo(channel_id, photo=filename)
                    print(f"Set channel photo for {channel_id} successfully!")
                    remove(filename)
                except Exception as e:
                    print(f"Failed to set channel photo: {e}")
                    await rep.report(f"Failed to set channel photo: {e}", "warning")
            else:
                print("Image file was not downloaded or does not exist.")
                await rep.report(f"Image file was not downloaded or does not exist.", "warning")
        else:
            print("No poster URL found in AniList data.")
            await rep.report(f"No poster URL found in AniList data.", "warning")

        try:
            from pyrogram.types import ChatPrivileges
            await user_client.promote_chat_member(
                channel_id,
                Var.CREATION_USERNAME,
                privileges=ChatPrivileges(
                    can_post_messages=True,
                    can_edit_messages=True,
                    can_delete_messages=True,
                    can_invite_users=True,
                    can_change_info=True,
                    can_pin_messages=True,
                    can_manage_chat=True,
                    can_manage_video_chats=False,
                    is_anonymous=False
                )
            )
            await rep.report(f"Successfully added and promoted bot in channel", "info")
        except Exception as e:
            await rep.report(f"Failed to add/promote bot in channel: {e}", "error")

        try:
            titles = aniInfo.adata.get("title", {})
            genres = ', '.join(aniInfo.adata.get("genres", [])[:3])
            anime_type = aniInfo.adata.get("format", "Unknown")
            avg_rating = aniInfo.adata.get("averageScore", "N/A")
            status = aniInfo.adata.get("status", "Unknown")
            start_date = aniInfo.adata.get("startDate", {})
            end_date = aniInfo.adata.get("endDate", {})
            runtime = aniInfo.adata.get("duration", "Unknown")
            episodes = aniInfo.adata.get("episodes", "Unknown")
            desc = aniInfo.adata.get("description", "No description available").replace("<br>", "").replace("<i>", "").replace("</i>", "").strip()
            desc = (desc[:200] + "...") if len(desc) > 200 else desc 
            site_url = aniInfo.adata.get("siteUrl", "https://anilist.co")
            media_id = aniInfo.adata.get("id")

            english = titles.get("english", "Unknown")
            romaji = titles.get("romaji", "Unknown")
            display_title = english if english != "Unknown" else romaji

            graphic_poster_url = f"https://img.anili.st/media/{media_id}"

            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.head(graphic_poster_url) as resp:
                    if resp.status != 200:
                        graphic_poster_url = poster_url or "https://imgs.search.brave.com/33oSQBqhBoRB6akjKPud5oFd-PxnMFEm1AjnBTS2ZhE/rs:fit:860:0:0:0/g:ce/aHR0cHM6Ly9pbWcu/ZnJlZXBpay5jb20v/dmVjdG9yLXByZW1p/dW0vaWx1c3RyYWNp/b24tYW5pbWUtZXJy/b3ItNDA0LXBhZ2lu/YS1uby1lbmNvbnRy/YWRhXzE1MDk3Mi0y/NTY0LmpwZw"

            if is_movie:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ Release Date : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Duration : {runtime} minutes</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""
            else:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ First aired : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Last aired : {end_date.get("year", '')}-{end_date.get("month", '')}-{end_date.get("day", '')}
â£ Runtime : {runtime} minutes
â£ No of episodes : {episodes}</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""

            await user_client.send_photo(
                chat_id=channel_id,
                photo=graphic_poster_url,
                caption=caption
            )

            STICKER_ID = "CAACAgUAAxkBAAIERmhruO6M5RpLtZjb2yrvPJxTuw0IAAIzEwACTrLwVSdrhgIPuK1TNgQ"
            await user_client.send_sticker(channel_id, STICKER_ID)

            await rep.report("Intro poster, caption, and sticker sent to channel", "info")

        except Exception as e:
            await rep.report(f"Failed to send intro content: {e}", "warning")

        await db.add_anime_channel_mapping(channel_name, channel_id)

        await asleep(2)

        await user_client.stop()

        await rep.report(f"Successfully created channel: {channel_name} (ID: {channel_id})", "info")
        return channel_id

    except Exception as e:
        await rep.report(f"Failed to create anime channel: {e}", "error")
        return None


async def build_anime_soul_poster(anime_info, anime_name, output_path, episode_label=None):
    """Create the ANIME SOUL split-screen streaming thumbnail (16:9)."""
    import io
    import textwrap
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter, ImageEnhance

    data = anime_info.adata or {}
    titles = data.get("title") or {}
    title = titles.get("english") or titles.get("romaji") or anime_name

    def clean_value(value, default=""):
        if isinstance(value, list):
            value = value[0] if value else default
        return str(value or default).strip()

    season = clean_value(anime_info.pdata.get("anime_season"), "01")
    episode = clean_value(episode_label or anime_info.pdata.get("episode_number"), "01")
    try:
        season = str(int(season))
    except Exception:
        pass
    try:
        episode = str(int(episode)).zfill(2)
    except Exception:
        pass

    cover_data = data.get("coverImage") or {}
    cover_url = cover_data.get("extraLarge") or cover_data.get("large") or data.get("poster_url")
    banner_url = data.get("bannerImage")
    # Prefer the portrait cover for the large poster panel; use banner if cover is unavailable.
    artwork_url = cover_url or banner_url
    if not artwork_url:
        await rep.report("ANIME SOUL split-screen thumbnail skipped: AniList has no artwork URL.", "warning")
        return None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(artwork_url)
            response.raise_for_status()
            art = Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception as exc:
        await rep.report(f"ANIME SOUL artwork download failed: {exc}", "warning")
        return None

    W, H = 1600, 900
    split_x = 690
    art = ImageEnhance.Color(art).enhance(1.08)
    art = ImageEnhance.Contrast(art).enhance(1.04)

    # Left: full-height poster artwork, cropped to fill without stretching.
    left_art = ImageOps.fit(
        art, (split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.45),
    )

    # Right: same anime artwork, blurred and washed out under a translucent light panel.
    right_art = ImageOps.fit(
        art, (W - split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.35),
    ).filter(ImageFilter.GaussianBlur(16))
    right_art = ImageEnhance.Brightness(right_art).enhance(1.8)
    right_art = ImageEnhance.Color(right_art).enhance(0.45)

    canvas = Image.new("RGB", (W, H), (245, 246, 250))
    canvas.paste(left_art, (0, 0))
    canvas.paste(right_art, (split_x, 0))
    overlay = Image.new("RGBA", (W - split_x, H), (250, 250, 253, 222))
    canvas.paste(overlay, (split_x, 0), overlay)
    draw = ImageDraw.Draw(canvas)

    regular_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    bold_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    def font(size, bold=False):
        for path in (bold_candidates if bold else regular_candidates):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def fit_line(text, fnt, max_width):
        text = str(text)
        while text and draw.textbbox((0, 0), text, font=fnt)[2] > max_width:
            text = text[:-2].rstrip() + "…"
        return text

    def wrapped(text, fnt, max_width, max_lines):
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
            if len(lines) >= max_lines:
                break
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) == max_lines and len(words) > sum(len(x.split()) for x in lines):
            lines[-1] = fit_line(lines[-1] + "…", fnt, max_width)
        return lines

    def wrap_complete(text, fnt, max_width):
        """Wrap every title word; never drop the tail of a long AniList title."""
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    # Left poster branding and title overlay.
    left_overlay = Image.new("RGBA", (split_x, H), (0, 0, 0, 0))
    left_draw = ImageDraw.Draw(left_overlay)
    for y in range(560, H):
        alpha = int(205 * ((y - 560) / (H - 560)))
        left_draw.line((0, y, split_x, y), fill=(0, 0, 0, alpha))
    canvas.paste(left_overlay, (0, 0), left_overlay)
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle((34, 30, 330, 90), radius=15, fill=(8, 12, 22), outline=(255,255,255), width=2)
    draw.text((55, 45), "ANIME SOUL", font=font(29, True), fill=(255,255,255))
    draw.text((235, 51), "/ STREAM", font=font(16, True), fill=(235,55,70))
    # Show the complete AniList title across lines instead of truncating it.
    left_title_font = font(38, True)
    left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    if len(left_title_lines) == 3:
        left_title_font = font(32, True)
        left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    left_title_y = 720 if len(left_title_lines) <= 2 else 690
    for title_line in left_title_lines:
        draw.text((42, left_title_y), title_line, font=left_title_font,
                  fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0))
        left_title_y += left_title_font.size + 8
    draw.text((48, 850), f"SEASON {season}   •   EPISODE {episode}",
              font=font(23, True), fill=(255,220,70))

    # Right-side streaming dashboard header and content geometry.
    rx = split_x + 58
    right_w = W - rx - 58
    draw.text((rx, 40), "ANIME SOUL", font=font(25, True), fill=(15,18,27))
    draw.text((rx+210, 45), "/ STREAM", font=font(18), fill=(210,35,55))
    nav = "HOME     ANIME     SCHEDULE     LIBRARY"
    nav_font = font(17, True)
    nav_width = draw.textbbox((0,0), nav, font=nav_font)[2]
    if nav_width < right_w:
        draw.text((W-58-nav_width, 48), nav, font=nav_font, fill=(35,38,48))

    # Wrap the complete AniList title instead of cutting it off with an ellipsis.
    title_font = font(50, True)
    title_lines = wrap_complete(str(title), title_font, right_w)
    if len(title_lines) == 3:
        title_font = font(42, True)
        title_lines = wrap_complete(str(title), title_font, right_w)
    title_y = 150
    for title_line in title_lines:
        draw.text((rx, title_y), title_line, font=title_font, fill=(8,10,18))
        title_y += title_font.size + 10

    rule_y = title_y + 8
    draw.rounded_rectangle((rx, rule_y, rx+right_w, rule_y+8), radius=4, fill=(25,92,190))
    pill_y = rule_y + 32
    draw.rounded_rectangle((rx, pill_y, rx+195, pill_y+56), radius=26, fill=(20,91,190))
    draw.text((rx+25, pill_y+13), f"SEASON {season}", font=font(23, True), fill=(255,255,255))
    draw.rounded_rectangle((rx+215, pill_y, rx+440, pill_y+56), radius=26, outline=(65,70,85), width=2, fill=(255,255,255))
    draw.text((rx+238, pill_y+13), f"EPISODE {episode}", font=font(23, True), fill=(18,20,28))
    draw.rounded_rectangle((rx+right_w-112, pill_y, rx+right_w, pill_y+56), radius=12, outline=(20,24,35), width=2)
    draw.text((rx+right_w-92, pill_y+13), "HD", font=font(25, True), fill=(18,20,28))

    # Place the AniList synopsis below the variable-height title block.
    synopsis = re.sub(r"<[^>]+>", " ", str(data.get("description") or "Discover the story in this anime."))
    synopsis = re.sub(r"\\s+", " ", synopsis).strip()
    syn_font = font(19)
    sy = pill_y + 82
    synopsis_lines = max(2, min(6, (735 - sy) // 29))
    for line in wrapped(synopsis, syn_font, right_w, synopsis_lines):
        draw.text((rx, sy), line, font=syn_font, fill=(35,38,48))
        sy += 29

    # Watch button.
    button_y = min(650, max(sy + 18, pill_y + 145))
    draw.rounded_rectangle((rx, button_y, rx+310, button_y+78), radius=38, fill=(12,14,20))
    draw.polygon([(rx+38,button_y+24),(rx+38,button_y+54),(rx+64,button_y+39)], fill=(255,255,255))
    draw.text((rx+83, button_y+22), "WATCH NOW", font=font(25, True), fill=(255,255,255))
    draw.ellipse((rx+340, button_y+2, rx+416, button_y+78), outline=(75,80,92), width=2)
    draw.text((rx+363, button_y+19), "☆", font=font(38), fill=(20,24,34))

    # Metadata row at the bottom.
    line_y = 770
    draw.line((rx, line_y, W-58, line_y), fill=(130,135,145), width=2)
    studio_nodes = (data.get("studios") or {}).get("nodes") or []
    studio = studio_nodes[0].get("name") if studio_nodes else "Not listed"
    year = (data.get("startDate") or {}).get("year") or data.get("seasonYear") or "—"
    genres = ", ".join((data.get("genres") or [])[:2]) or "Anime"
    status = str(data.get("status") or "—").replace("_", " ").title()
    metadata = [
        ("YEAR", str(year)),
        ("STUDIO", str(studio)),
        ("GENRES", genres),
        ("STATUS", status),
    ]
    col_w = right_w // 4
    for i, (label, value) in enumerate(metadata):
        x = rx + i * col_w
        draw.text((x, 795), label, font=font(14, True), fill=(100,105,115))
        draw.text((x, 823), fit_line(value, font(17, True), col_w-14), font=font(17, True), fill=(15,18,27))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    canvas.save(output_path, "JPEG", quality=94, optimize=True, progressive=True)
    return output_path


async def get_animes(name, torrent, force=False):
    mode = await db.get_mode()
    if mode != "anime":
        await rep.report(f"Skipping {name} because mode is not anime (current mode: {mode})", "info")
        return

    try:
        cleaned_name = clean_anime_title(name)
        base_name = extract_base_anime_name(cleaned_name)
        normalized_name = normalize_anime_name_for_search(base_name)
        
        gdrive_status = await db.get_gdrive_upload()
        if gdrive_status == "on":
            print(f"Checking GDrive mapping for: {normalized_name}")
            await asleep(1.5)
            possible_names = await get_all_possible_anime_names(normalized_name)
            
            for check_name in possible_names:
                for quality in Var.QUALS:
                    gdrive_link = await db.get_gdrive_mapping(check_name, quality)
                    if gdrive_link:
                        await rep.report(f"Found GDrive mapping for {check_name} - {quality}p", "info")
                        await db.saveAnime(torrent, "1", quality, None, gdrive_link)
                        break
                    
        upload_mode = await db.get_upload_mode()
        print(f"get_animes: upload_mode={upload_mode}")
        if upload_mode == "low_end":
            print(f"get_animes: calling process_low_end for {name}")
            await process_low_end(name, torrent)
            return
    except Exception as e:
        await rep.report(f"Error checking upload mode: {str(e)}", "error")
        return

    if not await is_valid_torrent_url(torrent):
        await rep.report(f"Invalid or non-torrent URL for {name}: {torrent}", "error")
        return
    
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        # Cache state per anime episode, not per series. A series can have
        # many episodes; using ani_id alone incorrectly blocks later episodes.
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        encoding_enabled = await db.get_encoding()
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        # If the Main Channel post already exists, never publish it twice.
        existing_main_post = await db.getAnimeMainPost(ani_id, ep_no)
        if existing_main_post:
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)
            return

        # Keep the torrent source in MongoDB so a restart can rediscover this
        # episode even after it falls out of the RSS feed window.
        await db.saveAnimeResume(ani_id, ep_no, name, torrent)
        
        if force or not existing_main_post:
            
            if "[Batch]" in name:
                await rep.report(f"Torrent Skipped!\n{name}", "warning")
                return
            
            await rep.report(f"New Anime Torrent Found!\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            channel_to_use = target_channel if destination_channel is not None else await get_or_create_anime_channel(name, aniInfo)
            
            channel_valid = False
            for attempt in range(3):
                if await validate_channel_id(channel_to_use):
                    channel_valid = True
                    break
                await asyncio.sleep(2)
            
            if not channel_valid:
                await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
                channel_to_use = Var.MAIN_CHANNEL
            
            post_msg = None
            photo_path = None
            all_banners = await db.list_anime_banners()
            banner_url = None
            
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break
            
            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")
            
            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")
            
            try:
                poster_path = f"thumbs/anime_soul_{aniInfo.adata.get('id') or hashlib.md5(cleaned_name.encode()).hexdigest()}.jpg"
                photo_path = await build_anime_soul_poster(aniInfo, cleaned_name, poster_path)
            except Exception as poster_error:
                await rep.report(f"ANIME SOUL poster builder failed; using original artwork: {poster_error}", "warning")
                photo_path = None
            post_msg = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")
            
            dl_path = await download_torrent(torrent, name)
            if not dl_path or not ospath.exists(dl_path):
                await rep.report(f"File Download Incomplete for {name}, Try Again", "error")
                await stat_msg.delete()
                return
            
            post_id = int(datetime.datetime.now().timestamp() * 1000000)
            ffEvent = Event()
            ff_queued[post_id] = ffEvent
            
            if ffLock.locked():
                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Queued to Encode...</i></pre>")
                await rep.report("Added Task to Queue...", "info")
            
            await ffQueue.put(post_id)
            await ffEvent.wait()
            await ffLock.acquire()
            try:
                btns = []
                uploaded_qualities = []
                required_qualities = list(Var.QUALS) if encoding_enabled else ["HDRi"]
                saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)

                async def add_quality_button(qual, msg_id):
                    link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(
                        await encode('get-' + str(int(msg_id) * abs(Var.FILE_STORE)))
                    )
                    label = RAW_BTN if qual == "HDRi" else btn_formatter.get(qual, qual + "p")
                    button = InlineKeyboardButton(label, url=link)
                    if btns and len(btns[-1]) == 1:
                        btns[-1].append(button)
                    else:
                        btns.append([button])

                for qual in required_qualities:
                    # Reuse a quality that was already uploaded before the last
                    # crash. Verify the message still exists in FILE_STORE.
                    saved_msg_id = saved_file_ids.get(qual)
                    if saved_msg_id:
                        try:
                            stored_msg = await bot.get_messages(
                                Var.FILE_STORE, message_ids=int(saved_msg_id)
                            )
                            if stored_msg and not getattr(stored_msg, "empty", False):
                                await add_quality_button(qual, saved_msg_id)
                                uploaded_qualities.append(qual)
                                await rep.report(
                                    f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                    "info"
                                )
                                continue
                        except Exception as verify_error:
                            await rep.report(
                                f"Saved {qual} message {saved_msg_id} is unavailable; re-uploading this quality: {verify_error}",
                                "warning"
                            )

                    filename = await aniInfo.get_upname(qual)
                    anime_format = aniInfo.adata.get("format", "").lower()
                    is_movie = anime_format == "movie"
                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n"
                        f"<pre><i>Encoding {qual}...</i></pre>"
                    )
                    await rep.report(f"Starting Encode for {qual}...", "info")

                    out_path = await FFEncoder(
                        stat_msg, dl_path, filename, qual, is_movie=is_movie
                    ).start_encode()
                    if not out_path or not ospath.exists(out_path):
                        raise RuntimeError(
                            f"{qual} encoding failed for {name}; earlier uploaded qualities are saved and will be reused on retry."
                        )

                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{filename}</i></b></blockquote>\n\n"
                        f"<pre><i>Uploading {qual}...</i></pre>"
                    )
                    msg = await TgUploader(stat_msg).upload(out_path, qual)
                    if not msg or not getattr(msg, "id", None):
                        raise RuntimeError(f"Telegram did not confirm {qual} upload for {name}.")

                    # Persist immediately after Telegram confirms the upload.
                    await db.saveAnimeFileId(ani_id, ep_no, qual, msg.id)
                    await db.saveAnime(ani_id, ep_no, qual, None)
                    saved_file_ids[qual] = msg.id
                    await add_quality_button(qual, msg.id)
                    uploaded_qualities.append(qual)
                    await rep.report(f"Successfully uploaded and checkpointed {qual} for {name}.", "info")
                    bot_loop.create_task(extra_utils(msg.id, out_path))

            finally:
                ffLock.release()
            
            await stat_msg.delete()
            if btns:
                markup = InlineKeyboardMarkup(btns)
                # Telegram may temporarily rate-limit the Main Channel post.
                # Retry the publish step without re-uploading the already stored files.
                last_publish_error = None
                for publish_attempt in range(5):
                    try:
                        if photo_path and ospath.exists(photo_path):
                            with open(photo_path, 'rb') as photo_file:
                                post_msg = await bot.send_photo(
                                    Var.MAIN_CHANNEL, photo=photo_file,
                                    caption=caption, reply_markup=markup
                                )
                        else:
                            post_msg = await bot.send_photo(
                                Var.MAIN_CHANNEL, photo=photo_url,
                                caption=caption, reply_markup=markup
                            )
                        last_publish_error = None
                        break
                    except FloodWait as flood:
                        wait_seconds = max(1, int(flood.value) + 1)
                        await rep.report(
                            f"Main Channel FLOOD_WAIT: retrying publish in {wait_seconds}s "
                            f"(attempt {publish_attempt + 1}/5).", "warning"
                        )
                        await asyncio.sleep(wait_seconds)
                        last_publish_error = flood
                    except Exception as publish_error:
                        last_publish_error = publish_error
                        break

                if last_publish_error is not None or post_msg is None:
                    raise RuntimeError(
                        f"Failed to publish completed episode to Main Channel: {last_publish_error}"
                    )

                # Persist the Main Channel post ID immediately after send succeeds.
                await db.saveAnimeMainPost(ani_id, ep_no, post_msg.id)
                for uploaded_qual in uploaded_qualities:
                    await db.saveAnime(ani_id, ep_no, uploaded_qual, post_msg.id)

                await db.markAnimeResumeComplete(ani_id, ep_no)
                # No standalone sticker for an episode uploaded through a
                # selected destination. Main Channel gets only the joining post.

                # Mark complete only after the Main Channel post exists.
                ani_cache['completed'].add(episode_key)
                ani_cache['ongoing'].discard(episode_key)
            else:
                await rep.report(f"No qualities uploaded for {name}; no Main Channel post sent", "warning")
                raise RuntimeError(f"No qualities uploaded for {name}; episode remains retryable")

            if encoding_enabled:
                await aioremove(dl_path)
        else:
            # Existing Main Channel post is already recorded.
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
    except Exception as error:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(format_exc(), "error")
        # Propagate the failure to fetch_animes so it does not mark this
        # episode as handled and advance to the next RSS item.
        raise

async def extra_utils(msg_id, out_path):
    try:
        msg = await bot.get_messages(Var.FILE_STORE, message_ids=msg_id)

        if hasattr(Var, 'BACKUP_FILE_STORE') and Var.BACKUP_FILE_STORE != 0:
            for chat_id in str(Var.BACKUP_FILE_STORE).split():
                try:
                    await msg.copy(int(chat_id))
                except Exception as e:
                    await rep.report(f"Failed to backup to {chat_id}: {e}", "error")
    except Exception as e:
        await rep.report(f"Error in extra_utils: {e}", "error")

def safe_dirname(name):
    return re.sub(r'[\\/*?:"<>|\'\n\r\t]', '_', name)

async def download_torrent(torrent_url, name=""):
    try:
        unique_dir = generate_unique_dir(name, torrent_url)
        
        if os.path.exists(unique_dir):
            shutil.rmtree(unique_dir)
        os.makedirs(unique_dir, exist_ok=True)
        
        downloader = TorDownloader(unique_dir)
        dl_path = await downloader.download(torrent_url)
        
        if not dl_path or not os.path.exists(dl_path):
            await rep.report(f"Failed to download torrent: {torrent_url}", "error")
            return None
        
        print(f"Download directory contents: {os.listdir(unique_dir)}")
        print(f"Successfully downloaded to: {dl_path}")
        return dl_path
    except Exception as e:
        await rep.report(f"Download error: {str(e)}", "error")
        return None

async def upload_file(file_path, quality):
    try:
        uploader = TgUploader() 
        msg = await uploader.upload(file_path, quality)
        return msg
    except Exception as e:
        await rep.report(f"Upload error: {str(e)}", "error")
        return None

async def process_low_end(name, torrent_url, force=False):
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        encoding_enabled = await db.get_encoding()
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        existing_main_post = await db.getAnimeMainPost(ani_id, ep_no)
        if existing_main_post:
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)
            return

        await db.saveAnimeResume(ani_id, ep_no, name, torrent_url)

        if force or (not (ani_data := await db.getAnime(ani_id)) \
            or (ani_data and not (qual_data := ani_data.get(ep_no))) \
            or (ani_data and qual_data and not all(qual for qual in qual_data.values()))):

            if "[Batch]" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if "(Batch)" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if hasattr(torrent_url, 'size') and torrent_url.size:
                if is_torrent_oversized(str(torrent_url.size), max_size_gb=4.5):
                    await rep.report(f"Torrent Skipped! - Size exceeds 4.5GB limit\n\n{name}", "warning")
                    return

            await rep.report(f"New Anime Torrent Found!\n\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            channel_to_use = target_channel if destination_channel is not None else await get_or_create_anime_channel(name, aniInfo)

            channel_valid = False
            for attempt in range(3):
                if await validate_channel_id(channel_to_use):
                    channel_valid = True
                    break
                await asyncio.sleep(2)

            if not channel_valid:
                await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
                channel_to_use = Var.MAIN_CHANNEL

            all_banners = await db.list_anime_banners()
            banner_url = None
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break

            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")

            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")

            post_msg = None
            main_post = backup_post = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")

            file_store_links = {}
            saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)
            buttons = []
            btns = []
            post_id = None
            for qual in ["480", "720", "1080"]:
                saved_msg_id = saved_file_ids.get(qual)
                if saved_msg_id:
                    try:
                        stored_msg = await bot.get_messages(
                            Var.FILE_STORE, message_ids=int(saved_msg_id)
                        )
                        if stored_msg and not getattr(stored_msg, "empty", False):
                            file_store_links[qual] = int(saved_msg_id)
                            await rep.report(
                                f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                "info"
                            )
                            continue
                    except Exception as verify_error:
                        await rep.report(
                            f"Saved {qual} upload is unavailable; retrying this quality: {verify_error}",
                            "warning"
                        )

                rss_link = Var.RSS_LOW_END.get(qual)
                if not rss_link:
                    continue
                feed = await getfeed(rss_link)
                if not feed or not feed.get("link"):
                    continue

                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Downloading...</i></pre>")
                dl_path = await download_torrent(feed["link"])
                if not dl_path:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to download {qual}p</i></pre>")
                    continue

                if Var.LOW_END_RENAME:
                        editor = TextEditor(name)
                        filename = await aniInfo.get_upname(qual)
                        anime_format = aniInfo.adata.get("format", "").lower()
                        is_movie = anime_format == "movie"
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Encode...</i></pre>")
                        await asleep(1.5)
                        await rep.report("Starting Encode...", "info")
                        try:
                            out_path = await FFEncoder(stat_msg, dl_path, filename, qual, is_movie=is_movie).start_encode()
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Compressed Now Going To Upload...", "info")
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Upload...</i></pre>")
                        await asleep(1.5)
                        try:
                            msg = await TgUploader(stat_msg).upload(out_path, qual)
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Uploaded File into Channel...", "info")
                        msg_id = msg.id
                        file_store_links[qual] = msg_id
                        await db.saveAnimeFileId(ani_id, ep_no, qual, msg_id)
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        saved_file_ids[qual] = msg_id
                        link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(await encode('get-'+str(msg_id * abs(Var.FILE_STORE))))
                        if len(btns) != 0 and len(btns[-1]) == 1:
                            btns[-1].insert(1, InlineKeyboardButton(f"{btn_formatter[qual]}", url=link))
                        else:
                            btns.append([InlineKeyboardButton(f"{btn_formatter[qual]}", url=link)])
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        bot_loop.create_task(extra_utils(msg_id, out_path))

                        gdrive_status = await db.get_gdrive_upload()
                        if gdrive_status == "on":
                            possible_names = await get_all_possible_anime_names(cleaned_name)
                            gdrive_link = None
                            for check_name in possible_names:
                                gdrive_link = await db.get_gdrive_mapping(check_name, qual)
                                if gdrive_link:
                                    try:
                                        from bot.core.gdrive import GdriveUploader
                                        await rep.report(f"Uploading to GDrive for {check_name} - {qual}p", "info")
                                        gdrive_uploader = GdriveUploader()
                                        await gdrive_uploader.upload_file(out_path, gdrive_link)
                                        await rep.report(f"Uploaded to GDrive for {check_name} - {qual}p", "info")
                                    except Exception as e:
                                        await rep.report(f"GDrive upload failed for {check_name} - {qual}p: {e}", "error")
                                    break
                else:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to upload {qual}p</i></pre>")

            quality_order = ["480", "720", "1080", "HDRi"]
            all_buttons = []

            for qual in quality_order:
                msg_id = file_store_links.get(qual)
                if not msg_id:
                    continue

                encoded = await encode(
                    "get-" + str(msg_id * abs(Var.FILE_STORE))
                )
                all_buttons.append(
                    InlineKeyboardButton(
                        text=btn_formatter[qual],
                        url=f"https://t.me/{Var.BOT_USERNAME}?start={encoded}"
                    )
                )

            grouped_buttons = [
                all_buttons[i:i + 2]
                for i in range(0, len(all_buttons), 2)
            ]

            final_caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    final_caption = obfuscate_text_for_copyright(final_caption)
            except Exception as e:
                await rep.report(
                    f"Error applying font changer: {e}",
                    "warning"
                )

            final_markup = (
                InlineKeyboardMarkup(grouped_buttons)
                if grouped_buttons else None
            )

            required_low_end = ["480", "720", "1080", "HDRi"]
            missing_low_end = [q for q in required_low_end if q not in file_store_links]
            if missing_low_end:
                raise RuntimeError(
                    f"Low-end episode {name} is not ready to publish; missing qualities: {', '.join(missing_low_end)}"
                )

            final_post = await bot.send_photo(
                Var.MAIN_CHANNEL, photo=photo_url,
                caption=final_caption, reply_markup=final_markup
            )
            if not final_post or not getattr(final_post, "id", None):
                raise RuntimeError(f"Main Channel did not confirm post for {name}.")

            await db.saveAnimeMainPost(ani_id, ep_no, final_post.id)
            for uploaded_qual in file_store_links:
                await db.saveAnime(ani_id, ep_no, uploaded_qual, final_post.id)
            await db.markAnimeResumeComplete(ani_id, ep_no)
            if ani_id and ep_no:
                await db.mark_episode_completed(ani_id, ep_no)


            if stat_msg:
                await stat_msg.delete()
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)

    except Exception as e:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(f"Low-end processing error: {str(e)}", "error")

async def fetch_animes():
    await rep.report("Fetching Anime Started !!!", "info")
    seen_rss_links = {}
    while True:
        await asleep(15)
        mode, upload_mode = await asyncio.gather(db.get_mode(), db.get_upload_mode())
        if ani_cache.get('global_pause') and not ani_cache.get('manual_task'):
            continue
        if mode != "anime" or not ani_cache['fetch_animes']:
            continue
        try:
            # Scan every configured quality feed in both upload modes.
            # The main RSS list may contain only 1080p; omitting the 480p/720p
            # feeds caused scheduled releases and Sub/Dual variants to be missed.
            configured_feeds = list(Var.RSS_ITEMS_ANIME or [])
            quality_feeds = [
                Var.RSS_LOW_END.get(q)
                for q in ("480", "720", "1080")
                if Var.RSS_LOW_END.get(q)
            ]
            rss_urls = list(dict.fromkeys(
                url for url in configured_feeds + quality_feeds if url
            ))

            # Recover unfinished episodes from MongoDB before scanning fresh RSS.
            # This works even if the source entry has fallen out of the RSS window.
            recovered_links = set()
            resume_failed = False
            try:
                resume_tasks = await db.getAnimeResumeTasks()
                for resume_task in resume_tasks:
                    resume_name = resume_task.get("name")
                    resume_link = resume_task.get("torrent_url")
                    if not resume_name or not resume_link:
                        continue
                    recovered_links.add(resume_link)
                    await rep.report(
                        f"Resuming unfinished episode from saved checkpoint: {resume_name}",
                        "info"
                    )
                    # Do not advance to another episode until this saved episode
                    # completes. get_animes re-raises failures after preserving
                    # successful per-quality checkpoints.
                    await get_animes(resume_name, resume_link)
            except Exception as resume_error:
                resume_failed = True
                await rep.report(
                    f"Episode resume failed; holding the queue on this unfinished episode. "
                    f"It will retry on the next scan: {resume_error}",
                    "error"
                )

            if resume_failed:
                # Never start fresh RSS jobs while an earlier episode is unfinished.
                continue

            feeds_by_url = await asyncio.gather(*(getfeeds(url) for url in rss_urls))
            pending = []
            for rss_url, entries in zip(rss_urls, feeds_by_url):
                if not entries:
                    continue
                seen = seen_rss_links.setdefault(rss_url, set())
                # Process oldest unseen entry first so a burst of releases is not skipped.
                for entry in entries:
                    link = getattr(entry, "link", None)
                    title = getattr(entry, "title", None)
                    if not link or not title or link in seen or link in recovered_links:
                        continue
                    seen.add(link)
                    pending.append((title, link))
                # Bound memory while retaining a recent de-duplication window.
                if len(seen) > 500:
                    seen_rss_links[rss_url] = set(list(seen)[-250:])

            # One episode at a time: finish/retry the current item before advancing.
            for title, link in pending:
                try:
                    await get_animes(title, link)
                except Exception:
                    await rep.report(f"Episode processing failed; will retry on next RSS scan: {title}\\n{format_exc()}", "error")
                    # Make it eligible for the next scan instead of permanently consuming it.
                    for rss_url in rss_urls:
                        seen_rss_links.get(rss_url, set()).discard(link)
                    break
        except Exception as e:
            await rep.report(f"Anime fetch error: {str(e)}", "error")

async def get_or_create_anime_channel(name: str, aniInfo):
    try:
        base_anime_name = extract_base_anime_name(name)
        normalized_name = normalize_anime_name_for_search(name)
        
        await rep.report(f"Searching for existing channel for: {base_anime_name}", "info")
        await asleep(1.5)
        custom_channel = await db.get_anime_channel(normalized_name)
        if custom_channel:
            await rep.report(f"Found custom channel mapping: {custom_channel}", "info")
            return custom_channel

        raw_entry = await db.get_raw_anime_channel(normalized_name)
        if raw_entry is not None and 'channel_id' not in raw_entry:
            await rep.report(f"Found banner-only mapping for {normalized_name} (no channel_id)", "info")
            channel_creation = await db.get_channel_creation()
            if channel_creation and Var.USER_SESSION:
                await rep.report("Banner-only mapping found â creating channel because channel creation is enabled", "info")
                new_channel = await create_anime_channel(name, aniInfo)
                if new_channel:
                    await db.add_anime_channel_mapping(normalized_name, new_channel)
                    await rep.report(f"Created channel for banner-only mapping and updated DB: {new_channel}", "info")
                    return new_channel
                else:
                    await rep.report("Failed to create channel for banner-only mapping, falling back to main channel", "error")
                    return Var.MAIN_CHANNEL
            else:
                await rep.report("Banner-only mapping present but channel creation is disabled or no user session; using main channel", "info")
                return Var.MAIN_CHANNEL

        existing_channel = await find_existing_anime_channel(name)
        if existing_channel:
            await rep.report(f"Found existing channel for {base_anime_name}: {existing_channel}", "info")
            await db.add_anime_channel_mapping(normalized_name, existing_channel)
            return existing_channel
        
        channel_creation = await db.get_channel_creation()
        if not channel_creation or not Var.USER_SESSION:
            await rep.report("Channel creation disabled or no user session, using main channel", "info")
            return Var.MAIN_CHANNEL

        await rep.report(f"Creating new channel for: {base_anime_name}", "info")
        new_channel = await create_anime_channel(name, aniInfo)
        
        if new_channel:
            await db.add_anime_channel_mapping(normalized_name, new_channel)
            await rep.report(f"Successfully created and saved channel mapping for {base_anime_name}", "info")
            return new_channel
        else:
            await rep.report("Failed to create channel, using main channel", "error")
            return Var.MAIN_CHANNEL
            
    except Exception as e:
        await rep.report(f"Error in get_or_create_anime_channel: {e}", "error")
        return Var.MAIN_CHANNEL

async def create_anime_channel(name, aniInfo):
    try:
        if not Var.USER_SESSION:
            await rep.report("User session not available for channel creation", "error")
            return None

        from pyrogram import Client
        user_client = Client("user_session", session_string=Var.USER_SESSION, 
                           api_id=Var.API_ID, api_hash=Var.API_HASH)

        await user_client.start()

        anime_format = (aniInfo.adata.get("format", "") or "").lower()
        is_movie = anime_format == "movie" or (
            hasattr(aniInfo, "is_movie_title") and aniInfo.is_movie_title(name)
        )

        titles = aniInfo.adata.get("title", {})
        channel_name = titles.get("english") or titles.get("romaji") or clean_anime_title(name)
        title = channel_name
        status = aniInfo.adata.get("status", "Unknown").capitalize()
        genres = aniInfo.adata.get("genres", [])[:3]
        genres_formatted = ", ".join(genres) if genres else "Unknown"

        if is_movie:
            description = (
                f"â¢ Movie: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )
        else:
            description = (
                f"â¢ Anime: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )

        created = await user_client.create_channel(channel_name, description)
        channel_id = created.id

        poster_url = (
            aniInfo.adata.get("coverImage", {}).get("large")
            or aniInfo.adata.get("coverImage", {}).get("medium")
            or aniInfo.adata.get("coverImage", {}).get("small")
        )
        if poster_url:
            filename = f"/tmp/{channel_id}_logo.jpg"
            filename = download_image(poster_url, filename)
            if filename:
                try:
                    await user_client.set_chat_photo(channel_id, photo=filename)
                    print(f"Set channel photo for {channel_id} successfully!")
                    remove(filename)
                except Exception as e:
                    print(f"Failed to set channel photo: {e}")
                    await rep.report(f"Failed to set channel photo: {e}", "warning")
            else:
                print("Image file was not downloaded or does not exist.")
                await rep.report(f"Image file was not downloaded or does not exist.", "warning")
        else:
            print("No poster URL found in AniList data.")
            await rep.report(f"No poster URL found in AniList data.", "warning")

        try:
            from pyrogram.types import ChatPrivileges
            await user_client.promote_chat_member(
                channel_id,
                Var.CREATION_USERNAME,
                privileges=ChatPrivileges(
                    can_post_messages=True,
                    can_edit_messages=True,
                    can_delete_messages=True,
                    can_invite_users=True,
                    can_change_info=True,
                    can_pin_messages=True,
                    can_manage_chat=True,
                    can_manage_video_chats=False,
                    is_anonymous=False
                )
            )
            await rep.report(f"Successfully added and promoted bot in channel", "info")
        except Exception as e:
            await rep.report(f"Failed to add/promote bot in channel: {e}", "error")

        try:
            titles = aniInfo.adata.get("title", {})
            genres = ', '.join(aniInfo.adata.get("genres", [])[:3])
            anime_type = aniInfo.adata.get("format", "Unknown")
            avg_rating = aniInfo.adata.get("averageScore", "N/A")
            status = aniInfo.adata.get("status", "Unknown")
            start_date = aniInfo.adata.get("startDate", {})
            end_date = aniInfo.adata.get("endDate", {})
            runtime = aniInfo.adata.get("duration", "Unknown")
            episodes = aniInfo.adata.get("episodes", "Unknown")
            desc = aniInfo.adata.get("description", "No description available").replace("<br>", "").replace("<i>", "").replace("</i>", "").strip()
            desc = (desc[:200] + "...") if len(desc) > 200 else desc 
            site_url = aniInfo.adata.get("siteUrl", "https://anilist.co")
            media_id = aniInfo.adata.get("id")

            english = titles.get("english", "Unknown")
            romaji = titles.get("romaji", "Unknown")
            display_title = english if english != "Unknown" else romaji

            graphic_poster_url = f"https://img.anili.st/media/{media_id}"

            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.head(graphic_poster_url) as resp:
                    if resp.status != 200:
                        graphic_poster_url = poster_url or "https://imgs.search.brave.com/33oSQBqhBoRB6akjKPud5oFd-PxnMFEm1AjnBTS2ZhE/rs:fit:860:0:0:0/g:ce/aHR0cHM6Ly9pbWcu/ZnJlZXBpay5jb20v/dmVjdG9yLXByZW1p/dW0vaWx1c3RyYWNp/b24tYW5pbWUtZXJy/b3ItNDA0LXBhZ2lu/YS1uby1lbmNvbnRy/YWRhXzE1MDk3Mi0y/NTY0LmpwZw"

            if is_movie:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ Release Date : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Duration : {runtime} minutes</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""
            else:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ First aired : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Last aired : {end_date.get("year", '')}-{end_date.get("month", '')}-{end_date.get("day", '')}
â£ Runtime : {runtime} minutes
â£ No of episodes : {episodes}</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""

            await user_client.send_photo(
                chat_id=channel_id,
                photo=graphic_poster_url,
                caption=caption
            )

            STICKER_ID = "CAACAgUAAxkBAAIERmhruO6M5RpLtZjb2yrvPJxTuw0IAAIzEwACTrLwVSdrhgIPuK1TNgQ"
            await user_client.send_sticker(channel_id, STICKER_ID)

            await rep.report("Intro poster, caption, and sticker sent to channel", "info")

        except Exception as e:
            await rep.report(f"Failed to send intro content: {e}", "warning")

        await db.add_anime_channel_mapping(channel_name, channel_id)

        await asleep(2)

        await user_client.stop()

        await rep.report(f"Successfully created channel: {channel_name} (ID: {channel_id})", "info")
        return channel_id

    except Exception as e:
        await rep.report(f"Failed to create anime channel: {e}", "error")
        return None


async def build_anime_soul_poster(anime_info, anime_name, output_path, episode_label=None):
    """Create the ANIME SOUL split-screen streaming thumbnail (16:9)."""
    import io
    import textwrap
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter, ImageEnhance

    data = anime_info.adata or {}
    titles = data.get("title") or {}
    title = titles.get("english") or titles.get("romaji") or anime_name

    def clean_value(value, default=""):
        if isinstance(value, list):
            value = value[0] if value else default
        return str(value or default).strip()

    season = clean_value(anime_info.pdata.get("anime_season"), "01")
    episode = clean_value(episode_label or anime_info.pdata.get("episode_number"), "01")
    try:
        season = str(int(season))
    except Exception:
        pass
    try:
        episode = str(int(episode)).zfill(2)
    except Exception:
        pass

    cover_data = data.get("coverImage") or {}
    cover_url = cover_data.get("extraLarge") or cover_data.get("large") or data.get("poster_url")
    banner_url = data.get("bannerImage")
    # Prefer the portrait cover for the large poster panel; use banner if cover is unavailable.
    artwork_url = cover_url or banner_url
    if not artwork_url:
        await rep.report("ANIME SOUL split-screen thumbnail skipped: AniList has no artwork URL.", "warning")
        return None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(artwork_url)
            response.raise_for_status()
            art = Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception as exc:
        await rep.report(f"ANIME SOUL artwork download failed: {exc}", "warning")
        return None

    W, H = 1600, 900
    split_x = 690
    art = ImageEnhance.Color(art).enhance(1.08)
    art = ImageEnhance.Contrast(art).enhance(1.04)

    # Left: full-height poster artwork, cropped to fill without stretching.
    left_art = ImageOps.fit(
        art, (split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.45),
    )

    # Right: same anime artwork, blurred and washed out under a translucent light panel.
    right_art = ImageOps.fit(
        art, (W - split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.35),
    ).filter(ImageFilter.GaussianBlur(16))
    right_art = ImageEnhance.Brightness(right_art).enhance(1.8)
    right_art = ImageEnhance.Color(right_art).enhance(0.45)

    canvas = Image.new("RGB", (W, H), (245, 246, 250))
    canvas.paste(left_art, (0, 0))
    canvas.paste(right_art, (split_x, 0))
    overlay = Image.new("RGBA", (W - split_x, H), (250, 250, 253, 222))
    canvas.paste(overlay, (split_x, 0), overlay)
    draw = ImageDraw.Draw(canvas)

    regular_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    bold_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    def font(size, bold=False):
        for path in (bold_candidates if bold else regular_candidates):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def fit_line(text, fnt, max_width):
        text = str(text)
        while text and draw.textbbox((0, 0), text, font=fnt)[2] > max_width:
            text = text[:-2].rstrip() + "…"
        return text

    def wrapped(text, fnt, max_width, max_lines):
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
            if len(lines) >= max_lines:
                break
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) == max_lines and len(words) > sum(len(x.split()) for x in lines):
            lines[-1] = fit_line(lines[-1] + "…", fnt, max_width)
        return lines

    def wrap_complete(text, fnt, max_width):
        """Wrap every title word; never drop the tail of a long AniList title."""
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    # Left poster branding and title overlay.
    left_overlay = Image.new("RGBA", (split_x, H), (0, 0, 0, 0))
    left_draw = ImageDraw.Draw(left_overlay)
    for y in range(560, H):
        alpha = int(205 * ((y - 560) / (H - 560)))
        left_draw.line((0, y, split_x, y), fill=(0, 0, 0, alpha))
    canvas.paste(left_overlay, (0, 0), left_overlay)
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle((34, 30, 330, 90), radius=15, fill=(8, 12, 22), outline=(255,255,255), width=2)
    draw.text((55, 45), "ANIME SOUL", font=font(29, True), fill=(255,255,255))
    draw.text((235, 51), "/ STREAM", font=font(16, True), fill=(235,55,70))
    # Show the complete AniList title across lines instead of truncating it.
    left_title_font = font(38, True)
    left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    if len(left_title_lines) == 3:
        left_title_font = font(32, True)
        left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    left_title_y = 720 if len(left_title_lines) <= 2 else 690
    for title_line in left_title_lines:
        draw.text((42, left_title_y), title_line, font=left_title_font,
                  fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0))
        left_title_y += left_title_font.size + 8
    draw.text((48, 850), f"SEASON {season}   •   EPISODE {episode}",
              font=font(23, True), fill=(255,220,70))

    # Right-side streaming dashboard header and content geometry.
    rx = split_x + 58
    right_w = W - rx - 58
    draw.text((rx, 40), "ANIME SOUL", font=font(25, True), fill=(15,18,27))
    draw.text((rx+210, 45), "/ STREAM", font=font(18), fill=(210,35,55))
    nav = "HOME     ANIME     SCHEDULE     LIBRARY"
    nav_font = font(17, True)
    nav_width = draw.textbbox((0,0), nav, font=nav_font)[2]
    if nav_width < right_w:
        draw.text((W-58-nav_width, 48), nav, font=nav_font, fill=(35,38,48))

    # Wrap the complete AniList title instead of cutting it off with an ellipsis.
    title_font = font(50, True)
    title_lines = wrap_complete(str(title), title_font, right_w)
    if len(title_lines) == 3:
        title_font = font(42, True)
        title_lines = wrap_complete(str(title), title_font, right_w)
    title_y = 150
    for title_line in title_lines:
        draw.text((rx, title_y), title_line, font=title_font, fill=(8,10,18))
        title_y += title_font.size + 10

    rule_y = title_y + 8
    draw.rounded_rectangle((rx, rule_y, rx+right_w, rule_y+8), radius=4, fill=(25,92,190))
    pill_y = rule_y + 32
    draw.rounded_rectangle((rx, pill_y, rx+195, pill_y+56), radius=26, fill=(20,91,190))
    draw.text((rx+25, pill_y+13), f"SEASON {season}", font=font(23, True), fill=(255,255,255))
    draw.rounded_rectangle((rx+215, pill_y, rx+440, pill_y+56), radius=26, outline=(65,70,85), width=2, fill=(255,255,255))
    draw.text((rx+238, pill_y+13), f"EPISODE {episode}", font=font(23, True), fill=(18,20,28))
    draw.rounded_rectangle((rx+right_w-112, pill_y, rx+right_w, pill_y+56), radius=12, outline=(20,24,35), width=2)
    draw.text((rx+right_w-92, pill_y+13), "HD", font=font(25, True), fill=(18,20,28))

    # Place the AniList synopsis below the variable-height title block.
    synopsis = re.sub(r"<[^>]+>", " ", str(data.get("description") or "Discover the story in this anime."))
    synopsis = re.sub(r"\\s+", " ", synopsis).strip()
    syn_font = font(19)
    sy = pill_y + 82
    synopsis_lines = max(2, min(6, (735 - sy) // 29))
    for line in wrapped(synopsis, syn_font, right_w, synopsis_lines):
        draw.text((rx, sy), line, font=syn_font, fill=(35,38,48))
        sy += 29

    # Watch button.
    button_y = min(650, max(sy + 18, pill_y + 145))
    draw.rounded_rectangle((rx, button_y, rx+310, button_y+78), radius=38, fill=(12,14,20))
    draw.polygon([(rx+38,button_y+24),(rx+38,button_y+54),(rx+64,button_y+39)], fill=(255,255,255))
    draw.text((rx+83, button_y+22), "WATCH NOW", font=font(25, True), fill=(255,255,255))
    draw.ellipse((rx+340, button_y+2, rx+416, button_y+78), outline=(75,80,92), width=2)
    draw.text((rx+363, button_y+19), "☆", font=font(38), fill=(20,24,34))

    # Metadata row at the bottom.
    line_y = 770
    draw.line((rx, line_y, W-58, line_y), fill=(130,135,145), width=2)
    studio_nodes = (data.get("studios") or {}).get("nodes") or []
    studio = studio_nodes[0].get("name") if studio_nodes else "Not listed"
    year = (data.get("startDate") or {}).get("year") or data.get("seasonYear") or "—"
    genres = ", ".join((data.get("genres") or [])[:2]) or "Anime"
    status = str(data.get("status") or "—").replace("_", " ").title()
    metadata = [
        ("YEAR", str(year)),
        ("STUDIO", str(studio)),
        ("GENRES", genres),
        ("STATUS", status),
    ]
    col_w = right_w // 4
    for i, (label, value) in enumerate(metadata):
        x = rx + i * col_w
        draw.text((x, 795), label, font=font(14, True), fill=(100,105,115))
        draw.text((x, 823), fit_line(value, font(17, True), col_w-14), font=font(17, True), fill=(15,18,27))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    canvas.save(output_path, "JPEG", quality=94, optimize=True, progressive=True)
    return output_path


async def get_animes(name, torrent, force=False):
    mode = await db.get_mode()
    if mode != "anime":
        await rep.report(f"Skipping {name} because mode is not anime (current mode: {mode})", "info")
        return

    try:
        cleaned_name = clean_anime_title(name)
        base_name = extract_base_anime_name(cleaned_name)
        normalized_name = normalize_anime_name_for_search(base_name)
        
        gdrive_status = await db.get_gdrive_upload()
        if gdrive_status == "on":
            print(f"Checking GDrive mapping for: {normalized_name}")
            await asleep(1.5)
            possible_names = await get_all_possible_anime_names(normalized_name)
            
            for check_name in possible_names:
                for quality in Var.QUALS:
                    gdrive_link = await db.get_gdrive_mapping(check_name, quality)
                    if gdrive_link:
                        await rep.report(f"Found GDrive mapping for {check_name} - {quality}p", "info")
                        await db.saveAnime(torrent, "1", quality, None, gdrive_link)
                        break
                    
        upload_mode = await db.get_upload_mode()
        print(f"get_animes: upload_mode={upload_mode}")
        if upload_mode == "low_end":
            print(f"get_animes: calling process_low_end for {name}")
            await process_low_end(name, torrent)
            return
    except Exception as e:
        await rep.report(f"Error checking upload mode: {str(e)}", "error")
        return

    if not await is_valid_torrent_url(torrent):
        await rep.report(f"Invalid or non-torrent URL for {name}: {torrent}", "error")
        return
    
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        # Cache state per anime episode, not per series. A series can have
        # many episodes; using ani_id alone incorrectly blocks later episodes.
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        encoding_enabled = await db.get_encoding()
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        # If the Main Channel post already exists, never publish it twice.
        existing_main_post = await db.getAnimeMainPost(ani_id, ep_no)
        if existing_main_post:
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)
            return

        # Keep the torrent source in MongoDB so a restart can rediscover this
        # episode even after it falls out of the RSS feed window.
        await db.saveAnimeResume(ani_id, ep_no, name, torrent)
        
        if force or not existing_main_post:
            
            if "[Batch]" in name:
                await rep.report(f"Torrent Skipped!\n{name}", "warning")
                return
            
            await rep.report(f"New Anime Torrent Found!\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            channel_to_use = target_channel if destination_channel is not None else await get_or_create_anime_channel(name, aniInfo)
            
            channel_valid = False
            for attempt in range(3):
                if await validate_channel_id(channel_to_use):
                    channel_valid = True
                    break
                await asyncio.sleep(2)
            
            if not channel_valid:
                await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
                channel_to_use = Var.MAIN_CHANNEL
            
            post_msg = None
            photo_path = None
            all_banners = await db.list_anime_banners()
            banner_url = None
            
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break
            
            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")
            
            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")
            
            try:
                poster_path = f"thumbs/anime_soul_{aniInfo.adata.get('id') or hashlib.md5(cleaned_name.encode()).hexdigest()}.jpg"
                photo_path = await build_anime_soul_poster(aniInfo, cleaned_name, poster_path)
            except Exception as poster_error:
                await rep.report(f"ANIME SOUL poster builder failed; using original artwork: {poster_error}", "warning")
                photo_path = None
            post_msg = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")
            
            dl_path = await download_torrent(torrent, name)
            if not dl_path or not ospath.exists(dl_path):
                await rep.report(f"File Download Incomplete for {name}, Try Again", "error")
                await stat_msg.delete()
                return
            
            post_id = int(datetime.datetime.now().timestamp() * 1000000)
            ffEvent = Event()
            ff_queued[post_id] = ffEvent
            
            if ffLock.locked():
                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Queued to Encode...</i></pre>")
                await rep.report("Added Task to Queue...", "info")
            
            await ffQueue.put(post_id)
            await ffEvent.wait()
            await ffLock.acquire()
            try:
                btns = []
                uploaded_qualities = []
                required_qualities = list(Var.QUALS) if encoding_enabled else ["HDRi"]
                saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)

                async def add_quality_button(qual, msg_id):
                    link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(
                        await encode('get-' + str(int(msg_id) * abs(Var.FILE_STORE)))
                    )
                    label = RAW_BTN if qual == "HDRi" else btn_formatter.get(qual, qual + "p")
                    button = InlineKeyboardButton(label, url=link)
                    if btns and len(btns[-1]) == 1:
                        btns[-1].append(button)
                    else:
                        btns.append([button])

                for qual in required_qualities:
                    # Reuse a quality that was already uploaded before the last
                    # crash. Verify the message still exists in FILE_STORE.
                    saved_msg_id = saved_file_ids.get(qual)
                    if saved_msg_id:
                        try:
                            stored_msg = await bot.get_messages(
                                Var.FILE_STORE, message_ids=int(saved_msg_id)
                            )
                            if stored_msg and not getattr(stored_msg, "empty", False):
                                await add_quality_button(qual, saved_msg_id)
                                uploaded_qualities.append(qual)
                                await rep.report(
                                    f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                    "info"
                                )
                                continue
                        except Exception as verify_error:
                            await rep.report(
                                f"Saved {qual} message {saved_msg_id} is unavailable; re-uploading this quality: {verify_error}",
                                "warning"
                            )

                    filename = await aniInfo.get_upname(qual)
                    anime_format = aniInfo.adata.get("format", "").lower()
                    is_movie = anime_format == "movie"
                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n"
                        f"<pre><i>Encoding {qual}...</i></pre>"
                    )
                    await rep.report(f"Starting Encode for {qual}...", "info")

                    out_path = await FFEncoder(
                        stat_msg, dl_path, filename, qual, is_movie=is_movie
                    ).start_encode()
                    if not out_path or not ospath.exists(out_path):
                        raise RuntimeError(
                            f"{qual} encoding failed for {name}; earlier uploaded qualities are saved and will be reused on retry."
                        )

                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{filename}</i></b></blockquote>\n\n"
                        f"<pre><i>Uploading {qual}...</i></pre>"
                    )
                    msg = await TgUploader(stat_msg).upload(out_path, qual)
                    if not msg or not getattr(msg, "id", None):
                        raise RuntimeError(f"Telegram did not confirm {qual} upload for {name}.")

                    # Persist immediately after Telegram confirms the upload.
                    await db.saveAnimeFileId(ani_id, ep_no, qual, msg.id)
                    await db.saveAnime(ani_id, ep_no, qual, None)
                    saved_file_ids[qual] = msg.id
                    await add_quality_button(qual, msg.id)
                    uploaded_qualities.append(qual)
                    await rep.report(f"Successfully uploaded and checkpointed {qual} for {name}.", "info")
                    bot_loop.create_task(extra_utils(msg.id, out_path))

            finally:
                ffLock.release()
            
            await stat_msg.delete()
            if btns:
                markup = InlineKeyboardMarkup(btns)
                # Telegram may temporarily rate-limit the Main Channel post.
                # Retry the publish step without re-uploading the already stored files.
                last_publish_error = None
                for publish_attempt in range(5):
                    try:
                        if photo_path and ospath.exists(photo_path):
                            with open(photo_path, 'rb') as photo_file:
                                post_msg = await bot.send_photo(
                                    Var.MAIN_CHANNEL, photo=photo_file,
                                    caption=caption, reply_markup=markup
                                )
                        else:
                            post_msg = await bot.send_photo(
                                Var.MAIN_CHANNEL, photo=photo_url,
                                caption=caption, reply_markup=markup
                            )
                        last_publish_error = None
                        break
                    except FloodWait as flood:
                        wait_seconds = max(1, int(flood.value) + 1)
                        await rep.report(
                            f"Main Channel FLOOD_WAIT: retrying publish in {wait_seconds}s "
                            f"(attempt {publish_attempt + 1}/5).", "warning"
                        )
                        await asyncio.sleep(wait_seconds)
                        last_publish_error = flood
                    except Exception as publish_error:
                        last_publish_error = publish_error
                        break

                if last_publish_error is not None or post_msg is None:
                    raise RuntimeError(
                        f"Failed to publish completed episode to Main Channel: {last_publish_error}"
                    )

                # Persist the Main Channel post ID immediately after send succeeds.
                await db.saveAnimeMainPost(ani_id, ep_no, post_msg.id)
                for uploaded_qual in uploaded_qualities:
                    await db.saveAnime(ani_id, ep_no, uploaded_qual, post_msg.id)

                await db.markAnimeResumeComplete(ani_id, ep_no)
                # Episode upload completed. Do not send a standalone sticker;
                # selected-channel uploads must not create extra Main Channel posts.

                # Mark complete only after the Main Channel post exists.
                ani_cache['completed'].add(episode_key)
                ani_cache['ongoing'].discard(episode_key)
            else:
                await rep.report(f"No qualities uploaded for {name}; no Main Channel post sent", "warning")
                raise RuntimeError(f"No qualities uploaded for {name}; episode remains retryable")

            if encoding_enabled:
                await aioremove(dl_path)
        else:
            # Existing Main Channel post is already recorded.
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
    except Exception as error:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(format_exc(), "error")
        # Propagate the failure to fetch_animes so it does not mark this
        # episode as handled and advance to the next RSS item.
        raise

async def extra_utils(msg_id, out_path):
    try:
        msg = await bot.get_messages(Var.FILE_STORE, message_ids=msg_id)

        if hasattr(Var, 'BACKUP_FILE_STORE') and Var.BACKUP_FILE_STORE != 0:
            for chat_id in str(Var.BACKUP_FILE_STORE).split():
                try:
                    await msg.copy(int(chat_id))
                except Exception as e:
                    await rep.report(f"Failed to backup to {chat_id}: {e}", "error")
    except Exception as e:
        await rep.report(f"Error in extra_utils: {e}", "error")

def safe_dirname(name):
    return re.sub(r'[\\/*?:"<>|\'\n\r\t]', '_', name)

async def download_torrent(torrent_url, name=""):
    try:
        unique_dir = generate_unique_dir(name, torrent_url)
        
        if os.path.exists(unique_dir):
            shutil.rmtree(unique_dir)
        os.makedirs(unique_dir, exist_ok=True)
        
        downloader = TorDownloader(unique_dir)
        dl_path = await downloader.download(torrent_url)
        
        if not dl_path or not os.path.exists(dl_path):
            await rep.report(f"Failed to download torrent: {torrent_url}", "error")
            return None
        
        print(f"Download directory contents: {os.listdir(unique_dir)}")
        print(f"Successfully downloaded to: {dl_path}")
        return dl_path
    except Exception as e:
        await rep.report(f"Download error: {str(e)}", "error")
        return None

async def upload_file(file_path, quality):
    try:
        uploader = TgUploader() 
        msg = await uploader.upload(file_path, quality)
        return msg
    except Exception as e:
        await rep.report(f"Upload error: {str(e)}", "error")
        return None

async def process_low_end(name, torrent_url, force=False):
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        encoding_enabled = await db.get_encoding()
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        existing_main_post = await db.getAnimeMainPost(ani_id, ep_no)
        if existing_main_post:
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)
            return

        await db.saveAnimeResume(ani_id, ep_no, name, torrent_url)

        if force or (not (ani_data := await db.getAnime(ani_id)) \
            or (ani_data and not (qual_data := ani_data.get(ep_no))) \
            or (ani_data and qual_data and not all(qual for qual in qual_data.values()))):

            if "[Batch]" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if "(Batch)" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if hasattr(torrent_url, 'size') and torrent_url.size:
                if is_torrent_oversized(str(torrent_url.size), max_size_gb=4.5):
                    await rep.report(f"Torrent Skipped! - Size exceeds 4.5GB limit\n\n{name}", "warning")
                    return

            await rep.report(f"New Anime Torrent Found!\n\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            channel_to_use = target_channel if destination_channel is not None else await get_or_create_anime_channel(name, aniInfo)

            channel_valid = False
            for attempt in range(3):
                if await validate_channel_id(channel_to_use):
                    channel_valid = True
                    break
                await asyncio.sleep(2)

            if not channel_valid:
                await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
                channel_to_use = Var.MAIN_CHANNEL

            all_banners = await db.list_anime_banners()
            banner_url = None
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break

            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")

            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")

            post_msg = None
            main_post = backup_post = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")

            file_store_links = {}
            saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)
            buttons = []
            btns = []
            post_id = None
            for qual in ["480", "720", "1080"]:
                saved_msg_id = saved_file_ids.get(qual)
                if saved_msg_id:
                    try:
                        stored_msg = await bot.get_messages(
                            Var.FILE_STORE, message_ids=int(saved_msg_id)
                        )
                        if stored_msg and not getattr(stored_msg, "empty", False):
                            file_store_links[qual] = int(saved_msg_id)
                            await rep.report(
                                f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                "info"
                            )
                            continue
                    except Exception as verify_error:
                        await rep.report(
                            f"Saved {qual} upload is unavailable; retrying this quality: {verify_error}",
                            "warning"
                        )

                rss_link = Var.RSS_LOW_END.get(qual)
                if not rss_link:
                    continue
                feed = await getfeed(rss_link)
                if not feed or not feed.get("link"):
                    continue

                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Downloading...</i></pre>")
                dl_path = await download_torrent(feed["link"])
                if not dl_path:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to download {qual}p</i></pre>")
                    continue

                if Var.LOW_END_RENAME:
                        editor = TextEditor(name)
                        filename = await aniInfo.get_upname(qual)
                        anime_format = aniInfo.adata.get("format", "").lower()
                        is_movie = anime_format == "movie"
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Encode...</i></pre>")
                        await asleep(1.5)
                        await rep.report("Starting Encode...", "info")
                        try:
                            out_path = await FFEncoder(stat_msg, dl_path, filename, qual, is_movie=is_movie).start_encode()
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Compressed Now Going To Upload...", "info")
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Upload...</i></pre>")
                        await asleep(1.5)
                        try:
                            msg = await TgUploader(stat_msg).upload(out_path, qual)
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Uploaded File into Channel...", "info")
                        msg_id = msg.id
                        file_store_links[qual] = msg_id
                        await db.saveAnimeFileId(ani_id, ep_no, qual, msg_id)
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        saved_file_ids[qual] = msg_id
                        link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(await encode('get-'+str(msg_id * abs(Var.FILE_STORE))))
                        if len(btns) != 0 and len(btns[-1]) == 1:
                            btns[-1].insert(1, InlineKeyboardButton(f"{btn_formatter[qual]}", url=link))
                        else:
                            btns.append([InlineKeyboardButton(f"{btn_formatter[qual]}", url=link)])
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        bot_loop.create_task(extra_utils(msg_id, out_path))

                        gdrive_status = await db.get_gdrive_upload()
                        if gdrive_status == "on":
                            possible_names = await get_all_possible_anime_names(cleaned_name)
                            gdrive_link = None
                            for check_name in possible_names:
                                gdrive_link = await db.get_gdrive_mapping(check_name, qual)
                                if gdrive_link:
                                    try:
                                        from bot.core.gdrive import GdriveUploader
                                        await rep.report(f"Uploading to GDrive for {check_name} - {qual}p", "info")
                                        gdrive_uploader = GdriveUploader()
                                        await gdrive_uploader.upload_file(out_path, gdrive_link)
                                        await rep.report(f"Uploaded to GDrive for {check_name} - {qual}p", "info")
                                    except Exception as e:
                                        await rep.report(f"GDrive upload failed for {check_name} - {qual}p: {e}", "error")
                                    break
                else:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to upload {qual}p</i></pre>")

            quality_order = ["480", "720", "1080", "HDRi"]
            all_buttons = []

            for qual in quality_order:
                msg_id = file_store_links.get(qual)
                if not msg_id:
                    continue

                encoded = await encode(
                    "get-" + str(msg_id * abs(Var.FILE_STORE))
                )
                all_buttons.append(
                    InlineKeyboardButton(
                        text=btn_formatter[qual],
                        url=f"https://t.me/{Var.BOT_USERNAME}?start={encoded}"
                    )
                )

            grouped_buttons = [
                all_buttons[i:i + 2]
                for i in range(0, len(all_buttons), 2)
            ]

            final_caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    final_caption = obfuscate_text_for_copyright(final_caption)
            except Exception as e:
                await rep.report(
                    f"Error applying font changer: {e}",
                    "warning"
                )

            final_markup = (
                InlineKeyboardMarkup(grouped_buttons)
                if grouped_buttons else None
            )

            required_low_end = ["480", "720", "1080", "HDRi"]
            missing_low_end = [q for q in required_low_end if q not in file_store_links]
            if missing_low_end:
                raise RuntimeError(
                    f"Low-end episode {name} is not ready to publish; missing qualities: {', '.join(missing_low_end)}"
                )

            final_post = await bot.send_photo(
                Var.MAIN_CHANNEL, photo=photo_url,
                caption=final_caption, reply_markup=final_markup
            )
            if not final_post or not getattr(final_post, "id", None):
                raise RuntimeError(f"Main Channel did not confirm post for {name}.")

            await db.saveAnimeMainPost(ani_id, ep_no, final_post.id)
            for uploaded_qual in file_store_links:
                await db.saveAnime(ani_id, ep_no, uploaded_qual, final_post.id)
            await db.markAnimeResumeComplete(ani_id, ep_no)
            if ani_id and ep_no:
                await db.mark_episode_completed(ani_id, ep_no)


            if stat_msg:
                await stat_msg.delete()
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)

    except Exception as e:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(f"Low-end processing error: {str(e)}", "error")

async def fetch_animes():
    await rep.report("Fetching Anime Started !!!", "info")
    seen_rss_links = {}
    while True:
        await asleep(15)
        mode, upload_mode = await asyncio.gather(db.get_mode(), db.get_upload_mode())
        if ani_cache.get('global_pause') and not ani_cache.get('manual_task'):
            continue
        if mode != "anime" or not ani_cache['fetch_animes']:
            continue
        try:
            # Scan every configured quality feed in both upload modes.
            # The main RSS list may contain only 1080p; omitting the 480p/720p
            # feeds caused scheduled releases and Sub/Dual variants to be missed.
            configured_feeds = list(Var.RSS_ITEMS_ANIME or [])
            quality_feeds = [
                Var.RSS_LOW_END.get(q)
                for q in ("480", "720", "1080")
                if Var.RSS_LOW_END.get(q)
            ]
            rss_urls = list(dict.fromkeys(
                url for url in configured_feeds + quality_feeds if url
            ))

            # Recover unfinished episodes from MongoDB before scanning fresh RSS.
            # This works even if the source entry has fallen out of the RSS window.
            recovered_links = set()
            resume_failed = False
            try:
                resume_tasks = await db.getAnimeResumeTasks()
                for resume_task in resume_tasks:
                    resume_name = resume_task.get("name")
                    resume_link = resume_task.get("torrent_url")
                    if not resume_name or not resume_link:
                        continue
                    recovered_links.add(resume_link)
                    await rep.report(
                        f"Resuming unfinished episode from saved checkpoint: {resume_name}",
                        "info"
                    )
                    # Do not advance to another episode until this saved episode
                    # completes. get_animes re-raises failures after preserving
                    # successful per-quality checkpoints.
                    await get_animes(resume_name, resume_link)
            except Exception as resume_error:
                resume_failed = True
                await rep.report(
                    f"Episode resume failed; holding the queue on this unfinished episode. "
                    f"It will retry on the next scan: {resume_error}",
                    "error"
                )

            if resume_failed:
                # Never start fresh RSS jobs while an earlier episode is unfinished.
                continue

            feeds_by_url = await asyncio.gather(*(getfeeds(url) for url in rss_urls))
            pending = []
            for rss_url, entries in zip(rss_urls, feeds_by_url):
                if not entries:
                    continue
                seen = seen_rss_links.setdefault(rss_url, set())
                # Process oldest unseen entry first so a burst of releases is not skipped.
                for entry in entries:
                    link = getattr(entry, "link", None)
                    title = getattr(entry, "title", None)
                    if not link or not title or link in seen or link in recovered_links:
                        continue
                    seen.add(link)
                    pending.append((title, link))
                # Bound memory while retaining a recent de-duplication window.
                if len(seen) > 500:
                    seen_rss_links[rss_url] = set(list(seen)[-250:])

            # One episode at a time: finish/retry the current item before advancing.
            for title, link in pending:
                try:
                    await get_animes(title, link)
                except Exception:
                    await rep.report(f"Episode processing failed; will retry on next RSS scan: {title}\\n{format_exc()}", "error")
                    # Make it eligible for the next scan instead of permanently consuming it.
                    for rss_url in rss_urls:
                        seen_rss_links.get(rss_url, set()).discard(link)
                    break
        except Exception as e:
            await rep.report(f"Anime fetch error: {str(e)}", "error")

async def get_or_create_anime_channel(name: str, aniInfo):
    try:
        base_anime_name = extract_base_anime_name(name)
        normalized_name = normalize_anime_name_for_search(name)
        
        await rep.report(f"Searching for existing channel for: {base_anime_name}", "info")
        await asleep(1.5)
        custom_channel = await db.get_anime_channel(normalized_name)
        if custom_channel:
            await rep.report(f"Found custom channel mapping: {custom_channel}", "info")
            return custom_channel

        raw_entry = await db.get_raw_anime_channel(normalized_name)
        if raw_entry is not None and 'channel_id' not in raw_entry:
            await rep.report(f"Found banner-only mapping for {normalized_name} (no channel_id)", "info")
            channel_creation = await db.get_channel_creation()
            if channel_creation and Var.USER_SESSION:
                await rep.report("Banner-only mapping found â creating channel because channel creation is enabled", "info")
                new_channel = await create_anime_channel(name, aniInfo)
                if new_channel:
                    await db.add_anime_channel_mapping(normalized_name, new_channel)
                    await rep.report(f"Created channel for banner-only mapping and updated DB: {new_channel}", "info")
                    return new_channel
                else:
                    await rep.report("Failed to create channel for banner-only mapping, falling back to main channel", "error")
                    return Var.MAIN_CHANNEL
            else:
                await rep.report("Banner-only mapping present but channel creation is disabled or no user session; using main channel", "info")
                return Var.MAIN_CHANNEL

        existing_channel = await find_existing_anime_channel(name)
        if existing_channel:
            await rep.report(f"Found existing channel for {base_anime_name}: {existing_channel}", "info")
            await db.add_anime_channel_mapping(normalized_name, existing_channel)
            return existing_channel
        
        channel_creation = await db.get_channel_creation()
        if not channel_creation or not Var.USER_SESSION:
            await rep.report("Channel creation disabled or no user session, using main channel", "info")
            return Var.MAIN_CHANNEL

        await rep.report(f"Creating new channel for: {base_anime_name}", "info")
        new_channel = await create_anime_channel(name, aniInfo)
        
        if new_channel:
            await db.add_anime_channel_mapping(normalized_name, new_channel)
            await rep.report(f"Successfully created and saved channel mapping for {base_anime_name}", "info")
            return new_channel
        else:
            await rep.report("Failed to create channel, using main channel", "error")
            return Var.MAIN_CHANNEL
            
    except Exception as e:
        await rep.report(f"Error in get_or_create_anime_channel: {e}", "error")
        return Var.MAIN_CHANNEL

async def create_anime_channel(name, aniInfo):
    try:
        if not Var.USER_SESSION:
            await rep.report("User session not available for channel creation", "error")
            return None

        from pyrogram import Client
        user_client = Client("user_session", session_string=Var.USER_SESSION, 
                           api_id=Var.API_ID, api_hash=Var.API_HASH)

        await user_client.start()

        anime_format = (aniInfo.adata.get("format", "") or "").lower()
        is_movie = anime_format == "movie" or (
            hasattr(aniInfo, "is_movie_title") and aniInfo.is_movie_title(name)
        )

        titles = aniInfo.adata.get("title", {})
        channel_name = titles.get("english") or titles.get("romaji") or clean_anime_title(name)
        title = channel_name
        status = aniInfo.adata.get("status", "Unknown").capitalize()
        genres = aniInfo.adata.get("genres", [])[:3]
        genres_formatted = ", ".join(genres) if genres else "Unknown"

        if is_movie:
            description = (
                f"â¢ Movie: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )
        else:
            description = (
                f"â¢ Anime: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )

        created = await user_client.create_channel(channel_name, description)
        channel_id = created.id

        poster_url = (
            aniInfo.adata.get("coverImage", {}).get("large")
            or aniInfo.adata.get("coverImage", {}).get("medium")
            or aniInfo.adata.get("coverImage", {}).get("small")
        )
        if poster_url:
            filename = f"/tmp/{channel_id}_logo.jpg"
            filename = download_image(poster_url, filename)
            if filename:
                try:
                    await user_client.set_chat_photo(channel_id, photo=filename)
                    print(f"Set channel photo for {channel_id} successfully!")
                    remove(filename)
                except Exception as e:
                    print(f"Failed to set channel photo: {e}")
                    await rep.report(f"Failed to set channel photo: {e}", "warning")
            else:
                print("Image file was not downloaded or does not exist.")
                await rep.report(f"Image file was not downloaded or does not exist.", "warning")
        else:
            print("No poster URL found in AniList data.")
            await rep.report(f"No poster URL found in AniList data.", "warning")

        try:
            from pyrogram.types import ChatPrivileges
            await user_client.promote_chat_member(
                channel_id,
                Var.CREATION_USERNAME,
                privileges=ChatPrivileges(
                    can_post_messages=True,
                    can_edit_messages=True,
                    can_delete_messages=True,
                    can_invite_users=True,
                    can_change_info=True,
                    can_pin_messages=True,
                    can_manage_chat=True,
                    can_manage_video_chats=False,
                    is_anonymous=False
                )
            )
            await rep.report(f"Successfully added and promoted bot in channel", "info")
        except Exception as e:
            await rep.report(f"Failed to add/promote bot in channel: {e}", "error")

        try:
            titles = aniInfo.adata.get("title", {})
            genres = ', '.join(aniInfo.adata.get("genres", [])[:3])
            anime_type = aniInfo.adata.get("format", "Unknown")
            avg_rating = aniInfo.adata.get("averageScore", "N/A")
            status = aniInfo.adata.get("status", "Unknown")
            start_date = aniInfo.adata.get("startDate", {})
            end_date = aniInfo.adata.get("endDate", {})
            runtime = aniInfo.adata.get("duration", "Unknown")
            episodes = aniInfo.adata.get("episodes", "Unknown")
            desc = aniInfo.adata.get("description", "No description available").replace("<br>", "").replace("<i>", "").replace("</i>", "").strip()
            desc = (desc[:200] + "...") if len(desc) > 200 else desc 
            site_url = aniInfo.adata.get("siteUrl", "https://anilist.co")
            media_id = aniInfo.adata.get("id")

            english = titles.get("english", "Unknown")
            romaji = titles.get("romaji", "Unknown")
            display_title = english if english != "Unknown" else romaji

            graphic_poster_url = f"https://img.anili.st/media/{media_id}"

            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.head(graphic_poster_url) as resp:
                    if resp.status != 200:
                        graphic_poster_url = poster_url or "https://imgs.search.brave.com/33oSQBqhBoRB6akjKPud5oFd-PxnMFEm1AjnBTS2ZhE/rs:fit:860:0:0:0/g:ce/aHR0cHM6Ly9pbWcu/ZnJlZXBpay5jb20v/dmVjdG9yLXByZW1p/dW0vaWx1c3RyYWNp/b24tYW5pbWUtZXJy/b3ItNDA0LXBhZ2lu/YS1uby1lbmNvbnRy/YWRhXzE1MDk3Mi0y/NTY0LmpwZw"

            if is_movie:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ Release Date : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Duration : {runtime} minutes</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""
            else:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ First aired : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Last aired : {end_date.get("year", '')}-{end_date.get("month", '')}-{end_date.get("day", '')}
â£ Runtime : {runtime} minutes
â£ No of episodes : {episodes}</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""

            await user_client.send_photo(
                chat_id=channel_id,
                photo=graphic_poster_url,
                caption=caption
            )

            STICKER_ID = "CAACAgUAAxkBAAIERmhruO6M5RpLtZjb2yrvPJxTuw0IAAIzEwACTrLwVSdrhgIPuK1TNgQ"
            await user_client.send_sticker(channel_id, STICKER_ID)

            await rep.report("Intro poster, caption, and sticker sent to channel", "info")

        except Exception as e:
            await rep.report(f"Failed to send intro content: {e}", "warning")

        await db.add_anime_channel_mapping(channel_name, channel_id)

        await asleep(2)

        await user_client.stop()

        await rep.report(f"Successfully created channel: {channel_name} (ID: {channel_id})", "info")
        return channel_id

    except Exception as e:
        await rep.report(f"Failed to create anime channel: {e}", "error")
        return None


async def build_anime_soul_poster(anime_info, anime_name, output_path, episode_label=None):
    """Create the ANIME SOUL split-screen streaming thumbnail (16:9)."""
    import io
    import textwrap
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter, ImageEnhance

    data = anime_info.adata or {}
    titles = data.get("title") or {}
    title = titles.get("english") or titles.get("romaji") or anime_name

    def clean_value(value, default=""):
        if isinstance(value, list):
            value = value[0] if value else default
        return str(value or default).strip()

    season = clean_value(anime_info.pdata.get("anime_season"), "01")
    episode = clean_value(episode_label or anime_info.pdata.get("episode_number"), "01")
    try:
        season = str(int(season))
    except Exception:
        pass
    try:
        episode = str(int(episode)).zfill(2)
    except Exception:
        pass

    cover_data = data.get("coverImage") or {}
    cover_url = cover_data.get("extraLarge") or cover_data.get("large") or data.get("poster_url")
    banner_url = data.get("bannerImage")
    # Prefer the portrait cover for the large poster panel; use banner if cover is unavailable.
    artwork_url = cover_url or banner_url
    if not artwork_url:
        await rep.report("ANIME SOUL split-screen thumbnail skipped: AniList has no artwork URL.", "warning")
        return None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(artwork_url)
            response.raise_for_status()
            art = Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception as exc:
        await rep.report(f"ANIME SOUL artwork download failed: {exc}", "warning")
        return None

    W, H = 1600, 900
    split_x = 690
    art = ImageEnhance.Color(art).enhance(1.08)
    art = ImageEnhance.Contrast(art).enhance(1.04)

    # Left: full-height poster artwork, cropped to fill without stretching.
    left_art = ImageOps.fit(
        art, (split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.45),
    )

    # Right: same anime artwork, blurred and washed out under a translucent light panel.
    right_art = ImageOps.fit(
        art, (W - split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.35),
    ).filter(ImageFilter.GaussianBlur(16))
    right_art = ImageEnhance.Brightness(right_art).enhance(1.8)
    right_art = ImageEnhance.Color(right_art).enhance(0.45)

    canvas = Image.new("RGB", (W, H), (245, 246, 250))
    canvas.paste(left_art, (0, 0))
    canvas.paste(right_art, (split_x, 0))
    overlay = Image.new("RGBA", (W - split_x, H), (250, 250, 253, 222))
    canvas.paste(overlay, (split_x, 0), overlay)
    draw = ImageDraw.Draw(canvas)

    regular_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    bold_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    def font(size, bold=False):
        for path in (bold_candidates if bold else regular_candidates):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def fit_line(text, fnt, max_width):
        text = str(text)
        while text and draw.textbbox((0, 0), text, font=fnt)[2] > max_width:
            text = text[:-2].rstrip() + "…"
        return text

    def wrapped(text, fnt, max_width, max_lines):
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
            if len(lines) >= max_lines:
                break
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) == max_lines and len(words) > sum(len(x.split()) for x in lines):
            lines[-1] = fit_line(lines[-1] + "…", fnt, max_width)
        return lines

    def wrap_complete(text, fnt, max_width):
        """Wrap every title word; never drop the tail of a long AniList title."""
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    # Left poster branding and title overlay.
    left_overlay = Image.new("RGBA", (split_x, H), (0, 0, 0, 0))
    left_draw = ImageDraw.Draw(left_overlay)
    for y in range(560, H):
        alpha = int(205 * ((y - 560) / (H - 560)))
        left_draw.line((0, y, split_x, y), fill=(0, 0, 0, alpha))
    canvas.paste(left_overlay, (0, 0), left_overlay)
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle((34, 30, 330, 90), radius=15, fill=(8, 12, 22), outline=(255,255,255), width=2)
    draw.text((55, 45), "ANIME SOUL", font=font(29, True), fill=(255,255,255))
    draw.text((235, 51), "/ STREAM", font=font(16, True), fill=(235,55,70))
    # Show the complete AniList title across lines instead of truncating it.
    left_title_font = font(38, True)
    left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    if len(left_title_lines) == 3:
        left_title_font = font(32, True)
        left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    left_title_y = 720 if len(left_title_lines) <= 2 else 690
    for title_line in left_title_lines:
        draw.text((42, left_title_y), title_line, font=left_title_font,
                  fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0))
        left_title_y += left_title_font.size + 8
    draw.text((48, 850), f"SEASON {season}   •   EPISODE {episode}",
              font=font(23, True), fill=(255,220,70))

    # Right-side streaming dashboard header and content geometry.
    rx = split_x + 58
    right_w = W - rx - 58
    draw.text((rx, 40), "ANIME SOUL", font=font(25, True), fill=(15,18,27))
    draw.text((rx+210, 45), "/ STREAM", font=font(18), fill=(210,35,55))
    nav = "HOME     ANIME     SCHEDULE     LIBRARY"
    nav_font = font(17, True)
    nav_width = draw.textbbox((0,0), nav, font=nav_font)[2]
    if nav_width < right_w:
        draw.text((W-58-nav_width, 48), nav, font=nav_font, fill=(35,38,48))

    # Wrap the complete AniList title instead of cutting it off with an ellipsis.
    title_font = font(50, True)
    title_lines = wrap_complete(str(title), title_font, right_w)
    if len(title_lines) == 3:
        title_font = font(42, True)
        title_lines = wrap_complete(str(title), title_font, right_w)
    title_y = 150
    for title_line in title_lines:
        draw.text((rx, title_y), title_line, font=title_font, fill=(8,10,18))
        title_y += title_font.size + 10

    rule_y = title_y + 8
    draw.rounded_rectangle((rx, rule_y, rx+right_w, rule_y+8), radius=4, fill=(25,92,190))
    pill_y = rule_y + 32
    draw.rounded_rectangle((rx, pill_y, rx+195, pill_y+56), radius=26, fill=(20,91,190))
    draw.text((rx+25, pill_y+13), f"SEASON {season}", font=font(23, True), fill=(255,255,255))
    draw.rounded_rectangle((rx+215, pill_y, rx+440, pill_y+56), radius=26, outline=(65,70,85), width=2, fill=(255,255,255))
    draw.text((rx+238, pill_y+13), f"EPISODE {episode}", font=font(23, True), fill=(18,20,28))
    draw.rounded_rectangle((rx+right_w-112, pill_y, rx+right_w, pill_y+56), radius=12, outline=(20,24,35), width=2)
    draw.text((rx+right_w-92, pill_y+13), "HD", font=font(25, True), fill=(18,20,28))

    # Place the AniList synopsis below the variable-height title block.
    synopsis = re.sub(r"<[^>]+>", " ", str(data.get("description") or "Discover the story in this anime."))
    synopsis = re.sub(r"\\s+", " ", synopsis).strip()
    syn_font = font(19)
    sy = pill_y + 82
    synopsis_lines = max(2, min(6, (735 - sy) // 29))
    for line in wrapped(synopsis, syn_font, right_w, synopsis_lines):
        draw.text((rx, sy), line, font=syn_font, fill=(35,38,48))
        sy += 29

    # Watch button.
    button_y = min(650, max(sy + 18, pill_y + 145))
    draw.rounded_rectangle((rx, button_y, rx+310, button_y+78), radius=38, fill=(12,14,20))
    draw.polygon([(rx+38,button_y+24),(rx+38,button_y+54),(rx+64,button_y+39)], fill=(255,255,255))
    draw.text((rx+83, button_y+22), "WATCH NOW", font=font(25, True), fill=(255,255,255))
    draw.ellipse((rx+340, button_y+2, rx+416, button_y+78), outline=(75,80,92), width=2)
    draw.text((rx+363, button_y+19), "☆", font=font(38), fill=(20,24,34))

    # Metadata row at the bottom.
    line_y = 770
    draw.line((rx, line_y, W-58, line_y), fill=(130,135,145), width=2)
    studio_nodes = (data.get("studios") or {}).get("nodes") or []
    studio = studio_nodes[0].get("name") if studio_nodes else "Not listed"
    year = (data.get("startDate") or {}).get("year") or data.get("seasonYear") or "—"
    genres = ", ".join((data.get("genres") or [])[:2]) or "Anime"
    status = str(data.get("status") or "—").replace("_", " ").title()
    metadata = [
        ("YEAR", str(year)),
        ("STUDIO", str(studio)),
        ("GENRES", genres),
        ("STATUS", status),
    ]
    col_w = right_w // 4
    for i, (label, value) in enumerate(metadata):
        x = rx + i * col_w
        draw.text((x, 795), label, font=font(14, True), fill=(100,105,115))
        draw.text((x, 823), fit_line(value, font(17, True), col_w-14), font=font(17, True), fill=(15,18,27))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    canvas.save(output_path, "JPEG", quality=94, optimize=True, progressive=True)
    return output_path


async def get_animes(name, torrent, force=False):
    mode = await db.get_mode()
    if mode != "anime":
        await rep.report(f"Skipping {name} because mode is not anime (current mode: {mode})", "info")
        return

    try:
        cleaned_name = clean_anime_title(name)
        base_name = extract_base_anime_name(cleaned_name)
        normalized_name = normalize_anime_name_for_search(base_name)
        
        gdrive_status = await db.get_gdrive_upload()
        if gdrive_status == "on":
            print(f"Checking GDrive mapping for: {normalized_name}")
            await asleep(1.5)
            possible_names = await get_all_possible_anime_names(normalized_name)
            
            for check_name in possible_names:
                for quality in Var.QUALS:
                    gdrive_link = await db.get_gdrive_mapping(check_name, quality)
                    if gdrive_link:
                        await rep.report(f"Found GDrive mapping for {check_name} - {quality}p", "info")
                        await db.saveAnime(torrent, "1", quality, None, gdrive_link)
                        break
                    
        upload_mode = await db.get_upload_mode()
        print(f"get_animes: upload_mode={upload_mode}")
        if upload_mode == "low_end":
            print(f"get_animes: calling process_low_end for {name}")
            await process_low_end(name, torrent)
            return
    except Exception as e:
        await rep.report(f"Error checking upload mode: {str(e)}", "error")
        return

    if not await is_valid_torrent_url(torrent):
        await rep.report(f"Invalid or non-torrent URL for {name}: {torrent}", "error")
        return
    
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        # Cache state per anime episode, not per series. A series can have
        # many episodes; using ani_id alone incorrectly blocks later episodes.
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        encoding_enabled = await db.get_encoding()
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        # If the Main Channel post already exists, never publish it twice.
        existing_main_post = await db.getAnimeMainPost(ani_id, ep_no)
        if existing_main_post:
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)
            return

        # Keep the torrent source in MongoDB so a restart can rediscover this
        # episode even after it falls out of the RSS feed window.
        await db.saveAnimeResume(ani_id, ep_no, name, torrent)
        
        if force or not existing_main_post:
            
            if "[Batch]" in name:
                await rep.report(f"Torrent Skipped!\n{name}", "warning")
                return
            
            await rep.report(f"New Anime Torrent Found!\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            channel_to_use = target_channel if destination_channel is not None else await get_or_create_anime_channel(name, aniInfo)
            
            channel_valid = False
            for attempt in range(3):
                if await validate_channel_id(channel_to_use):
                    channel_valid = True
                    break
                await asyncio.sleep(2)
            
            if not channel_valid:
                await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
                channel_to_use = Var.MAIN_CHANNEL
            
            post_msg = None
            photo_path = None
            all_banners = await db.list_anime_banners()
            banner_url = None
            
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break
            
            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")
            
            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")
            
            try:
                poster_path = f"thumbs/anime_soul_{aniInfo.adata.get('id') or hashlib.md5(cleaned_name.encode()).hexdigest()}.jpg"
                photo_path = await build_anime_soul_poster(aniInfo, cleaned_name, poster_path)
            except Exception as poster_error:
                await rep.report(f"ANIME SOUL poster builder failed; using original artwork: {poster_error}", "warning")
                photo_path = None
            post_msg = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")
            
            dl_path = await download_torrent(torrent, name)
            if not dl_path or not ospath.exists(dl_path):
                await rep.report(f"File Download Incomplete for {name}, Try Again", "error")
                await stat_msg.delete()
                return
            
            post_id = int(datetime.datetime.now().timestamp() * 1000000)
            ffEvent = Event()
            ff_queued[post_id] = ffEvent
            
            if ffLock.locked():
                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Queued to Encode...</i></pre>")
                await rep.report("Added Task to Queue...", "info")
            
            await ffQueue.put(post_id)
            await ffEvent.wait()
            await ffLock.acquire()
            try:
                btns = []
                uploaded_qualities = []
                required_qualities = list(Var.QUALS) if encoding_enabled else ["HDRi"]
                saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)

                async def add_quality_button(qual, msg_id):
                    link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(
                        await encode('get-' + str(int(msg_id) * abs(Var.FILE_STORE)))
                    )
                    label = RAW_BTN if qual == "HDRi" else btn_formatter.get(qual, qual + "p")
                    button = InlineKeyboardButton(label, url=link)
                    if btns and len(btns[-1]) == 1:
                        btns[-1].append(button)
                    else:
                        btns.append([button])

                for qual in required_qualities:
                    # Reuse a quality that was already uploaded before the last
                    # crash. Verify the message still exists in FILE_STORE.
                    saved_msg_id = saved_file_ids.get(qual)
                    if saved_msg_id:
                        try:
                            stored_msg = await bot.get_messages(
                                Var.FILE_STORE, message_ids=int(saved_msg_id)
                            )
                            if stored_msg and not getattr(stored_msg, "empty", False):
                                await add_quality_button(qual, saved_msg_id)
                                uploaded_qualities.append(qual)
                                await rep.report(
                                    f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                    "info"
                                )
                                continue
                        except Exception as verify_error:
                            await rep.report(
                                f"Saved {qual} message {saved_msg_id} is unavailable; re-uploading this quality: {verify_error}",
                                "warning"
                            )

                    filename = await aniInfo.get_upname(qual)
                    anime_format = aniInfo.adata.get("format", "").lower()
                    is_movie = anime_format == "movie"
                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n"
                        f"<pre><i>Encoding {qual}...</i></pre>"
                    )
                    await rep.report(f"Starting Encode for {qual}...", "info")

                    out_path = await FFEncoder(
                        stat_msg, dl_path, filename, qual, is_movie=is_movie
                    ).start_encode()
                    if not out_path or not ospath.exists(out_path):
                        raise RuntimeError(
                            f"{qual} encoding failed for {name}; earlier uploaded qualities are saved and will be reused on retry."
                        )

                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{filename}</i></b></blockquote>\n\n"
                        f"<pre><i>Uploading {qual}...</i></pre>"
                    )
                    msg = await TgUploader(stat_msg).upload(out_path, qual)
                    if not msg or not getattr(msg, "id", None):
                        raise RuntimeError(f"Telegram did not confirm {qual} upload for {name}.")

                    # Persist immediately after Telegram confirms the upload.
                    await db.saveAnimeFileId(ani_id, ep_no, qual, msg.id)
                    await db.saveAnime(ani_id, ep_no, qual, None)
                    saved_file_ids[qual] = msg.id
                    await add_quality_button(qual, msg.id)
                    uploaded_qualities.append(qual)
                    await rep.report(f"Successfully uploaded and checkpointed {qual} for {name}.", "info")
                    bot_loop.create_task(extra_utils(msg.id, out_path))

            finally:
                ffLock.release()
            
            await stat_msg.delete()
            if btns:
                markup = InlineKeyboardMarkup(btns)
                # Telegram may temporarily rate-limit the Main Channel post.
                # Retry the publish step without re-uploading the already stored files.
                last_publish_error = None
                for publish_attempt in range(5):
                    try:
                        if photo_path and ospath.exists(photo_path):
                            with open(photo_path, 'rb') as photo_file:
                                post_msg = await bot.send_photo(
                                    Var.MAIN_CHANNEL, photo=photo_file,
                                    caption=caption, reply_markup=markup
                                )
                        else:
                            post_msg = await bot.send_photo(
                                Var.MAIN_CHANNEL, photo=photo_url,
                                caption=caption, reply_markup=markup
                            )
                        last_publish_error = None
                        break
                    except FloodWait as flood:
                        wait_seconds = max(1, int(flood.value) + 1)
                        await rep.report(
                            f"Main Channel FLOOD_WAIT: retrying publish in {wait_seconds}s "
                            f"(attempt {publish_attempt + 1}/5).", "warning"
                        )
                        await asyncio.sleep(wait_seconds)
                        last_publish_error = flood
                    except Exception as publish_error:
                        last_publish_error = publish_error
                        break

                if last_publish_error is not None or post_msg is None:
                    raise RuntimeError(
                        f"Failed to publish completed episode to Main Channel: {last_publish_error}"
                    )

                # Persist the Main Channel post ID immediately after send succeeds.
                await db.saveAnimeMainPost(ani_id, ep_no, post_msg.id)
                for uploaded_qual in uploaded_qualities:
                    await db.saveAnime(ani_id, ep_no, uploaded_qual, post_msg.id)

                await db.markAnimeResumeComplete(ani_id, ep_no)

                # Mark complete only after the Main Channel post exists.
                ani_cache['completed'].add(episode_key)
                ani_cache['ongoing'].discard(episode_key)
            else:
                await rep.report(f"No qualities uploaded for {name}; no Main Channel post sent", "warning")
                raise RuntimeError(f"No qualities uploaded for {name}; episode remains retryable")

            if encoding_enabled:
                await aioremove(dl_path)
        else:
            # Existing Main Channel post is already recorded.
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
    except Exception as error:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(format_exc(), "error")
        # Propagate the failure to fetch_animes so it does not mark this
        # episode as handled and advance to the next RSS item.
        raise

async def extra_utils(msg_id, out_path):
    try:
        msg = await bot.get_messages(Var.FILE_STORE, message_ids=msg_id)

        if hasattr(Var, 'BACKUP_FILE_STORE') and Var.BACKUP_FILE_STORE != 0:
            for chat_id in str(Var.BACKUP_FILE_STORE).split():
                try:
                    await msg.copy(int(chat_id))
                except Exception as e:
                    await rep.report(f"Failed to backup to {chat_id}: {e}", "error")
    except Exception as e:
        await rep.report(f"Error in extra_utils: {e}", "error")

def safe_dirname(name):
    return re.sub(r'[\\/*?:"<>|\'\n\r\t]', '_', name)

async def download_torrent(torrent_url, name=""):
    try:
        unique_dir = generate_unique_dir(name, torrent_url)
        
        if os.path.exists(unique_dir):
            shutil.rmtree(unique_dir)
        os.makedirs(unique_dir, exist_ok=True)
        
        downloader = TorDownloader(unique_dir)
        dl_path = await downloader.download(torrent_url)
        
        if not dl_path or not os.path.exists(dl_path):
            await rep.report(f"Failed to download torrent: {torrent_url}", "error")
            return None
        
        print(f"Download directory contents: {os.listdir(unique_dir)}")
        print(f"Successfully downloaded to: {dl_path}")
        return dl_path
    except Exception as e:
        await rep.report(f"Download error: {str(e)}", "error")
        return None

async def upload_file(file_path, quality):
    try:
        uploader = TgUploader() 
        msg = await uploader.upload(file_path, quality)
        return msg
    except Exception as e:
        await rep.report(f"Upload error: {str(e)}", "error")
        return None
def extract_release_episode(title):
    """Extract the episode number from common SubsPlease naming formats."""
    value = str(title or "")
    patterns = (
        r'\bS\d{1,2}\s*[-.]\s*0*(\d{1,3})\b',
        r'\bS\d{1,2}E0*(\d{1,3})\b',
        r'\b(?:episode|ep)\s*[-._ ]?0*(\d{1,3})\b',
        r'\s-\s0*(\d{1,3})(?:\D|$)',
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            return str(int(match.group(1)))
    return None


async def find_matching_quality_release(rss_url, requested_title):
    """Find the same series and episode in a quality RSS; never use its newest unrelated item."""
    wanted_series = normalize_title_for_comparison(requested_title)
    wanted_episode = extract_release_episode(requested_title)
    if not wanted_series or wanted_episode is None:
        return None

    for entry in await getfeeds(rss_url):
        candidate_title = getattr(entry, "title", "") or ""
        candidate_link = getattr(entry, "link", None)
        if not candidate_link:
            continue
        candidate_series = normalize_title_for_comparison(candidate_title)
        candidate_episode = extract_release_episode(candidate_title)
        if candidate_episode != wanted_episode:
            continue
        if candidate_series == wanted_series or (
            candidate_series and wanted_series
            and (candidate_series in wanted_series or wanted_series in candidate_series)
        ):
            return {"title": candidate_title, "link": candidate_link}
    return None

async def process_low_end(name, torrent_url, force=False, destination_channel=None):
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        encoding_enabled = await db.get_encoding()
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        existing_main_post = await db.getAnimeMainPost(ani_id, ep_no) if destination_channel in (None, Var.MAIN_CHANNEL, str(Var.MAIN_CHANNEL)) else None
        if existing_main_post:
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)
            return

        await db.saveAnimeResume(ani_id, ep_no, name, torrent_url)

        if force or (not (ani_data := await db.getAnime(ani_id)) \
            or (ani_data and not (qual_data := ani_data.get(ep_no))) \
            or (ani_data and qual_data and not all(qual for qual in qual_data.values()))):

            if "[Batch]" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if "(Batch)" in name:
                await rep.report(f"Torrent Skipped!\n\n{name}", "warning")
                return

            if hasattr(torrent_url, 'size') and torrent_url.size:
                if is_torrent_oversized(str(torrent_url.size), max_size_gb=4.5):
                    await rep.report(f"Torrent Skipped! - Size exceeds 4.5GB limit\n\n{name}", "warning")
                    return

            await rep.report(f"New Anime Torrent Found!\n\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            channel_to_use = target_channel if destination_channel is not None else await get_or_create_anime_channel(name, aniInfo)

            channel_valid = False
            for attempt in range(3):
                if await validate_channel_id(channel_to_use):
                    channel_valid = True
                    break
                await asyncio.sleep(2)

            if not channel_valid:
                await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
                channel_to_use = Var.MAIN_CHANNEL

            all_banners = await db.list_anime_banners()
            banner_url = None
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break

            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")

            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")

            post_msg = None
            main_post = backup_post = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")

            file_store_links = {}
            saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)
            buttons = []
            btns = []
            post_id = None
            for qual in ["480", "720", "1080"]:
                saved_msg_id = saved_file_ids.get(qual)
                if saved_msg_id:
                    try:
                        stored_msg = await bot.get_messages(
                            Var.FILE_STORE, message_ids=int(saved_msg_id)
                        )
                        if stored_msg and not getattr(stored_msg, "empty", False):
                            file_store_links[qual] = int(saved_msg_id)
                            await rep.report(
                                f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                "info"
                            )
                            continue
                    except Exception as verify_error:
                        await rep.report(
                            f"Saved {qual} upload is unavailable; retrying this quality: {verify_error}",
                            "warning"
                        )

                rss_link = Var.RSS_LOW_END.get(qual)
                if not rss_link:
                    continue
                feed = await getfeed(rss_link)
                if not feed or not feed.get("link"):
                    continue

                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Downloading...</i></pre>")
                dl_path = await download_torrent(feed["link"])
                if not dl_path:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to download {qual}p</i></pre>")
                    continue

                if Var.LOW_END_RENAME:
                        editor = TextEditor(name)
                        filename = await aniInfo.get_upname(qual)
                        anime_format = aniInfo.adata.get("format", "").lower()
                        is_movie = anime_format == "movie"
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Encode...</i></pre>")
                        await asleep(1.5)
                        await rep.report("Starting Encode...", "info")
                        try:
                            out_path = await FFEncoder(stat_msg, dl_path, filename, qual, is_movie=is_movie).start_encode()
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Compressed Now Going To Upload...", "info")
                        await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name:</b> <b><i>{filename}</i></b></blockquote>\n\n<pre><i>Ready to Upload...</i></pre>")
                        await asleep(1.5)
                        try:
                            msg = await TgUploader(stat_msg).upload(out_path, qual)
                        except Exception as e:
                            await rep.report(f"Error: {e}, Cancelled, Retry Again !", "error")
                            await stat_msg.delete()
                            return
                        await rep.report("Successfully Uploaded File into Channel...", "info")
                        msg_id = msg.id
                        file_store_links[qual] = msg_id
                        await db.saveAnimeFileId(ani_id, ep_no, qual, msg_id)
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        saved_file_ids[qual] = msg_id
                        link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(await encode('get-'+str(msg_id * abs(Var.FILE_STORE))))
                        if len(btns) != 0 and len(btns[-1]) == 1:
                            btns[-1].insert(1, InlineKeyboardButton(f"{btn_formatter[qual]}", url=link))
                        else:
                            btns.append([InlineKeyboardButton(f"{btn_formatter[qual]}", url=link)])
                        await db.saveAnime(ani_id, ep_no, qual, None)
                        bot_loop.create_task(extra_utils(msg_id, out_path))

                        gdrive_status = await db.get_gdrive_upload()
                        if gdrive_status == "on":
                            possible_names = await get_all_possible_anime_names(cleaned_name)
                            gdrive_link = None
                            for check_name in possible_names:
                                gdrive_link = await db.get_gdrive_mapping(check_name, qual)
                                if gdrive_link:
                                    try:
                                        from bot.core.gdrive import GdriveUploader
                                        await rep.report(f"Uploading to GDrive for {check_name} - {qual}p", "info")
                                        gdrive_uploader = GdriveUploader()
                                        await gdrive_uploader.upload_file(out_path, gdrive_link)
                                        await rep.report(f"Uploaded to GDrive for {check_name} - {qual}p", "info")
                                    except Exception as e:
                                        await rep.report(f"GDrive upload failed for {check_name} - {qual}p: {e}", "error")
                                    break
                else:
                    await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Failed to upload {qual}p</i></pre>")

            quality_order = ["480", "720", "1080", "HDRi"]
            all_buttons = []

            for qual in quality_order:
                msg_id = file_store_links.get(qual)
                if not msg_id:
                    continue

                encoded = await encode(
                    "get-" + str(msg_id * abs(Var.FILE_STORE))
                )
                all_buttons.append(
                    InlineKeyboardButton(
                        text=btn_formatter[qual],
                        url=f"https://t.me/{Var.BOT_USERNAME}?start={encoded}"
                    )
                )

            grouped_buttons = [
                all_buttons[i:i + 2]
                for i in range(0, len(all_buttons), 2)
            ]

            final_caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    final_caption = obfuscate_text_for_copyright(final_caption)
            except Exception as e:
                await rep.report(
                    f"Error applying font changer: {e}",
                    "warning"
                )

            final_markup = (
                InlineKeyboardMarkup(grouped_buttons)
                if grouped_buttons else None
            )

            required_low_end = ["480", "720", "1080", "HDRi"]
            missing_low_end = [q for q in required_low_end if q not in file_store_links]
            if missing_low_end:
                raise RuntimeError(
                    f"Low-end episode {name} is not ready to publish; missing qualities: {', '.join(missing_low_end)}"
                )

            target_channel = destination_channel if destination_channel is not None else Var.MAIN_CHANNEL
            final_post = await bot.send_photo(
                target_channel, photo=photo_url,
                caption=final_caption, reply_markup=final_markup
            )
            if not final_post or not getattr(final_post, "id", None):
                raise RuntimeError(f"Main Channel did not confirm post for {name}.")

            if destination_channel is None or str(destination_channel) == str(Var.MAIN_CHANNEL):
                await db.saveAnimeMainPost(ani_id, ep_no, final_post.id)
            for uploaded_qual in file_store_links:
                await db.saveAnime(ani_id, ep_no, uploaded_qual, final_post.id)
            await db.markAnimeResumeComplete(ani_id, ep_no)
            if ani_id and ep_no:
                await db.mark_episode_completed(ani_id, ep_no)

            # Selected-channel episode uploads must never create a sticker
            # message in Main Channel. Keep this workflow sticker-free.


            if stat_msg:
                await stat_msg.delete()
            ani_cache['completed'].add(episode_key)
            ani_cache['ongoing'].discard(episode_key)

    except Exception as e:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(f"Low-end processing error: {str(e)}", "error")

async def fetch_animes():
    """Match today's scheduled anime against SubsPlease RSS titles and upload releases."""
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone
    from bot.modules.daily_schedule import fetch_today_schedule
    import aiohttp

    ist = ZoneInfo("Asia/Kolkata")
    alias_query = """
    query ($search: String!) {
      Page(page: 1, perPage: 6) {
        media(search: $search, type: ANIME, isAdult: false) {
          title { english romaji native }
          synonyms
        }
      }
    }
    """

    def normalize(value):
        value = str(value or "").casefold()
        value = re.sub(r"\b(?:480p|720p|1080p|2160p|web[- ]?dl|bluray|hevc|x26[45])\b", " ", value, flags=re.I)
        value = re.sub(r"\[[^\]]*(?:480p|720p|1080p|2160p|web[- ]?dl|bluray|hevc|x26[45]|subsplease|erai-raws|horriblesubs)[^\]]*\]", " ", value, flags=re.I)
        value = re.sub(r"\([^)]*(?:480p|720p|1080p|2160p|web[- ]?dl|bluray|hevc|x26[45])[^)]*\)", " ", value, flags=re.I)
        # Preserve unknown bracketed text because RSS may bracket the anime title.\n        value = re.sub(r"\([^)]*\)", " ", value)
        value = re.sub(r"\s+-\s+\d{1,3}(?:v\d+)?\s*$", " ", value)
        return re.sub(r"[^a-z0-9]+", "", value)

    def title_matches(rss_title, aliases):
        candidate = normalize(rss_title)
        if not candidate:
            return False
        for alias in aliases:
            target = normalize(alias)
            if len(target) >= 5 and (target in candidate or candidate in target):
                return True
            # Permit a source/release suffix around the actual title, while
            # requiring most meaningful title words to match.
            words = set(re.findall(r"[a-z0-9]+", str(alias).casefold()))
            words = {w for w in words if len(w) > 2 and not w.isdigit()}
            rss_words = set(re.findall(r"[a-z0-9]+", str(rss_title).casefold()))
            rss_words = {w for w in rss_words if len(w) > 2 and not w.isdigit()}
            if words and len(words & rss_words) / len(words) >= 0.60:
                return True
        return False

    def published_today(entry, today):
        stamp = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
        if not stamp:
            return True
        try:
            return datetime(*stamp[:6], tzinfo=timezone.utc).astimezone(ist).date() == today
        except Exception:
            return False

    await rep.report(
        "SCHEDULE-BASED ONGOING SCANNER STARTED. Resolving English, romaji, native and alternate anime titles.",
        "info"
    )
    seen_links = set()
    alias_cache = {}

    while True:
        await asleep(30)
        if (
            ani_cache.get("global_pause")
            or await db.get_mode() != "anime"
            or not ani_cache.get("fetch_animes")
        ):
            continue

        try:
            today = datetime.now(ist).date()
            schedule = await fetch_today_schedule()
            if not schedule:
                await rep.report("Today's schedule is empty; will retry.", "warning")
                continue

            timeout = aiohttp.ClientTimeout(total=18)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                scheduled = []
                for item in schedule:
                    if not isinstance(item, dict) or not item.get("title"):
                        continue
                    original = str(item["title"]).strip()
                    cache_key = original.casefold()
                    if cache_key not in alias_cache:
                        aliases = {original}
                        try:
                            async with session.post(
                                "https://graphql.anilist.co",
                                json={"query": alias_query, "variables": {"search": original}},
                            ) as response:
                                if response.status == 200:
                                    payload = await response.json(content_type=None)
                                    results = payload.get("data", {}).get("Page", {}).get("media", [])
                                    if results:
                                        media = results[0]
                                        titles = media.get("title") or {}
                                        aliases.update(v for v in titles.values() if v)
                                        aliases.update(v for v in (media.get("synonyms") or []) if v)
                        except Exception as alias_error:
                            await rep.report(f"AniList aliases unavailable for {original}: {alias_error}", "warning")
                        alias_cache[cache_key] = aliases
                    scheduled.append((original, alias_cache[cache_key]))

            feed_urls = list(dict.fromkeys(
                url for url in (
                    list(Var.RSS_ITEMS_ANIME or [])
                    + [Var.RSS_LOW_END.get(q) for q in ("480", "720", "1080")]
                ) if url
            ))
            if not feed_urls:
                await rep.report("No anime RSS feeds configured.", "error")
                continue

            feed_results = await asyncio.gather(
                *(getfeeds(url) for url in feed_urls),
                return_exceptions=True
            )
            eligible = []
            total_entries = 0
            for feed_url, entries in zip(feed_urls, feed_results):
                if isinstance(entries, Exception) or not entries:
                    continue
                total_entries += len(entries)
                for entry in entries:
                    link = getattr(entry, "link", None)
                    title = getattr(entry, "title", None)
                    if not link or not title or link in seen_links:
                        continue
                    if not published_today(entry, today):
                        continue
                    for scheduled_title, aliases in scheduled:
                        if title_matches(title, aliases):
                            eligible.append((title, link, feed_url, scheduled_title))
                            break

            # Different quality feeds can expose the same torrent link. Process
            # each link once, and do not mark it seen until the upload succeeds.
            unique = {}
            for row in eligible:
                unique.setdefault(row[1], row)
            eligible = list(unique.values())

            if not eligible:
                await rep.report(
                    f"Schedule scan: {len(scheduled)} scheduled shows, {len(feed_urls)} RSS feeds, "
                    f"{total_entries} RSS entries; no same-day scheduled release matched yet. "
                    f"Known schedule titles: {', '.join(x[0] for x in scheduled[:8])}",
                    "info"
                )
                continue

            await rep.report(
                f"Matched {len(eligible)} scheduled release(s): "
                + "; ".join(f"{row[3]} -> {row[0]}" for row in eligible),
                "success"
            )

            for title, link, feed_url, scheduled_title in eligible:
                try:
                    AUTO_UPLOAD_PRIORITY_PENDING.set()
                    async with AUTO_UPLOAD_PRIORITY_LOCK:
                        result = await get_animes(title, link)
                    if result != "already_published":
                        seen_links.add(link)
                    else:
                        seen_links.add(link)
                    await rep.report(f"Scheduled anime processed: {scheduled_title} | {title}", "success")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await rep.report(
                        f"Scheduled upload failed; will retry: {title}\n{format_exc()}",
                        "error"
                    )
                finally:
                    AUTO_UPLOAD_PRIORITY_PENDING.clear()

        except asyncio.CancelledError:
            raise
        except Exception as error:
            await rep.report(
                f"Scheduled anime scan error; retrying automatically: {error}\n{format_exc()}",
                "error"
            )

async def get_or_create_anime_channel(name: str, aniInfo):
    """Only explicitly selected anime use dedicated channels."""
    try:
        base_name = extract_base_anime_name(name)
        selected = await db.get_selected_anime_channel(normalize_anime_name_for_search(base_name))
        if not selected:
            return Var.MAIN_CHANNEL
        if not await validate_channel_id(selected):
            await rep.report(f"Dedicated channel {selected} for {base_name} is inaccessible; using Main Channel.", "error")
            return Var.MAIN_CHANNEL
        return selected
    except Exception as error:
        await rep.report(f"Dedicated-channel lookup failed for {name}: {error}", "error")
        return Var.MAIN_CHANNEL


async def create_anime_channel(name, aniInfo):
    try:
        if not Var.USER_SESSION:
            await rep.report("User session not available for channel creation", "error")
            return None

        from pyrogram import Client
        user_client = Client("user_session", session_string=Var.USER_SESSION, 
                           api_id=Var.API_ID, api_hash=Var.API_HASH)

        await user_client.start()

        anime_format = (aniInfo.adata.get("format", "") or "").lower()
        is_movie = anime_format == "movie" or (
            hasattr(aniInfo, "is_movie_title") and aniInfo.is_movie_title(name)
        )

        titles = aniInfo.adata.get("title", {})
        channel_name = titles.get("english") or titles.get("romaji") or clean_anime_title(name)
        title = channel_name
        status = aniInfo.adata.get("status", "Unknown").capitalize()
        genres = aniInfo.adata.get("genres", [])[:3]
        genres_formatted = ", ".join(genres) if genres else "Unknown"

        if is_movie:
            description = (
                f"â¢ Movie: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )
        else:
            description = (
                f"â¢ Anime: {title}\n"
                f"â¢ Status: {status}\n"
                f"â¢ Genres: {genres_formatted}\n\n"
                f"Powered by @{Var.BOT_USERNAME}"
            )

        created = await user_client.create_channel(channel_name, description)
        channel_id = created.id

        poster_url = (
            aniInfo.adata.get("coverImage", {}).get("large")
            or aniInfo.adata.get("coverImage", {}).get("medium")
            or aniInfo.adata.get("coverImage", {}).get("small")
        )
        if poster_url:
            filename = f"/tmp/{channel_id}_logo.jpg"
            filename = download_image(poster_url, filename)
            if filename:
                try:
                    await user_client.set_chat_photo(channel_id, photo=filename)
                    print(f"Set channel photo for {channel_id} successfully!")
                    remove(filename)
                except Exception as e:
                    print(f"Failed to set channel photo: {e}")
                    await rep.report(f"Failed to set channel photo: {e}", "warning")
            else:
                print("Image file was not downloaded or does not exist.")
                await rep.report(f"Image file was not downloaded or does not exist.", "warning")
        else:
            print("No poster URL found in AniList data.")
            await rep.report(f"No poster URL found in AniList data.", "warning")

        try:
            from pyrogram.types import ChatPrivileges
            await user_client.promote_chat_member(
                channel_id,
                Var.CREATION_USERNAME,
                privileges=ChatPrivileges(
                    can_post_messages=True,
                    can_edit_messages=True,
                    can_delete_messages=True,
                    can_invite_users=True,
                    can_change_info=True,
                    can_pin_messages=True,
                    can_manage_chat=True,
                    can_manage_video_chats=False,
                    is_anonymous=False
                )
            )
            await rep.report(f"Successfully added and promoted bot in channel", "info")
        except Exception as e:
            await rep.report(f"Failed to add/promote bot in channel: {e}", "error")

        try:
            titles = aniInfo.adata.get("title", {})
            genres = ', '.join(aniInfo.adata.get("genres", [])[:3])
            anime_type = aniInfo.adata.get("format", "Unknown")
            avg_rating = aniInfo.adata.get("averageScore", "N/A")
            status = aniInfo.adata.get("status", "Unknown")
            start_date = aniInfo.adata.get("startDate", {})
            end_date = aniInfo.adata.get("endDate", {})
            runtime = aniInfo.adata.get("duration", "Unknown")
            episodes = aniInfo.adata.get("episodes", "Unknown")
            desc = aniInfo.adata.get("description", "No description available").replace("<br>", "").replace("<i>", "").replace("</i>", "").strip()
            desc = (desc[:200] + "...") if len(desc) > 200 else desc 
            site_url = aniInfo.adata.get("siteUrl", "https://anilist.co")
            media_id = aniInfo.adata.get("id")

            english = titles.get("english", "Unknown")
            romaji = titles.get("romaji", "Unknown")
            display_title = english if english != "Unknown" else romaji

            graphic_poster_url = f"https://img.anili.st/media/{media_id}"

            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.head(graphic_poster_url) as resp:
                    if resp.status != 200:
                        graphic_poster_url = poster_url or "https://imgs.search.brave.com/33oSQBqhBoRB6akjKPud5oFd-PxnMFEm1AjnBTS2ZhE/rs:fit:860:0:0:0/g:ce/aHR0cHM6Ly9pbWcu/ZnJlZXBpay5jb20v/dmVjdG9yLXByZW1p/dW0vaWx1c3RyYWNp/b24tYW5pbWUtZXJy/b3ItNDA0LXBhZ2lu/YS1uby1lbmNvbnRy/YWRhXzE1MDk3Mi0y/NTY0LmpwZw"

            if is_movie:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ Release Date : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Duration : {runtime} minutes</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""
            else:
                caption = f"""<b><blockquote>{display_title}</blockquote>

<blockquote>â£ Genres : {genres}
â£ Type : {anime_type}
â£ Average Rating : {avg_rating}
â£ Status : {status}
â£ First aired : {start_date.get("year", '')}-{start_date.get("month", '')}-{start_date.get("day", '')}
â£ Last aired : {end_date.get("year", '')}-{end_date.get("month", '')}-{end_date.get("day", '')}
â£ Runtime : {runtime} minutes
â£ No of episodes : {episodes}</blockquote>
<blockquote expandable>â£ Synopsis : <i>{desc}</i></blockquote expandable>
</b>"""

            await user_client.send_photo(
                chat_id=channel_id,
                photo=graphic_poster_url,
                caption=caption
            )

            STICKER_ID = "CAACAgUAAxkBAAIERmhruO6M5RpLtZjb2yrvPJxTuw0IAAIzEwACTrLwVSdrhgIPuK1TNgQ"
            await user_client.send_sticker(channel_id, STICKER_ID)

            await rep.report("Intro poster, caption, and sticker sent to channel", "info")

        except Exception as e:
            await rep.report(f"Failed to send intro content: {e}", "warning")

        await db.add_anime_channel_mapping(channel_name, channel_id)

        await asleep(2)

        await user_client.stop()

        await rep.report(f"Successfully created channel: {channel_name} (ID: {channel_id})", "info")
        return channel_id

    except Exception as e:
        await rep.report(f"Failed to create anime channel: {e}", "error")
        return None


async def build_anime_soul_poster(anime_info, anime_name, output_path, episode_label=None):
    """Create the ANIME SOUL split-screen streaming thumbnail (16:9)."""
    import io
    import textwrap
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter, ImageEnhance

    data = anime_info.adata or {}
    titles = data.get("title") or {}
    title = titles.get("english") or titles.get("romaji") or anime_name

    def clean_value(value, default=""):
        if isinstance(value, list):
            value = value[0] if value else default
        return str(value or default).strip()

    season = clean_value(anime_info.pdata.get("anime_season"), "01")
    episode = clean_value(episode_label or anime_info.pdata.get("episode_number"), "01")
    try:
        season = str(int(season))
    except Exception:
        pass
    try:
        episode = str(int(episode)).zfill(2)
    except Exception:
        pass

    cover_data = data.get("coverImage") or {}
    cover_url = cover_data.get("extraLarge") or cover_data.get("large") or data.get("poster_url")
    banner_url = data.get("bannerImage")
    # Prefer the portrait cover for the large poster panel; use banner if cover is unavailable.
    artwork_url = cover_url or banner_url
    if not artwork_url:
        await rep.report("ANIME SOUL split-screen thumbnail skipped: AniList has no artwork URL.", "warning")
        return None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(artwork_url)
            response.raise_for_status()
            art = Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception as exc:
        await rep.report(f"ANIME SOUL artwork download failed: {exc}", "warning")
        return None

    W, H = 1600, 900
    split_x = 690
    art = ImageEnhance.Color(art).enhance(1.08)
    art = ImageEnhance.Contrast(art).enhance(1.04)

    # Left: full-height poster artwork, cropped to fill without stretching.
    left_art = ImageOps.fit(
        art, (split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.45),
    )

    # Right: same anime artwork, blurred and washed out under a translucent light panel.
    right_art = ImageOps.fit(
        art, (W - split_x, H), method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.35),
    ).filter(ImageFilter.GaussianBlur(16))
    right_art = ImageEnhance.Brightness(right_art).enhance(1.8)
    right_art = ImageEnhance.Color(right_art).enhance(0.45)

    canvas = Image.new("RGB", (W, H), (245, 246, 250))
    canvas.paste(left_art, (0, 0))
    canvas.paste(right_art, (split_x, 0))
    overlay = Image.new("RGBA", (W - split_x, H), (250, 250, 253, 222))
    canvas.paste(overlay, (split_x, 0), overlay)
    draw = ImageDraw.Draw(canvas)

    regular_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    bold_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    def font(size, bold=False):
        for path in (bold_candidates if bold else regular_candidates):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def fit_line(text, fnt, max_width):
        text = str(text)
        while text and draw.textbbox((0, 0), text, font=fnt)[2] > max_width:
            text = text[:-2].rstrip() + "…"
        return text

    def wrapped(text, fnt, max_width, max_lines):
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
            if len(lines) >= max_lines:
                break
        if current and len(lines) < max_lines:
            lines.append(current)
        if len(lines) == max_lines and len(words) > sum(len(x.split()) for x in lines):
            lines[-1] = fit_line(lines[-1] + "…", fnt, max_width)
        return lines

    def wrap_complete(text, fnt, max_width):
        """Wrap every title word; never drop the tail of a long AniList title."""
        words = re.sub(r"\\s+", " ", str(text or "")).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=fnt)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines

    # Left poster branding and title overlay.
    left_overlay = Image.new("RGBA", (split_x, H), (0, 0, 0, 0))
    left_draw = ImageDraw.Draw(left_overlay)
    for y in range(560, H):
        alpha = int(205 * ((y - 560) / (H - 560)))
        left_draw.line((0, y, split_x, y), fill=(0, 0, 0, alpha))
    canvas.paste(left_overlay, (0, 0), left_overlay)
    draw = ImageDraw.Draw(canvas)

    draw.rounded_rectangle((34, 30, 330, 90), radius=15, fill=(8, 12, 22), outline=(255,255,255), width=2)
    draw.text((55, 45), "ANIME SOUL", font=font(29, True), fill=(255,255,255))
    draw.text((235, 51), "/ STREAM", font=font(16, True), fill=(235,55,70))
    # Show the complete AniList title across lines instead of truncating it.
    left_title_font = font(38, True)
    left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    if len(left_title_lines) == 3:
        left_title_font = font(32, True)
        left_title_lines = wrap_complete(str(title), left_title_font, split_x - 84)
    left_title_y = 720 if len(left_title_lines) <= 2 else 690
    for title_line in left_title_lines:
        draw.text((42, left_title_y), title_line, font=left_title_font,
                  fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0))
        left_title_y += left_title_font.size + 8
    draw.text((48, 850), f"SEASON {season}   •   EPISODE {episode}",
              font=font(23, True), fill=(255,220,70))

    # Right-side streaming dashboard header and content geometry.
    rx = split_x + 58
    right_w = W - rx - 58
    draw.text((rx, 40), "ANIME SOUL", font=font(25, True), fill=(15,18,27))
    draw.text((rx+210, 45), "/ STREAM", font=font(18), fill=(210,35,55))
    nav = "HOME     ANIME     SCHEDULE     LIBRARY"
    nav_font = font(17, True)
    nav_width = draw.textbbox((0,0), nav, font=nav_font)[2]
    if nav_width < right_w:
        draw.text((W-58-nav_width, 48), nav, font=nav_font, fill=(35,38,48))

    # Wrap the complete AniList title instead of cutting it off with an ellipsis.
    title_font = font(50, True)
    title_lines = wrap_complete(str(title), title_font, right_w)
    if len(title_lines) == 3:
        title_font = font(42, True)
        title_lines = wrap_complete(str(title), title_font, right_w)
    title_y = 150
    for title_line in title_lines:
        draw.text((rx, title_y), title_line, font=title_font, fill=(8,10,18))
        title_y += title_font.size + 10

    rule_y = title_y + 8
    draw.rounded_rectangle((rx, rule_y, rx+right_w, rule_y+8), radius=4, fill=(25,92,190))
    pill_y = rule_y + 32
    draw.rounded_rectangle((rx, pill_y, rx+195, pill_y+56), radius=26, fill=(20,91,190))
    draw.text((rx+25, pill_y+13), f"SEASON {season}", font=font(23, True), fill=(255,255,255))
    draw.rounded_rectangle((rx+215, pill_y, rx+440, pill_y+56), radius=26, outline=(65,70,85), width=2, fill=(255,255,255))
    draw.text((rx+238, pill_y+13), f"EPISODE {episode}", font=font(23, True), fill=(18,20,28))
    draw.rounded_rectangle((rx+right_w-112, pill_y, rx+right_w, pill_y+56), radius=12, outline=(20,24,35), width=2)
    draw.text((rx+right_w-92, pill_y+13), "HD", font=font(25, True), fill=(18,20,28))

    # Place the AniList synopsis below the variable-height title block.
    synopsis = re.sub(r"<[^>]+>", " ", str(data.get("description") or "Discover the story in this anime."))
    synopsis = re.sub(r"\\s+", " ", synopsis).strip()
    syn_font = font(19)
    sy = pill_y + 82
    synopsis_lines = max(2, min(6, (735 - sy) // 29))
    for line in wrapped(synopsis, syn_font, right_w, synopsis_lines):
        draw.text((rx, sy), line, font=syn_font, fill=(35,38,48))
        sy += 29

    # Watch button.
    button_y = min(650, max(sy + 18, pill_y + 145))
    draw.rounded_rectangle((rx, button_y, rx+310, button_y+78), radius=38, fill=(12,14,20))
    draw.polygon([(rx+38,button_y+24),(rx+38,button_y+54),(rx+64,button_y+39)], fill=(255,255,255))
    draw.text((rx+83, button_y+22), "WATCH NOW", font=font(25, True), fill=(255,255,255))
    draw.ellipse((rx+340, button_y+2, rx+416, button_y+78), outline=(75,80,92), width=2)
    draw.text((rx+363, button_y+19), "☆", font=font(38), fill=(20,24,34))

    # Metadata row at the bottom.
    line_y = 770
    draw.line((rx, line_y, W-58, line_y), fill=(130,135,145), width=2)
    studio_nodes = (data.get("studios") or {}).get("nodes") or []
    studio = studio_nodes[0].get("name") if studio_nodes else "Not listed"
    year = (data.get("startDate") or {}).get("year") or data.get("seasonYear") or "—"
    genres = ", ".join((data.get("genres") or [])[:2]) or "Anime"
    status = str(data.get("status") or "—").replace("_", " ").title()
    metadata = [
        ("YEAR", str(year)),
        ("STUDIO", str(studio)),
        ("GENRES", genres),
        ("STATUS", status),
    ]
    col_w = right_w // 4
    for i, (label, value) in enumerate(metadata):
        x = rx + i * col_w
        draw.text((x, 795), label, font=font(14, True), fill=(100,105,115))
        draw.text((x, 823), fit_line(value, font(17, True), col_w-14), font=font(17, True), fill=(15,18,27))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    canvas.save(output_path, "JPEG", quality=94, optimize=True, progressive=True)
    return output_path


async def get_animes(name, torrent, force=False, destination_channel=None, manual_search=False):
    mode = await db.get_mode()
    if mode != "anime":
        await rep.report(f"Skipping {name} because mode is not anime (current mode: {mode})", "info")
        return

    try:
        cleaned_name = clean_anime_title(name)
        base_name = extract_base_anime_name(cleaned_name)
        normalized_name = normalize_anime_name_for_search(base_name)
        
        gdrive_status = await db.get_gdrive_upload()
        if gdrive_status == "on":
            print(f"Checking GDrive mapping for: {normalized_name}")
            await asleep(1.5)
            possible_names = await get_all_possible_anime_names(normalized_name)
            
            for check_name in possible_names:
                for quality in Var.QUALS:
                    gdrive_link = await db.get_gdrive_mapping(check_name, quality)
                    if gdrive_link:
                        await rep.report(f"Found GDrive mapping for {check_name} - {quality}p", "info")
                        await db.saveAnime(torrent, "1", quality, None, gdrive_link)
                        break
                    
        upload_mode = await db.get_upload_mode()
        print(f"get_animes: upload_mode={upload_mode}")
        # Manual /search provides one exact selected torrent. The low-end auto mode
        # ignores that torrent and substitutes configured RSS_LOW_END feeds, so use
        # the standard selected-torrent pipeline for manual searches.
        if upload_mode == "low_end" and not manual_search:
            print(f"get_animes: calling process_low_end for {name}")
            await process_low_end(name, torrent, force=force, destination_channel=destination_channel)
            return
    except Exception as e:
        await rep.report(f"Error checking upload mode: {str(e)}", "error")
        return

    if not await is_valid_torrent_url(torrent):
        await rep.report(f"Invalid or non-torrent URL for {name}: {torrent}", "error")
        return
    
    try:
        aniInfo = TextEditor(name)
        await aniInfo.load_info()
        ani_id, ep_no = aniInfo.adata.get('id'), aniInfo.pdata.get("episode_number")
        if isinstance(ep_no, list):
            ep_no = ep_no[0] if ep_no else None
        ep_no = str(ep_no) if ep_no else None

        # Cache state per anime episode, not per series. A series can have
        # many episodes; using ani_id alone incorrectly blocks later episodes.
        episode_key = f"{ani_id}:{ep_no or 'unknown'}"
        encoding_enabled = await db.get_encoding()
        if not force and episode_key in ani_cache['completed']:
            return
        if not force and episode_key in ani_cache['ongoing']:
            return
        ani_cache['ongoing'].add(episode_key)

        # If the Main Channel post already exists, never publish it twice.
        target_channel = int(destination_channel) if destination_channel is not None else int(Var.MAIN_CHANNEL)
        existing_target_post = (
            await db.getAnimeMainPost(ani_id, ep_no)
            if target_channel == int(Var.MAIN_CHANNEL)
            else await db.getAnimeDestinationPost(ani_id, ep_no, target_channel)
        )
        if existing_target_post:
            ani_cache['ongoing'].discard(episode_key)
            return "already_published"

        # Keep the torrent source in MongoDB so a restart can rediscover this
        # episode even after it falls out of the RSS feed window.
        await db.saveAnimeResume(ani_id, ep_no, name, torrent)
        
        if force or not existing_target_post:
            
            if "[Batch]" in name or "(Batch)" in name:
                await rep.report(f"Batch torrent detected; processing 720p batch: {name}", "info")
                batch_audio = "Dual" if re.search(r"\bdual\b", name, re.IGNORECASE) else ("Dub" if re.search(r"\b(dub|english)\b", name, re.IGNORECASE) else "Sub")
                await process_batch_anime(name, torrent, batch_audio)
                if 'ani_id' in locals() and 'ep_no' in locals():
                    await db.markAnimeResumeComplete(ani_id, ep_no)
                    ani_cache['completed'].add(episode_key)
                    ani_cache['ongoing'].discard(episode_key)
                return
            
            await rep.report(f"New Anime Torrent Found!\n{name}", "info")
            cleaned_name = clean_anime_title(name)
            channel_to_use = target_channel if destination_channel is not None else await get_or_create_anime_channel(name, aniInfo)
            
            channel_valid = False
            for attempt in range(3):
                if await validate_channel_id(channel_to_use):
                    channel_valid = True
                    break
                await asyncio.sleep(2)
            
            if not channel_valid:
                await rep.report(f"Final channel {channel_to_use} is invalid after retries, using main channel", "error")
                channel_to_use = Var.MAIN_CHANNEL
            
            post_msg = None
            photo_path = None
            all_banners = await db.list_anime_banners()
            banner_url = None
            
            for banner_key, url in all_banners:
                if banner_key.lower() in name.lower():
                    banner_url = url
                    break
            
            if banner_url:
                photo_url = banner_url
            elif hasattr(Var, 'ANIME') and Var.ANIME in name:
                photo_url = Var.CUSTOM_BANNER
            else:
                photo_url = aniInfo.adata.get("poster_url", "https://i.ibb.co/NdG0PXdx/not-found.png")
            
            caption = await aniInfo.get_caption()
            try:
                fontchanger_status = await db.get_fontchanger()
                if fontchanger_status == "on":
                    caption = obfuscate_text_for_copyright(caption)
            except Exception as e:
                await rep.report(f"Error applying font changer: {e}", "warning")
            
            try:
                poster_path = f"thumbs/anime_soul_{aniInfo.adata.get('id') or hashlib.md5(cleaned_name.encode()).hexdigest()}.jpg"
                photo_path = await build_anime_soul_poster(aniInfo, cleaned_name, poster_path)
            except Exception as poster_error:
                await rep.report(f"ANIME SOUL poster builder failed; using original artwork: {poster_error}", "warning")
                photo_path = None
            post_msg = None
            stat_msg = await bot.send_message(chat_id=Var.LOG_CHANNEL, text=f"<b>Processing:</b> {name}\nDownloading...")
            
            dl_path = await download_torrent(torrent, name)
            if not dl_path or not ospath.exists(dl_path):
                await rep.report(f"File Download Incomplete for {name}, Try Again", "error")
                await stat_msg.delete()
                return
            
            post_id = int(datetime.datetime.now().timestamp() * 1000000)
            ffEvent = Event()
            ff_queued[post_id] = ffEvent
            
            if ffLock.locked():
                await editMessage(stat_msg, f"<blockquote>â£ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n<pre><i>Queued to Encode...</i></pre>")
                await rep.report("Added Task to Queue...", "info")
            
            await ffQueue.put(post_id)
            await ffEvent.wait()
            await ffLock.acquire()
            try:
                btns = []
                uploaded_qualities = []
                required_qualities = list(Var.QUALS) if encoding_enabled else ["HDRi"]
                saved_file_ids = await db.getAnimeFileIds(ani_id, ep_no)

                async def add_quality_button(qual, msg_id):
                    link = f"https://telegram.me/{Var.BOT_USERNAME}?start={{}}".format(
                        await encode('get-' + str(int(msg_id) * abs(Var.FILE_STORE)))
                    )
                    label = RAW_BTN if qual == "HDRi" else btn_formatter.get(qual, qual + "p")
                    button = InlineKeyboardButton(label, url=link)
                    if btns and len(btns[-1]) == 1:
                        btns[-1].append(button)
                    else:
                        btns.append([button])

                for qual in required_qualities:
                    # Reuse a quality that was already uploaded before the last
                    # crash. Verify the message still exists in FILE_STORE.
                    saved_msg_id = saved_file_ids.get(qual)
                    if saved_msg_id:
                        try:
                            stored_msg = await bot.get_messages(
                                Var.FILE_STORE, message_ids=int(saved_msg_id)
                            )
                            if stored_msg and not getattr(stored_msg, "empty", False):
                                await add_quality_button(qual, saved_msg_id)
                                uploaded_qualities.append(qual)
                                await rep.report(
                                    f"Resume: reusing existing {qual} upload for {name}; skipping encode.",
                                    "info"
                                )
                                continue
                        except Exception as verify_error:
                            await rep.report(
                                f"Saved {qual} message {saved_msg_id} is unavailable; re-uploading this quality: {verify_error}",
                                "warning"
                            )

                    filename = await aniInfo.get_upname(qual)
                    anime_format = aniInfo.adata.get("format", "").lower()
                    is_movie = anime_format == "movie"
                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{name}</i></b></blockquote>\n\n"
                        f"<pre><i>Encoding {qual}...</i></pre>"
                    )
                    await rep.report(f"Starting Encode for {qual}...", "info")

                    out_path = await FFEncoder(
                        stat_msg, dl_path, filename, qual, is_movie=is_movie
                    ).start_encode()
                    if not out_path or not ospath.exists(out_path):
                        raise RuntimeError(
                            f"{qual} encoding failed for {name}; earlier uploaded qualities are saved and will be reused on retry."
                        )

                    await editMessage(
                        stat_msg,
                        f"<blockquote>‣ <b>Anime Name :</b> <b><i>{filename}</i></b></blockquote>\n\n"
                        f"<pre><i>Uploading {qual}...</i></pre>"
                    )
                    msg = await TgUploader(stat_msg).upload(out_path, qual)
                    if not msg or not getattr(msg, "id", None):
                        raise RuntimeError(f"Telegram did not confirm {qual} upload for {name}.")

                    # Persist immediately after Telegram confirms the upload.
                    await db.saveAnimeFileId(ani_id, ep_no, qual, msg.id)
                    await db.saveAnime(ani_id, ep_no, qual, None)
                    saved_file_ids[qual] = msg.id
                    await add_quality_button(qual, msg.id)
                    uploaded_qualities.append(qual)
                    await rep.report(f"Successfully uploaded and checkpointed {qual} for {name}.", "info")
                    bot_loop.create_task(extra_utils(msg.id, out_path))

            finally:
                ffLock.release()
            
            await stat_msg.delete()
            if btns:
                markup = InlineKeyboardMarkup(btns)
                # Telegram may temporarily rate-limit the Main Channel post.
                # Retry the publish step without re-uploading the already stored files.
                last_publish_error = None
                for publish_attempt in range(5):
                    try:
                        if destination_channel is not None:
                            if photo_path and ospath.exists(photo_path):
                                with open(photo_path, 'rb') as photo_file:
                                    post_msg = await bot.send_photo(destination_channel, photo=photo_file, caption=caption, reply_markup=markup)
                            else:
                                post_msg = await bot.send_photo(destination_channel, photo=photo_url, caption=caption, reply_markup=markup)
                        elif channel_to_use != Var.MAIN_CHANNEL:
                            if photo_path and ospath.exists(photo_path):
                                with open(photo_path, 'rb') as photo_file:
                                    episode_post = await bot.send_photo(channel_to_use, photo=photo_file, caption=caption, reply_markup=markup)
                            else:
                                episode_post = await bot.send_photo(channel_to_use, photo=photo_url, caption=caption, reply_markup=markup)
                            announcement_markup = await build_anime_watch_markup(channel_to_use)
                            if photo_path and ospath.exists(photo_path):
                                with open(photo_path, 'rb') as photo_file:
                                    post_msg = await bot.send_photo(Var.MAIN_CHANNEL, photo=photo_file, caption=caption, reply_markup=announcement_markup)
                            else:
                                post_msg = await bot.send_photo(Var.MAIN_CHANNEL, photo=photo_url, caption=caption, reply_markup=announcement_markup)
                            await db.saveAnimeDedicatedPost(ani_id, ep_no, episode_post.id)
                        elif photo_path and ospath.exists(photo_path):
                            with open(photo_path, 'rb') as photo_file:
                                post_msg = await bot.send_photo(Var.MAIN_CHANNEL, photo=photo_file, caption=caption, reply_markup=markup)
                        else:
                            post_msg = await bot.send_photo(Var.MAIN_CHANNEL, photo=photo_url, caption=caption, reply_markup=markup)
                        last_publish_error = None
                        break
                    except FloodWait as flood:
                        wait_seconds = max(1, int(flood.value) + 1)
                        await rep.report(
                            f"Main Channel FLOOD_WAIT: retrying publish in {wait_seconds}s "
                            f"(attempt {publish_attempt + 1}/5).", "warning"
                        )
                        await asyncio.sleep(wait_seconds)
                        last_publish_error = flood
                    except Exception as publish_error:
                        last_publish_error = publish_error
                        break

                if last_publish_error is not None or post_msg is None:
                    raise RuntimeError(
                        f"Failed to publish completed episode to Main Channel: {last_publish_error}"
                    )

                # Persist the Main Channel post ID immediately after send succeeds.
                if destination_channel is None or str(destination_channel) == str(Var.MAIN_CHANNEL):
                    await db.saveAnimeMainPost(ani_id, ep_no, post_msg.id)
                for uploaded_qual in uploaded_qualities:
                    await db.saveAnime(ani_id, ep_no, uploaded_qual, post_msg.id)

                # Send stickers only after the episode post is confirmed.
                sticker_targets = []
                if destination_channel is not None:
                    sticker_targets = [] if manual_search else [int(destination_channel)]
                elif channel_to_use != int(Var.MAIN_CHANNEL):
                    sticker_targets = [int(channel_to_use), int(Var.MAIN_CHANNEL)]
                else:
                    sticker_targets = [int(Var.MAIN_CHANNEL)]

                for sticker_target in dict.fromkeys(sticker_targets):
                    await asleep(max(0, getattr(Var, "STICKER_INTERVAL", 1)))
                    await send_sticker_to_channel(
                        sticker_target, getattr(Var, "STICKER_ID", None)
                    )

                await db.markAnimeResumeComplete(ani_id, ep_no)

                # Mark complete only after the Main Channel post exists.
                ani_cache['completed'].add(episode_key)
                ani_cache['ongoing'].discard(episode_key)
            else:
                await rep.report(f"No qualities uploaded for {name}; no Main Channel post sent", "warning")
                raise RuntimeError(f"No qualities uploaded for {name}; episode remains retryable")

            if encoding_enabled:
                await aioremove(dl_path)
        else:
            # Existing Main Channel post is already recorded.
            await db.markAnimeResumeComplete(ani_id, ep_no)
            ani_cache['completed'].add(episode_key)
        return "uploaded"
    except Exception as error:
        if 'episode_key' in locals():
            ani_cache['ongoing'].discard(episode_key)
        await rep.report(format_exc(), "error")
        # Propagate the failure to fetch_animes so it does not mark this
        # episode as handled and advance to the next RSS item.
        raise

async def extra_utils(msg_id, out_path):
    try:
        msg = await bot.get_messages(Var.FILE_STORE, message_ids=msg_id)

        if hasattr(Var, 'BACKUP_FILE_STORE') and Var.BACKUP_FILE_STORE != 0:
            for chat_id in str(Var.BACKUP_FILE_STORE).split():
                try:
                    await msg.copy(int(chat_id))
                except Exception as e:
                    await rep.report(f"Failed to backup to {chat_id}: {e}", "error")
    except Exception as e:
        await rep.report(f"Error in extra_utils: {e}", "error")

def safe_dirname(name):
    return re.sub(r'[\\/*?:"<>|\'\n\r\t]', '_', name)

async def download_torrent(torrent_url, name=""):
    try:
        unique_dir = generate_unique_dir(name, torrent_url)
        
        if os.path.exists(unique_dir):
            shutil.rmtree(unique_dir)
        os.makedirs(unique_dir, exist_ok=True)
        
        downloader = TorDownloader(unique_dir)
        dl_path = await downloader.download(torrent_url)
        
        if not dl_path or not os.path.exists(dl_path):
            await rep.report(f"Failed to download torrent: {torrent_url}", "error")
            return None
        
        print(f"Download directory contents: {os.listdir(unique_dir)}")
        print(f"Successfully downloaded to: {dl_path}")
        return dl_path
    except Exception as e:
        await rep.report(f"Download error: {str(e)}", "error")
        return None

async def upload_file(file_path, quality):
    try:
        uploader = TgUploader() 
        msg = await uploader.upload(file_path, quality)
        return msg
    except Exception as e:
        await rep.report(f"Upload error: {str(e)}", "error")
        return None
