from asyncio import (
    create_task, create_subprocess_exec, create_subprocess_shell,
    run as asyrun, all_tasks, gather, sleep as asleep, get_event_loop
)
from aiofiles import open as aiopen
from pyrogram import idle
from pyrogram.filters import command, user
from os import path as ospath, execl, kill, environ
from sys import executable
from signal import SIGKILL
from bot.core.auto_animes import fetch_manga, fetch_animes
from bot import bot, Var, bot_loop, sch, LOGS, ffQueue, ffLock, ffpids_cache, ff_queued
from bot.core.func_utils import clean_up, new_task, editMessage
from bot.modules.up_posts import upcoming_animes
from bot.web import start_server
from bot.core.database import db

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
    Var.SEND_SCHEDULE = await db.get_send_schedule()
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
    

async def main():
    try:
        LOGS.info("Starting Web Server...")
        await start_server()
        
        LOGS.info("Loading settings...")
        await load_settings()
        
        LOGS.info("Starting scheduler...")
        sch.add_job(upcoming_animes, "cron", hour=0, minute=30)
        sch.start()
        
        LOGS.info("Starting bot...")
        await bot.start()
        await restart()
        
        LOGS.info("Starting tasks...")
        tasks = [
            create_task(queue_loop()),
            create_task(fetch_animes()),
            create_task(fetch_manga()),
        ]
        
        LOGS.info('Auto Anime Bot Started!')
        await idle()
        
    except Exception as e:
        LOGS.error(f"Startup Error: {str(e)}")
        raise
    
    finally:
        LOGS.info('Shutting down...')
        if sch.running:
            sch.shutdown(wait=False)
        await bot.stop()
        for task in all_tasks():
            task.cancel()
        await clean_up()
        LOGS.info('Cleanup complete!')

if __name__ == '__main__':
    loop = get_event_loop()
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        LOGS.info("Bot stopped!")
    finally:
        loop.close()