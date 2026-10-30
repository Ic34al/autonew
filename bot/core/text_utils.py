import httpx
from calendar import month_name
from datetime import datetime
from random import choice
from asyncio import sleep as asleep
from aiohttp import ClientSession, ClientTimeout
from anitopy import parse
import re
from bot import Var, bot
from .ffargs import ffargs
from .func_utils import handle_logs
from .reporter import rep
from .database import db
import asyncio

_manga_anilist_lock = asyncio.Lock()
_manga_anilist_last_call = 0
_anime_anilist_lock = asyncio.Lock()
_anime_anilist_last_call = 0

MANUAL_ANILIST_NAMES = {
    "Im Living With an Otaku NEET Kunoichi": "I'm Living With an Otaku NEET Kunoichi"
}

CAPTION_FORMAT = """
<blockquote><b>❐<i> {title} </i></b></blockquote>
<b>✦─━━━━━━━━━━─✦</b>
<b>›› Sᴇᴀꜱᴏɴ: {anime_season}</b>
<b>›› Eᴘɪsᴏᴅᴇ: {ep_no}</b>
<b>››Qᴜᴀʟɪᴛʏ : 480ᴘ | 720ᴘ | 1080ᴘ</b>
<b>›› Lᴀɴɢᴜᴀɢᴇ: {lang}</b>
<b>✦─━━━━━━━━━━─✦</b>
<blockquote><b>≡ Sᴛᴀᴛᴜꜱ: {status}</b></blockquote>
<blockquote><b>≡ Pᴏᴡᴇʀᴇᴅ Bʏ : <a href='https://t.me/Ongoing_Anime_Time'>Ongoing Anime Time</a></b></blockquote>
<b>✦─━━━━━━━━━━─✦</b>
<blockquote><b>≡ Pᴏᴡᴇʀᴇᴅ Bʏ : <a href='https://t.me/Ongoing_Anime_Time'>Ongoing Anime Time</a></b></blockquote>
<blockquote><b>≡ ᴜꜱᴇ <a href='https://play.google.com/store/apps/details?id=com.mxtech.videoplayer.ad'>vlc player</a> or <a href='https://play.google.com/store/apps/details?id=com.mxtech.videoplayer.ad'>mx player</a> ᴛᴏ ᴄʜᴀɴɢᴇ ᴀᴜᴅɪᴏ ᴀɴᴅ ꜱᴜʙᴛɪᴛʟᴇꜱ ꜰᴏʀ ᴀ ʙᴇᴛᴛᴇʀ ᴠɪᴇᴡɪɴɢ ᴇxᴩᴇʀɪᴇɴᴄᴇ.</b></blockquote>
"""

MOVIE_CAPTION = """
<blockquote><b>❐<i> {title} </i></b></blockquote>
<b>✦─━━━━━━━━━━─✦</b>
<b>›› Sᴇᴀꜱᴏɴ: {anime_season}</b>
<b>›› Eᴘɪsᴏᴅᴇ: {ep_no}</b>
<b>››Qᴜᴀʟɪᴛʏ : 480ᴘ | 720ᴘ | 1080ᴘ</b>
<b>›› Lᴀɴɢᴜᴀɢᴇ: {lang}</b>
<b>✦─━━━━━━━━━━─✦</b>
<blockquote><b>≡ Sᴛᴀᴛᴜꜱ: {status}</b></blockquote>
<blockquote><b>≡ Pᴏᴡᴇʀᴇᴅ Bʏ : <a href='https://t.me/Ongoing_Anime_Time'>Ongoing Anime Time</a></b></blockquote>
<b>✦─━━━━━━━━━━─✦</b>
<blockquote><b>≡ Pᴏᴡᴇʀᴇᴅ Bʏ : <a href='https://t.me/Ongoing_Anime_Time'>Ongoing Anime Time</a></b></blockquote>
<blockquote><b>≡ ᴜꜱᴇ <a href='https://play.google.com/store/apps/details?id=com.mxtech.videoplayer.ad'>vlc player</a> or <a href='https://play.google.com/store/apps/details?id=com.mxtech.videoplayer.ad'>mx player</a> ᴛᴏ ᴄʜᴀɴɢᴇ ᴀᴜᴅɪᴏ ᴀɴᴅ ꜱᴜʙᴛɪᴛʟᴇꜱ ꜰᴏʀ ᴀ ʙᴇᴛᴛᴇʀ ᴠɪᴇᴡɪɴɢ ᴇxᴩᴇʀɪᴇɴᴄᴇ.</b></blockquote>
"""

