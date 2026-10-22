"""Shared feed logic: fetch RSS feeds, track what's been posted, and build Discord embeds."""

import asyncio
import html
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp
import discord
import feedparser

FEEDS = [u.strip() for u in os.getenv("FEEDS", "https://www.wowhead.com/news/rss/forever").split(",") if u.strip()]
STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).with_name("seen.json")))
# On first run, only post this many of the newest items instead of the whole backlog.
INITIAL_POSTS = int(os.getenv("INITIAL_POSTS", "3"))
MAX_SEEN = 1000
USER_AGENT = "Mozilla/5.0 (WowForeverDiscordBot)"

log = logging.getLogger("wowbot")


def load_seen() -> list[str] | None:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def save_seen(seen: list[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen[-MAX_SEEN:], indent=0), encoding="utf-8")
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


def make_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=30))


async def fetch(session: aiohttp.ClientSession, url: str):
    async with session.get(url) as resp:
        resp.raise_for_status()
        body = await resp.read()
    return feedparser.parse(body)


async def check_and_post(session: aiohttp.ClientSession, send: Callable[[discord.Embed], Awaitable[object]]) -> int:
    """Post every feed entry not seen before via `send`. Returns how many were posted."""
    seen = load_seen()
    first_run = seen is None
    seen = seen or []
    seen_set = set(seen)

    new = []
    for url in FEEDS:
        try:
            feed = await fetch(session, url)
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

    posted = 0
    for _, key, entry, title in to_post:
        try:
            await send(build_embed(entry, title))
        except discord.HTTPException:
            log.exception("Failed to post %s", key)
            continue
        seen.append(key)
        save_seen(seen)  # save after each post so a crash never causes duplicates
        posted += 1
        await asyncio.sleep(1)

    save_seen(seen)
    if posted:
        log.info("Posted %d new item(s)", posted)
    return posted
