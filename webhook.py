"""One-shot check: post any new WoW: Forever news to a Discord webhook, then exit.

Meant to run on a schedule (e.g. the GitHub Actions workflow in .github/workflows/news.yml).
"""

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

import discord  # noqa: E402

import news  # noqa: E402


async def main():
    async with news.make_session() as session:
        webhook = discord.Webhook.from_url(os.environ["WEBHOOK_URL"], session=session)
        await news.check_and_post(session, lambda embed: webhook.send(embed=embed))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
