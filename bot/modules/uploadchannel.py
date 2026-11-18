"""Interactive channel creation and serial completed-season RSS backfill.

This module reuses USER_SESSION, AniList relations, persistent anime-channel
mappings, the configured RSS sources, and the existing batch uploader. The
currently releasing season remains owned by the normal scheduled uploader.
"""
import asyncio
import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote_plus

import httpx
from pyrogram import filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ChatPrivileges

from bot import bot, Var, LOGS
from bot.core.database import db
import bot.core.auto_animes as auto_anime_core
from bot.core.auto_animes import process_batch_anime, AUTO_UPLOAD_PRIORITY_LOCK, AUTO_UPLOAD_PRIORITY_PENDING, find_video_files, build_anime_soul_poster, build_main_channel_announcement_caption, build_anime_watch_markup, send_sticker_to_channel
from bot.core.func_utils import getfeeds, encode
from bot.core.tordownload import TorDownloader
from bot.core.ffencoder import FFEncoder
from bot.core.tguploader import TgUploader
from bot.core.text_utils import TextEditor
import os
import shutil
import time
from bot.core.reporter import rep

# Photo and metadata caption are intentionally sent as one Telegram message.
ANILIST_URL = "https://graphql.anilist.co"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
UPLOAD_CHANNEL_SESSIONS = {}
UPLOADCHANNEL_PAUSED = False
UPLOADCHANNEL_ACTIVE_TASKS = set()
LOGS.info("UPLOADCHANNEL MODULE LOADED; /uploadchannel handler registered.")

