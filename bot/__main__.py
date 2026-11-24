import asyncio
from asyncio import (
    create_task, create_subprocess_exec, create_subprocess_shell,
    run as asyrun, gather, sleep as asleep, get_event_loop
)
from aiofiles import open as aiopen
from pyrogram import idle
from pyrogram.filters import command, user
from os import path as ospath, execl, kill, environ
from sys import executable
from signal import SIGKILL
from bot import bot, Var, bot_loop, sch, LOGS, ffQueue, ffLock, ffpids_cache, ff_queued
from bot.core.func_utils import clean_up, clean_old_files, new_task, editMessage
from bot.modules.up_posts import upcoming_animes as legacy_upcoming_animes
from bot.modules.daily_schedule import upcoming_animes
from bot.web import start_server
from bot.core.database import db
from bot.core.auto_animes import fetch_animes
@bot.on_message(command('restart') & user(Var.ADMINS))
@new_task
async def restart(client, message):
    rmessage = await message.reply('<i>Restarting...</i>')
    if sch.running:
        sch.shutdown(wait=False)
    await clean_up()
    if len(ffpids_cache) != 0: 
        for pid in ffpids_cache:
            try:
                LOGS.info(f"Process ID : {pid}")
                kill(pid, SIGKILL)
            except (OSError, ProcessLookupError):
                LOGS.error("Killing Process Failed !!")
                continue
    async with aiopen(".restartmsg", "w") as f:
        await f.write(f"{rmessage.chat.id}\n{rmessage.id}\n")
    execl(executable, executable, "-m", "bot")

async def restart():
    if ospath.isfile(".restartmsg"):
        async with aiopen(".restartmsg") as f:
            chat_id, msg_id = map(int, (await f.read()).split())
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="<i>Restarted !</i>")
        except Exception as e:
            LOGS.error(e)

async def queue_loop():
    LOGS.info("Queue Loop Started !!")
    while True:
        if not ffQueue.empty():
            post_id = await ffQueue.get()
            await asleep(1.5)
            ff_queued[post_id].set()
            await asleep(1.5)
            async with ffLock:
                ffQueue.task_done()
        await asleep(10)

async def load_settings():
    LOGS.info("Loading settings...")
    Var.AUTO_DEL = await db.get_auto_del()
    Var.DEL_TIMER = await db.get_del_timer()
    sticker_id = await db.get_sticker_id()
    if sticker_id:
        Var.STICKER_ID = sticker_id
    try:
        start_photo = await db.get_start_photo()
        if start_photo:
            Var.START_PHOTO = start_photo
    except Exception:
        pass
    try:
        schedule_photo = await db.get_schedule_photo()
        if schedule_photo:
            Var.SCHEDULE_PHOTO = schedule_photo
    except Exception:
        pass
    try:
        force_photo = await db.get_force_photo()
        if force_photo:
            Var.FORCE_PHOTO = force_photo
    except Exception:
        pass
    try:
        db_schedule_setting = await db.get_send_schedule()

        env_schedule_setting = (
            environ.get("SEND_SCHEDULE", "False").lower() == "true"
        )

        if "SEND_SCHEDULE" in environ:
            Var.SEND_SCHEDULE = env_schedule_setting
        elif db_schedule_setting is not None:
            Var.SEND_SCHEDULE = db_schedule_setting
        else:
            Var.SEND_SCHEDULE = False

    except Exception as e:
        LOGS.warning(
            f"Could not load schedule setting from database: {e}"
        )
        Var.SEND_SCHEDULE = (
            environ.get("SEND_SCHEDULE", "False").lower() == "true"
        )
    Var.LOW_END_RENAME = await db.get_low_end_rename()
    Var.FSUB_CHATS = await db.list_fsubs()
    static_admins = getattr(Var, "ADMINS", [])
    db_admins = await db.get_admins()
    Var.ADMINS = list({int(a) for a in static_admins + db_admins})
    Var.RSS_ITEMS_ANIME = list(getattr(Var, "RSS_ITEMS_ANIME", []))
    Var.RSS_ITEMS_MANGA = list(getattr(Var, "RSS_ITEMS_MANGA", []))
    Var.RSS_LOW_END = dict(getattr(Var, "RSS_LOW_END", {}))
    db_rss_links = await db.get_all_rss_links()
    for link in db_rss_links:
        if link["type"] == "anime":
            if link["link"] not in Var.RSS_ITEMS_ANIME:
                Var.RSS_ITEMS_ANIME.append(link["link"])
        elif link["type"] == "manga":
            if link["link"] not in Var.RSS_ITEMS_MANGA:
                Var.RSS_ITEMS_MANGA.append(link["link"])
        elif link["type"] == "lowend":
            if link.get("quality") and link["quality"] not in Var.RSS_LOW_END:
                Var.RSS_LOW_END[link["quality"]] = link["link"]
    
    LOGS.info(f"Settings loaded! Admins: {Var.ADMINS}")
    