MANGA_CAPTION_FORMAT = """<blockquote><b>✦ {title} ✦</b></blockquote>
<b>╔━━━━━━━━━━━━━━━━━━━━━╗</b>
<blockquote><b>⌲ 𝗧𝘆𝗽𝗲:</b> <i>Manga</i>
<b>❍ 𝗦𝘁𝗮𝘁𝘂𝘀:</b> <i>Releasing</i></blockquote>
<blockquote><b>◎ 𝗩𝗼𝗹𝘂𝗺𝗲:</b> <i>{volume}</i>
<b>⎇ 𝗖𝗵𝗮𝗽𝘁𝗲𝗿:</b> <i>{chapter}</i></blockquote>
<blockquote><b>♡ 𝗚𝗲𝗻𝗿𝗲𝘀:</b> <i>{genres}</i>
<b>▸ 𝗗𝗲𝘀𝗰𝗿𝗶𝗽𝘁𝗶𝗼𝗻:</b> <i>{description}</i></blockquote>
<b>╚━━━━━━━━━━━━━━━━━━━━━╝</b>"""


GENRES_EMOJI = {"Action": "👊", "Adventure": choice(['🪂', '🧗‍♀']), "Comedy": "🤣", "Drama": " 🎭", "Ecchi": choice(['💋', '🥵']), "Fantasy": choice(['🧞', '🧞‍♂', '🧞‍♀','🌗']), "Hentai": "🔞", "Horror": "☠", "Mahou Shoujo": "☯", "Mecha": "🤖", "Music": "🎸", "Mystery": "🔮", "Psychological": "♟", "Romance": "💞", "Sci-Fi": "🛸", "Slice of Life": choice(['☘','🍁']), "Sports": "⚽️", "Supernatural": "🫧", "Thriller": choice(['🥶', '🔪','🤯'])}


def obfuscate_text_for_copyright(text: str) -> str:
    import re
    
    replacements = {
        'O': '0',
        'o': '0',
        'R': 'π',
        'r': 'π',
        'E': '£',
        'e': '£',
        'S': '$',
        's': '$',
        'l': 'I',
        'C': '¢',
        'c': '¢',
    }
    
    excluded_words = {'season', 'episode'}
    
    parts = re.split(r'(<[^>]*>)', text)
    
    result = []
    for part in parts:
        if part.startswith('<'):
            result.append(part)
        else:
            def replace_word(match):
                word = match.group(0)
                if word.lower() in excluded_words:
                    return word
                modified = word
                for char, replacement in replacements.items():
                    modified = modified.replace(char, replacement)
                return modified
            
            modified = re.sub(r'\b\w+\b', replace_word, part)
            result.append(modified)
    
    return ''.join(result)


ANIME_GRAPHQL_QUERY = """
query ($id: Int, $search: String, $seasonYear: Int) {
  Media(id: $id, type: ANIME, format_not_in: [MUSIC, MOVIE, MANGA, NOVEL, ONE_SHOT], search: $search, seasonYear: $seasonYear) {
    id
    idMal
    title {
      romaji
      english
      native
    }
    type
    format
    status(version: 2)
    description(asHtml: false)
    startDate {
      year
      month
      day
    }
    endDate {
      year
      month
      day
    }
    season
    seasonYear
    episodes
    duration
    chapters
    volumes
    countryOfOrigin
    source
    hashtag
    trailer {
      id
      site
      thumbnail
    }
    updatedAt
    coverImage {
      large
    }
    bannerImage
    genres
    synonyms
    averageScore
    meanScore
    popularity
    trending
    favourites
    studios {
      nodes {
         name
         siteUrl
      }
    }
    isAdult
    nextAiringEpisode {
      airingAt
      timeUntilAiring
      episode
    }
    airingSchedule {
      edges {
        node {
          airingAt
          timeUntilAiring
          episode
        }
      }
    }
    externalLinks {
      url
      site
    }
    siteUrl
  }
}
"""

MOVIE_GRAPHQL_QUERY = """
query ($id: Int, $search: String, $seasonYear: Int) {
  Media(id: $id, type: ANIME, format: MOVIE, search: $search, seasonYear: $seasonYear) {
    id
    idMal
    title {
      romaji
      english
      native
    }
    type
    format
    status(version: 2)
    description(asHtml: false)
    startDate {
      year
      month
      day
    }
    endDate {
      year
      month
      day
    }
    duration
    countryOfOrigin
    source
    hashtag
    trailer {
      id
      site
      thumbnail
    }
    updatedAt
    coverImage {
      large
    }
    bannerImage
    genres
    synonyms
    averageScore
    meanScore
    popularity
    trending
    favourites
    studios {
      nodes {
         name
         siteUrl
      }
    }
    isAdult
    externalLinks {
      url
      site
    }
    siteUrl
  }
}
"""