SCHEDULE_QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage }
    media(type: ANIME, status: RELEASING, sort: POPULARITY_DESC) {
      id
      title { english romaji native }
      synonyms
      bannerImage
      season
      seasonYear
      description(asHtml: false)
      type
      format
      status
      episodes
      duration
      averageScore
      genres
      startDate { year month day }
      endDate { year month day }
      coverImage { extraLarge large }
      studios(isMain: true) { nodes { name } }
      relations {
        edges {
          relationType
          node {
            id
            title { english romaji native }
            synonyms
            format
            status
            episodes
            seasonYear
            relations {
              edges {
                relationType
                node {
                  id
                  title { english romaji native }
                  format
                  status
                  episodes
                  seasonYear
                }
              }
            }
          }
        }
      }
      nextAiringEpisode { airingAt episode timeUntilAiring }
    }
  }
}
"""


def _escape(value):
    return html.escape(str(value or "N/A"))


def _airing_day(media):
    next_ep = media.get("nextAiringEpisode") or {}
    airing_at = next_ep.get("airingAt")
    if not airing_at:
        return None
    return datetime.fromtimestamp(int(airing_at), LOCAL_TZ).strftime("%A")


def _display_title(media):
    titles = media.get("title") or {}
    return titles.get("english") or titles.get("romaji") or titles.get("native") or "Unknown anime"


async def _fetch_currently_airing():
    """Fetch every AniList RELEASING page, not only the first 50 popular titles."""
    timeout = httpx.Timeout(25.0, connect=10.0)
    media = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        page = 1
        while True:
            response = await client.post(
                ANILIST_URL,
                json={"query": SCHEDULE_QUERY, "variables": {"page": page, "perPage": 50}},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                raise RuntimeError("; ".join(str(e.get("message", "AniList error")) for e in payload["errors"]))
            page_data = (payload.get("data") or {}).get("Page") or {}
            media.extend(page_data.get("media") or [])
            if not (page_data.get("pageInfo") or {}).get("hasNextPage"):
                break
            page += 1
    result = []
    seen = set()
    for item in media:
        day = _airing_day(item)
        title = _display_title(item)
        media_id = item.get("id")
        if not day or not title or not media_id or media_id in seen:
            continue
        seen.add(media_id)
        item["_weekday"] = day
        result.append(item)
    return result



def _plain_title(value):
    value = str(value or "").casefold()
    value = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", value)
    value = re.sub(r"\b(?:batch|complete|season pack|full season|720p|1080p|480p|1080|720|480)\b", " ", value)
    value = re.sub(r"\bs\d{1,2}\s*[-._ ]*e?\d{1,3}\b|\bseason\s*\d+\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _season_number_from_release(title):
    patterns = (
        r"\bS0*(\d{1,2})(?:E|\s*[-._ ]*\d)",
        r"\bSeason\s*0*(\d{1,2})\b",
        r"\bS0*(\d{1,2})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, str(title or ""), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _prequel_seasons(media):
    """Return TV prequel ancestors oldest-first, bounded to two relation levels."""
    found = {}

    def walk(parent, depth):
        if depth > 2:
            return
        relations = (parent.get("relations") or {}).get("edges") or []
        for edge in relations:
            if str(edge.get("relationType") or "").upper() != "PREQUEL":
                continue
            node = edge.get("node") or {}
            media_id = node.get("id")
            if not media_id or media_id in found:
                continue
            fmt = str(node.get("format") or "").upper()
            if fmt not in ("TV", "TV_SHORT", "ONA"):
                continue
            found[media_id] = node
            walk(node, depth + 1)

    walk(media, 1)
    return sorted(
        found.values(),
        key=lambda item: (
            int(item.get("seasonYear") or 0),
            int(item.get("id") or 0),
        ),
    )


async def _find_720p_season_pack(media, season_number):
    """Find a clearly labelled complete-season 720p torrent in configured RSS feeds."""
    titles = media.get("title") or {}
    aliases = {
        _plain_title(titles.get(key))
        for key in ("english", "romaji", "native")
        if titles.get(key)
    }
    aliases.discard("")
    if not aliases:
        return None

    # Search Nyaa directly for complete 720p season packs as well as the
    # bot's configured RSS feeds. The query is title/season-specific; entries
    # are still strictly validated below before any download starts.
    search_titles = [
        titles.get(key) for key in ("english", "romaji", "native") if titles.get(key)
    ]
    nyaa_feeds = []
    for search_title in search_titles:
        queries = (
            f'"{search_title}" Season {int(season_number)} 720p Batch',
            f'"{search_title}" S{int(season_number):02d} 720p Batch',
            f'"{search_title}" Season {int(season_number)} 720p Complete',
        )
        nyaa_feeds.extend(
            f"https://nyaa.si/?page=rss&q={quote_plus(query)}&c=1_2&f=0"
            for query in queries
        )

    quality_feed = (getattr(Var, "RSS_LOW_END", {}) or {}).get("720")
    feeds = list(dict.fromkeys(
        url for url in nyaa_feeds + [quality_feed] + list(getattr(Var, "RSS_ITEMS_ANIME", []) or [])
        if url
    ))
    for feed_url in feeds:
        try:
            entries = await getfeeds(feed_url)
        except Exception as exc:
            await rep.report(f"Archive RSS scan failed for {feed_url}: {exc}", "warning", log=False)
            continue
        for entry in entries or []:
            release_title = str(getattr(entry, "title", "") or "")
            # Prefer an actual torrent enclosure. Nyaa's RSS alternate link is
            # usually a /view/<id> HTML page, not a torrent file.
            link = None
            for candidate in (getattr(entry, "links", None) or []):
                href = candidate.get("href") if isinstance(candidate, dict) else None
                rel = str(candidate.get("rel") or "").lower() if isinstance(candidate, dict) else ""
                media_type = str(candidate.get("type") or "").lower() if isinstance(candidate, dict) else ""
                if href and (rel == "enclosure" or "bittorrent" in media_type or "x-bittorrent" in media_type):
                    link = href
                    break
            link = link or getattr(entry, "link", None)
            if not link:
                continue
            nyaa_view = re.match(r"^https?://(?:www\.)?nyaa\.si/view/(\d+)(?:[/?#].*)?$", str(link), re.IGNORECASE)
            if nyaa_view:
                link = f"https://nyaa.si/download/{nyaa_view.group(1)}.torrent"
            title_key = _plain_title(release_title)
            if not title_key or not any(
                alias == title_key or alias in title_key or title_key in alias
                for alias in aliases
            ):
                continue
            # Only a season pack qualifies; single-episode entries cannot
            # accidentally create an incomplete range download.
            is_pack = bool(re.search(
                r"(?i)\b(batch|complete|full[ ._-]*season|season[ ._-]*pack)\b|"
                r"\b(?:01|1)[ ._-]*(?:-|to)[ ._-]*\d{1,3}\b",
                release_title,
            ))
            if not is_pack or not re.search(r"(?i)\b720p\b", release_title):
                continue
            release_season = _season_number_from_release(release_title)
            if release_season is None or release_season != int(season_number):
                continue
            return {"title": release_title, "link": link}
    return None



async def _find_episode_torrent(media, season_number, episode_number):
    """Search Nyaa RSS using AniList titles/synonyms and return only an exact episode match."""
    titles = media.get("title") or {}
    raw_aliases = [
        titles.get(key) for key in ("english", "romaji", "native")
        if titles.get(key)
    ]
    # AniList's synonyms often contain the short Japanese/romanized title
    # used by release groups when the English title is long.
    raw_aliases.extend(media.get("synonyms") or [])
    aliases = []
    for value in raw_aliases:
        value = str(value or "").strip()
        normalized = _plain_title(value)
        if value and normalized and normalized not in {            _plain_title(existing) for existing in aliases        }:            aliases.append(value)    if not aliases:        await rep.report(
            f"AniList supplied no searchable title aliases for anime ID {media.get('id')}.",
            "warning", log=False,
        )
        return None

    season = int(season_number)
    episode = int(episode_number)
    queries = []
    for alias in aliases:
        # Avoid quoted-only searches: Nyaa title punctuation and release-group
        # formatting varies. Keep each query title-specific and episode-specific.
        queries.extend([
            f"{alias} S{season:02d}E{episode:02d} 720p",
            f"{alias} S{season}E{episode:02d} 720p",
            f"{alias} Season {season} Episode {episode} 720p",
            f"{alias} {episode:02d} 720p",
        ])
    feeds = [
        f"https://nyaa.si/?page=rss&q={quote_plus(query)}&c=1_2&f=0"
        for query in queries
    ]
    feeds.append((getattr(Var, "RSS_LOW_END", {}) or {}).get("720"))
    feeds.extend(list(getattr(Var, "RSS_ITEMS_ANIME", []) or []))

    seen_entries = set()
    scanned = 0
    for feed_url in dict.fromkeys(url for url in feeds if url):
        try:
            entries = await getfeeds(feed_url)
        except Exception as exc:
            await rep.report(
                f"Episode RSS scan failed for {feed_url}: {type(exc).__name__}: {exc}",
                "warning", log=False,
            )
            continue

        for entry in entries or []:
            title = str(getattr(entry, "title", "") or "")
            link = getattr(entry, "link", None)
            entry_key = (title.casefold(), str(link or ""))
            if entry_key in seen_entries:
                continue
            seen_entries.add(entry_key)
            scanned += 1

            normalized_release = _plain_title(title)
            # First prefer a full normalized alias match. For abbreviated
            # release titles, permit a strong token overlap only (>=75% of
            # a meaningful alias and at least four matching words).
            title_match = any(
                _plain_title(alias) in normalized_release
                for alias in aliases
                if _plain_title(alias)
            )
            if not title_match:
                release_tokens = set(normalized_release.split())
                for alias in aliases:
                    alias_tokens = {
                        token for token in _plain_title(alias).split()
                        if len(token) > 2 and token not in {
                            "the", "and", "for", "with", "from", "that", "this",
                            "her", "his", "their", "you", "your", "into",
                        }
                    }
                    if len(alias_tokens) >= 4:
                        overlap = len(alias_tokens & release_tokens)
                        if overlap >= 4 and overlap / len(alias_tokens) >= 0.75:
                            title_match = True
                            break
            if not title_match:
                continue

            ep_patterns = (
                rf"(?i)\bS0*{season}[\s._-]*E0*{episode}\b",
                rf"(?i)\b{season}x0*{episode}\b",
                rf"(?i)\bSeason[\s._-]*0*{season}[\s._-]*Episode[\s._-]*0*{episode}\b",
                rf"(?i)\bEpisode[\s._-]*0*{episode}\b",
                rf"(?i)\bEp[\s._-]*0*{episode}\b",
            )
            episode_match = any(re.search(pattern, title) for pattern in ep_patterns)
            # Many Season 1 releases are numbered simply "- 01". Accept that
            # format only for S1 and only after a strong anime-title match.
            if season == 1 and not episode_match:
                episode_match = bool(re.search(
                    rf"(?i)(?:^|[\s._\-\[])0*{episode}(?:[\s._\-\]\[]|$)",
                    title,
                ))
            if not episode_match:
                continue

            detected_season = _season_number_from_release(title)
            if detected_season is not None and detected_season != season:
                continue
            if season > 1 and detected_season != season:
                continue

            torrent_link = None
            for candidate in (getattr(entry, "links", None) or []):
                href = candidate.get("href") if isinstance(candidate, dict) else None
                rel = str(candidate.get("rel") or "").lower() if isinstance(candidate, dict) else ""
                content_type = str(candidate.get("type") or "").lower() if isinstance(candidate, dict) else ""
                if href and (
                    rel == "enclosure"
                    or "bittorrent" in content_type
                    or "x-bittorrent" in content_type
                ):
                    torrent_link = href
                    break
            torrent_link = torrent_link or link
            if not torrent_link:
                continue

            match = re.match(
                r"^https?://(?:www\.)?nyaa\.si/view/(\d+)",
                str(torrent_link), re.I,
            )
            if match:
                torrent_link = f"https://nyaa.si/download/{match.group(1)}.torrent"
            return {"title": title, "link": str(torrent_link)}

    await rep.report(
        f"No exact RSS torrent match for AniList anime {media.get('id')} "
        f"Season {season} Episode {episode}; searched {len(aliases)} title aliases "
        f"across {len(dict.fromkeys(url for url in feeds if url))} feeds "
        f"and inspected {scanned} unique entries. No upload was started.",
        "warning", log=False,
    )
    return None


async def _upload_single_episode(media, destination_id, season_number, episode_number, destination_client):
    """Download one RSS episode, encode four qualities, and publish four buttons."""
    from bot import Var
    episode = await _find_episode_torrent(media, season_number, episode_number)
    if not episode:
        raise RuntimeError(
            f"RSS source missing or ambiguous for Season {season_number} Episode {episode_number}; "
            "stopping to avoid uploading the wrong episode."
        )
    title = _display_title(media)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{media.get('id')}_{season_number}_{episode_number}")
    workdir = os.path.join("downloads", "uc_episode_" + safe)
    os.makedirs(workdir, exist_ok=True)
    info = TextEditor(title)
    # The archive relation objects in AniList contain only partial metadata.
    # Fetch the exact media by AniList ID instead of searching by title again;
    # this prevents a similarly named season/remake from supplying the artwork
    # or synopsis for the wrong anime.
    details_query = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        id
        title { english romaji native }
        description(asHtml: false)
        coverImage { extraLarge large }
        bannerImage
        genres
        status
        format
        season
        seasonYear
        episodes
        averageScore
        duration
        startDate { year month day }
        endDate { year month day }
        studios(isMain: true) { nodes { name } }
      }
    }
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as metadata_client:
            metadata_response = await metadata_client.post(
                ANILIST_URL,
                json={"query": details_query, "variables": {"id": int(media.get("id") or 0)}},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            metadata_response.raise_for_status()
            exact_metadata = (metadata_response.json().get("data") or {}).get("Media") or {}
        if not exact_metadata or int(exact_metadata.get("id") or 0) != int(media.get("id") or 0):
            raise RuntimeError("AniList did not return the exact requested media ID.")
        info.adata = exact_metadata
    except Exception as metadata_error:
        raise RuntimeError(
            f"Could not retrieve exact AniList artwork/synopsis for {title} "
            f"(AniList ID {media.get('id')}): {metadata_error}. Refusing to publish an unverified thumbnail."
        ) from metadata_error
    info.pdata["anime_title"] = title
    info.pdata["episode_number"] = str(episode_number).zfill(2)
    info.pdata["anime_season"] = str(season_number).zfill(2)
    info.pdata["audio_type"] = (
        "Dual" if re.search(r"(?i)\bdual\b", episode["title"])
        else "Dub" if re.search(r"(?i)\b(dub|english)\b", episode["title"])
        else "Japanese [English Sub]"
    )
    # Episode cards are posted by the uploader, but all file-sharing deep
    # links must open the existing Monkey file-share bot. Do not derive this
    # username from BOT_TOKEN: that token belongs to Auto Anime Uploader.
    share_bot_username = str(
        getattr(Var, "BOT_USERNAME", "Orewa_Monkey_D_Kaizuko_Bot") or ""
    ).strip().lstrip("@")
    expected_share_bot = "Orewa_Monkey_D_Kaizuko_Bot"
    if share_bot_username.casefold() != expected_share_bot.casefold():
        raise RuntimeError(
            "File-share bot mismatch: expected @Orewa_Monkey_D_Kaizuko_Bot "
            f"but BOT_USERNAME is @{share_bot_username or 'not configured'}. "
            "Episode quality buttons were not created to avoid sending users to the wrong bot."
        )
    quality_order = ("720", "1080", "480", "HDRi")
    buttons_by_quality = {}

    # Recover files by parsed season/episode/quality/title rather than exact
    # generated filenames. The regular uploader stores names such as
    # [S01-07] Anime Title [480p] ... which do not exactly equal TextEditor
    # upload names used by this channel workflow.
    import difflib

    def _plain_storage_name(value):
        value = html.unescape(str(value or ""))
        value = re.sub(r"<[^>]+>", "", value)
        return os.path.basename(value).strip()

    def _normal_storage_title(value):
        value = _plain_storage_name(value).casefold()
        value = re.sub(r"\[[^\]]*\]", " ", value)
        value = re.sub(r"\b(?:480|720|1080|144|240)p\b|\bhdrip\b|\bhdr[iI]\b", " ", value)
        value = re.sub(r"\bs\d{1,2}e\d{1,3}\b|\bs\d{1,2}\s*[- ]\s*e?\d{1,3}\b", " ", value)
        value = re.sub(r"\bs\d{1,2}[- ]\d{1,3}\b", " ", value)
        value = re.sub(r"@[^\s]+", " ", value)
        value = re.sub(r"\.(mkv|mp4|avi|webm)$", " ", value)
        return re.sub(r"[^a-z0-9]+", " ", value).strip()

    def _parse_storage_record(value):
        raw = _plain_storage_name(value)
        ep_match = re.search(r"\[?s(\d{1,2})\s*[- _]\s*e?(\d{1,3})\]?", raw, re.I)
        if not ep_match:
            return None
        stored_season, stored_episode = int(ep_match.group(1)), int(ep_match.group(2))
        if re.search(r"\bhdrip\b|\[hdri\]", raw, re.I):
            stored_quality = "HDRi"
        else:
            quality_match = re.search(r"\[(144|240|480|720|1080)p\]", raw, re.I)
            if not quality_match:
                return None
            stored_quality = quality_match.group(1)
        stored_title = raw[ep_match.end():]
        stored_title = re.split(
            r"\[(?:144|240|480|720|1080)p\]|\[hdrip\]|\[hdri\]",
            stored_title,
            flags=re.I,
        )[0]
        return stored_season, stored_episode, stored_quality, _normal_storage_title(stored_title)

    title_aliases = [title]
    metadata_titles = media.get("title") or {}
    if isinstance(metadata_titles, dict):
        title_aliases.extend(metadata_titles.get(key) for key in ("english", "romaji", "native"))
    title_aliases.extend(media.get("synonyms") or [])
    target_titles = {_normal_storage_title(value) for value in title_aliases if value}
    target_titles.discard("")
    target_season = int(season_number)
    target_episode = int(episode_number)

    storage_records = []
    storage_chat_id = None
    # Explicit storage invite supplied by the owner; do not silently scan a stale FILE_STORE_LINK from Heroku.
    storage_link = "https://t.me/+Rn-HvMOx3ho5ZWVl"
    try:
        # FILE_STORE is a separate channel from the per-anime destination.
        # A fresh in-memory USER_SESSION may not know its numeric peer ID, and
        # it cannot read channel history until it has joined the storage channel.
        # Resolve/join it through the configured public username or invite link.
        if storage_link:
            try:
                storage_chat = await destination_client.join_chat(storage_link)
            except Exception as join_error:
                join_text = str(join_error).casefold()
                if "user_already_participant" in join_text or "already a participant" in join_text:
                    storage_chat = await destination_client.get_chat(storage_link)
                else:
                    # If already joined, get_chat(link) succeeds; if not, surface
                    # the original join failure rather than silently using a stale ID.
                    try:
                        storage_chat = await destination_client.get_chat(storage_link)
                    except Exception:
                        raise join_error
            if not storage_chat or not getattr(storage_chat, "id", None):
                raise RuntimeError("FILE_STORE_LINK did not resolve to a Telegram channel.")
            storage_chat_id = int(storage_chat.id)
            configured_storage_id = int(getattr(Var, "FILE_STORE", 0) or 0)
            if configured_storage_id and storage_chat_id != configured_storage_id:
                await rep.report(
                    f"FILE_STORE_LINK resolved to {storage_chat_id}, while FILE_STORE is "
                    f"{configured_storage_id}. Using the channel resolved from FILE_STORE_LINK "
                    "for safe existing-file reuse.",
                    "warning",                    log=False,                )
        # The existing Monkey file-share bot is already an administrator
        # in this storage channel. Do not promote Auto Anime Uploader here:        # its BOT_TOKEN is only used to publish episode cards, not to serve files.
        scanned_storage_messages = 0        async for stored in destination_client.get_chat_history(storage_chat_id, limit=5000):
            scanned_storage_messages += 1
            media_obj = getattr(stored, "video", None) or getattr(stored, "document", None)
            media_name = getattr(media_obj, "file_name", None) if media_obj else None
            # Captions contain the canonical [Sxx-yy] title [quality] tags in
            # this storage channel; prefer them over shortened/truncated names.
            parsed = _parse_storage_record(getattr(stored, "caption", None)) or _parse_storage_record(media_name)
            if parsed:
                storage_records.append((parsed, int(stored.id)))
        await rep.report(
            f"FILE_STORE reuse scan resolved channel {storage_chat_id}; "
            f"scanned {scanned_storage_messages} messages and indexed {len(storage_records)} episode-quality files.",
            "info",
            log=False,
        )
    except Exception as storage_error:
        # Do not blindly upload again when the USER_SESSION cannot join/read
        # the actual storage channel: duplicate prevention must fail closed.
        raise RuntimeError(
            f"USER_SESSION could not join or read FILE_STORE "
            f"({storage_link or Var.FILE_STORE}) for {title} "
            f"S{target_season:02d}E{target_episode:02d}; refusing duplicate uploads. "
            f"Check that FILE_STORE_LINK is the real storage channel invite/public link "
            f"and that this USER_SESSION account is allowed to join it: {storage_error}"
        ) from storage_error

    def _find_stored_quality(quality):
        wanted_quality = "HDRi" if quality.casefold() in ("hdri", "hdrip") else re.sub(r"p$", "", quality.casefold())
        for (stored_season, stored_episode, stored_quality, stored_title), stored_id in storage_records:
            if (stored_season, stored_episode) != (target_season, target_episode):
                continue
            if stored_quality.casefold() != wanted_quality.casefold() or not stored_title:
                continue
            for wanted_title in target_titles:
                if stored_title == wanted_title:
                    return stored_id
                wanted_tokens = set(wanted_title.split())
                stored_tokens = set(stored_title.split())
                overlap = len(wanted_tokens & stored_tokens) / max(1, min(len(wanted_tokens), len(stored_tokens)))
                similarity = difflib.SequenceMatcher(None, wanted_title, stored_title).ratio()
                if overlap >= 0.75 or similarity >= 0.82:
                    return stored_id
        return None

    missing_qualities = []
    for quality in quality_order:
        stored_id = _find_stored_quality(quality)
        if stored_id:
            try:
                stored_msg = await destination_client.get_messages(storage_chat_id, message_ids=int(stored_id))
                if stored_msg and not getattr(stored_msg, "empty", False):
                    # Match the proven /addanime selected-channel workflow:
                    # Monkey receives the standard get-<message_id * FILE_STORE>
                    # payload and copies the original storage message with its
                    # existing Share URL markup.
                    start_payload = f"get-{int(stored_id) * abs(int(Var.FILE_STORE))}"
                    url = f"https://t.me/{share_bot_username}?start={await encode(start_payload)}"
                    buttons_by_quality[quality] = InlineKeyboardButton(
                        f"{quality}p" if quality != "HDRi" else "HDRip", url=url
                    )
                    await rep.report(
                        f"Reusing existing {quality} for {title} "
                        f"S{target_season:02d}E{target_episode:02d} from FILE_STORE message {stored_id}; "
                        "skipping download/encode/upload.",
                        "info",
                        log=False,
                    )
                    continue
            except Exception as verify_error:
                raise RuntimeError(
                    f"Found existing {quality} file {stored_id} for {title} "
                    f"S{target_season:02d}E{target_episode:02d}, but could not verify it; "
                    f"refusing a duplicate upload: {verify_error}"
                ) from verify_error
        await rep.report(
            f"No reusable {quality} match found in FILE_STORE for {title} "
            f"S{target_season:02d}E{target_episode:02d}; this quality will be uploaded.",
            "warning",
            log=False,
        )
        missing_qualities.append(quality)

    source = None
    if missing_qualities:
        downloaded = await TorDownloader(workdir).download(episode["link"])
        if not downloaded or not os.path.exists(downloaded):
            raise RuntimeError(f"Torrent download failed for {title} S{season_number}E{episode_number}.")
        video_files = find_video_files(os.path.dirname(downloaded) if os.path.isfile(downloaded) else downloaded)
        if len(video_files) != 1:
            raise RuntimeError(
                f"Expected exactly one video for {title} S{season_number}E{episode_number}; found {len(video_files)}."
            )
        source = video_files[0]

    for quality in missing_qualities:
        upload_name = await info.get_upname(quality)
        quality_source = os.path.join("encode", f"uc_source_{safe}_{quality}.mkv")
        os.makedirs("encode", exist_ok=True)
        shutil.copy2(source, quality_source)
        encode_status = await bot.send_message(
            Var.LOG_CHANNEL,
            f"<b>Encoding:</b> {_escape(title)} S{season_number}E{episode_number} — {quality}p",
        )
        encoded_path = await FFEncoder(
            encode_status, quality_source, upload_name, quality
        ).start_encode()

        # A custom per-anime FFmpeg command can fail on a particular source
        # (e.g. missing subtitle/audio streams). Retry once with the repository's
        # known default quality command; preserve all earlier successful quality
        # uploads and do not restart the episode.
        if not encoded_path or not os.path.exists(encoded_path):
            partial_output = os.path.join("encode", "ffanimeadvout.mkv")
            if os.path.exists(partial_output):
                os.remove(partial_output)
            await rep.report(
                f"Custom FFmpeg encode failed for S{season_number}E{episode_number} at {quality}; "
                "retrying once with the safe default FFmpeg profile.",
                "warning",
                log=False,
            )
            encoded_path = await FFEncoder(
                encode_status, quality_source, upload_name, quality, force_default=True
            ).start_encode()

        if not encoded_path or not os.path.exists(encoded_path):
            raise RuntimeError(
                f"Encoding failed for S{season_number}E{episode_number} at {quality}, "
                "including the safe default FFmpeg retry. Earlier uploaded qualities remain saved."
            )
        uploaded = await TgUploader(await bot.send_message(
            Var.LOG_CHANNEL, f"<b>Uploading:</b> {_escape(title)} S{season_number}E{episode_number} — {quality}p"
        )).upload(encoded_path, quality)
        if not uploaded:
            raise RuntimeError(f"FILE_STORE upload failed for S{season_number}E{episode_number} at {quality}.")
        # Use the same single-file deep-link format as /addanime. Monkey
        # already has access to FILE_STORE and resolves this scaled message ID.
        start_payload = f"get-{int(uploaded.id) * abs(int(Var.FILE_STORE))}"
        url = f"https://t.me/{share_bot_username}?start={await encode(start_payload)}"
        buttons_by_quality[quality] = InlineKeyboardButton(f"{quality}p" if quality != "HDRi" else "HDRip", url=url)

    available_qualities = {quality: True for quality in buttons_by_quality}
    caption = _build_anime_info_caption(
        info, title, season_number, episode_number, available_qualities
    )
    keyboard_rows = [
        [buttons_by_quality[q] for q in ("480", "720") if q in buttons_by_quality],
        [buttons_by_quality[q] for q in ("1080", "HDRi") if q in buttons_by_quality],
    ]
    keyboard_rows = [row for row in keyboard_rows if row]
    if len(buttons_by_quality) != 4 or not keyboard_rows:
        raise RuntimeError(
            f"Refusing to publish {title} S{season_number}E{episode_number}: "
            f"only {len(buttons_by_quality)}/4 quality buttons were prepared."
        )
    keyboard = InlineKeyboardMarkup(keyboard_rows)

    # Generate the same ANIME SOUL 16:9 episode artwork used by the normal
    # uploader. Do not post progress, season-info, or sticker messages in the
    # anime channel: publish exactly one final photo/caption/buttons per episode,
    # and only after every configured quality has finished uploading.
    poster_path = os.path.join(workdir, f"anime_soul_s{season_number}e{episode_number}.jpg")
    poster = await build_anime_soul_poster(
        info, title, poster_path, episode_label=str(episode_number).zfill(2)
    )
    if poster and os.path.exists(poster):
        with open(poster, "rb") as photo_file:
            # Inline keyboards are a Bot API feature. USER_SESSION creates
            # channels and manages their permissions, but the actual episode
            # card must be posted by the promoted BOT_TOKEN account.
            published = await bot.send_photo(
                chat_id=destination_id,
                photo=photo_file,
                caption=caption,
                reply_markup=keyboard,
            )
    else:
        cover = (info.adata.get("coverImage") or {}).get("extraLarge") or (info.adata.get("coverImage") or {}).get("large")
        if not cover:
            raise RuntimeError(
                f"Could not generate episode poster or find fallback artwork for {title} S{season_number}E{episode_number}."
            )
        published = await bot.send_photo(
            chat_id=destination_id,
            photo=cover,
            caption=caption,
            reply_markup=keyboard,
        )
    if not published or not getattr(published, "id", None):
        raise RuntimeError(
            f"Telegram did not confirm the final episode post for {title} S{season_number}E{episode_number}."
        )

    # Publish the matching Main Channel announcement for each dedicated-channel episode.
    main_post = None
    if int(destination_id) != int(Var.MAIN_CHANNEL):
        try:
            watch_markup = await build_anime_watch_markup(destination_id)
            if poster and os.path.exists(poster):
                with open(poster, "rb") as photo_file:
                    main_post = await bot.send_photo(
                        chat_id=Var.MAIN_CHANNEL, photo=photo_file,
                        caption=caption, reply_markup=watch_markup,
                    )
            else:
                main_post = await bot.send_photo(
                    chat_id=Var.MAIN_CHANNEL, photo=cover,
                    caption=caption, reply_markup=watch_markup,
                )
            if not main_post or not getattr(main_post, "id", None):
                raise RuntimeError("Telegram did not confirm the Main Channel announcement.")
        except Exception as main_post_error:
            await rep.report(
                f"Dedicated episode card was published, but Main Channel announcement failed "
                f"for {title} S{season_number}E{episode_number}: {main_post_error}",
                "error",
            )

    # The priority Ongoing Anime Time sticker follows both episode posts.
    sticker_sent = await send_sticker_to_channel(
        destination_id, getattr(Var, "STICKER_ID", None), client=destination_client,
    )
    if main_post:
        await asyncio.sleep(max(0, getattr(Var, "STICKER_INTERVAL", 1)))
        main_sticker_sent = await send_sticker_to_channel(Var.MAIN_CHANNEL, getattr(Var, "STICKER_ID", None))
        if not main_sticker_sent:
            await rep.report(
                f"Main Channel priority sticker failed for {title} S{season_number}E{episode_number}.",
                "warning", log=False,
            )
    if not sticker_sent:
        # The episode card and all four file links are already live. A sticker
        # API/session error must never mark the episode upload as failed or stop
        # the remaining scheduled episodes; report it separately for retry.
        await rep.report(
            f"Episode card published successfully for {title} S{season_number}E{episode_number}, "
            "but the Ongoing Anime Time sticker could not be sent. "
            "The episode remains successful; check sticker diagnostics and retry the sticker only.",
            "warning",
            log=False,
        )
    else:
        await rep.report(
            f"Published {title} S{season_number}E{episode_number} with four quality buttons, "
            "AniList-ID-verified artwork/synopsis, and the Ongoing Anime Time sticker.",
            "info",
            log=False,
        )
    shutil.rmtree(workdir, ignore_errors=True)
    return True


async def _upload_episode_with_priority_retry(media, destination_id, season_number,
                                               episode_number, destination_client,
                                               context="archive"):
    """Keep the current episode durable; yield between retries to new ongoing releases."""
    attempt = 0
    while True:
        while (
            AUTO_UPLOAD_PRIORITY_PENDING.is_set()
            or time.monotonic() < auto_anime_core.AUTO_UPLOAD_PRIORITY_COOLDOWN_UNTIL
        ):
            await asyncio.sleep(5)

        await AUTO_UPLOAD_PRIORITY_LOCK.acquire()
        try:
            await _upload_single_episode(
                media, destination_id, season_number, episode_number, destination_client
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as episode_error:
            attempt += 1
            await rep.report(
                f"{context}: Season {season_number} Episode {episode_number} failed "
                f"on attempt {attempt}; the queue will retain this exact episode and retry "
                f"without skipping ahead. New ongoing releases can take priority meanwhile. "
                f"{type(episode_error).__name__}: {episode_error}",
                "error",
            )
        finally:
            AUTO_UPLOAD_PRIORITY_LOCK.release()

        # Back off in short intervals rather than sleeping in one long block.
        # This lets the priority scanner interrupt archive work as soon as a
        # scheduled episode is detected.
        for _ in range(12):
            if AUTO_UPLOAD_PRIORITY_PENDING.is_set():
                break
            await asyncio.sleep(5)


ARCHIVE_RESUME_LOCK = asyncio.Lock()
ARCHIVE_RESUME_STARTED = False

async def resume_archive_batches():
    """Resume persisted /uploadchannel season batches after a dyno restart."""
    if UPLOADCHANNEL_PAUSED:
        return
    recovery_task = asyncio.current_task()    if recovery_task:
        UPLOADCHANNEL_ACTIVE_TASKS.add(recovery_task)
    global ARCHIVE_RESUME_STARTED
    if ARCHIVE_RESUME_STARTED:
        return
    ARCHIVE_RESUME_STARTED = True
    pending = await db.get_in_progress_anime_archive_batches()
    if not pending:
        return
    if not getattr(Var, "USER_SESSION", None):
        await rep.report("Archive recovery is waiting: USER_SESSION is not configured.", "error")
        return

    user_client = __import__("pyrogram").Client(
        "autonew_archive_recovery",
        api_id=int(Var.API_ID),
        api_hash=Var.API_HASH,
        session_string=Var.USER_SESSION,
        in_memory=True,
        parse_mode=ParseMode.HTML,
    )
    try:
        await user_client.start()
        await rep.report(
            f"Restart recovery found {len(pending)} unfinished /uploadchannel season batch(es).",
            "info",
        )
        query = """
        query ($id: Int) {
          Media(id: $id, type: ANIME) {
            id
            title { english romaji native }
            synonyms
            episodes
            seasonYear
            format
            status
          }
        }
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=10.0)) as session:
            for batch in pending:
                anime_id = int(batch.get("anime_id") or 0)
                season_index = int(batch.get("season_number") or 0)
                destination_id = int(batch.get("channel_id") or 0)
                if not anime_id or not season_index or not destination_id:
                    continue
                key = f"{anime_id}:{season_index}:{destination_id}"
                if ARCHIVE_RESUME_LOCK.locked():
                    await ARCHIVE_RESUME_LOCK.acquire()
                    ARCHIVE_RESUME_LOCK.release()
                async with ARCHIVE_RESUME_LOCK:
                    try:
                        response = await session.post(
                            ANILIST_URL,
                            json={"query": query, "variables": {"id": anime_id}},
                            headers={"Accept": "application/json", "Content-Type": "application/json"},
                        )
                        response.raise_for_status()
                        payload = response.json()
                        media = (payload.get("data") or {}).get("Media") or {}
                        if int(media.get("id") or 0) != anime_id:
                            raise RuntimeError(f"AniList did not return exact media ID {anime_id}.")
                        episode_total = int(media.get("episodes") or 0)
                        if episode_total < 1:
                            raise RuntimeError(f"AniList episode count is unavailable for archive ID {anime_id}.")
                        title = _display_title(media)
                        invite = await db.get_anime_channel_invite_link(title)
                        if invite:
                            try:
                                chat = await user_client.get_chat(invite)
                            except Exception:
                                chat = await user_client.join_chat(invite)
                        else:
                            chat = await user_client.get_chat(destination_id)
                        if not chat or int(chat.id) != destination_id:
                            raise RuntimeError(
                                f"Saved channel reference for {title} resolved to the wrong destination."
                            )

                        existing_episodes = set()
                        async for message in user_client.get_chat_history(destination_id, limit=5000):
                            body = " ".join(str(x or "") for x in (
                                getattr(message, "text", ""), getattr(message, "caption", "")
                            ))
                            season_match = re.search(r"(?i)\bSeason\s*0*(\d+)\b", body)
                            episode_match = re.search(r"(?i)\bEpisode\s*[:#-]?\s*0*(\d+)\b", body)
                            compact_match = re.search(r"(?i)\[S0*(\d+)[-_]0*(\d+)\]", body)
                            if season_match and episode_match and int(season_match.group(1)) == season_index:
                                existing_episodes.add(int(episode_match.group(1)))
                            elif compact_match and int(compact_match.group(1)) == season_index:
                                existing_episodes.add(int(compact_match.group(2)))

                        await rep.report(
                            f"Resuming {title} Season {season_index} in {destination_id}; "
                            f"{len(existing_episodes)} episode card(s) already exist.",
                            "info",
                        )
                        for episode_number in range(1, episode_total + 1):
                            if episode_number in existing_episodes:
                                continue
                            await _upload_episode_with_priority_retry(
                                media, destination_id, season_index, episode_number,
                                user_client, context="Restart-recovered /uploadchannel batch",
                            )
                            existing_episodes.add(episode_number)
                        await db.set_anime_archive_batch(
                            anime_id, season_index, destination_id, "complete",
                            "", f"restart-recovered S{season_index} 4-quality",
                        )
                        await rep.report(f"Restart recovery completed {title} Season {season_index}.", "success")
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        await rep.report(
                            f"Archive recovery will retry after the next restart for batch {key}: {error}",
                            "error",
                        )
    finally:
        if recovery_task:
            UPLOADCHANNEL_ACTIVE_TASKS.discard(recovery_task)
        try:
            await user_client.stop()
        except Exception:
            pass


