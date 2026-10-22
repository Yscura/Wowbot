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


log = logging.getLogger("wowbot")


def webhook_urls() -> list[str]:
    # Every non-empty env var starting with WEBHOOK_URL (WEBHOOK_URL, WEBHOOK_URL2, ...) is a channel to post to.
    return [v for k, v in sorted(os.environ.items()) if k.startswith("WEBHOOK_URL") and v.strip()]


async def main():
    urls = webhook_urls()
    if not urls:
        raise SystemExit("No WEBHOOK_URL set")

    async with news.make_session() as session:
        webhooks = [discord.Webhook.from_url(url, session=session) for url in urls]

        async def send(embed):
            # A broken webhook shouldn't stop the others or cause reposts; only fail if every channel failed.
            failures = 0
            for i, webhook in enumerate(webhooks, 1):
                try:
                    await webhook.send(embed=embed)
                except discord.HTTPException:
                    log.exception("Failed to post to webhook #%d", i)
                    failures += 1
            if failures == len(webhooks):
                raise discord.DiscordException("All webhooks failed")

        await news.check_and_post(session, send)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
