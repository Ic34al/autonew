from multiprocessing import cpu_count
from concurrent.futures import ThreadPoolExecutor
from functools import partial, wraps
from json import loads as jloads
from re import findall
from math import floor
from os import path as ospath
from time import time, sleep
from traceback import format_exc
from asyncio import sleep as asleep, create_subprocess_shell
from asyncio.subprocess import PIPE
from base64 import urlsafe_b64encode, urlsafe_b64decode
import datetime
from aiohttp import ClientSession
from aiofiles import open as aiopen
from aioshutil import rmtree as aiormtree
from feedparser import parse as feedparse
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import InlineKeyboardButton
from pyrogram.errors import MessageNotModified, FloodWait, UserNotParticipant, ReplyMarkupInvalid, MessageIdInvalid
from bot.core.database import db
from bot import bot, bot_loop, LOGS, Var
from .reporter import rep

def handle_logs(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception:
            await rep.report(format_exc(), "error")
    return wrapper
    
async def sync_to_async(func, *args, wait=True, **kwargs):
    pfunc = partial(func, *args, **kwargs)
    future = bot_loop.run_in_executor(ThreadPoolExecutor(max_workers=cpu_count() * 125), pfunc)
    return await future if wait else future
    
def new_task(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return bot_loop.create_task(func(*args, **kwargs))
    return wrapper

async def getfeed(link, index=0):
    try:
        feed = await sync_to_async(feedparse, link)
        return feed.entries[index]
    except IndexError:
        return None
    except Exception as e:
        LOGS.error(format_exc())
        return None

@handle_logs
async def aio_urldownload(link):
    async with ClientSession() as sess:
        async with sess.get(link) as data:
            image = await data.read()
    path = f"thumbs/{link.split('/')[-1]}"
    if not path.endswith((".jpg" or ".png")):
        path += ".jpg"
    async with aiopen(path, "wb") as f:
        await f.write(image)
    return path

@handle_logs
async def get_telegraph(out):
    client = TelegraphPoster(use_api=True)
    client.create_api_token("Mediainfo")
    uname = Var.BRAND_UNAME.lstrip('@')
    page = client.post(
        title="Mediainfo",
        author=uname,
        author_url=f"https://t.me/{uname}",
        text=f"""<pre>
{out}
</pre>
""",
        )
    return page.get("url")
    
async def sendMessage(chat, text, buttons=None, get_error=False, **kwargs):
    try:
        if isinstance(chat, int):
            return await bot.send_message(
                chat_id=chat,
                text=text,
                disable_web_page_preview=True,
                disable_notification=False,
                reply_markup=buttons,
                **kwargs
            )
        else:
            return await chat.reply(
                text=text,
                quote=True,
                disable_web_page_preview=True,
                disable_notification=False,
                reply_markup=buttons,
                **kwargs
            )
    except FloodWait as f:
        await rep.report(f, "warning")
        await asleep(f.value * 1.2)
        return await sendMessage(chat, text, buttons, get_error, **kwargs)
    except ReplyMarkupInvalid:
        return await sendMessage(chat, text, None, get_error, **kwargs)
    except Exception as e:
        await rep.report(format_exc(), "error")
        if get_error:
            raise e
        return str(e)
        
async def editMessage(msg, text, buttons=None, get_error=False, **kwargs):
    try:
        if not msg:
            return None
        edited = await msg.edit_text(text=text, disable_web_page_preview=True,
                                     reply_markup=buttons, **kwargs)

        try:
            chat_id = None
            msg_id = None
            if hasattr(msg, 'chat') and getattr(msg.chat, 'id', None) is not None:
                chat_id = msg.chat.id
            elif getattr(msg, 'chat_id', None):
                chat_id = msg.chat_id
            if getattr(msg, 'message_id', None):
                msg_id = msg.message_id
            elif getattr(msg, 'id', None):
                msg_id = msg.id

            if chat_id and msg_id:
                mapping = await db.get_backup_mapping(int(chat_id), int(msg_id))
                if mapping:
                    backup_chat = mapping.get('backup_chat')
                    backup_msg_id = mapping.get('backup_msg_id')
                    try:
                        await bot.edit_message_text(
                            chat_id=int(backup_chat),
                            message_id=int(backup_msg_id),
                            text=text,
                            disable_web_page_preview=True,
                            reply_markup=buttons
                        )
                    except Exception:
                        pass
        except Exception:
            pass

        return edited
    except FloodWait as f:
        await rep.report(f, "warning")
        sleep(f.value * 1.2)
        return await editMessage(msg, text, buttons, get_error, **kwargs)
    except ReplyMarkupInvalid:
        return await editMessage(msg, text, None, get_error, **kwargs)
    except (MessageNotModified, MessageIdInvalid):
        pass
    except Exception as e:
        await rep.report(format_exc(), "error")
        if get_error:
            raise e
        return str(e)

async def encode(string):
    return (urlsafe_b64encode(string.encode("ascii")).decode("ascii")).strip("=")

async def decode(b64_str):
    return urlsafe_b64decode((b64_str.strip("=") + "=" * (-len(b64_str.strip("=")) % 4)).encode("ascii")).decode("ascii")

async def is_fsubbed(uid: int) -> bool:
    if not Var.FSUB_CHATS:
        return True
        
    try:
        for chat_id in Var.FSUB_CHATS:
            try:
                member = await bot.get_chat_member(chat_id, uid)
                if member.status in ["left", "kicked"]:
                    return False
            except UserNotParticipant:
                return False
            except Exception as e:
                await rep.report(f"FSub Check Error: {str(e)}", "warning")
                continue
        return True
    except Exception as e:
        await rep.report(f"FSub Error: {str(e)}", "error")
        return True

async def get_fsubs(uid: int, args=None) -> tuple:
    if not Var.FSUB_CHATS:
        return "", []
        
    txt = "<blockquote><b>Pʟᴇᴀsᴇ Jᴏɪɴ Fᴏʟʟᴏᴡɪɴɢ Cʜᴀɴɴᴇʟs ᴛᴏ Usᴇ ᴛʜɪs Bᴏᴛ!</b></blockquote>\n\n"
    btns = []
    
    try:
        for i, chat_id in enumerate(Var.FSUB_CHATS, 1):
            try:
                chat = await bot.get_chat(chat_id)
                invite_link = chat.invite_link or await bot.export_chat_invite_link(chat_id)
                
                try:
                    member = await bot.get_chat_member(chat_id, uid)
                    status = (
                        "<blockquote>Jᴏɪɴᴇᴅ ✅️</blockquote>"
                        if member.status not in ["left", "kicked"]
                        else "<blockquote>Nᴏᴛ Jᴏɪɴᴇᴅ ❌</blockquote>"
                    )
                except UserNotParticipant:
                    status = "<blockquote>Nᴏᴛ Jᴏɪɴᴇᴅ ❌</blockquote>"
                except Exception:
                    status = "<blockquote>Uɴᴋɴᴏᴡɴ ⚠️</blockquote>"
                    
                txt += f"<blockquote>{i}. Title : {chat.title}\n  Status : {status}</blockquote>\n\n"
                
                if "Nᴏᴛ Jᴏɪɴᴇᴅ" in status:
                    btns.append([InlineKeyboardButton(f"Jᴏɪɴ {chat.title}", url=invite_link)])
                    
            except Exception as e:
                await rep.report(f"Error getting chat {chat_id}: {e}", "warning")
                continue

        arg_val = None
        if args:
            if isinstance(args, (list, tuple)):
                for a in args:
                    if a and not a.startswith("/start"):
                        arg_val = str(a).strip()
                        break
            elif isinstance(args, str):
                parts = args.strip().split(maxsplit=1)
                if len(parts) > 1 and parts[0] == "/start":
                    arg_val = parts[1]
                elif parts[0] != "/start":
                    arg_val = parts[0]

        if arg_val:
            bot_username = (await bot.get_me()).username
            start_link = f"https://t.me/{bot_username}?start={arg_val}"
            btns.append([InlineKeyboardButton("Tʀʏ Aɢᴀɪɴ", url=start_link)])

        return txt, btns
        
    except Exception as e:
        await rep.report(f"FSub Error: {str(e)}", "error")
        return "Force subscribe check failed!", []

async def mediainfo(file, get_json=False, get_duration=False):
    try:
        outformat = "HTML"
        if get_duration or get_json:
            outformat = "JSON"
        process = await create_subprocess_shell(f"mediainfo '''{file}''' --Output={outformat}", stdout=PIPE, stderr=PIPE)
        stdout, _ = await process.communicate()
        if get_duration:
            try:
                return float(jloads(stdout.decode())['media']['track'][0]['Duration'])
            except Exception:
                return 1440
        return await get_telegraph(stdout.decode())
    except Exception as err:
        await rep.report(format_exc(), "error")
        return ""

async def get_messages(client, message_ids):
    messages = []
    total_messages = 0
    while total_messages != len(message_ids):
        temb_ids = message_ids[total_messages:total_messages+200]
        try:
            msgs = await client.get_messages(
                chat_id=Var.FILE_STORE,
                message_ids=temb_ids
            )
        except FloodWait as e:
            await asyncio.sleep(e.x)
            msgs = await client.get_messages(
                chat_id=Var.FILE_STORE,
                message_ids=temb_ids
            )
        except:
            pass
        total_messages += len(temb_ids)
        messages.extend(msgs)
    return messages

async def get_message_id(client, message):
    if message.forward_from_chat:
        if message.forward_from_chat.id == Var.FILE_STORE:
            return message.forward_from_message_id
        else:
            return 0
    elif message.forward_sender_name:
        return 0
    elif message.text:
        pattern = r"https://t.me/(?:c/?)?(.*/\d+)"
        matches = re.match(pattern,message.text)
        if not matches:
            return 0
        channel_id = matches.group(1)
        msg_id = int(matches.group(2))
        if channel_id.isdigit():
            if f"-100{channel_id}" == str(Var.FILE_STORE):
                return msg_id
        else:
            if channel_id == client.db_channel.username:
                return msg_id
    else:
        return 0
        
async def clean_up():
    try:
        (await aiormtree(dirtree) for dirtree in ("downloads", "thumbs", "encode"))
    except Exception as e:
        LOGS.error(str(e))

def convertTime(s: int) -> str:
    m, s = divmod(int(s), 60)
    hr, m = divmod(m, 60)
    days, hr = divmod(hr, 24)
    convertedTime = (f"{int(days)}d, " if days else "") + \
          (f"{int(hr)}h, " if hr else "") + \
          (f"{int(m)}m, " if m else "") + \
          (f"{int(s)}s, " if s else "")
    return convertedTime[:-2]

def convertBytes(sz) -> str:
    if not sz: 
        return ""
    sz = int(sz)
    ind = 0
    Units = {0: '', 1: 'K', 2: 'M', 3: 'G', 4: 'T', 5: 'P'}
    while sz > 2**10:
        sz /= 2**10
        ind += 1
    return f"{round(sz, 2)} {Units[ind]}B"
