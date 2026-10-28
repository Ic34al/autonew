from os import path as ospath, mkdir, system, getenv
from logging import INFO, ERROR, FileHandler, StreamHandler, basicConfig, getLogger
from traceback import format_exc
from asyncio import Queue, Lock
import time
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pyrogram import Client
from pyrogram.enums import ParseMode
from dotenv import load_dotenv
from uvloop import install
from datetime import datetime
import asyncio

class BotStartTime:
    def __init__(self):
        self.start_time = datetime.now()
    
    @property
    def uptime(self):
        return datetime.now() - self.start_time

install()
basicConfig(format="[%(asctime)s] [%(name)s | %(levelname)s] - %(message)s [%(filename)s:%(lineno)d]",
            datefmt="%m/%d/%Y, %H:%M:%S %p",
            handlers=[FileHandler('log.txt'), StreamHandler()],
            level=INFO)

getLogger("pyrogram").setLevel(ERROR)
LOGS = getLogger(__name__)

load_dotenv('config.env')

ani_cache = {
    'fetch_animes': True,
    'fetch_manga': True,
    'global_pause': False,
    'ongoing': set(),
    'completed': set()
}
ffpids_cache = list()

ffLock = Lock()
ffQueue = Queue()
ff_queued = dict()

class Var:
    API_ID, API_HASH, BOT_TOKEN = getenv("API_ID"), getenv("API_HASH"), getenv("BOT_TOKEN")
    MONGO_URI = getenv("MONGO_URI")
    
    if not BOT_TOKEN or not API_HASH or not API_ID or not MONGO_URI:
        LOGS.critical('Important Variables Missing. Fill Up and Retry..!! Exiting Now...')
        exit(1)

    MONGO_NAME = getenv("MONGO_NAME", "AutoAnime")
    USER_SESSION = getenv("USER_SESSION")
    RSS_LOW_END = {
    "480": "https://subsplease.org/rss/?r=sd",
    "720": "https://subsplease.org/rss/?r=720", 
    "1080": "https://subsplease.org/rss/?r=1080"
    }
    RSS_ITEMS_ANIME= getenv("RSS_ITEMS_ANIME", "https://subsplease.org/rss/?r=1080").split()
    RSS_ITEMS_MANGA= getenv("RSS_ITEMS_MANGA", "https://mdrss.tijlvdb.me/feed?q=tl:en").split()
    FSUB_CHATS = list(map(int, getenv('FSUB_CHATS').split()))
    MAIN_CHANNEL = int(getenv("MAIN_CHANNEL"))
    BACKUP_CHANNEL = int(getenv("BACKUP_CHANNEL") or 0)
    LOG_CHANNEL = int(getenv("LOG_CHANNEL") or 0)
    FILE_STORE = int(getenv("FILE_STORE"))
    FILE_STORE_LINK = getenv("FILE_STORE_LINK") or ""
    BACKUP_FILE_STORE = getenv("BACKUP_FILE_STORE") or ""
    ADMINS = list(map(int, getenv("ADMINS", "").split()))
    SEND_STICKER = getenv('SEND_STICKER', 'True').lower() == 'true'
    STICKER_ID = getenv('STICKER_ID', 'CAACAgUAAxkBAAK6F2oMCSFNIBlAsb5RdifwsOBMTUfCAAJxIQACYkSBVsIdBxU9iqTYHgQ')
    STICKER_INTERVAL = int(getenv('STICKER_INTERVAL', 2))

    MAX_RETRIES = int(getenv("MAX_RETRIES", "15"))

    GDRIVE_UPLOAD = getenv("GDRIVE_UPLOAD", "off")
    GDRIVE_CREDENTIALS_FILE = getenv("GDRIVE_CREDENTIALS_FILE", "gdrive_service_account.json")
    GDRIVE_FOLDER_ID = getenv("GDRIVE_FOLDER_ID", "")

    LOW_END_RENAME = True

    BOT_USERNAME = getenv('BOT_USERNAME', "uchiha_Sasuke_itachi_bot")
    CREATION_USERNAME = getenv('CREATION_USERNAME', "OtakuFlix")
    
    SEND_SCHEDULE = getenv("SEND_SCHEDULE", "False").lower() == "true"
    SCHEDULE_TYPE = getenv("SCHEDULE_TYPE", "sub").lower()

    BRAND_UNAME = getenv("BRAND_UNAME", "@OtakuFlix_Net")

    LOW_END_FFMPEG = getenv("LOW_END_FFMPEG", 'ffmpeg -i "{}" -metadata title="{}" -metadata:s:s title="{}" -metadata:s:a title="{}" -metadata:s:v title="{}" -c copy "{}" -y')
    FFCODE_HDRi = getenv("FFCODE_HDRi") or """ffmpeg -i '{}' -progress '{}' -preset superfast -c:v libx264 -s 1920x1080 -pix_fmt yuv420p -crf 30 -c:a libopus -b:a 32k -c:s copy -map 0 -ac 2 -ab 32k -vbr 2 -level 3.1 '{}' -y"""
    FFCODE_1080 = getenv("FFCODE_1080") or """ffmpeg -i '{}' -progress '{}' -preset veryfast -c:v libx264 -s 1920x1080 -pix_fmt yuv420p -crf 30 -c:a libopus -b:a 32k -c:s copy -map 0 -ac 2 -ab 32k -vbr 2 -level 3.1 '{}' -y"""
    FFCODE_720 = getenv("FFCODE_720") or """ffmpeg -i '{}' -progress '{}' -preset veryfast -c:v libx264 -s 1280x720 -pix_fmt yuv420p -crf 30 -c:a libopus -b:a 32k -c:s copy -map 0 -ac 2 -ab 32k -vbr 2 -level 3.1 '{}' -y"""
    FFCODE_480 = getenv("FFCODE_480") or """ffmpeg -i '{}' -progress '{}' -preset veryfast -c:v libx264 -s 854x480 -pix_fmt yuv420p -crf 30 -c:a libopus -b:a 32k -c:s copy -map 0 -ac 2 -ab 32k -vbr 2 -level 3.1 '{}' -y"""
    FFCODE_360 = getenv("FFCODE_360") or """ffmpeg -i '{}' -progress '{}' -preset veryfast -c:v libx264 -s 640x360 -pix_fmt yuv420p -crf 30 -c:a libopus -b:a 32k -c:s copy -map 0 -ac 2 -ab 32k -vbr 2 -level 3.1 '{}' -y"""
    FFCODE_240 = getenv("FFCODE_240") or """ffmpeg -i '{}' -progress '{}' -preset veryfast -c:v libx264 -s 340x240 -pix_fmt yuv420p -crf 30 -c:a libopus -b:a 32k -c:s copy -map 0 -ac 2 -ab 32k -vbr 2 -level 3.1 '{}' -y"""
    FFCODE_144 = getenv("FFCODE_144") or """ffmpeg -i '{}' -progress '{}' -preset veryfast -c:v libx264 -s 256x144 -pix_fmt yuv420p -crf 30 -c:a libopus -b:a 32k -c:s copy -map 0 -ac 2 -ab 32k -vbr 2 -level 3.1 '{}' -y"""
    QUALS = getenv("QUALS", "360 480 720 1080 HDRi").split()
    
    AS_DOC = getenv("AS_DOC", "True").lower() == "true"
    THUMB = getenv("THUMB")
    ANIME = getenv("ANIME", "Is It Wrong to Try to Pick Up Girls in a Dungeon?")
    CUSTOM_BANNER = getenv("CUSTOM_BANNER", "https://i.ibb.co/spp1Yn1S/thumb.jpg") 

    AUTO_DEL = getenv("AUTO_DEL", "True").lower() == "true"
    DEL_TIMER = int(getenv("DEL_TIMER", "1800"))

    START_PHOTO = getenv("START_PHOTO", "https://i.ibb.co/3mYQNCvV/Chat-GPT-Image-May-19-2026-12-43-13-PM.png")
    FORCE_PHOTO = getenv("FORCE_PHOTO", "https://i.pinimg.com/736x/b4/8a/a3/b48aa3b14b2d3063835005272006d49a.jpg")
    SCHEDULE_PHOTO = getenv("SCHEDULE_PHOTO", "https://i.pinimg.com/736x/c3/94/8a/c3948af8def4c98f5c47b95e920611b3.jpg")
    PHOTO_LIST = getenv("PHOTO_LIST", "").split()

    START_MSG = getenv("START_MSG", "<blockquote><b>ʜᴇʏ {first_name}</b>,</blockquote>\n<i><blockquote>ɪ'ᴍ ᴀɴ ᴀᴜᴛᴏ ᴀɴɪᴍᴇ ꜱᴛᴏʀᴇ & ᴇɴᴄᴏᴅɪɴɢ ʙᴏᴛ, ʙᴜɪʟᴛ ᴡɪᴛʜ ʟᴏᴠᴇ.</blockquote></i>\n\n<blockquote>❝ ɴᴏ ᴍᴀᴛᴛᴇʀ ʜᴏᴡ ᴅᴇᴇᴘ ᴛʜᴇ ɴɪɢʜᴛ, ɪᴛ ᴀʟᴡᴀʏꜱ ᴛᴜʀɴꜱ ᴛᴏ ᴅᴀʏ, ᴇᴠᴇɴᴛᴜᴀʟʟʏ. ᴀꜱ ʟᴏɴɢ ᴀꜱ ʏᴏᴜ ᴅᴏɴ’ᴛ ɢɪᴠᴇ ᴜᴘ, ᴛʜᴇʀᴇ ᴡɪʟʟ ᴀʟᴡᴀʏꜱ ʙᴇ ᴀ ᴘᴀᴛʜ ꜰᴏʀᴡᴀʀᴅ. ❞</blockquote>\n<blockquote>― <i>ᴋᴇɴ ᴋᴀɴᴇᴋɪ</i></blockquote>")
    START_BUTTONS = getenv("START_BUTTONS", "ᴍᴀɪɴ-ᴄʜᴀɴɴᴇʟ|https://telegram.me/BotifyX_Pro_Botz sᴜᴘᴘᴏʀᴛ|https://t.me/+ij3pcPOXv2U4MDll")

    HELP_MSG = [
        (
            "<b><blockquote>Core Commands</blockquote>\n"
            "<blockquote>• /start - Start the bot\n"
            "• /help - Show this help message\n"
            "• /status - Check bot status\n"
            "• /settings - Bot settings panel\n"
            "• /setstartpic - Set start picture\n"
            "• /setforcepic - Set force picture\n"
            "• /setschedulepic - Set schedule picture\n"
            "• /setschedule - Set auto-fetch schedule\n"
            "• /stats - Show bot statistics\n"
            "• /broadcast - Broadcast message to all users\n"
            "• /ping - Check bot response time\n"
            "• /pause - Pause auto-fetching\n"
            "• /resume - Resume auto-fetching</b></blockquote>"
        ),
        (
            "<b><blockquote>Manga Management</blockquote>\n"
            "<blockquote>• /setmanga - Map manga to channel\n"
            "• /addspecificmanga - Map specific manga chapters for upload\n"
            "• /delspecificmanga - Remove specific manga upload mapping\n"
            "• /delallspecificmangas - Remove all specific manga upload mappings\n"
            "• /listspecificmangas - List specific manga upload mappings\n"
            "• /delmanga - Remove manga mapping\n"
            "• /delallmanga - Remove all manga mapping\n"
            "• /listmangas - List manga channels\n"
            "• /setmangabanner - Set manga banner\n"
            "• /viewmangabanner - View manga banner\n"
            "• /delmangabanner - Remove manga banner\n"
            "• /delallmangabanner - Remove all manga banner\n"
            "• /listmangabanners - List all manga banners</b></blockquote>"
        ),
        (
            "<b><blockquote>Anime Management</blockquote>\n"
            "<blockquote>• /addanime - Map anime to channel\n"
            "• /delanime - Remove anime mapping\n"
            "• /delallanimes - Remove all anime mapping\n"
            "• /listanimes - List anime channels\n"
            "• /setbanner - Set anime banner\n"
            "• /viewbanner - View anime banner\n"
            "• /delbanner - Remove anime banner\n"
            "• /delallbanners - Remove all anime banner\n"
            "• /listbanners - List anime banners</b></blockquote>"
        ),
        (
            "<b><blockquote>GDrive Management</blockquote>\n"
            "<blockquote>• /addgdrive - Map anime to GDrive folder\n"
            "• /delgdrive - Remove GDrive mapping\n"
            "• /delallgdrive - Remove all GDrive mapping\n"
            "• /listgdrive - List GDrive mappings\n"
            "• /gdrive - Turn on/off GDrive upload</b></blockquote>"
        ),
        (
            "<b><blockquote>Channel Management</blockquote>\n"
            "<blockquote>• /addfsub - Add force sub channel\n"
            "• /delfsub - Remove force sub channel\n"
            "• /listfsubs - List force sub channels</b></blockquote>"
        ),
        (
            "<b><blockquote>Admin Management</blockquote>\n"
            "<blockquote>• /addadmin - Add new admin\n"
            "• /deladmin - Delete any admin\n"
            "• /listadmins - List all admins</b></blockquote>"
        ),
        (
            "<b><blockquote>Task Management</blockquote>\n"
            "<blockquote>• /addtask - Add new anime/manga task\n"
            "• /forcetask - Add new anime/manga task (ignore database)\n"
            "• /addbatch - Add batch task\n"
            "• /tasks - List active tasks\n"
            "• /cleartasks - Clear task lists</b></blockquote>"
        ),
        (
            "<b><blockquote>RSS Management</blockquote>\n"
            "<blockquote>• /addlink - Add RSS link for Anime, Manga, or Low-End\n"
            "• /listlinks - List all RSS links\n"
            "• /dellink - Remove RSS link for Anime or Manga</b></blockquote>"
        ),
        (
            "<b><blockquote>Content Settings</blockquote>\n"
            "<blockquote>• /batchlink - Generate multiple file sharing link\n"
            "• /genlink - Generate single file sharing link\n"
            "• /fontchanger - Toggle font changer\n"
            "• /setthumb - Set global thumbnail\n"
            "• /delthumb - Remove global thumbnail\n"
            "• /viewthumb - View global thumbnail\n"
            "• /setsticker - Set completion sticker\n"
            "• /delsticker - Remove sticker\n"
            "• /viewsticker - View completion sticker\n"
            "• /setdeltimer - Set auto-delete timer\n"
            "• /setdel - Toggle auto-delete feature\n"
            "• /setffmpeg - Set FFMPEG config for specific anime\n"
            "• /delffmpeg - Remove FFMPEG config\n"
            "• /delallffmpeg - Remove all FFMPEG config\n"
            "• /listffmpeg - List all FFMPEG configs\n"
            "• /fontchanger - Toggle font changer</b></blockquote>"
        ),
        (
            "<b><blockquote>System Commands</blockquote>\n"
            "<blockquote>• /shell - Execute shell command\n"
            "• /restart - Restart the bot\n"
            "• /log - Get bot logs\n"
            "• /cleanup - Cleanup bot directories\n"
            "• /api - Change API source</b></blockquote>"
        ),
    ]

    HELP_PAGE_TEXT = getenv("HELP_PAGE_TEXT", HELP_MSG)

if Var.THUMB and not ospath.exists("thumb.jpg"):
    system(f"wget -q {Var.THUMB} -O thumb.jpg")
    LOGS.info("Thumbnail has been Saved!!")
if not ospath.isdir("encode/"):
    mkdir("encode/")
if not ospath.isdir("thumbs/"):
    mkdir("thumbs/")
if not ospath.isdir("downloads/"):
    mkdir("downloads/")

try:
    try:
        bot_loop = asyncio.get_running_loop()
    except RuntimeError:
        bot_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(bot_loop)

    bot = Client(name="AutoAnime", api_id=Var.API_ID, api_hash=Var.API_HASH, bot_token=Var.BOT_TOKEN, plugins=dict(root="bot/modules"), parse_mode=ParseMode.HTML)
    try:
        bot_loop = bot.loop or bot_loop
    except Exception:
        pass
    sch = AsyncIOScheduler(timezone="Asia/Kolkata", event_loop=bot_loop)
    bot.start_time_helper = BotStartTime()
except Exception as ee:
    LOGS.error(str(ee))
    exit(1)