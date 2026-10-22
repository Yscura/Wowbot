"""Shared feed logic: fetch news sources, track what's been posted, and build Discord embeds."""

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
from urllib.parse import urljoin

import aiohttp
import discord
import feedparser

FEEDS = [u.strip() for u in os.getenv("FEEDS", "https://www.wowhead.com/news/rss/forever").split(",") if u.strip()]
WOWFOREVER_NEWS = "https://wowforever.gg/en/news/"
STATE_FILE = Path(os.getenv("STATE_FILE", Path(__file__).with_name("seen.json")))
# The first time a source is seen, only post this many of its newest items instead of the whole backlog.
INITIAL_POSTS = int(os.getenv("INITIAL_POSTS", "3"))
MAX_SEEN = 1000
USER_AGENT = "Mozilla/5.0 (WowForeverDiscordBot)"

log = logging.getLogger("wowbot")


def load_seen() -> list[str]:
    if not STATE_FILE.exists():
        return []
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
    published = entry.get("published", "")
    try:
        return parsedate_to_datetime(published)  # RSS dates
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(published)  # ISO dates, e.g. 2026-10-01
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def build_embed(entry, source_title: str) -> discord.Embed:
    embed = discord.Embed(
        title=entry.get("title", "Untitled")[:256],
        url=entry.get("link"),
        description=clean_text(entry.get("summary", "")),
        color=0x1F8B4C,
        timestamp=entry_time(entry),
    )
    embed.set_footer(text=source_title)
    for media in entry.get("media_content", []):
        if media.get("medium") == "image" or media.get("type", "").startswith("image"):
            embed.set_image(url=media["url"])
            break
    return embed


def parse_rss(body: bytes, url: str):
    feed = feedparser.parse(body)
    return feed.feed.get("title", url), feed.entries


def parse_wowforever(body: bytes, url: str):
    """wowforever.gg has no RSS feed, so read the article cards from its news page."""
    page = body.decode("utf-8", "replace")
    entries = []
    for card in re.findall(r'<article class="card">(.*?)</article>', page, re.S):
        link = re.search(r'<a href="([^"]+)"', card)
        title = re.search(r"<h3>(.*?)</h3>", card, re.S)
        if not (link and title):
            continue
        summary = re.search(r"<p>(.*?)</p>", card, re.S)
        date = re.search(r'<time datetime="([^"]+)"', card)
        image = re.search(r'<img src="([^"]+)"', card)
        entries.append({
            "title": html.unescape(title.group(1).strip()),
            "link": urljoin(url, link.group(1)),
            "summary": summary.group(1) if summary else "",
            "published": date.group(1) if date else "",
            "media_content": [{"url": urljoin(url, image.group(1)), "medium": "image"}] if image else [],
        })
    if not entries:
        log.warning("Found no articles on %s; the page layout may have changed", url)
    return "wowforever.gg", entries


SOURCES = [(url, parse_rss) for url in FEEDS] + [(WOWFOREVER_NEWS, parse_wowforever)]


def make_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=30))


async def fetch(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.read()


async def check_and_post(session: aiohttp.ClientSession, send: Callable[[discord.Embed], Awaitable[object]]) -> int:
    """Post every news item not seen before via `send`. Returns how many were posted."""
    seen = load_seen()
    seen_set = set(seen)

    to_post = []
    for url, parse in SOURCES:
        try:
            title, entries = parse(await fetch(session, url), url)
        except Exception:
            log.exception("Failed to fetch %s", url)
            continue
        keyed = [(entry.get("id") or entry.get("link"), entry) for entry in entries]
        keyed = [(key, entry) for key, entry in keyed if key]
        new = [(entry_time(entry), key, entry, title) for key, entry in keyed if key not in seen_set]
        new.sort(key=lambda x: x[0])
        # A source with nothing in the state yet is new: skip its backlog except the newest few.
        if new and not any(key in seen_set for key, _ in keyed):
            cutoff = max(len(new) - INITIAL_POSTS, 0)
            seen.extend(key for _, key, _, _ in new[:cutoff])
            new = new[cutoff:]
        seen_set.update(key for _, key, _, _ in new)
        to_post.extend(new)

    to_post.sort(key=lambda x: x[0])  # oldest first so the channel reads chronologically

    posted = 0
    for _, key, entry, title in to_post:
        try:
            await send(build_embed(entry, title))
        except discord.DiscordException:
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