def is_movie_title(name: str) -> bool:

    name_lower = name.lower()
    if "movie" in name_lower:
        return True
    if re.search(r'\\b(19|20)\\d{2}\\b', name):
        return True
    return False

MANGA_GRAPHQL_QUERY = '''
query ($search: String) {
  Media(type: MANGA, search: $search) {
    id
    title {
      romaji
      english
      native
    }
    description
    status(version: 2)
    volumes
    chapters
    genres
    averageScore
    meanScore
    popularity
    coverImage {
      large
    }
    bannerImage
    siteUrl
  }
}
'''


async def fetch_anilist_v2(
    anime_name: str,
    year: int,
    is_movie: bool = False,
):
    query = MOVIE_GRAPHQL_QUERY if is_movie else ANIME_GRAPHQL_QUERY

    try:
        timeout = httpx.Timeout(20.0, connect=10.0)

        async with httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "AutoAnimeBot/1.0",
            },
        ) as client:

            response = await client.post(
                "https://graphql.anilist.co",
                json={
                    "query": query,
                    "variables": {
                        "search": anime_name,
                    },
                },
            )

            response.raise_for_status()
            data = response.json()

        if data.get("errors"):
            return None

        return data.get("data", {}).get("Media")

    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
    ) as exc:

        print(
            f"AniList lookup failed for {anime_name}: "
            f"{type(exc).__name__}: {exc}"
        )

        return None

class AniLister:


    def __init__(self, anime_name: str, year: int, is_movie: bool = False) -> None:
        self.__api = "https://graphql.anilist.co"
        self.__ani_name = anime_name
        self.__ani_year = year
        self.__vars = {'search': self.__ani_name, 'seasonYear': self.__ani_year}
        self.is_movie = is_movie

    def __update_vars(self, year=True) -> None:
        if year:
            self.__ani_year -= 1
            self.__vars['seasonYear'] = self.__ani_year
        else:
            self.__vars = {'search': self.__ani_name}

    async def post_data(self):
        global _anime_anilist_last_call

        async with _anime_anilist_lock:
            now = asyncio.get_event_loop().time()
            wait_time = 2.1 - (now - _anime_anilist_last_call)

            if wait_time > 0:
                await asyncio.sleep(wait_time)

            _anime_anilist_last_call = asyncio.get_event_loop().time()

        query = (
            MOVIE_GRAPHQL_QUERY
            if self.is_movie
            else ANIME_GRAPHQL_QUERY
        )

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "AutoAnimeBot/1.0",
        }

        try:
            async with ClientSession(
                timeout=ClientTimeout(total=25),
                headers=headers,
            ) as session:
                async with session.post(
                    self.__api,
                    json={
                        "query": query,
                        "variables": self.__vars,
                    },
                ) as response:
                    if response.status != 200:
                        body = await response.text()

                        await rep.report(
                            f"AniList HTTP {response.status}: {body[:500]}",
                            "warning",
                            log=False,
                        )

                        return (
                            response.status,
                            None,
                            response.headers,
                        )

                    try:
                        data = await response.json(content_type=None)
                    except Exception as exc:
                        await rep.report(
                            f"AniList returned invalid JSON: {exc}",
                            "warning",
                            log=False,
                        )
                        return 0, None, response.headers

                    return response.status, data, response.headers

        except Exception as exc:
            await rep.report(
                f"AniList request failed: {type(exc).__name__}: {exc}",
                "warning",
                log=False,
            )
            return 0, None, {}


    async def get_anidata(
        self,
        retry_count=0,
        max_retries=Var.MAX_RETRIES,
    ):
        self.__vars = {"search": self.__ani_name}

        res_code, resp_json, res_heads = await self.post_data()

        if res_code == 200 and resp_json:
            errors = resp_json.get("errors") or []
            data = resp_json.get("data") or {}
            media = data.get("Media")

            if media:
                return media

            if errors:
                messages = "; ".join(
                    str(error.get("message", "Unknown GraphQL error"))
                    for error in errors
                )
                await rep.report(
                    f"AniList GraphQL error for {self.__ani_name}: "
                    f"{messages[:500]}",
                    "warning",
                    log=False,
                )
            else:
                await rep.report(
                    f"AniList found no matching anime: {self.__ani_name}",
                    "warning",
                    log=False,
                )
            return {}

        if res_code == 429 or 500 <= res_code <= 599:
            if retry_count >= max_retries:
                await rep.report(
                    f"AniList request failed with HTTP {res_code}; "
                    f"maximum retries reached for {self.__ani_name}",
                    "error",
                    log=False,
                )
                return {}

            if res_code == 429:
                try:
                    wait_seconds = max(
                        1, int(res_heads.get("Retry-After", 60))
                    )
                except (TypeError, ValueError):
                    wait_seconds = 60
            else:
                wait_seconds = min(5 * (retry_count + 1), 20)

            await asleep(wait_seconds)

            return await self.get_anidata(
                retry_count=retry_count + 1,
                max_retries=max_retries,
            )

        if res_code == 404:
            await rep.report(
                f"AniList returned HTTP 404 for "
                f"{self.__ani_name}. Continuing without metadata.",
                "warning",
                log=False,
            )
            return {}

        await rep.report(
            f"AniList request failed with HTTP {res_code} "
            f"for {self.__ani_name}",
            "error",
            log=False,
        )
        return {}

