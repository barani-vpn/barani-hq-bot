import argparse
import asyncio
from pathlib import Path

from kultura_bot.media import DEFAULT_LOGO, check_tools, probe_video, render


async def main() -> None:
    parser = argparse.ArgumentParser(description="Add a 24% logo to a video without Telegram or a token.")
    parser.add_argument("video", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--logo", type=Path, default=DEFAULT_LOGO)
    args = parser.parse_args()
    check_tools()
    info = await probe_video(args.video)
    print((await render(args.video, args.logo, args.output, info)).resolve())


if __name__ == "__main__":
    asyncio.run(main())
