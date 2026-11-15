import asyncio
import logging
import os
from pathlib import Path
import tempfile

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv

from .media import DEFAULT_LOGO, MAX_DURATION, ROOT, SIZE
from .media import MediaError, check_tools, probe_video, render


logger = logging.getLogger(__name__)
MAX_DOWNLOAD_BYTES = 20_000_000
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def make_router(allowed_users: set[int]) -> Router:
    router = Router()
    processing = asyncio.Lock()

    async def authorized(message: Message) -> bool:
        if allowed_users and (not message.from_user or message.from_user.id not in allowed_users):
            await message.reply("This bot is only available to authorized users.")
            return False
        return True

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def help_message(message: Message) -> None:
        if not await authorized(message):
            return
        await message.reply(
            "Send a video or video note. I will return a video note with the Kultura logo "
            "in the upper right, sized to 24% of the frame diameter.\n\n"
            "You can also send a video as a file. The input limit is 20 MB. "
            "Videos longer than one minute are trimmed to the first 60 seconds. "
            "Rectangular videos are cropped to a square from the center."
        )

    @router.message(F.video | F.video_note | F.document)
    async def process_video(message: Message, bot: Bot) -> None:
        if not await authorized(message):
            return
        media = message.video or message.video_note or message.document
        if media is None:
            return
        if message.document:
            mime = message.document.mime_type or ""
            suffix = Path(message.document.file_name or "").suffix.lower()
            if not mime.startswith("video/") and suffix not in VIDEO_EXTENSIONS:
                await message.reply("Send a video, such as an MP4 file, or a video note.")
                return
        if media.file_size and media.file_size > MAX_DOWNLOAD_BYTES:
            await message.reply("The file exceeds 20 MB. Send a shorter or compressed video.")
            return
        if processing.locked():
            await message.reply("I am processing another video. Please send yours again shortly.")
            return
        async with processing:
            status = await message.reply("Preparing your video note with the logo…")
            try:
                with tempfile.TemporaryDirectory(prefix="kultura-") as temporary:
                    work = Path(temporary)
                    source = work / "input-video"
                    await bot.download(media, destination=source, timeout=120)
                    if source.stat().st_size > MAX_DOWNLOAD_BYTES:
                        raise MediaError("The input file exceeds 20 MB.")
                    info = await probe_video(source)
                    if info.duration > MAX_DURATION:
                        await message.reply("I will use the first 60 seconds, the video note duration limit.")
                    rendered = await render(source, DEFAULT_LOGO, work, info)
                    await message.reply_video_note(
                        video_note=FSInputFile(rendered),
                        duration=max(1, round(min(info.duration, MAX_DURATION))), length=SIZE,
                    )
                await status.edit_text("Done.")
            except asyncio.CancelledError:
                raise
            except (MediaError, TimeoutError, TelegramAPIError, OSError, ValueError):
                logger.exception("Video processing failed for message %s", message.message_id)
                await status.edit_text(
                    "Could not process or send the video. "
                    "Try an MP4 file under 20 MB. Error details are in the bot console."
                )

    @router.message()
    async def fallback(message: Message) -> None:
        if await authorized(message):
            await message.reply("Send a video or video note. Instructions: /help")

    return router


async def main() -> None:
    load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    if not token:
        raise SystemExit("Add TELEGRAM_TOKEN to .env and start the bot again.")
    check_tools()
    if not DEFAULT_LOGO.is_file():
        raise SystemExit(f"Logo not found: {DEFAULT_LOGO}")
    try:
        allowed_users = {int(value.strip()) for value in
                         os.getenv("ALLOWED_USER_IDS", "").split(",") if value.strip()}
    except ValueError as error:
        raise SystemExit("ALLOWED_USER_IDS must contain numeric IDs separated by commas.") from error
    dispatcher = Dispatcher()
    dispatcher.include_router(make_router(allowed_users))
    async with Bot(token, session=AiohttpSession(timeout=120)) as bot:
        await dispatcher.start_polling(bot, close_bot_session=False,
                                       allowed_updates=dispatcher.resolve_used_update_types())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
