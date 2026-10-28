from time import time, sleep
from traceback import format_exc
from math import floor
from os import path as ospath
from aiofiles.os import remove as aioremove
from pyrogram.errors import FloodWait
import datetime
from bot import bot, Var
from .gdrive import gdrive
from .database import db
from .func_utils import editMessage, sendMessage, convertBytes, convertTime
from .reporter import rep
from .database import db
from .base_clean import extract_base_anime_name
import re

class TgUploader:
    def __init__(self, message):
        self.cancelled = False
        self.message = message
        self.__name = ""
        self.__qual = ""
        self.__client = bot
        self.__start = time()
        self.__updater = time()

    async def upload(self, path, qual):
        self.__name = ospath.basename(path)
        self.__qual = qual
        try:
            if not ospath.exists(path):
                await rep.report(f"File not found for upload: {path}", "error")
                raise FileNotFoundError(f"File not found: {path}")

            gdrive_status = await db.get_gdrive_upload()
            if gdrive_status == "on":
                anime_name = extract_base_anime_name(self.__name)
                gdrive_link = await db.get_gdrive_mapping(anime_name, qual)
                if gdrive_link:
                    folder_id = None
                    import re
                    folder_match = re.search(r"/folders?/([a-zA-Z0-9_-]+)", gdrive_link)
                    if folder_match:
                        folder_id = folder_match.group(1)
                    else:
                        id_match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", gdrive_link)
                        if not id_match:
                            id_match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", gdrive_link)
                        if id_match:
                            folder_id = id_match.group(1)
                    log_msg = (
                        f"<i>Uploading to mapped GDrive</i>\n\n"
                        f"    • File: {self.__name}\n"
                        f"    • Quality: {qual}p\n"
                        f"    • Folder ID: {folder_id or 'N/A'}"
                    )
                    await bot.send_message(Var.LOG_CHANNEL, log_msg)
                    file_id, shared_link = await gdrive.upload_file(path, folder_id)
                    if file_id and shared_link:
                        success_msg = (
                            f"Uploaded to mapped GDrive folder\n\n"
                            f"    • File : {self.__name}\n"
                            f"    • Quality : {qual}p"
                        )
                        await bot.send_message(Var.LOG_CHANNEL, success_msg)
                    else:
                        await rep.report(f"GDrive upload failed for {self.__name}, falling back to Telegram", "warning")

            log_msg = f"Starting upload for:\n{self.__name}"
            await bot.send_message(Var.LOG_CHANNEL, log_msg)

            if ospath.getsize(path) == 0:
                await rep.report(f"File is empty: {path}", "error")
                raise ValueError(f"File is empty: {path}")
            thumb = "thumb.jpg" if ospath.exists("thumb.jpg") else None
            try:
                if Var.AS_DOC:
                    msg = await self.__client.send_document(
                        chat_id=Var.FILE_STORE,
                        document=path,
                        thumb=thumb,
                        caption=f"<b><i>{self.__name}</i></b>",
                        force_document=True,
                        progress=self.progress_status
                    )
                else:
                    msg = await self.__client.send_video(
                        chat_id=Var.FILE_STORE,
                        video=path,
                        thumb=thumb,
                        caption=f"<b><i>{self.__name}</i></b>",
                        progress=self.progress_status
                    )
            
                return msg

            except FloodWait as e:
                await asleep(e.value * 1.5)
                return await self.upload(path, qual)

        except Exception as e:
            await rep.report(f"Upload failed: {str(e)}", "error")
            raise e
        
        finally:
            try:
                if ospath.exists(path):
                    await aioremove(path)
            except Exception as e:
                await rep.report(f"Failed to remove file after upload: {str(e)}", "warning")

    async def progress_status(self, current, total):
        if self.cancelled:
            self.__client.stop_transmission()
        now = time()
        diff = now - self.__start
        if (now - self.__updater) >= 7 or current == total:
            self.__updater = now
            percent = round(current / total * 100, 2)
            speed = current / diff 
            eta = round((total - current) / speed)
            bar = floor(percent/8)*"●" + (12 - floor(percent/8))*"○"

            try:
                encoding = await db.get_encoding()
                if not encoding:
                    files_encoded = "1"
                    total_files = "1"
                else:
                    try:
                        files_encoded = str(Var.QUALS.index(self.__qual) + 1)
                    except Exception:
                        files_encoded = "1"
                    total_files = str(len(Var.QUALS) if Var.QUALS else 1)
            except Exception:
                files_encoded = "1"
                total_files = "1"

            progress_str = f"""<blockquote>‣ <b>Anime Name :</b> <b><i>{self.__name}</i></b></blockquote>

<blockquote>‣ <b>Status :</b> <i>Uploading</i>
    <code>[{bar}]</code> {percent}%</blockquote>
    
<blockquote>    ‣ <b>Size :</b> {convertBytes(current)} out of ~ {convertBytes(total)}
    ‣ <b>Speed :</b> {convertBytes(speed)}/s
    ‣ <b>Time Took :</b> {convertTime(diff)}
    ‣ <b>Time Left :</b> {convertTime(eta)}</blockquote>

<blockquote>‣ <b>File(s) Encoded:</b> <code>{files_encoded} / {total_files}</code></blockquote>"""
            
            await editMessage(self.message, progress_str)
