"""Export a persistent Pyrogram session string for the AutoAnime bot.

Run locally after Telegram bot authorization is working:
    python bot_session_gen.py

Copy the printed value into Heroku Config Vars as BOT_SESSION_STRING.
Treat that value like a password. Never commit it or paste it into chat/logs.
"""
import asyncio
import os

from dotenv import load_dotenv
from pyrogram import Client


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


async def main() -> None:
    load_dotenv("config.env")

    api_id = int(required_env("API_ID"))
    api_hash = required_env("API_HASH")
    bot_token = required_env("BOT_TOKEN")

    app = Client(
        "bot-session-export",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        in_memory=True,
    )

    async with app:
        me = await app.get_me()
        session_string = await app.export_session_string()
        username = f"@{me.username}" if me.username else str(me.id)
        print(f"Authorized bot: {username}")
        print("\nBOT_SESSION_STRING (keep secret; add to Heroku Config Vars):")
        print(session_string)


if __name__ == "__main__":
    asyncio.run(main())