def _build_anime_info_caption(info, title, season_number, episode_number, qualities):
    """Rich expandable anime metadata caption matching the approved info-card layout."""
    data = getattr(info, "adata", {}) or {}
    titles = data.get("title") or {}
    display_title = titles.get("english") or titles.get("romaji") or title
    genres = ", ".join(str(x) for x in (data.get("genres") or [])[:8]) or "Not listed"
    media_type = data.get("format") or data.get("type") or "Anime"
    score = data.get("averageScore")
    rating = f"{float(score) / 10:.1f}/10" if score is not None else "Not rated"
    status = str(data.get("status") or "Unknown").replace("_", " ")
    start = data.get("startDate") or {}
    end = data.get("endDate") or {}
    first_aired = "-".join(str(start.get(k)).zfill(2) for k in ("year", "month", "day") if start.get(k)) or "Unknown"
    last_aired = "Ongoing" if status.upper() == "RELEASING" else ("-".join(str(end.get(k)).zfill(2) for k in ("year", "month", "day") if end.get(k)) or "Unknown")
    runtime = f"{data.get('duration')} minutes" if data.get("duration") else "Unknown"
    episodes = data.get("episodes") or "Unknown"
    studios = (data.get("studios") or {}).get("nodes") or []
    studio = ", ".join(str(x.get("name")) for x in studios if x.get("name")) or "Not listed"
    description = re.sub(r"<[^>]+>", "", str(data.get("description") or ""))
    description = re.sub(r"\s+", " ", description).strip()
    if len(description) > 150:
        description = description[:147].rsplit(" ", 1)[0] + "..."
    qualities_text = ", ".join(label for key, label in (("480", "480p"), ("720", "720p"), ("1080", "1080p"), ("HDRi", "HDRip")) if key in (qualities or {}))
    synopsis = _escape(description) if description else "Synopsis unavailable"
    return (
        f"<b>{_escape(display_title)} Season {int(season_number)}</b>\n\n"
        f"<b>Genres:</b> {_escape(genres)}\n<b>Type:</b> {_escape(media_type)}\n"
        f"<b>Average Rating:</b> {_escape(rating)}\n<b>Status:</b> {_escape(status)}\n"
        f"<b>First aired:</b> {_escape(first_aired)}\n<b>Last aired:</b> {_escape(last_aired)}\n"
        f"<b>Runtime:</b> {_escape(runtime)}\n<b>No. of episodes:</b> {_escape(episodes)}\n"
        f"<b>Studio:</b> {_escape(studio)}\n<b>Episode:</b> {str(episode_number).zfill(2)}  |  <b>Quality:</b> {_escape(qualities_text)}\n\n"
        f"<blockquote expandable>▸ <b>Synopsis:</b> <i>{synopsis}</i></blockquote>"
    )


