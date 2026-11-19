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


class TorDownloader:
    def __init__(self, path="."):
        self.__downdir = path
        self.__torpath = "torrents/"
        try:
            os.makedirs(self.__downdir, exist_ok=True)
            os.makedirs(self.__torpath, exist_ok=True)
        except Exception as e:
            LOGS.error(f"Failed to create directories: {str(e)}")

    video_exts = ('.mp4', '.mkv', '.avi', '.webm', '.wmv')

    def is_video_file(self, filename):
        return filename.lower().endswith(self.video_exts)

    async def _get_downloaded_file(self, directory, max_retries=10):
        # Compatibility helper: recursively return a video path if present.
        for attempt in range(max_retries):
            try:
                for root, _, files in os.walk(directory):
                    for name in files:
                        if self.is_video_file(name) and not name.endswith(('.part', '.tmp', '.crdownload')):
                            return os.path.join(root, name)
            except Exception:
                pass
            if attempt < max_retries - 1:
                await asyncio.sleep(2)
        return None

    def _torrent_status_snapshot(self, torp):
        """Return best-effort libtorrent status without depending on public torrentp APIs."""
        try:
            downloader = getattr(torp, "_downloader", None)
            handle = getattr(downloader, "_file", None)
            if handle is None:
                return "status unavailable (torrent handle not initialized)"
            status = handle.status()
            name = getattr(status, "name", "unknown")
            state = getattr(status, "state", "unknown")
            peers = getattr(status, "num_peers", 0)
            seeds = getattr(status, "num_seeds", 0)
            done = getattr(status, "total_done", 0)
            wanted = getattr(status, "total_wanted", 0)
            rate = getattr(status, "download_rate", 0)
            progress = (100.0 * done / wanted) if wanted else 0.0
            return (f"name={name}; state={state}; peers={peers}; seeds={seeds}; "
                    f"download={rate / 1024:.1f} KiB/s; data={done / 1024 / 1024:.1f}/"
                    f"{wanted / 1024 / 1024:.1f} MiB ({progress:.1f}%)")
        except Exception as e:
            return f"status unavailable ({type(e).__name__}: {e})"

    async def _wait_for_completed_video(self, directory, torrent_task=None, torp=None, timeout=3600, stable_seconds=120, no_progress_timeout=600):
        """
        torrentp may return after the torrent is added rather than after the
        payload is complete. Never hand a growing/partial MKV to FFmpeg.
        Wait for a video file to exist and remain size-stable, with timeouts
        and periodic progress logs.
        """
        started = asyncio.get_running_loop().time()
        last_progress = started
        last_report = started
        candidate = None
        previous_size = -1
        stable_since = None

        while True:
            now = asyncio.get_running_loop().time()
            if torrent_task is not None and torrent_task.done():
                # Surface torrentp errors instead of silently waiting for a file.
                task_error = torrent_task.exception()
                if task_error:
                    raise task_error
            if now - started > timeout:
                raise TimeoutError(f"Torrent download exceeded {timeout // 60} minutes without a completed video payload.")
            if now - last_progress > no_progress_timeout:
                raise TimeoutError(f"Torrent download made no file-size progress for {no_progress_timeout // 60} minutes. The torrent may have no active seeders/peers or torrentp may not be delivering payload.")

            videos = []
            for root, _, files in os.walk(directory):
                for name in files:
                    if not self.is_video_file(name) or name.endswith(('.part', '.tmp', '.crdownload')):
                        continue
                    path = os.path.join(root, name)
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        continue
                    if size > 0:
                        videos.append((path, size))

            if videos:
                path, size = max(videos, key=lambda item: item[1])
                if path != candidate:
                    candidate = path
                    previous_size = size
                    stable_since = now
                    last_progress = now
                    await rep.report(f"Torrent payload detected: {os.path.basename(path)} ({size / 1024 / 1024:.1f} MiB). Waiting for completion.", "info")
                elif size != previous_size:
                    previous_size = size
                    stable_since = now
                    last_progress = now
                elif stable_since is not None and now - stable_since >= stable_seconds:
                    await rep.report(f"Torrent payload stable for {stable_seconds}s; treating as complete: {os.path.basename(path)} ({size / 1024 / 1024:.1f} MiB).", "info")
                    return path

                if now - last_report >= 60:
                    await rep.report(f"Torrent download still running: {os.path.basename(path)} ({size / 1024 / 1024:.1f} MiB; stable {int(now - (stable_since or now))}s).", "info")
                    last_report = now
            elif now - last_report >= 60:
                snapshot = self._torrent_status_snapshot(torp) if torp else "torrent status unavailable"
                await rep.report(f"Torrent active; waiting for video payload. {snapshot}", "info")
                last_report = now

            await asyncio.sleep(5)

    @handle_logs
    async def download(self, torrent):
        filename = None
        try:
            torrent_file = None
            if torrent.startswith("magnet:"):
                await rep.report("Starting magnet download...", "info")
                torp = TorrentDownloader(torrent, self.__downdir)
            else:
                await rep.report("Starting torrent file download...", "info")
                torrent_file = await self.get_torfile(torrent)
                if not torrent_file:
                    await rep.report("Failed to download torrent file", "error")
                    return None
                torp = TorrentDownloader(torrent_file, self.__downdir)

            # Run torrentp concurrently with file monitoring. Its start_download()
            # may block silently for the entire transfer, which previously made
            # Heroku logs/status appear frozen after the torrent was "added".
            download_task = asyncio.create_task(torp.start_download())
            await rep.report("Torrent added to session; monitoring payload size while it downloads.", "info")
            try:
                file_path = await self._wait_for_completed_video(self.__downdir, torrent_task=download_task, torp=torp)
            finally:
                if not download_task.done():
                    download_task.cancel()
                    try:
                        await download_task
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                # Explicitly remove the torrent and shut down its libtorrent session.
                # Without this, repeated attempts can leave stale sessions/handles.
                try:
                    torp.stop_download()
                except Exception as stop_error:
                    LOGS.warning(f"Torrent cleanup failed: {stop_error}")
                if torrent_file:
                    try:
                        await aioremove(torrent_file)
                    except Exception:
                        pass
            if not os.path.exists(file_path):
                await rep.report(f"File not found at {file_path}", "error")
                return None

            file_size = os.path.getsize(file_path)
            if file_size <= 0:
                await rep.report("Downloaded file is empty", "error")
                return None

            await rep.report(f"Successfully downloaded and verified: {file_path} ({file_size / 1024 / 1024:.1f} MiB)", "info")
            return file_path

        except Exception as e:
            error_msg = f"Torrent download error: {str(e)}"
            if filename:
                error_msg += f" (file: {filename})"
            await rep.report(error_msg, "error")
            return None

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
