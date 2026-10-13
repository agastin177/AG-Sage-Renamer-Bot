import asyncio
import contextlib
import logging
import os
import re
import shutil
import time
import urllib.request
from collections import deque
from pathlib import Path

from PIL import Image, ImageOps
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Render Environment variables required: API_ID, API_HASH, BOT_TOKEN
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("ag-sage-renamer")

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("Set API_ID, API_HASH and BOT_TOKEN in Render Environment.")

MAX_TRANSMISSIONS = int(os.getenv("MAX_TRANSMISSIONS", "8"))

DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/ag_sage_renamer"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR = DATA_DIR / "work"
WORK_DIR.mkdir(parents=True, exist_ok=True)
THUMB_PATH = DATA_DIR / "custom_thumbnail.jpg"

# One pending file per user/chat.
pending = {}

CANCEL_MARKUP = InlineKeyboardMarkup(
    [[InlineKeyboardButton("✖️ CANCEL ✖️", callback_data="cancel")]]
)

# Pyrogram/Kurigram binds to the event loop at Client creation, so create
# one loop first and run everything on that same loop (fixes "different loop").
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)


def build_client():
    kwargs = dict(
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        workers=16,
        sleep_threshold=30,
    )
    try:
        return Client(
            "ag_sage_renamer_bot",
            max_concurrent_transmissions=MAX_TRANSMISSIONS,
            **kwargs,
        )
    except TypeError:
        log.warning("max_concurrent_transmissions not supported; using defaults")
        return Client("ag_sage_renamer_bot", **kwargs)


app = build_client()


class TransferCancelled(Exception):
    pass


def original_filename(message):
    media = message.document or message.video or message.audio or message.animation
    if media and getattr(media, "file_name", None):
        return media.file_name
    if message.video:
        return f"video_{message.id}.mp4"
    if message.audio:
        return f"audio_{message.id}.mp3"
    return f"file_{message.id}.bin"


def safe_filename(user_text, original_name):
    name = (user_text or "").strip().replace("\n", " ").replace("\r", " ")
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name).strip(" .")
    if not name:
        name = Path(original_name).stem or "renamed_file"
    if not Path(name).suffix and Path(original_name).suffix:
        name += Path(original_name).suffix
    return Path(name).name[:180]


def make_thumbnail(source_path):
    # Telegram custom thumbnails must be JPEG, <=320x320 and <200 KB.
    with Image.open(source_path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((320, 320), Image.Resampling.LANCZOS)
        for quality in (88, 80, 72, 64, 56, 48, 40):
            image.save(THUMB_PATH, "JPEG", quality=quality, optimize=True)
            if THUMB_PATH.stat().st_size <= 190 * 1024:
                break
        if THUMB_PATH.stat().st_size > 200 * 1024:
            raise ValueError("Could not compress thumbnail below 200 KB.")


def human_size(size):
    if not size:
        return "Unknown size"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def fmt_size(size, decimals=2):
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.{decimals}f} {unit}"
        value /= 1024


def fmt_eta(seconds):
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h {minutes}m {secs}s"