def _build_channel_intro_caption(media):
    """Metadata-only ANIME SOUL welcome card; deliberately has no episode number."""
    titles = media.get("title") or {}
    title = titles.get("english") or titles.get("romaji") or titles.get("native") or "Unknown anime"
    genres = ", ".join(str(x) for x in (media.get("genres") or [])[:8]) or "Not listed"
    media_type = media.get("format") or media.get("type") or "Anime"
    score = media.get("averageScore")
    rating = f"{float(score) / 10:.1f}/10" if score is not None else "Not rated"
    status = str(media.get("status") or "Unknown").replace("_", " ")
    start = media.get("startDate") or {}
    end = media.get("endDate") or {}
    first_aired = "-".join(str(start.get(k)).zfill(2) for k in ("year", "month", "day") if start.get(k)) or "Unknown"
    last_aired = "Ongoing" if status.upper() == "RELEASING" else ("-".join(str(end.get(k)).zfill(2) for k in ("year", "month", "day") if end.get(k)) or "Unknown")
    runtime = f"{media.get('duration')} minutes" if media.get("duration") else "Unknown"
    episodes = media.get("episodes") or "Unknown"
    studios = (media.get("studios") or {}).get("nodes") or []
    studio = ", ".join(str(x.get("name")) for x in studios if x.get("name")) or "Not listed"
    description = re.sub(r"<[^>]+>", "", str(media.get("description") or ""))
    description = re.sub(r"\s+", " ", description).strip()
    if len(description) > 150:
        description = description[:147].rsplit(" ", 1)[0] + "..."
    synopsis = _escape(description) if description else "Synopsis unavailable"
    # Invisible marker lets later runs detect the one-time intro card without
    # adding visible control text to the channel caption.
    marker = "\u2063AS_CHANNEL_INTRO\u2063"
    return (
        f"<b>{_escape(title)}</b>\n\n"
        f"<b>Genres:</b> {_escape(genres)}\n<b>Type:</b> {_escape(media_type)}\n"
        f"<b>Average Rating:</b> {_escape(rating)}\n<b>Status:</b> {_escape(status)}\n"
        f"<b>First aired:</b> {_escape(first_aired)}\n<b>Last aired:</b> {_escape(last_aired)}\n"
        f"<b>Runtime:</b> {_escape(runtime)}\n<b>No. of episodes:</b> {_escape(episodes)}\n"
        f"<b>Studio:</b> {_escape(studio)}\n\n"
        f"<blockquote expandable>▸ <b>Synopsis:</b> <i>{synopsis}</i></blockquote>{marker}"
    )