class MangaLister:
    def __init__(self, manga_name: str, year: int) -> None:
        self.__api = "https://graphql.anilist.co"
        self.__manga_name = manga_name
        self.__manga_year = year
        self.__vars = {'search': self.__manga_name, 'seasonYear': self.__manga_year}

    def __update_vars(self, year=True) -> None:
        if year:
            self.__manga_year -= 1
            self.__vars['seasonYear'] = self.__manga_year
        else:
            self.__vars = {'search': self.__manga_name}

    async def post_data(self):
        global _manga_anilist_last_call
        async with _manga_anilist_lock:
            now = asyncio.get_event_loop().time()
            wait_time = 1 - (now - _manga_anilist_last_call)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            _manga_anilist_last_call = asyncio.get_event_loop().time()
        async with ClientSession() as sess:
            async with sess.post(self.__api, json={'query': MANGA_GRAPHQL_QUERY, 'variables': self.__vars}) as resp:
                res_code = resp.status
                resp_json = await resp.json()
                res_heads = resp.headers
                return res_code, resp_json, res_heads

    async def get_mangadata(self, retry_count=0, max_retries=Var.MAX_RETRIES):
        res_code, resp_json, res_heads = await self.post_data()
        while res_code == 404 and self.__manga_year > 2000:
            self.__update_vars()
            await rep.report(f"AniList Manga Query Name: {self.__manga_name}, Retrying with {self.__manga_year}", "warning", log=False)
            res_code, resp_json, res_heads = await self.post_data()
        if res_code == 404:
            self.__update_vars(year=False)
            res_code, resp_json, res_heads = await self.post_data()
        if res_code == 200 and resp_json:
            return resp_json.get('data', {}).get('Media', {}) or {}
        elif res_code == 429:
            if retry_count >= max_retries:
                return {}
            f_timer = int(res_heads.get('Retry-After', 60))
            await rep.report(f"AniList Manga API FloodWait: {res_code}, Sleeping for {f_timer} !! (Retry {retry_count+1}/{max_retries})", "error")
            await asleep(f_timer)
            return await self.get_mangadata(retry_count=retry_count+1, max_retries=max_retries)
        elif res_code in [500, 501, 502]:
            if retry_count >= max_retries:
                return {}
            await rep.report(f"AniList Manga Server API Error: {res_code}, Waiting 5s to Try Again !! (Retry {retry_count+1}/{max_retries})", "error")
            await asleep(5)
            return await self.get_mangadata(retry_count=retry_count+1, max_retries=max_retries)
        else:
            await rep.report(f"AniList Manga API Error: {res_code}", "error", log=False)
            return {}


async def get_anilist_titles_from_cleaned(name: str) -> list:
    try:
        lister = MangaLister(name, datetime.now().year)
        data = await lister.get_mangadata()
        if not data:
            return []
        titles = set()
        t = data.get('title', {})
        for key in ('romaji', 'english', 'native'):
            val = t.get(key) if isinstance(t, dict) else None
            if val and isinstance(val, str):
                titles.add(val.strip())

        syns = data.get('synonyms') or []
        for s in syns:
            if s and isinstance(s, str):
                titles.add(s.strip())

        if not titles:
            titles.add(name.strip())

        return list(titles)
    except Exception as e:
        await rep.report(f"get_anilist_titles_from_cleaned error: {e}", "warning")
        return []
