"""Quick check: is the Telethon session authorized?"""
import asyncio
import os
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID   = int(os.environ["TELETHON_API_ID"])
API_HASH = os.environ["TELETHON_API_HASH"]
SESSION  = os.environ.get("TELETHON_SESSION", "sessions/userbot")


async def main():
    os.makedirs(os.path.dirname(SESSION), exist_ok=True)
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"OK — מחובר כ: {me.first_name} (@{me.username}, id={me.id})")
    else:
        print("NOT AUTHORIZED — יש להריץ: python main.py setup")

    await client.disconnect()


asyncio.run(main())
