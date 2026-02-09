import os
import logging
import asyncio
import hashlib
import hmac
import xml.etree.ElementTree as ET

import discord
from discord.ext import commands
from dotenv import load_dotenv

from aiohttp import web, ClientSession


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")  # e.g. https://xxxx.ngrok-free.app
PORT = int(os.getenv("PORT", "3000"))
WEBSUB_SECRET = os.getenv("WEBSUB_SECRET", "")

HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"
TOPIC_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
CALLBACK_URL = f"{PUBLIC_BASE_URL}/websub"

handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# in-memory dedupe (good enough for now)
seen_video_ids = set()


def _verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    If hub.secret is used, hub includes X-Hub-Signature: sha1=...
    We'll verify it (optional but recommended).
    """
    if not WEBSUB_SECRET:
        return True  # no secret configured

    if not signature_header or not signature_header.startswith("sha1="):
        return False

    their_sig = signature_header.split("=", 1)[1].strip()
    mac = hmac.new(WEBSUB_SECRET.encode("utf-8"), msg=raw_body, digestmod=hashlib.sha1)
    our_sig = mac.hexdigest()
    return hmac.compare_digest(our_sig, their_sig)


async def subscribe_websub(session: ClientSession) -> None:
    """
    Subscribe to YouTube's WebSub topic.
    """
    data = {
        "hub.mode": "subscribe",
        "hub.topic": TOPIC_URL,
        "hub.callback": CALLBACK_URL,
        "hub.verify": "async",
    }
    if WEBSUB_SECRET:
        data["hub.secret"] = WEBSUB_SECRET

    async with session.post(HUB_URL, data=data) as resp:
        text = await resp.text()
        if resp.status not in (202, 204, 200):
            raise RuntimeError(f"WebSub subscribe failed: {resp.status} {text}")
        print(f"WebSub subscribe request sent ({resp.status})")


def parse_atom_for_video(atom_xml: str):
    """
    Parse Atom XML and return (video_id, title, link) if present, else None.
    """
    # namespaces used by YouTube Atom feeds
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }

    root = ET.fromstring(atom_xml)
    entry = root.find("atom:entry", ns)
    if entry is None:
        return None

    video_id_el = entry.find("yt:videoId", ns)
    title_el = entry.find("atom:title", ns)
    link_el = entry.find("atom:link", ns)

    video_id = video_id_el.text if video_id_el is not None else None
    title = title_el.text if title_el is not None else "(no title)"

    link = None
    if link_el is not None and "href" in link_el.attrib:
        link = link_el.attrib["href"]
    if not link and video_id:
        link = f"https://www.youtube.com/watch?v={video_id}"

    if not video_id:
        return None

    return video_id, title, link


async def announce_video(video_id: str, title: str, link: str) -> None:
    """
    Send message to the configured Discord channel.
    """
    if video_id in seen_video_ids:
        return
    seen_video_ids.add(video_id)

    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if channel is None:
        # fetch if not cached
        channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)

    await channel.send(f"📢 **New upload:** {title}\n{link}")


# ---------- aiohttp web server handlers ----------

async def websub_get(request: web.Request) -> web.Response:
    # hub.challenge verification
    challenge = request.query.get("hub.challenge")
    if challenge:
        return web.Response(text=challenge)
    return web.Response(text="ok")


async def websub_post(request: web.Request) -> web.Response:
    raw = await request.read()
    sig = request.headers.get("X-Hub-Signature", "")

    if not _verify_signature(raw, sig):
        return web.Response(status=403, text="Invalid signature")

    try:
        atom_xml = raw.decode("utf-8", errors="replace")
        parsed = parse_atom_for_video(atom_xml)
        if not parsed:
            return web.Response(status=204)

        video_id, title, link = parsed
        # Schedule Discord send on the bot loop
        bot.loop.create_task(announce_video(video_id, title, link))
        return web.Response(status=204)
    except Exception as e:
        print("WebSub POST error:", e)
        return web.Response(status=500, text="error")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/websub", websub_get)
    app.router.add_post("/websub", websub_post)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    print(f"Web server listening on http://0.0.0.0:{PORT} (public: {PUBLIC_BASE_URL})")


# ---------- Discord events ----------

@bot.event
async def on_ready():
    print(f"Ready to go, {bot.user.name}")

    # start web server
    await start_web_server()

    # subscribe to WebSub now + periodically re-subscribe
    async with ClientSession() as session:
        await subscribe_websub(session)

    async def resubscribe_loop():
        # Re-subscribe every 12 hours
        while True:
            await asyncio.sleep(60 * 60 * 12)
            try:
                async with ClientSession() as session:
                    await subscribe_websub(session)
            except Exception as e:
                print("Resubscribe failed:", e)

    bot.loop.create_task(resubscribe_loop())


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if "test message" in message.content.lower():
        await message.delete()
        await message.channel.send(f"{message.author.mention} - Trigger hit")

    await bot.process_commands(message)


bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)
