from re import findall 
from math import floor
from time import time
from os import path as ospath
from aiofiles import open as aiopen
from aiofiles.os import remove as aioremove, rename as aiorename
from shlex import split as ssplit
from asyncio import sleep as asleep, gather, create_subprocess_shell, create_task
import asyncio
from asyncio.subprocess import PIPE
import datetime
from bot import Var, bot_loop, ffpids_cache, LOGS
from .func_utils import mediainfo, convertBytes, convertTime, sendMessage, editMessage
from .reporter import rep
from .text_utils import TextEditor
from bot.core.database import db

ffargs = {
    'HDRi': Var.FFCODE_HDRi,
    '1080': Var.FFCODE_1080,
    '720': Var.FFCODE_720,
    '480': Var.FFCODE_480,
    '360': Var.FFCODE_360,
    '240': Var.FFCODE_240,
    '144': Var.FFCODE_144,
}

quality_settings = {
    '1080': {'crf_diff': 0, 'audio_bitrate': None},
    '720': {'crf_diff': 1, 'audio_bitrate': '72k'},
    '480': {'crf_diff': 2, 'audio_bitrate': '54k'},
    '360': {'crf_diff': 3, 'audio_bitrate': '35k'},
    '240': {'crf_diff': 4, 'audio_bitrate': '25k'},
    '144': {'crf_diff': 4, 'audio_bitrate': '20k'}
}

