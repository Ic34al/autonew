"""Interactive channel creation and released-episode backfill for ongoing anime.

This module reuses USER_SESSION, AniList metadata, persistent anime-channel
mappings, configured RSS sources, and the existing uploader. It backfills
only episodes confirmed released in the selected currently airing season;
it never walks or uploads prequel/archive seasons.
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
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ChatPrivileges, InputMediaPhoto

from bot import bot, Var, LOGS
from bot.core.database import db
import bot.core.auto_animes as auto_anime_core
from bot.core.auto_animes import process_batch_anime, AUTO_UPLOAD_PRIORITY_LOCK, AUTO_UPLOAD_PRIORITY_PENDING, find_video_files, build_anime_soul_poster, build_main_channel_announcement_caption, send_sticker_to_channel
from bot.core.func_utils import getfeeds, encode
from bot.core.tordownload import TorDownloader
from bot.core.ffencoder import FFEncoder
from bot.core.tguploader import TgUploader
from bot.core.text_utils import TextEditor
import os
import shutil
import time
import uuid
from bot.core.reporter import rep

# Photo and metadata caption are intentionally sent as one Telegram message.
ANILIST_URL = "https://graphql.anilist.co"
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
UPLOAD_CHANNEL_SESSIONS = {}
# Per-process FILE_STORE search cache: one Telegram server-side lookup per anime,
# reused for all episodes in the same backfill run.
_FILE_STORE_INDEX_CACHE = {}
_ANILIST_DETAILS_CACHE = {}
_FILE_STORE_CHAT_CACHE = {}
ANIME_INFO_CARD_VERSION = "anime-soul-split-v2"
ANIME_EPISODE_CARD_VERSION = "anime-soul-episode-v2"


def _coerce_path(value, fallback_path=None):
    """Return a real filesystem path; never pass an open file object to os.path APIs."""
    if isinstance(value, (str, bytes, os.PathLike)):
        return os.fspath(value)

    # Pillow/Telegram helpers may return an open file handle instead of the
    # documented output path. Prefer its filename only when it is path-like.
    candidate = getattr(value, "name", None)
    if isinstance(candidate, (str, bytes, os.PathLike)):
        candidate = os.fspath(candidate)
        if os.path.isfile(candidate):
            return candidate

    # The poster builder is given a deterministic output path and saves there.
    # If it returns a BufferedReader (or another file object), use that path
    # only after confirming the generated file exists.
    if fallback_path is not None and isinstance(fallback_path, (str, bytes, os.PathLike)):
        fallback_path = os.fspath(fallback_path)
        if os.path.isfile(fallback_path):
            return fallback_path

    # Unknown file-like return values must fail safely, not reach os.path.exists.
    return None


LOGS.info("UPLOADCHANNEL MODULE LOADED; /uploadchannel handler registered.")

SCHEDULE_QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
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
    timeout = httpx.Timeout(25.0, connect=10.0)
    # AniList caps Page.perPage at 50. Fetch several pages instead of
    # silently limiting /uploadchannel to the 50 most popular currently
    # releasing titles. Deduplicate by AniList ID across pages.
    result = []
    seen = set()
    async with httpx.AsyncClient(timeout=timeout) as client:
        for page in range(1, 11):
            response = await client.post(
                ANILIST_URL,
                json={"query": SCHEDULE_QUERY, "variables": {"page": page, "perPage": 50}},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errors"):
                raise RuntimeError("; ".join(
                    str(e.get("message", "AniList error")) for e in payload["errors"]
                ))
            media = (payload.get("data") or {}).get("Page", {}).get("media") or []
            if not media:
                break
            for item in media:
                media_id = item.get("id")
                day = _airing_day(item)
                title = _display_title(item)
                if not title or not media_id or media_id in seen:
                    continue
                seen.add(media_id)
                # Keep AniList RELEASING entries even when the API has not
                # published a nextAiringEpisode timestamp yet.
                item["_weekday"] = day or "Schedule unknown"
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
        r"\bS0*(\d{1,2})(?:E|\s*[._\-–—− ]*\d)",
        r"\bSeason\s*0*(\d{1,2})\b",
        r"\bS0*(\d{1,2})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, str(title or ""), re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _prequel_seasons(media):
    """Return TV prequel ancestors oldest-first; the schedule query fetches up to five levels."""
    found = {}

    def walk(parent, depth):
        if depth > 5:
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
        if value and normalized and normalized not in {
            _plain_title(existing) for existing in aliases
        }:
            aliases.append(value)
    if not aliases:
        await rep.report(
            f"AniList supplied no searchable title aliases for anime ID {media.get('id')}.",
            "warning", log=False,
        )
        return None

    season = int(season_number)
    episode = int(episode_number)
    queries = []
    search_aliases = []
    for alias in aliases:
        # AniList often labels this "... 2nd Season", while SubsPlease uses
        # the base romaji title followed by "S2 - 01". Search both forms so
        # older episodes remain discoverable outside the latest RSS window.
        base_alias = re.sub(
            r"(?i)\s+(?:season\s+\d{1,2}|s\d{1,2}|\d{1,2}(?:st|nd|rd|th)?\s+season)\s*$",
            "",
            alias,
        ).strip()
        for candidate in (alias, base_alias):
            if candidate and candidate.casefold() not in {
                item.casefold() for item in search_aliases
            }:
                search_aliases.append(candidate)

    for alias in search_aliases:
        # Include SubsPlease's exact "S2 - 01" naming as well as S02E01.
        queries.extend([
            f"{alias} S{season:02d}E{episode:02d} 1080p",
            f"{alias} S{season}E{episode:02d} 1080p",
            f"{alias} S{season} - {episode:02d} 1080p",
            f"{alias} Season {season} Episode {episode} 1080p",
            f"{alias} {episode:02d} 1080p",
        ])
    queries = list(dict.fromkeys(queries))
    feeds = [
        f"https://nyaa.si/?page=rss&q={quote_plus(query)}&c=1_2&f=0"
        for query in queries
    ]
    # Always source the highest configured SubsPlease release. The 480p and
    # 720p outputs are downscaled from this original; sourcing a 720p torrent
    # and then upscaling it to 1080p creates a fake 1080p file.
    feeds.append((getattr(Var, "RSS_LOW_END", {}) or {}).get("1080"))
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

            # Do not silently accept a 720p/480p release as the source for
            # the 1080p master. That was the reason every output could look
            # like the same low-resolution source. Only encode from an actual
            # 1080p source; if SubsPlease has none, fail clearly instead of
            # upscaling and labelling it 1080p.
            if not re.search(r"(?i)\b1080p\b", title):
                continue

            normalized_release = _plain_title(title)
            # Match the 16-Nov scheduled uploader's 60% meaningful-token
            # overlap. SubsPlease uses the Japanese/romaji base title and
            # appends "S2 - 01", while AniList may say "... 2nd Season".
            title_match = any(
                _plain_title(alias) in normalized_release
                for alias in search_aliases
                if _plain_title(alias)
            )
            if not title_match:
                release_tokens = {
                    token for token in normalized_release.split()
                    if len(token) > 2 and not token.isdigit()
                }
                for alias in search_aliases:
                    alias_tokens = {
                        token for token in _plain_title(alias).split()
                        if len(token) > 2 and not token.isdigit()
                        and token not in {
                            "the", "and", "for", "with", "from", "that", "this",
                            "her", "his", "their", "you", "your", "into",
                        }
                    }
                    if len(alias_tokens) >= 2:
                        overlap = len(alias_tokens & release_tokens)
                        if overlap / len(alias_tokens) >= 0.60:
                            title_match = True
                            break
            if not title_match:
                continue

            # Keep the 16-Nov safety gate: never treat a season pack or
            # numbered episode range as one exact single-episode release.
            if re.search(r"(?i)\b(?:batch|complete\s+series|full\s+season|season\s+pack)\b", title):
                continue
            if re.search(r"(?i)(?<!S)\b0*\d{1,3}\s*[-–~]\s*0*\d{1,3}\b", title):
                continue

            # Reuse the SubsPlease naming parser already used by the
            # 16-Nov scheduled uploader. It recognizes "S2 - 01", not just
            # "S02E01" or "Episode 01".
            release_episode = None
            try:
                release_episode = auto_anime_core.extract_release_episode(title)
            except (AttributeError, TypeError, ValueError):
                release_episode = None
            episode_match = (
                release_episode is not None
                and int(release_episode) == episode
            )
            if not episode_match:
                ep_patterns = (
                    rf"(?i)\bS0*{season}[\s._-]*E0*{episode}\b",
                    rf"(?i)\b{season}x0*{episode}\b",
                    rf"(?i)\bSeason[\s._-]*0*{season}[\s._-]*Episode[\s._-]*0*{episode}\b",
                    rf"(?i)\bEpisode[\s._-]*0*{episode}\b",
                    rf"(?i)\bEp[\s._-]*0*{episode}\b",
                )
                episode_match = any(re.search(pattern, title) for pattern in ep_patterns)
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

    # RSS is a rolling window and may not contain an older episode even
    # though SubsPlease still exposes it through its search API. Query the
    # official API for each AniList title/synonym, then validate the exact
    # show, episode, season, and downloadable quality before accepting it.
    api_scanned = 0
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            headers={"User-Agent": "Mozilla/5.0 (compatible; AnimeSoulBot/1.0)"},
        ) as api_client:
            api_payloads = []
            for alias in search_aliases:
                try:
                    response = await api_client.get(
                        "https://subsplease.org/api/",
                        params={"f": "search", "tz": "Etc/GMT", "s": alias},
                    )
                    response.raise_for_status()
                    api_payloads.append(response.json())
                except Exception as api_error:
                    await rep.report(
                        f"SubsPlease API search failed for '{alias}': "
                        f"{type(api_error).__name__}: {api_error}",
                        "warning", log=False,
                    )
            try:
                latest_response = await api_client.get(
                    "https://subsplease.org/api/",
                    params={"f": "latest", "tz": "Etc/GMT"},
                )
                latest_response.raise_for_status()
                api_payloads.append(latest_response.json())
            except Exception as latest_error:
                await rep.report(
                    f"SubsPlease latest API fallback failed: {type(latest_error).__name__}: {latest_error}",
                    "warning", log=False,
                )

        api_seen = set()
        for payload in api_payloads:
            if not isinstance(payload, dict):
                continue
            items = (
                payload.get("results") or payload.get("latest")
                or payload.get("releases") or payload.get("data") or {}
            )
            iterable = items.items() if isinstance(items, dict) else enumerate(items) if isinstance(items, list) else []
            for release_key, item in iterable:
                if not isinstance(item, dict):
                    continue
                api_scanned += 1
                show_name = str(item.get("show") or "").strip()
                episode_name = str(item.get("episode") or "").strip()
                release_label = str(item.get("title") or release_key or "").strip()
                candidate_show = _plain_title(show_name or release_label)
                show_match = any(
                    _plain_title(alias) == candidate_show
                    or (
                        len(_plain_title(alias).split()) >= 2
                        and set(_plain_title(alias).split()).issubset(set(candidate_show.split()))
                    )
                    for alias in search_aliases
                    if _plain_title(alias)
                )
                if not show_match:
                    continue

                # SubsPlease search results usually expose episode as "08";
                # some API responses include the season marker in title/episode.
                episode_text = episode_name or release_label
                season_episode_match = re.search(
                    r"(?i)\bS\s*0*(\d{1,2})\s*[-._ ]*E\s*0*(\d{1,3})\b",
                    episode_text,
                )
                if season_episode_match:
                    detected_inline_season = int(season_episode_match.group(1))
                    if detected_inline_season != season:
                        continue
                    found_episode = int(season_episode_match.group(2))
                elif re.fullmatch(r"0*\d{1,3}(?:v\d+)?", episode_text, re.I):
                    numeric_episode = re.match(r"0*(\d+)", episode_text)
                    found_episode = int(numeric_episode.group(1)) if numeric_episode else None
                else:
                    found_episode = auto_anime_core.extract_release_episode(
                        f"{show_name} {episode_text} {release_label}"
                    )
                    found_episode = int(found_episode) if found_episode is not None else None
                if found_episode != episode:
                    continue

                detected_season = (
                    _season_number_from_release(episode_text)
                    or _season_number_from_release(release_label)
                    or _season_number_from_release(show_name)
                )
                if detected_season is not None and detected_season != season:
                    continue
                if season > 1 and detected_season != season:
                    continue

                downloads = item.get("downloads") or []
                if isinstance(downloads, dict):
                    downloads = list(downloads.values())
                ranked = []
                for download in downloads:
                    if not isinstance(download, dict):
                        continue
                    candidate = (
                        download.get("magnet") or download.get("torrent")
                        or download.get("url")
                    )
                    if not candidate:
                        continue
                    candidate = str(candidate).strip()
                    resolution = str(
                        download.get("res") or download.get("resolution") or ""
                    ).strip()
                    rank = {"720": 0, "1080": 1, "480": 2}.get(
                        re.sub(r"[^0-9]", "", resolution), 3
                    )
                    ranked.append((rank, candidate, resolution))
                for _, candidate, resolution in sorted(ranked):
                    key = (candidate, season, episode)
                    if key in api_seen:
                        continue
                    api_seen.add(key)
                    display = f"{show_name} - Episode {episode:02d}"
                    if resolution:
                        display += f" [{resolution}p]"
                    return {"title": display, "link": candidate}
    except Exception as api_error:
        await rep.report(
            f"SubsPlease API episode lookup failed: {type(api_error).__name__}: {api_error}",
            "warning", log=False,
        )

    await rep.report(
        f"No exact RSS/API torrent match for AniList anime {media.get('id')} "
        f"Season {season} Episode {episode}; searched {len(aliases)} title aliases, "
        f"{len(dict.fromkeys(url for url in feeds if url))} RSS feeds "
        f"({scanned} unique RSS entries), and {api_scanned} SubsPlease API entries. "
        "No download or encoding was started.",
        "warning", log=False,
    )
    return None


async def _upload_single_episode(media, destination_id, season_number, episode_number, destination_client, progress_callback=None, info_only=False, reuse_only=False, preflight_only=False, force_repost=False, publish_attempt_id=None):
    """Download one RSS episode, encode four qualities, and publish four buttons."""
    async def set_progress(stage):
        if progress_callback:
            await progress_callback(stage)
        else:
            await rep.report(stage, "info", log=False)

    progress_message = None

    async def create_optional_log_message(text):
        """Create one per-episode status message and edit it for each quality."""
        nonlocal progress_message
        try:
            if progress_message is None:
                progress_message = await bot.send_message(Var.LOG_CHANNEL, text)
            else:
                await progress_message.edit_text(text)
            return progress_message
        except Exception as log_error:
            LOGS.warning("Could not update upload status message: %s", log_error)
            return progress_message

    from bot import Var
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
    anime_metadata_id = int(media.get("id") or 0)
    cached_metadata = _ANILIST_DETAILS_CACHE.get(anime_metadata_id)
    try:
        if cached_metadata and int(cached_metadata.get("id") or 0) == anime_metadata_id:
            info.adata = cached_metadata
        else:
            async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as metadata_client:
                metadata_response = await metadata_client.post(
                    ANILIST_URL,
                    json={"query": details_query, "variables": {"id": anime_metadata_id}},
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                )
                metadata_response.raise_for_status()
                exact_metadata = (metadata_response.json().get("data") or {}).get("Media") or {}
            if not exact_metadata or int(exact_metadata.get("id") or 0) != anime_metadata_id:
                raise RuntimeError("AniList did not return the exact requested media ID.")
            _ANILIST_DETAILS_CACHE[anime_metadata_id] = exact_metadata
            info.adata = exact_metadata
    except Exception as metadata_error:
        raise RuntimeError(
            f"Could not retrieve exact AniList artwork/synopsis for {title} "
            f"(AniList ID {media.get('id')}): {metadata_error}. Refusing to publish an unverified thumbnail."
        ) from metadata_error
    info.pdata["anime_title"] = title
    info.pdata["episode_number"] = str(episode_number).zfill(2)
    info.pdata["anime_season"] = str(season_number).zfill(2)
    # The audio label is refined from RSS if a source download is needed.
    # A complete FILE_STORE set can be reused without a live torrent.
    info.pdata["audio_type"] = "Japanese [English Sub]"

    if not preflight_only:
        # Exactly ONE split-screen ANIME SOUL synopsis card per anime/channel.
        # Upgrade an older square/portrait card in place; never append another synopsis.
        info_post_id = await db.getAnimeArchiveChannelInfoPost(media.get("id"), destination_id)
        info_card_version = await db.getAnimeArchiveChannelInfoVersion(media.get("id"), destination_id)
        legacy_info_post_id = await db.getAnimeArchiveInfoPost(
            media.get("id"), season_number, episode_number, destination_id
        )
        if not info_post_id and int(episode_number) == 1 and legacy_info_post_id:
            info_post_id = legacy_info_post_id
            await db.saveAnimeArchiveChannelInfoPost(media.get("id"), destination_id, info_post_id)
        if info_post_id and legacy_info_post_id and int(legacy_info_post_id) != int(info_post_id):
            try:
                await bot.delete_messages(destination_id, int(legacy_info_post_id))
            except Exception as cleanup_error:
                LOGS.warning("Could not remove legacy duplicate synopsis card %s: %s", legacy_info_post_id, cleanup_error)
    
        if not info_post_id or info_card_version != ANIME_INFO_CARD_VERSION:
            data = info.adata or {}
            title_data = data.get("title") or {}
            info_title = title_data.get("english") or title_data.get("romaji") or title
            description = re.sub(r"<[^>]+>", "", str(data.get("description") or ""))
            description = html.unescape(re.sub(r"\s+", " ", description)).strip()
            if len(description) > 560:
                description = description[:557].rsplit(" ", 1)[0] + "..."
            genres = data.get("genres") or []
            studios_data = data.get("studios") or {}
            studio_nodes = studios_data.get("nodes") or []
            studio_names = [str(item.get("name")) for item in studio_nodes if item.get("name")]
            media_type = data.get("format") or data.get("type") or "Anime"
            rating = data.get("averageScore")
            rating_text = f"{float(rating) / 10:.1f}/10" if rating is not None else "N/A"
            season_text = f"Season {season_number}"
            year = data.get("seasonYear")
            if year:
                season_text += f" • {year}"
            total_episodes = data.get("episodes")
            status = str(data.get("status") or "Unknown").replace("_", " ").title()
            info_lines = [
                f"<blockquote>❝ <b>{html.escape(str(info_title))}</b> ❞</blockquote>",
                f"<b>Season:</b> {season_text}",
                f"<b>Type:</b> {html.escape(str(media_type))}",
                f"<b>Genres:</b> {html.escape(', '.join(str(x) for x in genres) if genres else 'N/A')}",
                f"<b>Rating:</b> {rating_text}",
                f"<b>Status:</b> {html.escape(status)}",
            ]
            if total_episodes:
                info_lines.append(f"<b>Episodes:</b> {int(total_episodes)}")
            if studio_names:
                info_lines.append(f"<b>Studio:</b> {html.escape(', '.join(studio_names[:3]))}")
            info_lines.append("──────────────────────")
            if description:
                info_lines.append(f"<blockquote expandable>≡ {html.escape(description)}</blockquote>")
            info_caption = "\n".join(info_lines)
            if len(info_caption) > 1024:
                info_caption = info_caption[:1000].rsplit(" ", 1)[0] + "…"
    
            info_poster_path = os.path.join(workdir, f"anime_soul_info_s{season_number}.jpg")
            info_poster = _coerce_path(
                await build_anime_soul_poster(info, title, info_poster_path, info_card=True),
                fallback_path=info_poster_path,
            )
            if not info_poster or not os.path.exists(info_poster):
                raise RuntimeError(
                    f"Could not generate the ANIME SOUL split-screen synopsis thumbnail for {title}."
                )
            # Pass the path directly; Pyrogram handles opening/stat'ing it.
            if info_post_id:
                info_post = await bot.edit_message_media(
                    chat_id=destination_id,
                    message_id=int(info_post_id),
                    media=InputMediaPhoto(
                        media=info_poster, caption=info_caption, parse_mode=ParseMode.HTML
                    ),
                )
            else:
                info_post = await bot.send_photo(
                    chat_id=destination_id,
                    photo=info_poster,
                    caption=info_caption,
                    parse_mode=ParseMode.HTML,
                )
            if not info_post or not getattr(info_post, "id", None):
                raise RuntimeError(f"Telegram did not confirm ANIME SOUL synopsis card for {title}.")
            info_post_id = int(info_post.id)
            await db.saveAnimeArchiveChannelInfoPost(media.get("id"), destination_id, info_post_id)
            await db.saveAnimeArchiveChannelInfoVersion(
                media.get("id"), destination_id, ANIME_INFO_CARD_VERSION
            )
            await rep.report(
                f"{'Upgraded' if info_card_version else 'Published'} the single ANIME SOUL split-screen synopsis card for {title}.",
                "info", log=False,
            )
        else:
            await rep.report(
                f"Reusing the current single synopsis card {info_post_id} for {title}; no extra card.",
                "info", log=False,
            )
    
        async def send_checkpointed_sticker(stage_key):
            sticker_episode_key = str(episode_number) if stage_key != "info" else "info"
            saved_sticker = await db.getAnimeArchiveStickerPost(
                media.get("id"), season_number, sticker_episode_key,
                destination_id, stage_key,
            )
            if saved_sticker and not force_repost:
                return
            sticker_message_id = await send_sticker_to_channel(
                destination_id, getattr(Var, "STICKER_ID", None), client=bot
            )
            if sticker_message_id:
                # Replace the old checkpoint only after Telegram confirms the new sticker.
                if force_repost and saved_sticker and int(saved_sticker) != int(sticker_message_id):
                    try:
                        await bot.delete_messages(destination_id, int(saved_sticker))
                    except Exception as cleanup_error:
                        LOGS.warning("Could not remove replaced sticker %s: %s", saved_sticker, cleanup_error)
                await db.saveAnimeArchiveStickerPost(
                    media.get("id"), season_number, sticker_episode_key,
                    destination_id, stage_key, sticker_message_id,
                )
    
        saved_info_sticker = await db.getAnimeArchiveStickerPost(
            media.get("id"), season_number, "info", destination_id, "info"
        )
        if not saved_info_sticker or (force_repost and int(episode_number) == 1):
            await send_checkpointed_sticker("info")
    
        if info_only:
            await rep.report(
                f"Backfilled synopsis/info card for existing {title} S{season_number}E{episode_number}; "
                "existing episode post and FILE_STORE qualities were left untouched.",
                "success", log=False,
            )
            shutil.rmtree(workdir, ignore_errors=True)
            return True
    
    
    # Episode cards are posted by the uploader, but all file-sharing deep
    # links must open the existing Monkey file-share bot.
    # Do not derive this username from BOT_TOKEN.
    # File-share bot is independent of the uploader bot.

    share_bot_username = str(
        getattr(
            Var,
            "FILE_SHARE_BOT_USERNAME",
            "Orewa_Monkey_D_Kaizuko_Bot"
        ) or ""
    ).strip().lstrip("@")

    if not share_bot_username:
        raise RuntimeError(
            "FILE_SHARE_BOT_USERNAME is not configured."
        )

    quality_order = ("720", "1080", "480", "HDRi")
    buttons_by_quality = {}
    # Prefer the exact quality message IDs already persisted by the normal
    # uploader. This is O(4) and avoids a Telegram title search or RSS download
    # for episodes that were encoded/uploaded earlier.
    saved_quality_ids = await db.getAnimeFileIds(media.get("id"), str(episode_number))
    if not isinstance(saved_quality_ids, dict):
        saved_quality_ids = {}
    for quality in quality_order:
        stored_id = saved_quality_ids.get(quality)
        if not stored_id:
            continue
        try:
            stored_msg = await destination_client.get_messages(
                int(getattr(Var, "FILE_STORE", 0)), message_ids=int(stored_id)
            )
            if not stored_msg or getattr(stored_msg, "empty", False):
                continue
            start_payload = f"get-{int(stored_id) * abs(int(Var.FILE_STORE))}"
            url = f"https://t.me/{share_bot_username}?start={await encode(start_payload)}"
            buttons_by_quality[quality] = InlineKeyboardButton(
                f"{quality}p" if quality != "HDRi" else "HDRip", url=url
            )
            await rep.report(
                f"Reusing saved database FILE_STORE ID for {quality} "
                f"of {title} S{target_season:02d}E{target_episode:02d}; no encode/upload.",
                "info", log=False,
            )
        except Exception as saved_id_error:
            await rep.report(
                f"Saved {quality} file ID for {title} could not be verified "
                f"({saved_id_error}); checking the configured FILE_STORE index.",
                "warning", log=False,
            )

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
        ep_match = re.search(r"\[?s(\d{1,2})\s*(?:[-_ ]\s*e?\s*|e\s*)(\d{1,3})\]?", raw, re.I)
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

    await set_progress(f"Checking FILE_STORE reuse for {title} S{target_season:02d}E{target_episode:02d}.")
    storage_records = []
    storage_chat_id = None
    if len(buttons_by_quality) < len(quality_order):
        storage_records = []
        storage_chat_id = None
        # Explicit storage invite supplied by the owner; do not silently scan a stale FILE_STORE_LINK from Heroku.
        storage_link = str(getattr(Var, "FILE_STORE_LINK", "") or "").strip()
        storage_cache_key = storage_link or str(getattr(Var, "FILE_STORE", 0) or 0)
        try:
            # Resolve FILE_STORE once per process, then reuse its chat ID
            # for every episode in the current and subsequent backfill runs.
            storage_chat_id = _FILE_STORE_CHAT_CACHE.get(storage_cache_key)
            if not storage_chat_id:
                if storage_link:
                    try:
                        storage_chat = await destination_client.join_chat(storage_link)
                    except Exception as join_error:
                        join_text = str(join_error).casefold()
                        if "user_already_participant" in join_text or "already a participant" in join_text:
                            storage_chat = await destination_client.get_chat(storage_link)
                        else:
                            try:
                                storage_chat = await destination_client.get_chat(storage_link)
                            except Exception:
                                raise join_error
                else:
                    configured_storage_id = int(getattr(Var, "FILE_STORE", 0) or 0)
                    if not configured_storage_id:
                        raise RuntimeError("FILE_STORE is not configured.")
                    storage_chat = await destination_client.get_chat(configured_storage_id)

                if not storage_chat or not getattr(storage_chat, "id", None):
                    raise RuntimeError("Configured FILE_STORE did not resolve to a Telegram channel.")
                storage_chat_id = int(storage_chat.id)
                configured_storage_id = int(getattr(Var, "FILE_STORE", 0) or 0)
                if configured_storage_id and storage_chat_id != configured_storage_id:
                    raise RuntimeError(
                        f"Configured FILE_STORE link resolved to {storage_chat_id}, "
                        f"but FILE_STORE is {configured_storage_id}; refusing to scan the wrong channel."
                    )
                _FILE_STORE_CHAT_CACHE[storage_cache_key] = storage_chat_id
    
            # Use Telegram's server-side message search against the anime title.
            # The old implementation walked 5,000 messages for EVERY episode,
            # making a 24-episode backfill perform up to 120,000 history reads.
            # Search once per anime and cache the parsed FILE_STORE message IDs.
            cache_key = (storage_chat_id, tuple(sorted(target_titles)))
            cached_index = _FILE_STORE_INDEX_CACHE.get(cache_key)
            if cached_index and (time.monotonic() - cached_index[0]) < 600:
                storage_records = cached_index[1]
                await set_progress(
                    f"Reusing cached FILE_STORE index for {title}; "
                    f"{len(storage_records)} matching file record(s) indexed."
                )
            else:
                search_queries = []
                # Search only the two strongest AniList title aliases. One
                # compact prefix fallback each keeps the first FILE_STORE index fast.
                for alias in title_aliases[:2]:
                    query = str(alias or "").strip()
                    if not query:
                        continue
                    candidates = [query[:64]]
                    words = re.findall(r"[A-Za-z0-9]+", query)
                    if len(words) >= 4:
                        candidates.append(" ".join(words[:4]))
                    for candidate in candidates:
                        candidate = candidate.strip()[:64]
                        if candidate and candidate not in search_queries:
                            search_queries.append(candidate)
                if not search_queries:
                    raise RuntimeError(f"No searchable title aliases are available for {title}.")
    
                found_messages = {}
                try:
                    async def _search_storage():
                        for query in search_queries:
                            async for stored in destination_client.search_messages(
                                storage_chat_id, query=query, limit=100
                            ):
                                if not stored or not getattr(stored, "id", None):
                                    continue
                                found_messages[int(stored.id)] = stored
    
                    await asyncio.wait_for(_search_storage(), timeout=90)
                except asyncio.TimeoutError as search_error:
                    raise RuntimeError(
                        f"Telegram FILE_STORE title search timed out for {title} after 90 seconds; "
                        "no duplicate upload was started. Check USER_SESSION access and retry."
                    ) from search_error
                except Exception as search_error:
                    raise RuntimeError(
                        f"Telegram could not search FILE_STORE captions for {title}: "
                        f"{type(search_error).__name__}: {search_error}. "
                        "No full-history scan or duplicate upload was attempted."
                    ) from search_error
    
                for stored in found_messages.values():
                    media_obj = getattr(stored, "video", None) or getattr(stored, "document", None)
                    media_name = getattr(media_obj, "file_name", None) if media_obj else None
                    parsed = (
                        _parse_storage_record(getattr(stored, "caption", None))
                        or _parse_storage_record(media_name)
                    )
                    if parsed:
                        storage_records.append((parsed, int(stored.id)))
    
                _FILE_STORE_INDEX_CACHE[cache_key] = (time.monotonic(), storage_records)
                await rep.report(
                    f"FILE_STORE search resolved channel {storage_chat_id}; "
                    f"searched {len(search_queries)} title alias(es), inspected "
                    f"{len(found_messages)} matching message(s), and indexed "
                    f"{len(storage_records)} episode-quality file(s).",
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
    
    else:
        await set_progress(
            f"All four qualities for {title} S{target_season:02d}E{target_episode:02d} "
            "were recovered from the uploader database; skipping FILE_STORE search."
        )

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
        if quality in buttons_by_quality:
            continue
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
                    )
                    continue
            except Exception as verify_error:
                raise RuntimeError(
                    f"Found existing {quality} file {stored_id} for {title} "
                    f"S{target_season:02d}E{target_episode:02d}, but could not verify it; "
                    f"refusing a duplicate upload: {verify_error}"
                ) from verify_error
        # A cache miss is normal for a new episode/quality, not an error.
        # The full FILE_STORE scan above succeeded; upload this quality now.
        await rep.report(
            f"No existing {quality} copy in FILE_STORE for {title} "
            f"S{target_season:02d}E{target_episode:02d}; this quality will be uploaded.",
            "info",
        )
        missing_qualities.append(quality)

    if preflight_only:
        shutil.rmtree(workdir, ignore_errors=True)
        return not missing_qualities

    # A repost/backfill must reuse the already uploaded FILE_STORE qualities.
    # Never download or encode again in this mode: if any quality is genuinely
    # missing, stop safely rather than create a duplicate or spend time re-encoding.
    if reuse_only and missing_qualities:
        raise RuntimeError(
            f"Cannot safely republish {title} S{target_season:02d}E{target_episode:02d}: "
            f"FILE_STORE is missing verified qualities: {', '.join(missing_qualities)}. "
            "No torrent download or encoding was started."
        )

    source = None
    if missing_qualities:
        await set_progress(f"Searching RSS for {title} S{target_season:02d}E{target_episode:02d}.")
        await rep.report(
            f"Searching Nyaa/Anime RSS for {title} S{target_season:02d}E{target_episode:02d}; "
            f"{len(missing_qualities)} quality file(s) still need encoding.",
            "info",
        )
        episode = await _find_episode_torrent(media, season_number, episode_number)
        if not episode:
            raise RuntimeError(
                f"No exact RSS torrent found for {title} S{target_season:02d}E{target_episode:02d}; "
                "no download or encoding was started. Existing FILE_STORE qualities remain reusable."
            )
        info.pdata["audio_type"] = (
            "Dual" if re.search(r"(?i)\bdual\b", episode["title"])
            else "Dub" if re.search(r"(?i)\b(dub|english)\b", episode["title"])
            else "Japanese [English Sub]"
        )
        await rep.report(
            f"RSS source matched: {episode['title']} — {episode['link']}",
            "success",
        )
        download_started_at = time.monotonic()
        download_status = await create_optional_log_message(
            f"<b>Download starting:</b> {_escape(title)} "
            f"S{target_season:02d}E{target_episode:02d}\\n"
            "Torrent client is being started; encoding has not started yet."
        )
        await set_progress(
            f"Starting torrent download for {title} S{target_season:02d}E{target_episode:02d}."
        )
        await rep.report(
            f"Starting torrent download for {title} S{target_season:02d}E{target_episode:02d}.",
            "info",
        )
        download_task = asyncio.create_task(
            TorDownloader(workdir).download(episode["link"])
        )
        # Torrent clients can be quiet for several minutes while connecting to
        # peers. Keep both the command status and Anime Logs alive so the job
        # never appears stuck at "Downloading" without explaining its state.
        while not download_task.done():
            done, _ = await asyncio.wait({download_task}, timeout=15)
            if not done:
                elapsed = int(time.monotonic() - download_started_at)
                heartbeat = (
                    f"<b>Downloading torrent:</b> {_escape(title)} "
                    f"S{target_season:02d}E{target_episode:02d}\\n"
                    f"Torrent client is still running ({elapsed}s elapsed). "
                    "FFmpeg encoding has not started yet."
                )
                await create_optional_log_message(heartbeat)
                await set_progress(
                    f"Downloading torrent for {title} S{target_season:02d}E{target_episode:02d}; "
                    f"torrent client still running ({elapsed}s)."
                )
        downloaded = _coerce_path(await download_task)
        download_elapsed = int(time.monotonic() - download_started_at)
        if download_status:
            await create_optional_log_message(
                f"<b>Download completed:</b> {_escape(title)} "
                f"S{target_season:02d}E{target_episode:02d}\\n"
                f"Torrent download finished in {download_elapsed}s; checking source video."
            )
        if not downloaded or not os.path.exists(downloaded):
            raise RuntimeError(f"Torrent download failed for {title} S{season_number}E{episode_number}.")
        video_files = find_video_files(os.path.dirname(downloaded) if os.path.isfile(downloaded) else downloaded)
        if len(video_files) != 1:
            raise RuntimeError(
                f"Expected exactly one video for {title} S{season_number}E{episode_number}; found {len(video_files)}."
            )
        source = video_files[0]
        await rep.report(
            f"Torrent download completed for {title} S{target_season:02d}E{target_episode:02d}; "
            f"found one video source. Preparing {len(missing_qualities)} missing quality encode(s).",
            "success",
        )

    for quality in missing_qualities:
        await set_progress(f"Encoding {title} S{target_season:02d}E{target_episode:02d} at {quality}p with FFmpeg.")
        upload_name = await info.get_upname(quality)
        quality_source = os.path.join("encode", f"uc_source_{safe}_{quality}.mkv")
        os.makedirs("encode", exist_ok=True)
        shutil.copy2(source, quality_source)
        encode_status = await create_optional_log_message(
            f"<b>Preparing encode:</b> {_escape(title)} S{season_number}E{episode_number} — {quality}p\\n"
            "FFmpeg has not started yet."
        )
        encoder = FFEncoder(
            encode_status, quality_source, upload_name, quality,
            force_encode=True, bypass_pause=True, quiet_errors=True,
        )
        encoded_path = None
        encode_error = None
        max_encode_attempts = 3
        for encode_attempt in range(1, max_encode_attempts + 1):
            encoder = FFEncoder(
                encode_status, quality_source, upload_name, quality,
                force_default=(encode_attempt > 1),
                fallback_mode=encode_attempt - 1,
                force_encode=True, bypass_pause=True, quiet_errors=True,
            )
            try:
                encoded_path = _coerce_path(await encoder.start_encode())
                if encoded_path and os.path.exists(encoded_path) and os.path.getsize(encoded_path) > 0:
                    encode_error = None
                    break
                encode_error = getattr(encoder, "last_error", None) or (
                    "FFmpeg did not produce a valid output file."
                )
            except Exception as current_encode_error:
                encode_error = (
                    f"{type(current_encode_error).__name__}: {current_encode_error}"
                )
                encoded_path = None
            if encode_attempt < max_encode_attempts:
                await asyncio.sleep(2 * encode_attempt)

        if not encoded_path or not os.path.exists(encoded_path) or os.path.getsize(encoded_path) <= 0:
            raise RuntimeError(
                f"Encoding failed for {title} S{target_season:02d}E{target_episode:02d} "
                f"at {quality} after {max_encode_attempts} attempts: "
                f"{encode_error or 'no output produced'}. Earlier qualities remain saved."
            )
        await set_progress(f"Uploading {title} S{target_season:02d}E{target_episode:02d} {quality}p to FILE_STORE.")
        upload_status = await create_optional_log_message(
            f"<b>Uploading:</b> {_escape(title)} S{season_number}E{episode_number} — {quality}p"
        )
        uploaded = await TgUploader(upload_status).upload(encoded_path, quality)
        if not uploaded:
            raise RuntimeError(f"FILE_STORE upload failed for S{season_number}E{episode_number} at {quality}.")
        # Keep the in-memory search index current so later episodes/retries in
        # this same /uploadchannel run can reuse the newly stored quality.
        uploaded_record = _parse_storage_record(os.path.basename(encoded_path))
        if uploaded_record:
            known_ids = {int(message_id) for _, message_id in storage_records}
            if int(uploaded.id) not in known_ids:
                storage_records.append((uploaded_record, int(uploaded.id)))
        # Use the same single-file deep-link format as /addanime. Monkey
        # already has access to FILE_STORE and resolves this scaled message ID.
        start_payload = f"get-{int(uploaded.id) * abs(int(Var.FILE_STORE))}"
        url = f"https://t.me/{share_bot_username}?start={await encode(start_payload)}"
        buttons_by_quality[quality] = InlineKeyboardButton(f"{quality}p" if quality != "HDRi" else "HDRip", url=url)

    await set_progress(f"Preparing final episode card for {title} S{target_season:02d}E{target_episode:02d}.")
    available_qualities = {quality: True for quality in buttons_by_quality}
    caption = await build_main_channel_announcement_caption(
        info, title, str(episode_number).zfill(2), available_qualities, bold_title=True
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

    # Required sequence: synopsis card -> sticker -> episode card -> sticker.
    await set_progress(f"Publishing episode card to channel {destination_id} for {title} S{target_season:02d}E{target_episode:02d}.")
    poster_path = os.path.join(workdir, f"anime_soul_s{season_number}e{episode_number}.jpg")
    poster = _coerce_path(
        await build_anime_soul_poster(
            info, title, poster_path, episode_label=str(episode_number).zfill(2)
        ),
        fallback_path=poster_path,
    )
    # A stable attempt ID is supplied by the episode retry loop. MongoDB claims
    # the publication key before Telegram send_photo, preventing a retry after
    # an accepted-but-unacknowledged send from creating a duplicate card.
    publish_attempt_id = str(publish_attempt_id or uuid.uuid4().hex)
    publish_claimed = await db.claimAnimeArchiveEpisodePublish(
        media.get("id"), season_number, episode_number, destination_id, publish_attempt_id
    )
    if not publish_claimed:
        prior_publish = await db.getAnimeArchiveEpisodePublishState(
            media.get("id"), season_number, episode_number, destination_id
        )
        if prior_publish and prior_publish.get("publish_state") == "confirmed":
            prior_message_id = prior_publish.get("message_id")
            if prior_message_id:
                await db.saveAnimeArchiveEpisodePost(
                    media.get("id"), season_number, episode_number, destination_id,
                    prior_message_id, card_version=ANIME_EPISODE_CARD_VERSION,
                )
                if int(destination_id) == int(getattr(Var, "MAIN_CHANNEL", 0) or 0):
                    await db.saveAnimeMainPost(media.get("id"), str(episode_number), prior_message_id)
                else:
                    await db.saveAnimeDestinationPost(
                        media.get("id"), str(episode_number), destination_id, prior_message_id
                    )
                await set_progress(
                    f"Recovered confirmed Telegram post for {title} "
                    f"S{season_number:02d}E{episode_number:02d}; skipped duplicate send."
                )
                shutil.rmtree(workdir, ignore_errors=True)
                return True
        state = (prior_publish or {}).get("publish_state", "unknown")
        raise RuntimeError(
            f"Publication for {title} S{season_number:02d}E{episode_number:02d} "
            f"is already {state}; refusing an unsafe duplicate retry. "
            "Reconcile the destination channel before clearing this publication lock."
        )

    if poster and os.path.exists(poster):
        # Send the filesystem path, not a BufferedReader. Pyrogram opens it
        # itself and can determine the upload size correctly.
        # Inline keyboards are a Bot API feature. USER_SESSION creates
        # channels and manages their permissions, but the actual episode
        # card must be posted by the promoted BOT_TOKEN account.
        published = await bot.send_photo(
            chat_id=destination_id,
            photo=poster,
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
    # Mark Telegram acceptance durably before any later checkpoint or cleanup.
    # If a following DB write fails, the retry recovers this message ID instead
    # of sending a second card.
    publish_confirmed = await db.finishAnimeArchiveEpisodePublish(
        media.get("id"), season_number, episode_number, destination_id,
        publish_attempt_id, published.id,
    )
    if not publish_confirmed:
        raise RuntimeError(
            f"Telegram accepted {title} S{season_number:02d}E{episode_number:02d}, "
            "but its publication claim could not be confirmed in MongoDB. "
            "The card may already exist; automatic resend is blocked to prevent duplicates."
        )

    # In explicit full-repost mode, replace the old episode card only after
    # Telegram confirms the new card, so a failed repost never loses the old post.
    previous_episode_post_id = await db.getAnimeArchiveEpisodePost(
        media.get("id"), season_number, episode_number, destination_id
    )
    if force_repost and previous_episode_post_id and int(previous_episode_post_id) != int(published.id):
        try:
            await bot.delete_messages(destination_id, int(previous_episode_post_id))
        except Exception as cleanup_error:
            LOGS.warning("Could not remove replaced episode post %s: %s", previous_episode_post_id, cleanup_error)

    # Commit the latest confirmed Telegram post ID.
    await db.saveAnimeArchiveEpisodePost(
        media.get("id"), season_number, episode_number, destination_id, published.id,
        card_version=ANIME_EPISODE_CARD_VERSION,
    )
    if int(destination_id) == int(getattr(Var, "MAIN_CHANNEL", 0) or 0):
        await db.saveAnimeMainPost(media.get("id"), str(episode_number), published.id)
    else:
        await db.saveAnimeDestinationPost(
            media.get("id"), str(episode_number), destination_id, published.id
        )
    await send_checkpointed_sticker("episode")

    await rep.report(
        f"Published {title} S{season_number}E{episode_number} with four quality buttons "
        "and AniList-ID-verified artwork/synopsis.",
        "info",
        log=False,
    )
    shutil.rmtree(workdir, ignore_errors=True)
    return True


def _selection_keyboard(items, selected, page=0):
    weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Schedule unknown"]
    page_size = 14
    page = max(0, min(int(page), max(0, (len(items) - 1) // page_size)))
    visible_indices = set(range(page * page_size, min(len(items), (page + 1) * page_size)))
    rows = []
    for day in weekdays:
        indices = [i for i, item in enumerate(items) if i in visible_indices and item["_weekday"] == day]
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
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"uc:page:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{max(1, (len(items) + page_size - 1) // page_size)}", callback_data="uc:noop"))
    if (page + 1) * page_size < len(items):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"uc:page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([
        InlineKeyboardButton(f"Create {len(selected)} channel(s)", callback_data="uc:create"),
        InlineKeyboardButton("Cancel", callback_data="uc:cancel"),
    ])
    return InlineKeyboardMarkup(rows)



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
    # Respond explicitly to non-admins instead of silently filtering the command.
    if not message.from_user or message.from_user.id not in Var.ADMINS:
        return await message.reply("⛔ This command is restricted to bot administrators.")
    if not getattr(Var, "USER_SESSION", None):
        return await message.reply(
            "⚠️ USER_SESSION is not configured. Add your Telegram user session to the deployment environment first."
        )
    status = await message.reply("Fetching currently airing anime and their release weekdays…")
    try:
        items = await _fetch_currently_airing()
        if not items:
            return await status.edit("AniList did not return any currently airing anime with a known next-episode day.")
        UPLOAD_CHANNEL_SESSIONS[message.from_user.id] = {
            "items": items,
            "selected": set(),
            "created": [],
            "page": 0,
        }
        requested_title = message.text.split(None, 1)[1].strip() if len(message.text.split(None, 1)) > 1 else ""
        if requested_title:
            matches = []
            needle = _plain_title(requested_title)
            for index, item in enumerate(items):
                aliases = (item.get("title") or {}).values()
                if any(
                    _plain_title(alias) == needle
                    for alias in aliases if alias
                ):
                    matches.append(index)
            if len(matches) != 1:
                return await status.edit(
                    f"Could not uniquely match <b>{_escape(requested_title)}</b> "
                    f"against the {len(items)} currently airing AniList entries. "
                    "Use /uploadchannel without a title to choose from the paginated list."
                )
            UPLOAD_CHANNEL_SESSIONS[message.from_user.id]["selected"] = {matches[0]}
            await status.edit(
                f"Matched <b>{_escape(_display_title(items[matches[0]]))}</b>. "
                "Starting channel creation and released-episode backfill automatically…"
            )
            from types import SimpleNamespace
            async def _command_answer(*args, **kwargs):
                return None
            synthetic_callback = SimpleNamespace(
                from_user=message.from_user,
                data="uc:create",
                message=status,
                answer=_command_answer,
            )
            return await uploadchannel_callbacks(client, synthetic_callback)
        keyboard = _selection_keyboard(items, set(), page=0)
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
    if data.startswith("uc:page:"):
        try:
            page = int(data.rsplit(":", 1)[1])
        except ValueError:
            return await callback.answer("Invalid page.", show_alert=True)
        state["page"] = max(0, page)
        await callback.message.edit_reply_markup(
            _selection_keyboard(state["items"], state["selected"], page=state["page"])
        )
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

    await callback.answer("Starting channel creation…")
    await callback.message.edit("Creating selected anime channels. Please keep the bot running…")
    user_client = None
    created = []
    archive_jobs = []
    channel_stage = "starting USER_SESSION"
    current_anime_title = "unknown"

    # Multiple archive workers share this one command message. Serialize edits
    # so concurrent workers cannot race Telegram's edit RPC or trigger FloodWait.
    archive_progress_lock = asyncio.Lock()

    async def report_stage(stage, anime_title=None):
        """Publish worker progress; status updates must never cancel archive work."""
        safe_stage = _escape(stage)
        safe_title = _escape(anime_title or current_anime_title)
        status_text = (
            f"⏳ <b>/uploadchannel progress</b>\n"
            f"<b>Anime:</b> {safe_title}\n"
            f"<b>Stage:</b> {safe_stage}\n"
            f"<b>Channels prepared:</b> {len(created)}/{len(state['selected'])}"
        )
        async with archive_progress_lock:
            try:
                await callback.message.edit(status_text)
            except Exception as status_error:
                LOGS.warning(f"Could not edit /uploadchannel progress message: {status_error}")


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
        await report_stage(channel_stage)
        await asyncio.wait_for(user_client.start(), timeout=120)
        channel_stage = "resolving bot account in USER_SESSION"
        await report_stage(channel_stage)
        # Resolve the bot through its public username in the USER session.
        # A bot-session numeric ID is not necessarily an input peer known to this
        # separate MTProto session and can raise PEER_ID_INVALID.
        # The bot account authenticated by BOT_TOKEN is the only account we
        # are allowed to promote. Never let a stale BOT_USERNAME config promote
        # a different bot into every newly created channel.
        token_bot = await asyncio.wait_for(bot.get_me(), timeout=45)
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
        bot_user = await asyncio.wait_for(user_client.get_users(bot_username), timeout=45)
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
            await report_stage(channel_stage, title)
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
                    await report_stage(channel_stage, title)
                    chat = await asyncio.wait_for(
                        user_client.get_chat(saved_invite if saved_invite else wanted_id),
                        timeout=45,
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
                        await report_stage(channel_stage, title)
                        raw_dialogs = await asyncio.wait_for(user_client.invoke(
                            GetDialogs(
                                offset_date=0,
                                offset_id=0,
                                offset_peer=InputPeerEmpty(),
                                limit=100,
                                hash=0,
                            )
                        ), timeout=60)
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
                    await report_stage(channel_stage, title)
                    chat = await asyncio.wait_for(
                        user_client.create_channel(title, f"Official episode channel for {title}"),
                        timeout=90,
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
                channel_stage = f"creating Telegram channel for {title}"
                await report_stage(channel_stage, title)
                chat = await asyncio.wait_for(
                    user_client.create_channel(title, f"Official episode channel for {title}"),
                    timeout=90,
                )

            # Persist a durable invite reference for private channels. Unlike a
            # numeric channel ID, the invite URL can repopulate Pyrogram's peer
            # cache after a fresh in-memory USER_SESSION on Heroku.
            if not persistent_invite:
                try:
                    channel_stage = f"creating persistent invite link for {title}"
                    await report_stage(channel_stage, title)
                    invite_obj = await asyncio.wait_for(
                        user_client.create_chat_invite_link(
                            chat.id, name="Anime uploader persistent peer"
                        ),
                        timeout=60,
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
                await report_stage(channel_stage, title)
                current_member = await asyncio.wait_for(
                    user_client.get_chat_member(chat.id, bot_user.id), timeout=45
                )
                current_status = str(getattr(current_member, "status", "")).lower()
            except Exception:
                current_status = ""

            if not any(role in current_status for role in ("administrator", "owner", "creator")):
                channel_stage = f"promoting bot administrator for {title}"
                await report_stage(channel_stage, title)
                await asyncio.wait_for(user_client.promote_chat_member(
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
                ), timeout=60)
                # Confirm the promotion before any season/episode upload begins.
                channel_stage = f"verifying bot administrator rights for {title}"
                await report_stage(channel_stage, title)
                confirmed_member = await asyncio.wait_for(
                    user_client.get_chat_member(chat.id, bot_user.id), timeout=45
                )
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
            # Publish only the sticker and final ANIME SOUL episode cards.
            created.append((title, chat.id, None, reused))
            archive_jobs.append((media, int(chat.id)))
            await report_stage(
                f"channel ready: {title} ({chat.id}); reused={reused}",
                title,
            )
            await asyncio.sleep(1)

        # Backfill ONLY the selected anime's currently releasing season.
        # Do not traverse prequel seasons: an ongoing Season 2 must never start
        # uploading Season 1 just because the prequel exists in AniList.
        channel_stage = "processing released episodes of the selected ongoing season"
        await report_stage(channel_stage)
        # Run at most two different anime backfills concurrently. Each worker
        # still processes that anime's episodes in strict episode order; a
        # third/fourth selected anime remains queued until a worker completes.
        archive_worker_semaphore = asyncio.Semaphore(2)

        async def process_archive_job(selected_media, destination_id):
            async with archive_worker_semaphore:
                selected_title = _display_title(selected_media)
                await report_stage(
                    f"archive worker started ({len(archive_jobs)} selected; concurrency limit 2)",
                    selected_title,
                )
                selected_title = _display_title(selected_media)
                media_status = str(selected_media.get("status") or "").upper()
                next_airing = selected_media.get("nextAiringEpisode") or {}
                next_episode_number = int(next_airing.get("episode") or 0)
                released_episode_count = max(0, next_episode_number - 1)
                season_index = (
                    _season_number_from_release(selected_title)
                    or (len(_prequel_seasons(selected_media)) + 1)
                )
                anime_id = int(selected_media.get("id") or 0)
        
                if not anime_id:
                    raise RuntimeError(
                        f"AniList ID is missing for selected anime {selected_title}; no upload was started."
                    )
                if media_status != "RELEASING":
                    raise RuntimeError(
                        f"{selected_title} is not currently RELEASING in AniList "
                        f"(status: {media_status or 'unknown'}); only ongoing-season releases are eligible."
                    )
                if released_episode_count < 1:
                    raise RuntimeError(
                        f"AniList has not confirmed a released episode for {selected_title} "
                        f"Season {season_index}; no torrent download was started."
                    )
        
                # Never call get_chat_history here: the deployed uploader may be
                # running as a Telegram bot, and bots receive BOT_METHOD_INVALID.
                # Use only durable MongoDB checkpoints for duplicate detection.
                existing_episodes = set()
                for episode_number in range(1, released_episode_count + 1):
                    if await db.getAnimeArchiveEpisodePost(
                        anime_id, season_index, episode_number, destination_id
                    ):
                        existing_episodes.add(episode_number)
                await report_stage(
                    f"ongoing season confirmed: {selected_title} S{season_index:02d}; "
                    f"{released_episode_count} episode(s) released, "
                    f"{len(existing_episodes)} already published",
                    selected_title,
                )
                await db.set_anime_archive_batch(
                    anime_id, season_index, destination_id, "in_progress",
                    "", f"released-episodes-only S{season_index} 4-quality",
                )
        
                # Preflight all released episodes first, but do not publish any episode
                # until the synopsis card and its sticker have been confirmed in-channel.
                episodes_ready_to_reuse = []
                episodes_requiring_encode = []
        
                for episode_number in range(1, released_episode_count + 1):
                    await report_stage(
                        f"checking saved FILE_STORE qualities for {selected_title} "
                        f"S{season_index:02d}E{episode_number:02d}",
                        selected_title,
                    )
                    ready = await _upload_single_episode(
                        selected_media, destination_id, season_index, episode_number,
                        user_client, progress_callback=report_stage, preflight_only=True,
                    )
                    if ready:
                        episodes_ready_to_reuse.append(episode_number)
                    else:
                        episodes_requiring_encode.append(episode_number)
        
                # Explicitly publish/reuse the single synopsis thumbnail first, then
                # its sticker. info_only exits before any episode download or upload.
                await report_stage(
                    f"publishing the ANIME SOUL synopsis card first for {selected_title}",
                    selected_title,
                )
                await _upload_single_episode(
                    selected_media, destination_id, season_index, 1,
                    user_client, progress_callback=report_stage, info_only=True,
                )
        
                await report_stage(
                    f"synopsis card and opening sticker confirmed; publishing episodes "
                    f"in order for {selected_title}",
                    selected_title,
                )
        
                # Merge both work queues and process strictly E01, E02, E03...
                # This prevents the old two-pass behavior from publishing a later
                # reusable episode before an earlier episode that needs encoding.
                reusable_set = set(episodes_ready_to_reuse)
                encode_set = set(episodes_requiring_encode)
                for episode_number in sorted(reusable_set | encode_set):
                    async def episode_progress(stage):
                        nonlocal channel_stage
                        channel_stage = stage
                        await report_stage(stage, selected_title)

                    # Retry only transient transport failures. Every completed
                    # quality is saved in FILE_STORE/MongoDB immediately, so the
                    # next attempt resumes missing work rather than re-uploading
                    # completed qualities.
                    transient_markers = (
                        "timeout", "timed out", "connection reset",
                        "connection aborted", "server disconnected",
                        "temporarily unavailable", "network is unreachable",
                        "internal server error", "floodwait", "rate limit",
                    )
                    publish_attempt_id = uuid.uuid4().hex
                    for attempt in range(1, 4):
                        try:
                            if episode_number in reusable_set:
                                await _upload_single_episode(
                                    selected_media, destination_id, season_index, episode_number,
                                    user_client, progress_callback=episode_progress,
                                    reuse_only=True, force_repost=True,
                                    publish_attempt_id=publish_attempt_id,
                                )
                                action = "reposted using saved FILE_STORE qualities"
                            else:
                                await _upload_single_episode(
                                    selected_media, destination_id, season_index, episode_number,
                                    user_client, progress_callback=episode_progress,
                                    force_repost=True,
                                    publish_attempt_id=publish_attempt_id,
                                )
                                action = "encoded missing qualities and published"
                            break
                        except Exception as episode_error:
                            error_text = f"{type(episode_error).__name__}: {episode_error}".casefold()
                            transient = isinstance(
                                episode_error,
                                (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError),
                            ) or any(marker in error_text for marker in transient_markers)
                            if not transient or attempt >= 3:
                                raise
                            delay = min(30, 5 * (2 ** (attempt - 1)))
                            await report_stage(
                                f"temporary Telegram/network error for "
                                f"S{season_index:02d}E{episode_number:02d}; "
                                f"retrying in {delay}s (attempt {attempt + 1}/3)",
                                selected_title,
                            )
                            await asyncio.sleep(delay)

                    await report_stage(
                        f"{action}: {selected_title} S{season_index:02d}E{episode_number:02d}",
                        selected_title,
                    )
        
                await db.set_anime_archive_batch(
                    anime_id, season_index, destination_id, "complete",
                    "", f"released-episodes-only S{season_index} 4-quality",
                )
                await report_stage(
                    f"ONGOING SEASON BACKFILL COMPLETE: {selected_title} Season {season_index}; "
                    f"processed only the {released_episode_count} episode(s) confirmed released by AniList",
                    selected_title,
                )
        
        

        results = await asyncio.gather(
            *(process_archive_job(media, destination_id) for media, destination_id in archive_jobs),
            return_exceptions=True,
        )
        failures = [
            f"{_display_title(archive_jobs[index][0])}: {type(result).__name__}: {result}"
            for index, result in enumerate(results)
            if isinstance(result, BaseException)
        ]
        if failures:
            await rep.report(
                "One or more /uploadchannel anime workers failed:\n" + "\n".join(failures),
                "error",
            )
            raise RuntimeError(
                f"{len(failures)} of {len(archive_jobs)} anime backfill worker(s) failed. "
                "Other workers were allowed to finish. " + "; ".join(failures)
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
        LOGS.exception(
            f"/uploadchannel failed at stage '{channel_stage}' for '{current_anime_title}'"
        )
    finally:
        if user_client:
            try:
                await user_client.stop()
            except Exception:
                pass