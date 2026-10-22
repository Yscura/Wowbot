"""Discord bot that posts WoW: Forever news from RSS feeds into a channel."""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp
import discord
import feedparser
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
FEEDS = [u.strip() for u in os.getenv("FEEDS", "https://www.wowhead.com/news/rss/forever").split(",") if u.strip()]
POLL_MINUTES = float(os.getenv("POLL_MINUTES", "10"))
STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).with_name("seen.json")))
# On first run, only post this many of the newest items instead of the whole backlog.
INITIAL_POSTS = int(os.getenv("INITIAL_POSTS", "3"))
MAX_SEEN = 1000
ICON_FILE = Path(__file__).with_name("icon.png")
ICON_HASH_FILE = Path(__file__).with_name(".icon_hash")

log = logging.getLogger("wowbot")


def load_seen() -> list[str] | None:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_seen(seen: list[str]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen[-MAX_SEEN:]), encoding="utf-8")
    tmp.replace(STATE_FILE)


def clean_text(raw: str, limit: int = 300) -> str:
    text = re.sub(r"<br\s*/?>|<p>", "\n", raw, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = text.replace("Continue reading »", "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def entry_time(entry) -> datetime:
    try:
        return parsedate_to_datetime(entry.published)
    except (AttributeError, TypeError, ValueError):
        return datetime.now(timezone.utc)


def build_embed(entry, feed_title: str) -> discord.Embed:
    embed = discord.Embed(
        title=entry.get("title", "Untitled")[:256],
        url=entry.get("link"),
        description=clean_text(entry.get("summary", "")),
        color=0x1F8B4C,
        timestamp=entry_time(entry),
    )
    embed.set_footer(text=feed_title)
    for media in entry.get("media_content", []):
        if media.get("medium") == "image" or media.get("type", "").startswith("image"):
            embed.set_image(url=media["url"])
            break
    return embed


class WowBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.session: aiohttp.ClientSession | None = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (WowForeverDiscordBot)"},
            timeout=aiohttp.ClientTimeout(total=30),
        )
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

    async def fetch(self, url: str):
        async with self.session.get(url) as resp:
            resp.raise_for_status()
            body = await resp.read()
        return feedparser.parse(body)

    @tasks.loop(minutes=10)
    async def poll(self):
        channel = self.get_channel(CHANNEL_ID) or await self.fetch_channel(CHANNEL_ID)
        seen = load_seen()
        first_run = seen is None
        seen = seen or []
        seen_set = set(seen)

        new = []
        for url in FEEDS:
            try:
                feed = await self.fetch(url)
            except Exception:
                log.exception("Failed to fetch %s", url)
                continue
            title = feed.feed.get("title", url)
            for entry in feed.entries:
                key = entry.get("id") or entry.get("link")
                if key and key not in seen_set:
                    seen_set.add(key)
                    new.append((entry_time(entry), key, entry, title))

        new.sort(key=lambda x: x[0])  # oldest first so the channel reads chronologically
        to_post = new
        if first_run:
            cutoff = max(len(new) - INITIAL_POSTS, 0)
            seen.extend(key for _, key, _, _ in new[:cutoff])  # mark the backlog as seen without posting
            to_post = new[cutoff:]

        for _, key, entry, title in to_post:
            try:
                await channel.send(embed=build_embed(entry, title))
            except discord.HTTPException:
                log.exception("Failed to post %s", key)
                continue
            seen.append(key)
            save_seen(seen)  # save after each post so a crash never causes duplicates
            await asyncio.sleep(1)

        save_seen(seen)
        if to_post:
            log.info("Posted %d new item(s)", len(to_post))

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
