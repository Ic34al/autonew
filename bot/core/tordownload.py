from aiofiles import open as aiopen
from aiofiles.os import path as aiopath, remove as aioremove, mkdir
import datetime
import os
import asyncio
from aiohttp import ClientSession
from torrentp import TorrentDownloader
from bot import LOGS
from bot.core.func_utils import handle_logs
from bot.core.reporter import rep
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


# Extra tracker routes improve peer discovery when the torrent's embedded
# tracker list is incomplete or unreachable. Trackers cannot create seeders.
FALLBACK_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.moeking.me:6969/announce",
    "udp://tracker1.bt.moack.co.kr:80/announce",
    "https://tracker.tamersunion.org:443/announce",
)


def _bdecode(data, offset=0):
    token = data[offset:offset + 1]
    if token == b'i':
        end = data.index(b'e', offset)
        return int(data[offset + 1:end]), end + 1
    if token == b'l':
        values, offset = [], offset + 1
        while data[offset:offset + 1] != b'e':
            value, offset = _bdecode(data, offset)
            values.append(value)
        return values, offset + 1
    if token == b'd':
        values, offset = {}, offset + 1
        while data[offset:offset + 1] != b'e':
            key, offset = _bdecode(data, offset)
            value, offset = _bdecode(data, offset)
            values[key] = value
        return values, offset + 1
    colon = data.index(b':', offset)
    size = int(data[offset:colon])
    start = colon + 1
    return data[start:start + size], start + size


def _bencode(value):
    if isinstance(value, int):
        return b'i' + str(value).encode('ascii') + b'e'
    if isinstance(value, bytes):
        return str(len(value)).encode('ascii') + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(_bencode(item) for item in value) + b'e'
    if isinstance(value, dict):
        return b'd' + b''.join(_bencode(key) + _bencode(value[key]) for key in sorted(value)) + b'e'
    raise TypeError('Unsupported bencode value: %r' % (type(value),))


def _add_trackers_to_torrent_file(torrent_path):
    """Add fallback announce URLs to a .torrent without changing its info hash."""
    try:
        with open(torrent_path, 'rb') as torrent_file:
            raw = torrent_file.read()
        metadata, consumed = _bdecode(raw)
        if consumed != len(raw) or not isinstance(metadata, dict) or b'info' not in metadata:
            raise ValueError('Invalid torrent metainfo dictionary')
        tracker_bytes = [tracker.encode('utf-8') for tracker in FALLBACK_TRACKERS]
        existing = set(metadata.get(b'announce-list', []))
        tiers = []
        for tier in metadata.get(b'announce-list', []):
            if isinstance(tier, list):
                tiers.append(tier)
            elif isinstance(tier, bytes):
                tiers.append([tier])
        known = {url for tier in tiers for url in tier}
        for tracker in tracker_bytes:
            if tracker not in known:
                tiers.append([tracker])
        metadata[b'announce-list'] = tiers
        if not metadata.get(b'announce'):
            metadata[b'announce'] = tracker_bytes[0]
        with open(torrent_path, 'wb') as torrent_file:
            torrent_file.write(_bencode(metadata))
        return len(tracker_bytes)
    except Exception as error:
        LOGS.warning('Could not add fallback trackers to torrent file %s: %s', torrent_path, error)
        return 0

def _with_fallback_trackers(magnet_uri):
    """Add fallback tracker URLs without changing the torrent info-hash."""
    if not isinstance(magnet_uri, str) or not magnet_uri.lower().startswith("magnet:?"):
        return magnet_uri
    try:
        parts = urlsplit(magnet_uri)
        query = parse_qsl(parts.query, keep_blank_values=True)
        existing = {value.rstrip("/").casefold() for key, value in query if key == "tr"}
        for tracker in FALLBACK_TRACKERS:
            if tracker.rstrip("/").casefold() not in existing:
                query.append(("tr", tracker))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except Exception as error:
        LOGS.warning("Could not augment magnet tracker list: %s", error)
        return magnet_uri


# A stalled/seedless torrent must not hold one of the two archive workers forever.
# Large episode releases can legitimately take longer than five minutes on
# a healthy but slow swarm. Keep a generous hard ceiling; the orchestrator
# reports progress while this runs and stops the session on cancellation.
TORRENT_DOWNLOAD_TIMEOUT_SECONDS = max(
    300, int(os.environ.get("TORRENT_DOWNLOAD_TIMEOUT_SECONDS", "1800"))
)


