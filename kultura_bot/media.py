from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
from pathlib import Path
import shutil

from PIL import Image, ImageFilter


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOGO = ROOT / "logos" / "kult.png"
SIZE = 640
MAX_DURATION = 60


class MediaError(Exception):
    pass


@dataclass(frozen=True)
class Variant:
    name: str
    label: str
    fraction: float


VARIANTS = (
    Variant("small", "Small · 24%", 0.24),
)


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


@dataclass(frozen=True)
class RenderedVideo:
    path: Path
    variant: Variant
    duration: float


def check_tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
    if missing:
        raise MediaError("Missing: " + ", ".join(missing) + ". Install FFmpeg.")


async def run_process(*args: str, timeout: float = 300) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        detail = stderr.decode("utf-8", errors="replace")[-3000:]
        raise MediaError(f"{Path(args[0]).name}: {detail}")
    return stdout


async def probe_video(source: Path) -> VideoInfo:
    raw = await run_process(
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(source), timeout=30,
    )
    data = json.loads(raw)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if not video or video.get("width", 0) < 2 or video.get("height", 0) < 2:
        raise MediaError("No video stream found in the file.")
    raw_duration = video.get("duration") or data.get("format", {}).get("duration")
    try:
        duration = float(raw_duration)
    except (TypeError, ValueError) as error:
        raise MediaError("Could not determine the video duration.") from error
    if not math.isfinite(duration) or duration <= 0:
        raise MediaError("Invalid video duration.")
    return VideoInfo(duration, video["width"], video["height"],
                     any(s.get("codec_type") == "audio" for s in streams))


def logo_position(width: int, height: int, size: int = SIZE) -> tuple[int, int]:
    x = round(size * 0.84) - width
    y = round(size * 0.18)
    radius = size * 0.48
    center = size / 2
    if any(math.hypot(px - center, py - center) > radius
           for px in (x, x + width) for py in (y, y + height)):
        raise MediaError("The logo does not fit inside the circle: use a wide logo.")
    return x, y


def prepare_overlay(logo_path: Path, destination: Path, variant: Variant,
                    size: int = SIZE) -> tuple[int, int]:
    supersampling = 2
    width = round(size * variant.fraction) * supersampling
    with Image.open(logo_path) as original:
        logo = original.convert("RGBA")
    bounds = logo.getbbox()
    if not bounds:
        raise MediaError("The logo is fully transparent.")
    logo = logo.crop(bounds)
    height = max(1, round(width * logo.height / logo.width))
    logo = logo.resize((width, height), Image.Resampling.LANCZOS)
    pad = 4
    layer = Image.new("RGBA", (width + 2 * pad, height + 2 * pad))
    layer.paste(logo, (pad, pad))
    alpha = layer.getchannel("A")
    outline_alpha = alpha.filter(ImageFilter.MaxFilter(5)).filter(
        ImageFilter.GaussianBlur(0.6)
    ).point(lambda value: round(value * 0.85))
    outline = Image.new("RGBA", layer.size, "white")
    outline.putalpha(outline_alpha)
    overlay = Image.alpha_composite(outline, layer)
    overlay.save(destination)
    return logo_position(overlay.width, overlay.height, size * supersampling)


async def render_variant(source: Path, logo: Path, output_dir: Path,
                         variant: Variant, info: VideoInfo,
                         max_duration: float = MAX_DURATION) -> RenderedVideo:
    if not 0 < max_duration <= MAX_DURATION:
        raise ValueError("Duration must be greater than 0 and at most 60 seconds.")
    output_dir.mkdir(parents=True, exist_ok=True)
    overlay = output_dir / f"logo-{variant.name}.png"
    x, y = prepare_overlay(logo, overlay, variant)
    output = output_dir / f"kultura-{variant.name}.mp4"
    duration = min(info.duration, max_duration)
    filters = (
        "[0:V:0]setpts=PTS-STARTPTS,"
        "scale=1280:1280:force_original_aspect_ratio=increase:flags=lanczos,"
        "crop=1280:1280,setsar=1,fps=30,format=yuv444p[base];"
        "[1:v]format=rgba[logo];"
        f"[base][logo]overlay={x}:{y}:format=auto:"
        "eof_action=repeat:shortest=0,"
        "scale=640:640:flags=lanczos,format=yuv420p[out]"
    )
    await run_process(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-i", str(overlay),
        "-filter_complex", filters, "-map", "[out]", "-map", "0:a:0?",
        "-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "fast",
        "-crf", "20", "-maxrate", "4M", "-bufsize", "8M",
        "-threads", "2", "-c:a", "aac", "-b:a", "96k", "-ar", "48000",
        "-af", "asetpts=PTS-STARTPTS", "-movflags", "+faststart",
        "-map_metadata", "-1", str(output),
    )
    if output.stat().st_size > 49_000_000:
        raise MediaError("The output file is too large to send.")
    return RenderedVideo(output, variant, duration)