def _selection_keyboard(items, selected):
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    rows = []
    for day in weekdays:
        indices = [i for i, item in enumerate(items) if item["_weekday"] == day]
        if not indices:
            continue
        rows.append([InlineKeyboardButton(f"— {day} —", callback_data="uc:noop")])
        for offset in range(0, len(indices), 2):
            row = []
            for index in indices[offset:offset + 2]:
                title = _display_title(items[index])
                if len(title) > 28:
                    title = title[:25] + "..."
                prefix = "✅ " if index in selected else "▫️ "
                row.append(InlineKeyboardButton(prefix + title, callback_data=f"uc:toggle:{index}"))
            rows.append(row)
    rows.append([
        InlineKeyboardButton(f"Create {len(selected)} channel(s)", callback_data="uc:create"),
        InlineKeyboardButton("Cancel", callback_data="uc:cancel"),
    ])
    return InlineKeyboardMarkup(rows)



@bot.on_message(filters.command("stopuploadchannel") & filters.private)
async def stopuploadchannel_command(client, message):
    global UPLOADCHANNEL_PAUSED
    if not message.from_user or message.from_user.id not in Var.ADMINS:
        return await message.reply("⛔ This command is restricted to bot administrators.")
    UPLOADCHANNEL_PAUSED = True
    current = asyncio.current_task()
    tasks = [task for task in list(UPLOADCHANNEL_ACTIVE_TASKS) if task is not current and not task.done()]
    for task in tasks:
        task.cancel()
    await message.reply(f"🛑 /uploadchannel is PAUSED. Cancelled {len(tasks)} active task(s). New jobs and restart recovery are blocked.")