class FFEncoder:
    def __init__(self, message, path, name, qual, is_movie=False):
        self.__proc = None
        self.is_cancelled = False
        self.message = message
        self.__name = name
        self.__qual = qual
        self.resolution_map = {
            '1080': '1920x1080',
            '720': '1280x720',
            '480': '854x480',
            '360': '640x360',
            '240': '320x240',
            '144': '256x144'
        }
        self.dl_path = path
        self.__total_time = None
        self.out_path = ospath.join("encode", name)
        self.__prog_file = 'prog.txt'
        self.__start_time = time()
        self.editor = TextEditor(name)
        self.pdata = self.editor.pdata
        self.is_movie = is_movie

    async def progress(self):
        self.__total_time = await mediainfo(self.dl_path, get_duration=True)
        try:
            self.__total_time = float(self.__total_time)
        except (ValueError, TypeError):
            self.__total_time = 20 * 60

        while not (self.__proc is None or self.is_cancelled):
            if self.__proc is not None and getattr(self.__proc, 'returncode', None) is not None:
                break
            async with aiopen(self.__prog_file, 'r+') as p:
                text = await p.read()

            if text:
                time_done = int(t[-1]) / 1_000_000 if (t := findall(r"out_time_ms=(\d+)", text)) else 0.0
                ensize = int(s[-1]) if (s := findall(r"total_size=(\d+)", text)) else 0

                diff = time() - self.__start_time
                speed = ensize / diff if diff else 1

                percent = (time_done / self.__total_time) * 100
                percent = min(round(percent, 2), 100)

                tsize = ensize / (percent / 100) if percent > 0 else ensize
                eta = (tsize - ensize) / speed if speed > 0 else 0
                eta = max(0, min(eta, 36000))

                bar_progress = min(floor(percent / 8), 12)
                bar = bar_progress * "●" + (12 - bar_progress) * "○"

                try:
                    qual_index = Var.QUALS.index(self.__qual) + 1
                except Exception:
                    qual_index = 1
                total_quals = len(Var.QUALS) if Var.QUALS else 1

                progress_str = f"""<blockquote>‣ <b>Anime Name :</b> <b><i>{self.__name}</i></b></blockquote>
<blockquote>‣ <b>Status :</b> <i>Encoding</i>
    <code>[{bar}]</code> {percent}%</blockquote> 
<blockquote>   ‣ <b>Size :</b> {convertBytes(ensize)} out of ~ {convertBytes(tsize)}
    ‣ <b>Speed :</b> {convertBytes(speed)}/s
    ‣ <b>Time Took :</b> {convertTime(diff)}
    ‣ <b>Time Left :</b> {convertTime(eta)}
    ‣ <b>Quality:</b> {self.__qual}p</blockquote>
<blockquote>‣ <b>File(s) Encoded:</b> <code>{qual_index} / {total_quals}</code></blockquote>"""

                await editMessage(self.message, progress_str)

                if (prog := findall(r"progress=(\w+)", text)) and prog[-1] == 'end':
                    break


            await asleep(2)

    async def get_ffmpeg_command(self, dl_npath, out_npath):
        try:
            anime_name = self.pdata.get('title', '').strip() if self.pdata else None
            if not anime_name:
                match = findall(r'\] (.+?) [-–] \d+', self.dl_path)
                if match:
                    anime_name = match[0].strip()

            if anime_name:
                LOGS.info(f"Checking FFmpeg config for anime: {anime_name}")
                config_to_use = None
                config_is_anime = False
                config_key = f"FFCODE_{self.__qual}" if self.__qual != 'HDRi' else 'FFCODE_HDRi'
                global_config = await db.get_global_ffmpeg(config_key)

                candidates = []
                if anime_name:
                    candidates.append(anime_name)

                if self.__name and self.__name not in candidates:
                    candidates.append(self.__name)

                expanded = []
                try:
                    from bot.core.auto_animes import get_all_possible_anime_names
                except Exception:
                    get_all_possible_anime_names = None

                for cand in candidates:
                    try:
                        if get_all_possible_anime_names:
                            variants = await get_all_possible_anime_names(cand)
                        else:
                            variants = []
                        if variants:
                            expanded.extend(variants)
                    except Exception:
                        pass

                search_list = candidates + expanded

                for key in search_list:
                    try:
                        cfg = await db.get_anime_ffmpeg(key)
                        if cfg:
                            config_to_use = cfg
                            config_is_anime = True
                            print(f"Using anime-specific FFmpeg config for: {key}")
                            break
                    except Exception:
                        continue

                if not config_to_use:
                    config_to_use = global_config

                if config_to_use:
                    await rep.report(f"Found custom FFmpeg config for: {anime_name}", "info")

                    try:
                        if config_is_anime and self.__qual != 'HDRi' and '|||' not in config_to_use:
                            quality_adjust = quality_settings.get(self.__qual, {'crf_diff': 0, 'audio_bitrate': None})
                            base_crf_match = findall(r'-crf\s+(\d+)', config_to_use)
                            if base_crf_match:
                                base_crf = int(base_crf_match[0])
                                new_crf = base_crf + quality_adjust.get('crf_diff', 0)
                                config_to_use = config_to_use.replace(f"-crf {base_crf}", f"-crf {new_crf}")
                            if quality_adjust.get('audio_bitrate'):
                                audio_matches = findall(r'-b:a\s+\w+k', config_to_use)
                                if audio_matches:
                                    config_to_use = config_to_use.replace(audio_matches[0], f"-b:a {quality_adjust['audio_bitrate']}")
                    except Exception:
                        pass

                    async def try_format(templ: str, attempts: list):
                        try:
                            mapping = {
                                'in': dl_npath,
                                'prog': self.__prog_file,
                                'res': self.resolution_map.get(self.__qual, '1920x1080'),
                                'out': out_npath,
                            }
                            try:
                                return templ.format_map(mapping)
                            except Exception:
                                pass
                        except Exception:
                            pass

                        for args in attempts:
                            try:
                                return templ.format(*args)
                            except Exception:
                                continue
                        return None

                    if "|||" in config_to_use:
                        resolution_config, hdrip_config = config_to_use.split("|||", 1)
                        resolution_config = resolution_config.strip()
                        hdrip_config = hdrip_config.strip()


                        if self.__qual == 'HDRi':
                            formatted = await try_format(hdrip_config, [
                                (dl_npath, self.__prog_file, out_npath),
                                (dl_npath, self.__prog_file, out_npath),
                            ])
                            if formatted:
                                resolution = self.resolution_map.get(self.__qual, '1920x1080')
                                if out_npath not in formatted and resolution in formatted:
                                    fixed = formatted[::-1].replace(resolution[::-1], out_npath[::-1], 1)[::-1]
                                    print(f"Fixed misplaced resolution in FFmpeg command for {anime_name}")
                                    print(f"Formatted FFmpeg command (fixed): {fixed}")
                                    return fixed
                                print(f"Formatted FFmpeg command: {formatted}")
                                return formatted
                            await rep.report(f"HDRip FFmpeg config for {anime_name} failed to format; using default.", "warning")
                            return ffargs[self.__qual].format(dl_npath, self.__prog_file, out_npath)


                        base_crf_match = findall(r'-crf\s+(\d+)', resolution_config)
                        base_crf = int(base_crf_match[0]) if base_crf_match else 23

                        modified_config = resolution_config
                        if config_is_anime:
                            quality_adjust = quality_settings.get(self.__qual, {'crf_diff': 0, 'audio_bitrate': None})
                            new_crf = base_crf + quality_adjust.get('crf_diff', 0)

                            if quality_adjust.get('audio_bitrate'):
                                audio_matches = findall(r'-b:a\s+\w+k', modified_config)
                                if audio_matches:
                                    modified_config = modified_config.replace(audio_matches[0], f"-b:a {quality_adjust['audio_bitrate']}")

                            modified_config = modified_config.replace(f"-crf {base_crf}", f"-crf {new_crf}")

                        resolution = self.resolution_map.get(self.__qual, '1920x1080')
                        print(f"Using modified config with CRF {new_crf} and resolution {resolution}", "info")


                        attempts = [
                            (dl_npath, self.__prog_file, out_npath),
                            (dl_npath, self.__prog_file, out_npath, resolution),
                            (dl_npath, self.__prog_file, resolution, out_npath),
                        ]
                        formatted = await try_format(modified_config, attempts)
                        if formatted:
                            resolution = self.resolution_map.get(self.__qual, '1920x1080')
                            if out_npath not in formatted and resolution in formatted:
                                fixed = formatted[::-1].replace(resolution[::-1], out_npath[::-1], 1)[::-1]
                                LOGS.warning(f"Fixed misplaced resolution in FFmpeg command for {anime_name}")
                                LOGS.info(f"Formatted FFmpeg command (fixed): {fixed}")
                                return fixed
                            LOGS.info(f"Formatted FFmpeg command: {formatted}")
                            return formatted

                        LOGS.error("Resolution config formatting failed; falling back to default")
                        await rep.report(f"Resolution FFmpeg config for {anime_name} failed to format; using default.", "warning")
                        return ffargs[self.__qual].format(dl_npath, self.__prog_file, out_npath)

                    else:
                        if self.__qual == 'HDRi':
                            formatted = await try_format(config_to_use, [
                                (dl_npath, self.__prog_file, out_npath),
                                (dl_npath, self.__prog_file, out_npath),
                            ])
                            if formatted:
                                resolution = self.resolution_map.get(self.__qual, '1920x1080')
                                if out_npath not in formatted and resolution in formatted:
                                    fixed = formatted[::-1].replace(resolution[::-1], out_npath[::-1], 1)[::-1]
                                    LOGS.warning(f"Fixed misplaced resolution in FFmpeg command for {anime_name}")
                                    LOGS.info(f"Formatted FFmpeg command (fixed): {fixed}")
                                    return fixed
                                LOGS.info(f"Formatted FFmpeg command: {formatted}")
                                return formatted
                            LOGS.warning("HDRi single config formatting failed; falling back")
                            await rep.report(f"HDRi FFmpeg config for {anime_name} failed to format; using default.", "warning")
                            return ffargs[self.__qual].format(dl_npath, self.__prog_file, out_npath)
                        else:
                            resolution = self.resolution_map.get(self.__qual, '1920x1080')
                            attempts = [
                                (dl_npath, self.__prog_file, out_npath),
                            ]
                            formatted = await try_format(config_to_use, attempts)
                            if formatted:
                                resolution = self.resolution_map.get(self.__qual, '1920x1080')
                                if out_npath not in formatted and resolution in formatted:
                                    fixed = formatted[::-1].replace(resolution[::-1], out_npath[::-1], 1)[::-1]
                                    LOGS.warning(f"Fixed misplaced resolution in FFmpeg command for {anime_name}")
                                    LOGS.info(f"Formatted FFmpeg command (fixed): {fixed}")
                                    return fixed
                                LOGS.info(f"Formatted FFmpeg command: {formatted}")
                                return formatted
                            LOGS.error("Single config formatting failed for resolution; falling back")
                            await rep.report(f"FFmpeg config for {anime_name} failed to format; using default.", "warning")
                            return ffargs[self.__qual].format(dl_npath, self.__prog_file, out_npath)

            LOGS.info(f"No custom config found, using default {self.__qual} config")
            return ffargs[self.__qual].format(dl_npath, self.__prog_file, out_npath)

        except Exception as e:
            LOGS.error(f"Error getting FFmpeg command: {e}")
            await rep.report(f"Error getting FFmpeg command: {e}", "warning")
            return ffargs['1080'].format(dl_npath, self.__prog_file, out_npath)

    async def start_encode(self):
        encoding_enabled = await db.get_encoding()
        if not encoding_enabled:
            if self.__qual != 'HDRi':
                LOGS.info("Skipping non-HDRi encode when encoding is disabled")
                return self.dl_path
            else:
                LOGS.info("Processing HDRi encode even though encoding is disabled")
                await rep.report("Processing HDRi encode...", "info")

        if ospath.exists(self.__prog_file):
            import logging
            logging.info(f"[CLEANUP] Removing ffmpeg progress file: {self.__prog_file}")
            await aioremove(self.__prog_file)
        async with aiopen(self.__prog_file, 'w+'):
            LOGS.info("Progress Temp Generated !")
            pass

        dl_npath = ospath.join("encode", "ffanimeadvin.mkv")
        out_npath = ospath.join("encode", "ffanimeadvout.mkv")
        await aiorename(self.dl_path, dl_npath)

        ffcode = await self.get_ffmpeg_command(dl_npath, out_npath)
        LOGS.info(f'Using FFmpeg command: {ffcode}')

        if not ffcode:
            LOGS.error("No FFmpeg command generated")
            ffcode = None

        if ffcode and out_npath not in ffcode:
            resolution = self.resolution_map.get(self.__qual, '1920x1080')
            if resolution in ffcode:
                fixed = ffcode[::-1].replace(resolution[::-1], out_npath[::-1], 1)[::-1]
                LOGS.warning(f"Auto-fixed misplaced resolution in FFmpeg command for {self.__name}")
                LOGS.info(f"FFmpeg command after auto-fix: {fixed}")
                ffcode = fixed

        if not ffcode or (self.__prog_file not in ffcode) or ('-progress' not in ffcode) or (out_npath not in ffcode):
            LOGS.error("FFmpeg command validation failed: progress file missing in command")
            await rep.report(
                f"Invalid FFmpeg command generated for {self.__name}. Falling back to default config.",
                "warning"
            )
            try:
                ffcode = ffargs[self.__qual].format(dl_npath, self.__prog_file, out_npath)
                LOGS.info(f"Falling back to default FFmpeg command: {ffcode}")
            except Exception as e:
                LOGS.error(f"Failed to build fallback ffmpeg command: {e}")
                await rep.report(f"Failed to build fallback ffmpeg command: {e}", "error")
                return None

        self.__proc = await create_subprocess_shell(ffcode, stdout=PIPE, stderr=PIPE)
        proc_pid = self.__proc.pid
        ffpids_cache.append(proc_pid)

        progress_task = create_task(self.progress())

        try:
            await self.__proc.wait()
        finally:
            try:
                progress_task.cancel()
                await progress_task
            except asyncio.CancelledError:
                pass

        return_code = self.__proc.returncode

        try:
            ffpids_cache.remove(proc_pid)
        except ValueError:
            pass

        await aiorename(dl_npath, self.dl_path)

        if self.is_cancelled:
            LOGS.info("Encoding was cancelled.")
            await rep.report("Encoding was cancelled.", "warning")
            return None

        if return_code == 0:
            if ospath.exists(out_npath):
                await aiorename(out_npath, self.out_path)
            LOGS.info(f"Encoding successful! Output file: {self.out_path}")
            return self.out_path
        else:
            try:
                error_message = (await self.__proc.stderr.read()).decode().strip()
            except Exception:
                error_message = "(failed to read stderr)"
            LOGS.error(f"Encoding failed with error: {error_message}")
            await rep.report(f"Encoding failed: {error_message[:200]}...", "error")
            return None
    
    async def cancel_encode(self):
        self.is_cancelled = True
        if self.__proc is not None:
            try:
                self.__proc.kill()
            except:
                pass
