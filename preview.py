import argparse
import asyncio
from pathlib import Path

from kultura_bot.media import DEFAULT_LOGO, VARIANTS, check_tools, probe_video, render_variant


async def main() -> None:
    parser = argparse.ArgumentParser(description="Add a 24% logo to a video without Telegram or a token.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--logo", type=Path, default=DEFAULT_LOGO)
    args = parser.parse_args()
    check_tools()
    info = await probe_video(args.video)
    for variant in VARIANTS:
        result = await render_variant(args.video, args.logo, args.output, variant, info)
        print(f"{variant.label}: {result.path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