class TorDownloader:
    def __init__(self, path="."):
        self.__downdir = path
        self.__torpath = "torrents/"
        try:
            os.makedirs(self.__downdir, exist_ok=True)
            os.makedirs(self.__torpath, exist_ok=True)
        except Exception as e:
            LOGS.error(f"Failed to create directories: {str(e)}")

    def stop(self):
        """Stop the active libtorrent session immediately (safe to call more than once)."""
        client = getattr(self, "_TorDownloader__active_client", None)
        if client is None:
            return False
        try:
            client.stop_download()
            return True
        except Exception as stop_error:
            LOGS.warning("Could not stop torrent client cleanly: %s", stop_error)
            return False

    video_exts = ('.mp4', '.mkv', '.avi', '.webm', '.wmv')

    def is_video_file(self, filename):
        return filename.lower().endswith(self.video_exts)

    async def _get_downloaded_file(self, directory, max_retries=10):
        for attempt in range(max_retries):
            try:
                items = os.listdir(directory)
                
                files = [f for f in items if os.path.isfile(os.path.join(directory, f)) and self.is_video_file(f)]
                if files:
                    valid_files = [f for f in files if not f.endswith(('.part', '.tmp', '.crdownload'))]
                    if valid_files:
                        return valid_files[0]
                
                dirs = [d for d in items if os.path.isdir(os.path.join(directory, d))]
                for subdir in dirs:
                    subdir_path = os.path.join(directory, subdir)
                    subdir_files = [f for f in os.listdir(subdir_path) 
                                    if os.path.isfile(os.path.join(subdir_path, f)) and self.is_video_file(f)]
                    if subdir_files:
                        return os.path.join(subdir, subdir_files[0])
                
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
                    
        return None

    @handle_logs
    async def download(self, torrent):
        filename = None
        try:
            if torrent.startswith("magnet:"):
                torrent = _with_fallback_trackers(torrent)
                await rep.report(
                    f"Starting magnet download with {len(FALLBACK_TRACKERS)} additional tracker routes...",
                    "info",
                )
                torp = TorrentDownloader(torrent, self.__downdir)
                self.__active_client = torp
                try:
                    await asyncio.wait_for(
                        torp.start_download(),
                        timeout=TORRENT_DOWNLOAD_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as timeout_error:
                    raise TimeoutError(
                        f"Torrent download exceeded {TORRENT_DOWNLOAD_TIMEOUT_SECONDS}s "
                        "without completing. This archive worker is being released so "
                        "the next selected anime can proceed."
                    ) from timeout_error
            else:
                await rep.report("Starting torrent file download...", "info")
                torrent_file = await self.get_torfile(torrent)
                if not torrent_file:
                    await rep.report("Failed to download torrent file", "error")
                    return None
                added_trackers = _add_trackers_to_torrent_file(torrent_file)
                await rep.report(
                    f"Torrent metadata prepared with {added_trackers} fallback tracker routes.",
                    "info",
                )
                torp = TorrentDownloader(torrent_file, self.__downdir)
                self.__active_client = torp
                try:
                    await asyncio.wait_for(
                        torp.start_download(),
                        timeout=TORRENT_DOWNLOAD_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as timeout_error:
                    raise TimeoutError(
                        f"Torrent download exceeded {TORRENT_DOWNLOAD_TIMEOUT_SECONDS}s "
                        "without completing. This archive worker is being released so "
                        "the next selected anime can proceed."
                    ) from timeout_error
                try:
                    await aioremove(torrent_file)
                except:
                    pass

            await asyncio.sleep(3)

            filename = await self._get_downloaded_file(self.__downdir)
            if not filename:
                await rep.report("Download failed - no files found", "error")
                return None
                     
            file_path = os.path.join(self.__downdir, filename)
            if not os.path.exists(file_path):
                await rep.report(f"File not found at {file_path}", "error")
                return None
                     
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                await rep.report("Downloaded file is empty", "error")
                return None
                     
            await rep.report(f"Successfully downloaded: {filename}", "info")
            return file_path

        except asyncio.CancelledError:
            # Cancellation must stop libtorrent itself, not only the Python task.
            self.stop()
            raise
        except Exception as e:
            error_msg = f"Torrent download error: {str(e)}"
            if filename:
                error_msg += f" (file: {filename})"
            await rep.report(error_msg, "error")
            return None
        finally:
            # Do not leave a seed/peer session running after completion or failure.
            self.stop()
            self.__active_client = None

    @handle_logs
    async def get_torfile(self, url, max_retries=10, delay=5):
        if not await aiopath.isdir(self.__torpath):
            await mkdir(self.__torpath)
        tor_name = url.split('/')[-1]
        des_dir = os.path.join(self.__torpath, tor_name)
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                async with ClientSession() as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            async with aiopen(des_dir, 'wb') as file:
                                async for chunk in response.content.iter_any():
                                    await file.write(chunk)
                            return des_dir
                        else:
                            await rep.report(f"Failed to download torrent file: {response.status} (Attempt {attempt}/{max_retries})", "error")
                            last_error = f"HTTP {response.status}"
            except Exception as e:
                await rep.report(f"Error downloading torrent file: {str(e)} (Attempt {attempt}/{max_retries})", "error")
                last_error = str(e)
            if attempt < max_retries:
                await asyncio.sleep(delay)
        await rep.report(f"All attempts to download torrent file failed. Last error: {last_error}", "error")
        return None
