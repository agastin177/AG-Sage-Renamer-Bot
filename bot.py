import os
import re
import time
import shutil
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pyrogram import Client, filters
from pyrogram.types import ForceReply

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
# Optional: Telegram Premium user session string (for files > 2GB, up to 4GB)
SESSION_STRING = os.environ.get("SESSION_STRING")
# Optional: private channel ID (e.g. -100123456789). Bot must be admin, user account must be member.
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", "0"))

TWO_GB = 2 * 1024 * 1024 * 1024
FOUR_GB = 4 * 1024 * 1024 * 1024

bot = Client(
    "renamer_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=100,
    max_concurrent_transmissions=8,
)

user = None
if SESSION_STRING and LOG_CHANNEL:
    user = Client(
        "premium_user",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        max_concurrent_transmissions=8,
    )

pending = {}  # user_id -> original message


def human(size):
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def clean_name(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()


async def progress(current, total, status, start, label, st):
    now = time.time()
    if now - st["t"] < 5 and current != total:
        return
    st["t"] = now
    speed = current / max(now - start, 1)
    pct = current * 100 / total
    try:
        await status.edit_text(
            f"{label}\n{pct:.1f}% | {human(current)} / {human(total)}\n"
            f"Speed: {human(speed)}/s"
        )
    except Exception:
        pass


def get_media(msg):
    return msg.document or msg.video or msg.audio


@bot.on_message(filters.command("start") & filters.private)
async def start(_, m):
    await m.reply_text(
        "Hi! File anuppunga, naan rename pannuren.\n"
        "Bot limit: 2GB. Premium helper irundha: 4GB."
    )


@bot.on_message((filters.document | filters.video | filters.audio) & filters.private)
async def got_file(_, m):
    media = get_media(m)
    if media.file_size > FOUR_GB:
        return await m.reply_text("File 4GB ku mela irukku. Telegram support pannadhu.")
    if media.file_size > TWO_GB and not user:
        return await m.reply_text(
            "2GB ku mela file ku Premium SESSION_STRING + LOG_CHANNEL venum."
        )
    pending[m.from_user.id] = m
    await m.reply_text(
        f"Current name: `{media.file_name}`\nSize: {human(media.file_size)}\n\n"
        "Pudhu name anuppunga (extension oda). Cancel panna /cancel",
        reply_markup=ForceReply(selective=True),
        quote=True,
    )


@bot.on_message(filters.command("cancel") & filters.private)
async def cancel(_, m):
    pending.pop(m.from_user.id, None)
    await m.reply_text("Cancelled.")


@bot.on_message(filters.text & filters.private & ~filters.command(["start", "cancel"]))
async def got_name(client, m):
    orig = pending.pop(m.from_user.id, None)
    if not orig:
        return await m.reply_text("Mudhala oru file anuppunga.")

    media = get_media(orig)
    new_name = clean_name(m.text)
    if "." not in new_name:
        old_ext = os.path.splitext(media.file_name or "")[1]
        new_name += old_ext

    status = await m.reply_text("Starting download...")
    folder = f"downloads/{m.from_user.id}_{int(time.time())}"
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, new_name)

    try:
        # Download
        st = {"t": 0}
        start_t = time.time()
        await orig.download(
            file_name=path,
            progress=progress,
            progress_args=(status, start_t, "Downloading...", st),
        )

        # Upload
        st = {"t": 0}
        start_t = time.time()
        thumb = None
        if media.thumbs:
            try:
                thumb = await client.download_media(media.thumbs[0].file_id)
            except Exception:
                thumb = None

        if media.file_size > TWO_GB and user:
            sent = await user.send_document(
                LOG_CHANNEL,
                path,
                file_name=new_name,
                thumb=thumb,
                force_document=True,
                progress=progress,
                progress_args=(status, start_t, "Uploading (Premium)...", st),
            )
            await client.copy_message(m.chat.id, LOG_CHANNEL, sent.id)
        else:
            await client.send_document(
                m.chat.id,
                path,
                file_name=new_name,
                thumb=thumb,
                force_document=True,
                caption=new_name,
                progress=progress,
                progress_args=(status, start_t, "Uploading...", st),
            )
        await status.edit_text("Done ✅")
    except Exception as e:
        await status.edit_text(f"Error: {e}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        if "thumb" in locals() and thumb and os.path.exists(thumb):
            os.remove(thumb)


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def start_health_server():
    # Koyeb/Render web services need a port to answer health checks
    port = int(os.environ.get("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()


async def main():
    start_health_server()
    await bot.start()
    if user:
        await user.start()
    print("Bot started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    bot.loop = asyncio.get_event_loop()
    asyncio.get_event_loop().run_until_complete(main())