@bot.on_message(filters.command("resumeuploadchannel") & filters.private)
async def resumeuploadchannel_command(client, message):
    global UPLOADCHANNEL_PAUSED
    if not message.from_user or message.from_user.id not in Var.ADMINS:
        return await message.reply("⛔ This command is restricted to bot administrators.")
    UPLOADCHANNEL_PAUSED = False
    await message.reply("▶️ /uploadchannel has been resumed. New jobs and recovery may run again.")


@bot.on_message(filters.command("setchannelinvite") & filters.private)
async def setchannelinvite_command(client, message):
    if not message.from_user or message.from_user.id not in Var.ADMINS:
        return await message.reply("⛔ This command is restricted to bot administrators.")
    raw = message.text.split(None, 1)
    if len(raw) < 2 or "|" not in raw[1]:
        return await message.reply(
            "Usage: /setchannelinvite Anime Title | https://t.me/+INVITE_LINK"
        )
    anime_title, invite_link = [part.strip() for part in raw[1].split("|", 1)]
    if not anime_title or not re.match(r"^https?://t\.me/(?:\+|joinchat/)", invite_link):
        return await message.reply("Send the exact private channel invite link, not a channel ID.")
    user_client = __import__("pyrogram").Client(
        "autonew_channel_creator",
        api_id=int(Var.API_ID),
        api_hash=Var.API_HASH,
        session_string=Var.USER_SESSION,
        in_memory=True,
        parse_mode=ParseMode.HTML,
    )
    try:
        await user_client.start()
        chat = await user_client.get_chat(invite_link)
        if not chat or not getattr(chat, "id", None) or not str(getattr(chat, "type", "")).lower().endswith("channel"):
            return await message.reply("That invite link did not resolve to a channel.")
        await db.add_anime_channel_mapping(anime_title, chat.id)
        await db.set_anime_channel_invite_link(anime_title, chat.id, invite_link)
        await db.set_anime_channel_selected(anime_title, chat.id)
        await message.reply(
            f"✅ Saved the private channel reference for <b>{_escape(anime_title)}</b> "
            f"({chat.id}). Retry /uploadchannel now."
        )
    except Exception as exc:
        await message.reply(
            f"❌ Could not validate that invite link: {_escape(type(exc).__name__)}: {_escape(exc)}"
        )
    finally:
        if user_client.is_connected:
            await user_client.stop()

@bot.on_message(filters.command("uploadchannel") & filters.private)
async def uploadchannel_command(client, message):
    if UPLOADCHANNEL_PAUSED:
        return await message.reply("🛑 /uploadchannel is temporarily paused by the owner.")
    # Respond explicitly to non-admins instead of silently filtering the command.
    if not message.from_user or message.from_user.id not in Var.ADMINS:
        return await message.reply("⛔ This command is restricted to bot administrators.")
    if not getattr(Var, "USER_SESSION", None):        return await message.reply(
            "⚠️ USER_SESSION is not configured. Add your Telegram user session to the deployment environment first."
        )
    status = await message.reply("Fetching currently airing anime and their release weekdays…")    try:
        items = await _fetch_currently_airing()
        if not items:            return await status.edit("AniList did not return any currently airing anime with a known next-episode day.")
        UPLOAD_CHANNEL_SESSIONS[message.from_user.id] = {
            "items": items,            "selected": set(),
            "created": [],
        }
        keyboard = _selection_keyboard(items, set())
        await status.edit(
            "<b>Choose ongoing anime</b>\nSelect as many titles as you want. The weekday is based on the next scheduled episode in India time.",
            reply_markup=keyboard,
        )
    except Exception as exc:
        await rep.report(f"/uploadchannel schedule lookup failed: {type(exc).__name__}: {exc}", "warning")
        await status.edit(f"Could not load the AniList schedule: <code>{_escape(exc)}</code>")


