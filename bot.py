"""Always-on Discord bot that posts WoW: Forever news into a channel."""

import hashlib
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import aiohttp  # noqa: E402
import discord  # noqa: E402
from discord.ext import tasks  # noqa: E402

import news  # noqa: E402

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "10"))
ICON_FILE = Path(__file__).with_name("icon.png")
ICON_HASH_FILE = Path(__file__).with_name(".icon_hash")

log = logging.getLogger("wowbot")


class WowBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = news.make_session()
        await self.sync_avatar()
        self.poll.change_interval(minutes=POLL_MINUTES)
        self.poll.start()

    async def sync_avatar(self):
        # Discord rate-limits avatar changes, so only upload when icon.png actually changed.
        if not ICON_FILE.exists():
            return
        data = ICON_FILE.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if ICON_HASH_FILE.exists() and ICON_HASH_FILE.read_text().strip() == digest:
            return
        try:
            await self.user.edit(avatar=data)
        except discord.HTTPException:
            log.exception("Failed to update avatar")
            return
        ICON_HASH_FILE.write_text(digest)
        log.info("Updated bot avatar from %s", ICON_FILE.name)

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    @tasks.loop(minutes=10)
    async def poll(self):
        channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
        await news.check_and_post(self.session, lambda embed: channel.send(embed=embed))

    @poll.before_loop
    async def before_poll(self):
        await self.wait_until_ready()

    @poll.error
    async def poll_error(self, error):
        log.exception("Poll loop crashed; restarting", exc_info=error)
        self.poll.restart()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    WowBot().run(TOKEN, log_handler=None)
