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
                await rep.report("Starting magnet download...", "info")
                torp = TorrentDownloader(torrent, self.__downdir)
                await torp.start_download()
            else:
                await rep.report("Starting torrent file download...", "info")
                torrent_file = await self.get_torfile(torrent)
                if not torrent_file:
                    await rep.report("Failed to download torrent file", "error")
                    return None
                                 
                torp = TorrentDownloader(torrent_file, self.__downdir)
                await torp.start_download()
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