@bot.on_callback_query(filters.regex(r"^uc:"))
async def uploadchannel_callbacks(client, callback):
    owner_id = callback.from_user.id
    state = UPLOAD_CHANNEL_SESSIONS.get(owner_id)
    if owner_id not in Var.ADMINS or not state:
        return await callback.answer("This selection has expired. Run /uploadchannel again.", show_alert=True)
    data = callback.data
    if data == "uc:noop":
        return await callback.answer()
    if data == "uc:cancel":
        UPLOAD_CHANNEL_SESSIONS.pop(owner_id, None)
        return await callback.message.edit("Channel creation cancelled.")
    if data.startswith("uc:toggle:"):
        index = int(data.rsplit(":", 1)[1])
        if index < 0 or index >= len(state["items"]):
            return await callback.answer("Invalid anime selection.", show_alert=True)
        if index in state["selected"]:
            state["selected"].remove(index)
        else:
            state["selected"].add(index)
        await callback.message.edit_reply_markup(
            _selection_keyboard(state["items"], state["selected"])
        )
        return await callback.answer(f"{len(state['selected'])} selected")

    if data != "uc:create":
        return await callback.answer()
    if not state["selected"]:
        return await callback.answer("Select at least one anime first.", show_alert=True)

    if UPLOADCHANNEL_PAUSED:
        return await callback.answer("/uploadchannel is paused by the owner.", show_alert=True)
    current_task = asyncio.current_task()
    if current_task:
        UPLOADCHANNEL_ACTIVE_TASKS.add(current_task)
    await callback.answer("Starting channel creation…")
    await callback.message.edit("Creating selected anime channels. Please keep the bot running…")
    user_client = None
    created = []
    archive_jobs = []
    channel_stage = "starting USER_SESSION"
    current_anime_title = "unknown"
    try:
        user_client = __import__("pyrogram").Client(
            "autonew_channel_creator",
            api_id=int(Var.API_ID),
            api_hash=Var.API_HASH,
            session_string=Var.USER_SESSION,
            in_memory=True,
            parse_mode=ParseMode.HTML,
        )
        channel_stage = "starting USER_SESSION client"
        await user_client.start()
        channel_stage = "resolving bot account in USER_SESSION"
        # Resolve the bot through its public username in the USER session.
        # A bot-session numeric ID is not necessarily an input peer known to this
        # separate MTProto session and can raise PEER_ID_INVALID.
        # The bot account authenticated by BOT_TOKEN is the only account we
        # are allowed to promote. Never let a stale BOT_USERNAME config promote
        # a different bot into every newly created channel.
        token_bot = await bot.get_me()
        token_bot_username = str(getattr(token_bot, "username", "") or "").strip().lstrip("@")
        configured_bot_username = str(getattr(Var, "BOT_USERNAME", "") or "").strip().lstrip("@")
        if not token_bot_username:
            raise RuntimeError("The account authenticated by BOT_TOKEN has no public username; cannot promote it by username.")
        # BOT_USERNAME may be stale from an older deployment. It is not an
        # authority for channel permissions: always use the identity returned
        # by the live BOT_TOKEN client. A mismatch is diagnostic only.
        if configured_bot_username and configured_bot_username.casefold() != token_bot_username.casefold():
            await rep.report(
                f"Ignoring stale BOT_USERNAME=@{configured_bot_username}; "
                f"BOT_TOKEN authenticates as @{token_bot_username}. "
                "Only the BOT_TOKEN account will be promoted in anime channels.",
                "warning",
            )
        bot_username = token_bot_username
        bot_user = await user_client.get_users(bot_username)
        if not bot_user or not getattr(bot_user, "id", None):
            raise RuntimeError(
                f"Could not resolve the actual BOT_TOKEN account @{bot_username} in USER_SESSION."
            )
        if int(bot_user.id) != int(token_bot.id):
            raise RuntimeError(
                f"Bot identity mismatch: BOT_TOKEN is user {token_bot.id}, but USER_SESSION resolved "
                f"@{bot_username} as {bot_user.id}. Refusing to promote the wrong account."
            )

        selected_items = [state["items"][i] for i in sorted(state["selected"])]
        for media in selected_items:
            title = _display_title(media)
            current_anime_title = title
            channel_stage = f"reading saved channel mapping for {title}"
            aliases = media.get("title") or {}
            names = []
            for key in ("english", "romaji", "native"):
                value = aliases.get(key)
                if value and value not in names:
                    names.append(value)
            # Persist AniList synonyms too, so the scheduled RSS resolver can
            # recognize release-group/alternate titles and route them back to
            # this exact automatically created channel.
            for value in (media.get("synonyms") or []):
                if value and value not in names:
                    names.append(value)
            names.append(title)
            names = list(dict.fromkeys(names))

            # Idempotency: check every known title alias before creating a channel.
            mapped_id = None
            for name in names:
                mapped_id = await db.get_anime_channel(name)
                if not mapped_id:
                    mapped_id = await db.get_selected_anime_channel(name)
                if mapped_id:
                    break

            chat = None
            reused = False
            persistent_invite = None
            if mapped_id:
                wanted_id = int(mapped_id)
                # The saved channels were created by USER_SESSION. Resolve them
                # through that same creator account, not the bot token: the bot
                # may not have been promoted yet if an earlier run stopped midway.
                # Do not scan get_dialogs(); stale dialog peers can trigger
                # channels.GetChannels / CHANNEL_INVALID.
                saved_invite = await db.get_anime_channel_invite_link(title)
                persistent_invite = saved_invite
                resolve_error = None
                try:
                    # First use the persisted invite if present; otherwise try the
                    # numeric peer directly (works when this session has its hash).
                    channel_stage = f"opening saved channel peer for {title}"
                    chat = (
                        await user_client.get_chat(saved_invite)
                        if saved_invite else await user_client.get_chat(wanted_id)
                    )
                except Exception as exc:
                    resolve_error = exc
                    chat = None

                if chat is None:
                    # Recover a legacy channel peer through raw messages.GetDialogs.
                    # Unlike Pyrogram's high-level iterator, this does not try to
                    # resolve each dialog's offset peer through channels.GetChannels.
                    try:
                        from pyrogram.raw.functions.messages import GetDialogs
                        from pyrogram.raw.types import InputPeerEmpty
                        channel_stage = f"recovering legacy peer from raw Telegram dialogs for {title}"
                        raw_dialogs = await user_client.invoke(
                            GetDialogs(
                                offset_date=0,
                                offset_id=0,
                                offset_peer=InputPeerEmpty(),
                                limit=100,
                                hash=0,
                            )
                        )
                        for raw_chat in getattr(raw_dialogs, "chats", []) or []:
                            raw_id = getattr(raw_chat, "id", None)
                            raw_hash = getattr(raw_chat, "access_hash", None)
                            if raw_id is None or raw_hash is None:
                                continue
                            # Pyrogram storage.update_peers expects normalized
                            # (marked_id, access_hash, type, username, phone) tuples.
                            marked_id = int(f"-100{int(raw_id)}")
                            peer_type = "channel" if getattr(raw_chat, "broadcast", False) else "supergroup"
                            await user_client.storage.update_peers([
                                (
                                    marked_id,
                                    int(raw_hash),
                                    peer_type,
                                    getattr(raw_chat, "username", None),
                                    None,
                                )
                            ])
                            if marked_id == wanted_id:
                                chat = await user_client.get_chat(wanted_id)
                                break
                    except Exception as raw_error:
                        resolve_error = raw_error

                if chat is None:
                    # The saved peer is irrecoverable for this USER_SESSION.
                    # Do not abort the whole selection or keep retrying the same
                    # invalid ID forever. Create a replacement channel and update
                    # every title alias below after Telegram confirms creation.
                    await rep.report(
                        f"Saved channel mapping for {title} ({wanted_id}) is not accessible "
                        f"({type(resolve_error).__name__}: {resolve_error}). Creating a replacement channel; "
                        "the old channel is left untouched.",
                        "warning",
                    )
                    channel_stage = f"creating replacement channel for {title}"
                    chat = await user_client.create_channel(
                        title, f"Official episode channel for {title}"
                    )
                    reused = False
                elif not chat or int(chat.id) != wanted_id:
                    raise RuntimeError(
                        f"Saved mapping for '{title}' points to {wanted_id}, but Telegram "
                        "returned a different channel. Refusing to overwrite the mapping."
                    )
                else:
                    reused = True
            else:
                chat = await user_client.create_channel(
                    title, f"Official episode channel for {title}"
                )

            # Persist a durable invite reference for private channels. Unlike a
            # numeric channel ID, the invite URL can repopulate Pyrogram's peer
            # cache after a fresh in-memory USER_SESSION on Heroku.
            if not persistent_invite:
                try:
                    channel_stage = f"creating persistent invite link for {title}"
                    invite_obj = await user_client.create_chat_invite_link(
                        chat.id, name="Anime uploader persistent peer"
                    )
                    persistent_invite = invite_obj.invite_link
                except Exception as invite_error:
                    raise RuntimeError(
                        f"Could not create a persistent invite reference for '{title}': {invite_error}"
                    ) from invite_error

            # Save aliases and invite reference only after the real channel is resolved.
            for name in names:
                await db.add_anime_channel_mapping(name, chat.id)
                await db.set_anime_channel_invite_link(name, chat.id, persistent_invite)
            await db.set_anime_channel_selected(title, chat.id)

            # USER_SESSION is the channel creator/admin. Verify the bot's status
            # through this session, and repair missing admin rights automatically.
            try:
                channel_stage = f"checking bot admin rights for {title}"
                current_member = await user_client.get_chat_member(chat.id, bot_user.id)
                current_status = str(getattr(current_member, "status", "")).lower()
            except Exception:
                current_status = ""

            if not any(role in current_status for role in ("administrator", "owner", "creator")):
                channel_stage = f"promoting bot administrator for {title}"
                await user_client.promote_chat_member(
                    chat.id,
                    bot_user.id,
                    privileges=ChatPrivileges(
                        can_manage_chat=True,
                        can_post_messages=True,
                        can_edit_messages=True,
                        can_delete_messages=True,
                        can_invite_users=True,
                        can_change_info=True,
                        can_pin_messages=True,
                    ),
                )
                # Confirm the promotion before any season/episode upload begins.
                channel_stage = f"verifying bot administrator rights for {title}"
                confirmed_member = await user_client.get_chat_member(chat.id, bot_user.id)
                confirmed_status = str(getattr(confirmed_member, "status", "")).lower()
                if not any(role in confirmed_status for role in ("administrator", "owner", "creator")):
                    raise RuntimeError(
                        f"Bot promotion could not be confirmed for '{title}' ({chat.id}). "
                        "No episode upload was started."
                    )
                if getattr(confirmed_member, "can_post_messages", True) is False:
                    raise RuntimeError(
                        f"@{bot_username} is an admin in '{title}' ({chat.id}) but Telegram denied "
                        "can_post_messages. Episode uploads cannot start until posting permission is granted."
                    )
            # Publish one ANIME SOUL channel introduction before any episode cards.
            # Existing channels are checked first so rerunning /uploadchannel never
            # duplicates the welcome post or its sticker.
            intro_exists = False
            try:
                async for old_message in user_client.get_chat_history(chat.id, limit=1000):
                    old_caption = str(getattr(old_message, "caption", "") or "")
                    if (                        "AS_CHANNEL_INTRO" in old_caption
                        or (
                            getattr(old_message, "photo", None)
                            and "Genres:" in old_caption
                            and "Synopsis:" in old_caption                            and "Episode:" not in old_caption
                        )
                    ):
                        intro_exists = True                        break
            except Exception as history_error:
                raise RuntimeError(
                    f"Could not verify whether the ANIME SOUL intro already exists for {title}; "                    f"refusing to create a possible duplicate: {history_error}"
                ) from history_error

            if not intro_exists:
                intro_info = TextEditor(title)
                intro_info.adata = media
                intro_info.pdata["anime_season"] = "01"
                intro_path = os.path.join(
                    "downloads", f"anime_soul_channel_intro_{int(chat.id)}.jpg"
                )
                os.makedirs(os.path.dirname(intro_path), exist_ok=True)
                intro_poster = await build_anime_soul_poster(
                    intro_info, title, intro_path, episode_label="CHANNEL"
                )
                intro_caption = _build_channel_intro_caption(media)
                cover = (media.get("coverImage") or {}).get("extraLarge") or (media.get("coverImage") or {}).get("large")
                if intro_poster and os.path.exists(intro_poster):
                    with open(intro_poster, "rb") as intro_photo:
                        intro_post = await bot.send_photo(
                            chat_id=chat.id, photo=intro_photo, caption=intro_caption
                        )                elif cover:
                    intro_post = await bot.send_photo(
                        chat_id=chat.id, photo=cover, caption=intro_caption
                    )
                else:
                    raise RuntimeError(
                        f"No verified artwork was available for the ANIME SOUL channel intro: {title}."
                    )
                if not intro_post or not getattr(intro_post, "id", None):
                    raise RuntimeError(
                        f"Telegram did not confirm the ANIME SOUL intro post for {title}."
                    )
                try:
                    sticker_ok = await send_sticker_to_channel(
                        chat.id, getattr(Var, "STICKER_ID", None), client=user_client
                    )
                    if not sticker_ok:
                        await rep.report(
                            f"ANIME SOUL intro posted for {title}, but its Ongoing Anime Time sticker failed.",
                            "warning", log=False,
                        )
                except Exception as sticker_error:
                    await rep.report(
                        f"ANIME SOUL intro posted for {title}, but sticker delivery failed: {sticker_error}",
                        "warning", log=False,
                    )
                try:
                    os.remove(intro_path)
                except OSError:
                    pass
                await rep.report(
                    f"Published one-time ANIME SOUL channel intro for {title} in {chat.id}.",
                    "success", log=False,
                )
            else:
                await rep.report(
                    f"ANIME SOUL channel intro already exists for {title}; skipping duplicate welcome post.",
                    "info", log=False,
                )

            # Episode posts start only after the intro card is confirmed.
            created.append((title, chat.id, None, reused))
            archive_jobs.append((media, int(chat.id)))
            await asyncio.sleep(1)

        # Scheduled ongoing releases have priority over the archive backlog.
        # Lock only one archive episode at a time, then yield to any scheduled
        # upload that became pending while this episode was being processed.
        channel_stage = "processing archive episodes with scheduled-release priority"
        # Process one selected anime completely before starting the next.
        # Older seasons first, then the currently airing season.
        for selected_media, destination_id in archive_jobs:
            selected_title = _display_title(selected_media)
            prequels = _prequel_seasons(selected_media)
            season_jobs = [(index, item, int(item.get("episodes") or 0))
                           for index, item in enumerate(prequels, start=1)]
            current_episode_count = max(0, int((selected_media.get("nextAiringEpisode") or {}).get("episode") or 1) - 1)
            if current_episode_count:
                season_jobs.append((len(season_jobs) + 1, selected_media, current_episode_count))
            if not season_jobs:
                raise RuntimeError(f"No released episodes are known for {selected_title}; no upload was started.")
            for season_index, prequel, available_episodes in season_jobs:
                season_title = _display_title(prequel)
                archive_id = int(prequel.get("id") or 0)
                if not archive_id:
                    raise RuntimeError(f"AniList ID is missing for archive season {season_title}.")
                previous = await db.get_anime_archive_batch(
                    archive_id, season_index, destination_id
                )
                previous_status = (previous or {}).get("status")
                # Resume safely from the channel history; a stale DB status must not
                # suppress uploads when the destination channel is actually empty.
                # Check the destination channel's history; existing episodes are skipped.
                existing_episodes = set()
                channel_stage = f"reading existing episode history in {destination_id} via USER_SESSION"
                async for channel_message in user_client.get_chat_history(destination_id, limit=5000):
                    body = " ".join(str(x or "") for x in (
                        getattr(channel_message, "text", ""),
                        getattr(channel_message, "caption", ""),
                    ))
                    season_match = re.search(r"(?i)\\bSeason\\s*0*(\\d+)\\b", body)
                    episode_match = re.search(r"(?i)\\bEpisode\\s*[:#-]?\\s*0*(\\d+)\\b", body)
                    compact_match = re.search(r"(?i)\\[S0*(\\d+)[-_]0*(\\d+)\\]", body)
                    if season_match and episode_match and int(season_match.group(1)) == int(season_index):
                        existing_episodes.add(int(episode_match.group(1)))
                    elif compact_match and int(compact_match.group(1)) == int(season_index):
                        existing_episodes.add(int(compact_match.group(2)))
                season_year = prequel.get("seasonYear") or "N/A"
                episode_total = available_episodes or "Unknown"
                season_format = prequel.get("format") or "TV"
                season_status = prequel.get("status") or "N/A"
                season_info = (
                    f"<b>{_escape(selected_title)} — Season {season_index}</b>\n"
                    f"<b>Season title:</b> {_escape(season_title)}\n"
                    f"<b>Year:</b> {_escape(season_year)}\n"
                    f"<b>Episodes:</b> {_escape(episode_total)}\n"
                    f"<b>Format:</b> {_escape(season_format)}\n"
                    f"<b>Status:</b> {_escape(season_status)}\n"
                    f"<b>Qualities:</b> HDRip, 1080p, 720p, 480p"
                )
                # No standalone season-info or sticker posts. Each episode
                # will publish one final ANIME SOUL card after all qualities finish.
                if not available_episodes:
                    raise RuntimeError(
                        f"AniList has no released episode count for {season_title}; refusing to guess."
                    )
                await db.set_anime_archive_batch(
                    archive_id, season_index, destination_id, "in_progress",
                    "", f"episode-by-episode S{season_index} 4-quality",
                )
                for episode_number in range(1, available_episodes + 1):
                    if episode_number in existing_episodes:
                        continue
                    await rep.report(
                        f"Uploading {selected_title} Season {season_index} Episode {episode_number} "
                        f"to dedicated channel {destination_id}.",
                        "info", log=False,
                    )
                    channel_stage = f"uploading Season {season_index} Episode {episode_number} to {destination_id}"
                    # Never abandon an archive batch because one source/encode/
                    # Telegram attempt fails. Retry this same episode and yield the
                    # shared priority lock between attempts so new ongoing releases
                    # are published first.
                    channel_stage = (
                        f"uploading Season {season_index} Episode {episode_number} "
                        f"with ongoing-release priority"
                    )
                    await _upload_episode_with_priority_retry(
                        prequel, destination_id, season_index, episode_number,
                        user_client, context="Active /uploadchannel batch",
                    )
                    existing_episodes.add(episode_number)
                await db.set_anime_archive_batch(
                    archive_id, season_index, destination_id, "complete",
                    "", f"episode-by-episode S{season_index} 4-quality",
                )

        UPLOAD_CHANNEL_SESSIONS.pop(owner_id, None)
        summary = "\n".join(f"• <b>{_escape(name)}</b> — <code>{chat_id}</code>" for name, chat_id, _, _ in created)
        await callback.message.edit(
            f"✅ Processed {len(created)} anime channel(s); existing channel mappings are reused to prevent duplicate creation.\n\n{summary}\n\nThe existing scheduled uploader can now route matching new releases through these mappings."
        )
    except Exception as exc:
        await rep.report(
            f"Automatic anime channel creation failed at stage '{channel_stage}' "
            f"for '{current_anime_title}': {type(exc).__name__}: {exc}",
            "error",
        )
        await callback.message.edit(
            f"⚠️ Channel setup stopped at <b>{_escape(channel_stage)}</b> for "
            f"<b>{_escape(current_anime_title)}</b>. {len(created)} channel(s) were completed before the error.\n"
            f"<code>{_escape(exc)}</code>"
        )
    finally:
        if current_task:
            UPLOADCHANNEL_ACTIVE_TASKS.discard(current_task)
        if user_client:
            try:
                await user_client.stop()
            except Exception:
                pass