import asyncio
import json
from pathlib import Path
import subprocess

from PIL import Image
import pytest

from kultura_bot.media import (
    DEFAULT_LOGO, SIZE, MediaError, logo_position,
    prepare_overlay, probe_video, render,
)


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    source = tmp_path_factory.mktemp("video") / "square.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x640:rate=30:duration=1.5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
    ], check=True)
    return source


def test_render_playable_square_with_audio(video, tmp_path):
    async def run():
        info = await probe_video(video)
        return await render(video, DEFAULT_LOGO, tmp_path, info, max_duration=0.7)
    rendered = asyncio.run(run())
    raw = subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(rendered),
    ])
    data = json.loads(raw)
    visual = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next(s for s in data["streams"] if s["codec_type"] == "audio")
    assert visual["codec_name"] == "h264"
    assert visual["pix_fmt"] == "yuv420p"
    assert visual["width"] == visual["height"] == SIZE
    assert visual["avg_frame_rate"] == "30/1"
    assert audio["codec_name"] == "aac"
    assert 0.65 <= float(data["format"]["duration"]) <= 0.8
    content = rendered.read_bytes()
    assert content.index(b"moov") < content.index(b"mdat")
    frame = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(rendered), "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ])
    reference = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(video), "-frames:v", "1",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ])
    result_image = Image.frombytes("RGB", (SIZE, SIZE), frame)
    input_image = Image.frombytes("RGB", (SIZE, SIZE), reference)
    region = (round(SIZE * .44), round(SIZE * .18), round(SIZE * .84), round(SIZE * .30))
    changed = sum(abs(a-b) for a,b in zip(result_image.crop(region).tobytes(),
                                         input_image.crop(region).tobytes()))
    assert changed > 50_000


@pytest.mark.parametrize("dimensions", ["960x540", "360x640"])
def test_rectangular_silent_input(tmp_path, dimensions):
    source = tmp_path / "silent.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        f"color=c=gray:size={dimensions}:rate=15:duration=0.4",
        "-c:v", "libx264", str(source),
    ], check=True)

    async def run():
        info = await probe_video(source)
        assert not info.has_audio
        result = await render(source, DEFAULT_LOGO, tmp_path, info)
        return await probe_video(result)

    result = asyncio.run(run())
    assert result.width == result.height == SIZE
    assert not result.has_audio


def test_logo_stays_inside_circle_with_antialiasing(tmp_path):
    path = tmp_path / "overlay.png"
    x, y = prepare_overlay(DEFAULT_LOGO, path)
    with Image.open(path) as overlay:
        assert overlay.mode == "RGBA"
        assert logo_position(overlay.width, overlay.height, SIZE * 2) == (x, y)
        alpha = overlay.getchannel("A")
        assert any(alpha.histogram()[1:255])
        assert overlay.getpixel((0, 0))[3] == 0


def test_logo_has_transparency_instead_of_checkerboard():
    with Image.open(DEFAULT_LOGO) as logo:
        assert logo.mode == "RGBA"
        assert logo.getpixel((10, 10))[3] == 0
        assert logo.getpixel((20, 10))[3] == 0
        assert logo.getpixel((700, 30))[3] > 200


def test_reject_non_video(tmp_path):
    source = tmp_path / "invalid.mp4"
    source.write_text("not a video")
    with pytest.raises(MediaError):
        asyncio.run(probe_video(source))


def test_reject_logo_outside_circle():
    with pytest.raises(MediaError, match="does not fit"):
        logo_position(200, 620)
