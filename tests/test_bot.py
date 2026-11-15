import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from kultura_bot import __main__ as app
from kultura_bot.media import VideoInfo


def make_message(user_id=123, size=100):
    return SimpleNamespace(
        video=SimpleNamespace(file_size=size), video_note=None, document=None,
        from_user=SimpleNamespace(id=user_id), message_id=42,
        reply=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())),
        reply_video_note=AsyncMock(),
    )


def get_handler(router):
    return next(handler.callback for handler in router.message.handlers
                if handler.callback.__name__ == "process_video")


def test_sends_one_video_note_and_cleans_files(monkeypatch):
    message = make_message()
    paths = []

    async def download(media, destination, **kwargs):
        destination.write_bytes(b"video")
        paths.append(destination)

    async def render(source, logo, output, info):
        result = output / "out.mp4"
        result.write_bytes(b"rendered")
        paths.append(result)
        return result

    monkeypatch.setattr(app, "probe_video", AsyncMock(return_value=VideoInfo(2, 640, 640, True)))
    monkeypatch.setattr(app, "render", AsyncMock(side_effect=render))
    bot = SimpleNamespace(download=AsyncMock(side_effect=download))
    asyncio.run(get_handler(app.make_router(set()))(message, bot))
    message.reply_video_note.assert_awaited_once()
    call = message.reply_video_note.await_args
    assert call.kwargs["length"] == 640
    assert call.kwargs["duration"] == 2
    assert Path(call.kwargs["video_note"].path) in paths
    assert all(not path.exists() for path in paths)


def test_unauthorized_user_does_not_download():
    message = make_message(user_id=999)
    bot = SimpleNamespace(download=AsyncMock())
    asyncio.run(get_handler(app.make_router({123}))(message, bot))
    bot.download.assert_not_awaited()
    message.reply_video_note.assert_not_awaited()


def test_oversized_video_does_not_download():
    message = make_message(size=20_000_001)
    bot = SimpleNamespace(download=AsyncMock())
    asyncio.run(get_handler(app.make_router(set()))(message, bot))
    bot.download.assert_not_awaited()
    message.reply_video_note.assert_not_awaited()
