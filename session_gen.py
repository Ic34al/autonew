from pyrogram import Client

API_ID = input("Enter your API_ID: ").strip()
API_HASH = input("Enter your API_HASH: ").strip()

with Client("usergen", api_id=API_ID, api_hash=API_HASH) as app:
    print("\nYour user session string:\n")
    print(app.export_session_string())
    print("\nCopy and paste this string into your config.env as USER_SESSION.")
