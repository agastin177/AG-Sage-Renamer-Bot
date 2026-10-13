import asyncio
import logging
import os
import re
import shutil
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

DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/ag_sage_renamer"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR = DATA_DIR / "work"
WORK_DIR.mkdir(parents=True, exist_ok=True)
THUMB_PATH = DATA_DIR / "custom_thumbnail.jpg"

# One pending file per user/chat.
pending = {}

# Pyrogram/Kurigram binds to the event loop at Client creation, so create
# one loop first and run everything on that same loop (fixes "different loop").
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

app = Client(
    "ag_sage_renamer_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=8,
    sleep_threshold=30,
)


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


async def delete_message(chat_id, message_id):
    if not message_id:
        return
    try:
        await app.delete_messages(chat_id, message_id)
    except Exception:
        log.debug("Unable to delete message %s", message_id, exc_info=True)


@app.on_message(filters.private & filters.command("start"))
async def start_handler(client, message):
    await client.send_message(
        message.chat.id,
        "Hi! File anuppunga, naan rename pannuren.\n"
        "Custom thumbnail set panna first oru image anuppunga.\n"
        "Vera image anuppina, old thumbnail replace aagum.",
        reply_to_message_id=message.id,
    )


@app.on_message(filters.private & filters.photo)
async def save_thumbnail_handler(client, message):
    status = None
    downloaded = None
    try:
        status = await client.send_message(
            message.chat.id, "Image save pannuren... ⏳",
            reply_to_message_id=message.id,
        )
        downloaded = await client.download_media(
            message, file_name=str(DATA_DIR / "incoming_thumbnail")
        )
        if not downloaded:
            raise RuntimeError("Image download failed.")
        make_thumbnail(downloaded)
        await client.edit_message_text(
            message.chat.id,
            status.id,
            "✅ Image Saved Successful\n"
            "Intha image files-oda thumbnail-ah use aagum. "
            "Vera image anuppina old image replace aagidum.",
        )
    except Exception as exc:
        log.exception("Could not save thumbnail")
        if status:
            try:
                await client.edit_message_text(
                    message.chat.id, status.id,
                    f"❌ Image save aagala: {str(exc)[:250]}",
                )
            except Exception:
                pass
        else:
            await client.send_message(message.chat.id, "❌ Image save aagala. Marubadi try pannunga.")
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
        "original_name": name,
        "prompt_id": prompt.id,
        "choice_id": None,
        "new_name": None,
    }


@app.on_message(filters.private & filters.text & ~filters.command("start"))
async def receive_filename_handler(client, message):
    user_id = message.from_user.id if message.from_user else message.chat.id
    key = (message.chat.id, user_id)
    job = pending.get(key)
    if not job or job.get("new_name"):
        return

    job["new_name"] = safe_filename(message.text, job["original_name"])
    # Remove the bot's filename prompt as soon as the user enters a name.
    await delete_message(message.chat.id, job.get("prompt_id"))
    job["prompt_id"] = None

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("📁 Document", callback_data="output:document")],
        [InlineKeyboardButton("🎥 Video", callback_data="output:video")],
    ])
    choice = await client.send_message(
        message.chat.id,
        "Select The Output File Type\n\n"
        f"File Name :- {job['new_name']}",
        reply_to_message_id=job["source_message_id"],
        reply_markup=buttons,
    )
    job["choice_id"] = choice.id


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
        await query.answer("Session expired. File-a marubadi anuppunga.", show_alert=True)
        return

    kind = query.data.split(":", 1)[1]
    await query.answer("Processing...")
    progress = await client.send_message(
        chat_id, "⏳ File download/upload nadakkuthu. Please wait...",
        reply_to_message_id=job["source_message_id"],
    )
    downloaded = None
    output = None

    try:
        source = await client.get_messages(chat_id, job["source_message_id"])
        if not source or not (source.document or source.video or source.audio or source.animation):
            raise RuntimeError("Original file kidaikkala. File-a marubadi anuppunga.")

        downloaded = await client.download_media(
            source, file_name=str(WORK_DIR / f"download_{source.id}")
        )
        if not downloaded or not Path(downloaded).is_file():
            raise RuntimeError("Download failed.")

        output = str(WORK_DIR / safe_filename(job["new_name"], job["original_name"]))
        if Path(downloaded).resolve() != Path(output).resolve():
            shutil.move(downloaded, output)
            downloaded = output

        thumbnail = str(THUMB_PATH) if THUMB_PATH.is_file() else None
        common = {
            "chat_id": chat_id,
            "reply_to_message_id": job["source_message_id"],
            "caption": Path(output).name,
        }

        if kind == "video":
            try:
                await client.send_video(
                    video=output,
                    thumb=thumbnail,
                    supports_streaming=True,
                    **common,
                )
            except Exception as video_error:
                # MKV and some containers may not be accepted as Telegram videos.
                log.warning("Video upload failed; retrying as document: %s", video_error)
                await client.send_document(
                    document=output, thumb=thumbnail, **common
                )
        else:
            await client.send_document(
                document=output, thumb=thumbnail, **common
            )

        await delete_message(chat_id, progress.id)
        await delete_message(chat_id, job.get("choice_id"))
        pending.pop(key, None)

    except Exception as exc:
        log.exception("Rename/upload failed")
        try:
            await client.edit_message_text(
                chat_id, progress.id,
                "❌ File process aagala.\n"
                f"Reason: {str(exc)[:400]}\n"
                "Render disk space, file size and connection check pannunga.",
            )
        except Exception:
            await client.send_message(chat_id, f"❌ File process aagala: {str(exc)[:400]}")
    finally:
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


async def main():
    await app.start()
    log.info("AG Sage Renamer Bot started successfully")
    task = asyncio.create_task(health_server())
    try:
        await asyncio.Event().wait()
    finally:
        task.cancel()
        await app.stop()


if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