def fmt_elapsed(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


class Transfer:
    """Live download/upload progress.

    The transfer callback only stores numbers (fast, never blocks the transfer).
    A separate background task edits the Telegram message every few seconds.
    Speed is measured over a sliding window, ETA = remaining / speed, and the
    elapsed time comes from a monotonic clock, so all values stay accurate.
    """

    WINDOW = 8.0  # seconds used for the speed average
    EDIT_EVERY = 3.0  # seconds between message edits

    def __init__(self, client, message, job):
        self.client = client
        self.chat_id = message.chat.id
        self.msg_id = message.id
        self.job = job
        self.icon = "📥"
        self.verb = "Downloading"
        self.task = None
        self.last_text = None
        self._reset()

    def _reset(self):
        self.start = time.monotonic()
        self.current = 0
        self.total = 0
        self.samples = deque([(self.start, 0)])
        self.last_sample = self.start

    async def callback(self, current, total):
        if self.job.get("cancel"):
            self.client.stop_transmission()
        now = time.monotonic()
        self.current = current
        self.total = total
        if now - self.last_sample >= 0.5 or (total and current >= total):
            self.samples.append((now, current))
            self.last_sample = now

    def _speed(self, now):
        while len(self.samples) > 2 and now - self.samples[0][0] > self.WINDOW:
            self.samples.popleft()
        t0, c0 = self.samples[0]
        c1 = self.samples[-1][1]
        dt = now - t0
        if dt <= 0 or c1 < c0:
            return 0.0
        return (c1 - c0) / dt

    def text(self):
        now = time.monotonic()
        total = self.total
        current = min(self.current, total) if total else self.current
        percent = (current * 100 / total) if total else 0.0
        filled = min(10, int(percent // 10))
        bar = "■" * filled + "□" * (10 - filled)
        speed = self._speed(now)
        if total and current >= total:
            eta = "0h 0m 0s"
        elif speed > 0 and total:
            eta = fmt_eta((total - current) / speed)
        else:
            eta = "Calculating..."
        return (
            f"Progress: [{bar}] {percent:.1f}%\n"
            f"{self.icon} {self.verb}: {fmt_size(current)} | {fmt_size(total)}\n"
            f"⚡ Speed: {fmt_size(speed, 1)}/s\n"
            f"⏳ ETA: {eta}\n"
            f"⏱️ Time elapsed: {fmt_elapsed(now - self.start)}"
        )

    async def _updater(self):
        await asyncio.sleep(1)
        while True:
            text = self.text()
            if text != self.last_text:
                try:
                    await self.client.edit_message_text(
                        self.chat_id, self.msg_id, text, reply_markup=CANCEL_MARKUP
                    )
                    self.last_text = text
                except Exception:
                    log.debug("Progress edit failed", exc_info=True)
            await asyncio.sleep(self.EDIT_EVERY)

    async def begin(self, icon, verb):
        await self.stop()
        self.icon = icon
        self.verb = verb
        self.last_text = None
        self._reset()
        self.task = asyncio.create_task(self._updater())

    def restart(self):
        # Used when an upload is retried (e.g. video -> document).
        self.last_text = None
        self._reset()

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
            self.task = None


async def delete_message(chat_id, message_id):
    if not message_id:
        return
    try:
        await app.delete_messages(chat_id, message_id)
    except Exception:
        log.debug("Unable to delete message %s", message_id, exc_info=True)


async def safe_edit(chat_id, message_id, text):
    try:
        await app.edit_message_text(chat_id, message_id, text)
    except Exception:
        log.debug("Edit failed", exc_info=True)


@app.on_message(filters.private & filters.command("start"))
async def start_handler(client, message):
    await client.send_message(
        message.chat.id,
        "Hi! Send me a file and I will rename it.\n"
        "To set a custom thumbnail, send an image first.\n"
        "Sending another image replaces the old thumbnail.",
        reply_to_message_id=message.id,
    )


@app.on_message(filters.private & filters.photo)
async def save_thumbnail_handler(client, message):
    status = None
    downloaded = None
    try:
        status = await client.send_message(
            message.chat.id, "Saving image... ⏳",
            reply_to_message_id=message.id,
        )
        downloaded = await client.download_media(
            message, file_name=str(DATA_DIR / "incoming_thumbnail")
        )
        if not downloaded:
            raise RuntimeError("Image download failed.")
        make_thumbnail(downloaded)
        await client.edit_message_text(
            message.chat.id, status.id, "✅ Image Saved Successful"
        )
    except Exception as exc:
        log.exception("Could not save thumbnail")
        if status:
            await safe_edit(
                message.chat.id, status.id,
                f"❌ Failed to save image: {str(exc)[:250]}",
            )
        else:
            await client.send_message(
                message.chat.id, "❌ Failed to save image. Please try again."
            )
    finally:
        if downloaded:
            try:
                Path(downloaded).unlink(missing_ok=True)
            except Exception:
                pass


@app.on_message(filters.private & (filters.document | filters.video | filters.audio | filters.animation))
async def receive_file_handler(client, message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    key = (message.chat.id, user_id)
    old = pending.pop(key, None)
    if old:
        await delete_message(message.chat.id, old.get("prompt_id"))
        await delete_message(message.chat.id, old.get("choice_id"))

    name = original_filename(message)
    media = message.document or message.video or message.audio or message.animation
    prompt = await client.send_message(
        message.chat.id,
        "Please Enter New Filename...\n\n"
        f"Old File Name :- {name}\n"
        f"File Size :- {human_size(getattr(media, 'file_size', None))}",
        reply_to_message_id=message.id,
    )
    pending[key] = {
        "source_message_id": message.id,
        "keyword_id": None,
        "original_name": name,
        "prompt_id": prompt.id,
        "choice_id": None,
        "new_name": None,
        "running": False,
        "cancel": False,
    }


@app.on_message(filters.private & filters.text & ~filters.command("start"))
async def receive_filename_handler(client, message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    key = (message.chat.id, user_id)
    job = pending.get(key)
    if not job or job.get("new_name"):
        return

    job["new_name"] = safe_filename(message.text, job["original_name"])
    job["keyword_id"] = message.id
    # Remove the bot's filename prompt as soon as the user enters a name.
    await delete_message(message.chat.id, job.get("prompt_id"))
    job["prompt_id"] = None

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 Document", callback_data="output:document")],
        [InlineKeyboardButton("🎥 Video", callback_data="output:video")],
    ])
    # Reply directly to the user's filename (keyword) message.
    choice = await client.send_message(
        message.chat.id,
        "Select The Output File Type\n\n"
        f"File Name :- {job['new_name']}",
        reply_to_message_id=message.id,
        reply_markup=buttons,
    )
    job["choice_id"] = choice.id


@app.on_callback_query(filters.regex(r"^cancel$"))
async def cancel_transfer(client, query):
    if not query.message:
        await query.answer("Message not found.", show_alert=True)
        return
    key = (query.message.chat.id, query.from_user.id)
    job = pending.get(key)
    if not job or not job.get("running"):
        await query.answer("Nothing to cancel.", show_alert=True)
        return
    job["cancel"] = True
    await query.answer("Cancelling...")


@app.on_callback_query(filters.regex(r"^output:(document|video)$"))
async def send_renamed_file(client, query):
    msg = query.message
    if not msg:
        await query.answer("Message not found.", show_alert=True)
        return

    chat_id = msg.chat.id
    key = (chat_id, query.from_user.id)
    job = pending.get(key)
    if not job or not job.get("new_name"):
        await query.answer("Session expired. Please send the file again.", show_alert=True)
        return
    if job.get("running"):
        await query.answer("Already processing.", show_alert=True)
        return

    job["running"] = True
    kind = query.data.split(":", 1)[1]
    await query.answer("Processing...")

    # Output type selection message is removed as soon as a button is pressed.
    await delete_message(chat_id, msg.id)
    job["choice_id"] = None

    reply_to = job.get("keyword_id") or job["source_message_id"]
    downloaded = None
    output = None
    progress = None
    transfer = None

    try:
        progress = await client.send_message(
            chat_id, "Starting download... ⏳",
            reply_to_message_id=reply_to,
            reply_markup=CANCEL_MARKUP,
        )
        transfer = Transfer(client, progress, job)

        source = await client.get_messages(chat_id, job["source_message_id"])
        if not source or not (source.document or source.video or source.audio or source.animation):
            raise RuntimeError("Original file not found. Please send the file again.")

        await transfer.begin("📥", "Downloading")
        downloaded = await client.download_media(
            source,
            file_name=str(WORK_DIR / f"download_{source.id}"),
            progress=transfer.callback,
        )
        await transfer.stop()
        if job.get("cancel"):
            raise TransferCancelled()
        if not downloaded or not Path(downloaded).is_file():
            raise RuntimeError("Download failed.")

        output = str(WORK_DIR / safe_filename(job["new_name"], job["original_name"]))
        if Path(downloaded).resolve() != Path(output).resolve():
            shutil.move(downloaded, output)
            downloaded = output

        thumbnail = str(THUMB_PATH) if THUMB_PATH.is_file() else None
        common = {
            "chat_id": chat_id,
            "reply_to_message_id": reply_to,
            "caption": Path(output).name,
        }

        await transfer.begin("📤", "Uploading")
        sent = None
        if kind == "video":
            try:
                sent = await client.send_video(
                    video=output,
                    thumb=thumbnail,
                    supports_streaming=True,
                    progress=transfer.callback,
                    **common,
                )
            except Exception as video_error:
                if job.get("cancel"):
                    raise TransferCancelled()
                # MKV and some containers may not be accepted as Telegram videos.
                log.warning("Video upload failed; retrying as document: %s", video_error)
                transfer.restart()
                sent = await client.send_document(
                    document=output, thumb=thumbnail,
                    progress=transfer.callback, **common
                )
        else:
            sent = await client.send_document(
                document=output, thumb=thumbnail,
                progress=transfer.callback, **common
            )
        await transfer.stop()
        if sent is None:
            if job.get("cancel"):
                raise TransferCancelled()
            raise RuntimeError("Upload failed.")

        await delete_message(chat_id, progress.id)

    except TransferCancelled:
        if progress:
            await safe_edit(chat_id, progress.id, "❌ Process Cancelled.")
    except Exception as exc:
        log.exception("Rename/upload failed")
        reason = f"❌ Process failed.\nReason: {str(exc)[:400]}"
        if progress:
            await safe_edit(chat_id, progress.id, reason)
        else:
            await client.send_message(chat_id, reason)
    finally:
        if transfer:
            await transfer.stop()
        job["running"] = False
        if pending.get(key) is job:
            pending.pop(key, None)
        # Remove temporary copies so large files don't fill the instance disk.
        for path in {downloaded, output}:
            if path:
                try:
                    Path(path).unlink(missing_ok=True)
                except Exception:
                    pass


async def health_server():
    # Minimal HTTP endpoint for Render web-service health checks.
    port = int(os.getenv("PORT", "10000"))

    async def handle(reader, writer):
        try:
            await reader.read(4096)
            body = b"AG Sage Renamer Bot is running"
            response = (
                b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n" + body
            )
            writer.write(response)
            await writer.drain()
        except Exception:
            log.debug("Health endpoint error", exc_info=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    log.info("Health server listening on port %s", port)
    async with server:
        await server.serve_forever()


async def keep_alive():
    # Self-ping so Render free instance doesn't spin down during long transfers.
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return

    def ping():
        urllib.request.urlopen(url, timeout=15).read()

    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(ping)
        except Exception:
            log.debug("Keep-alive ping failed", exc_info=True)


async def main():
    await app.start()
    log.info("AG Sage Renamer Bot started successfully")
    tasks = [asyncio.create_task(health_server()), asyncio.create_task(keep_alive())]
    try:
        await asyncio.Event().wait()
    finally:
        for t in tasks:
            t.cancel()
        await app.stop()


if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