async def start_bot_with_retry():
    """Keep the Heroku worker alive during temporary Telegram auth RPC 500s."""
    attempt = 0
    while True:
        attempt += 1
        try:
            await asyncio.wait_for(bot.start(), timeout=120)
            LOGS.info("Telegram bot client started successfully with BOT_TOKEN.")
            return
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}".casefold()
            is_rpc_500 = (
                "rpc_call_fail" in error_text
                and ("500" in error_text or "internal_server_error" in error_text)
            ) or "internal_server_error_500" in error_text
            is_auth_rpc_500 = (
                "auth.importbotauthorization" in error_text and is_rpc_500
            ) or (
                "auth.importbotauthorization" in error_text
                and "internal_server_error" in error_text
            )

            # A bad/revoked token is a configuration error, not a transient
            # server failure. Stop immediately with a clear log instead of
            # retrying forever.
            if not is_auth_rpc_500:
                LOGS.exception(
                    "Telegram bot startup failed (non-retryable). "
                    "Verify API_ID, API_HASH and BOT_TOKEN."
                )
                raise

            try:
                if bot.is_connected:
                    await bot.stop()
            except Exception as cleanup_error:
                LOGS.warning(
                    "Telegram startup cleanup failed on attempt %s: %s",
                    attempt, cleanup_error,
                )

            delay = min(600, 60 * (2 ** min(attempt - 1, 4)))
            LOGS.warning(
                "Telegram auth.ImportBotAuthorization returned RPC 500 "
                "(attempt %s). Keeping the Heroku worker alive; retrying "
                "in %s seconds. This is a Telegram-side transient error.",
                attempt, delay,
            )
            await asyncio.sleep(delay)


async def main():
    # Keep references to application-owned tasks; Pyrofork owns its
    # internal recv/ping workers and must control their lifecycle.
    tasks = []
    try:
        LOGS.info("Starting Web Server...")
        await start_server()

        LOGS.info("Loading settings...")
        await load_settings()

        LOGS.info("Starting scheduler...")
        sch.add_job(
            upcoming_animes,
            "cron",
            hour=0,
            minute=30,
            timezone="Asia/Kolkata",
        )
        sch.add_job(clean_old_files, "interval", hours=1)
        sch.start()

        # Connect immediately. A Telegram RPC 500 is retried without
        # terminating the Heroku worker; BOT_TOKEN remains the auth method.
        await start_bot_with_retry()
        try:
            await restart()

            LOGS.info("Starting tasks...")
            LOGS.info(
                "Automatic RSS anime uploads are OFF. "
                "Manual /uploadchannel remains available."
            )
            tasks = [
                create_task(queue_loop(), name="app-queue-loop"),
                create_task(fetch_animes(), name="app-anime-rss"),
            ]

            LOGS.info("Auto Anime Bot Started!")
            await idle()
        finally:
            # Cancel only application-owned tasks, then close our Telegram client.
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await gather(*tasks, return_exceptions=True)
            if bot.is_connected:
                await bot.stop()

    except Exception:
        LOGS.exception("Application startup/runtime error")
        raise

    finally:
        LOGS.info("Shutting down...")
        if sch.running:
            sch.shutdown(wait=False)
        await clean_up()
        LOGS.info("Cleanup complete!")

if __name__ == '__main__':
    try:
        # Let Pyrofork drive its client coroutine on the owning event loop.
        bot.run(main())
    except (KeyboardInterrupt, SystemExit):
        LOGS.info("Bot stopped!")
    except Exception:
        LOGS.exception("Fatal worker error")
        raise
