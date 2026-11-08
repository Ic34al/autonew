import asyncio
from pyrogram.errors import FloodWait
from bot import Var, LOGS, bot

class Reporter:
    def __init__(self, client, chat_id, log):
        self.__client = client
        self.__cid = chat_id
        self.__logger = log

    async def report(self, msg, log_type, log=True):
        txt = [f"[{log_type.upper()}] {msg}", log_type.lower()]
        if txt[1] == "error":
            self.__logger.error(txt[0])
        elif txt[1] == "warning":
            self.__logger.warning(txt[0])
        elif txt[1] == "critical":
            self.__logger.critical(txt[0])
        else:
            self.__logger.info(txt[0])
        # Keep the Telegram Anime Logs channel actionable: routine INFO and
        # SUCCESS events stay in the Heroku/process logger. Only warnings and
        # failures are forwarded to Telegram, avoiding hundreds of messages
        # during a multi-release scan or a multi-quality upload.
        if log and self.__cid != 0 and txt[1] in ("warning", "error", "critical"):
            # Reporter is best-effort: logging must never freeze the asyncio
            # worker or interrupt encoding/uploading.
            for attempt in range(2):
                try:
                    await self.__client.send_message(self.__cid, f"{txt[0][:4096]}")
                    break
                except FloodWait as f:
                    self.__logger.warning(str(f))
                    if attempt == 0:
                        await asyncio.sleep(max(1, int(f.value) + 1))
                    else:
                        self.__logger.warning("Log message skipped after repeated FloodWait.")
                except Exception as err:
                    self.__logger.error(str(err))
                    break

rep = Reporter(bot, Var.LOG_CHANNEL, LOGS)
