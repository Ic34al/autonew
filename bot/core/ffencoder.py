from re import findall 
from math import floor
from time import time
from os import path as ospath
from aiofiles import open as aiopen
from aiofiles.os import remove as aioremove, rename as aiorename
from shlex import split as ssplit
from asyncio import sleep as asleep, gather, create_subprocess_shell, create_task
import asyncio
import re
import shlex
import uuid
import os
import shutil
import imageio_ffmpeg
from asyncio.subprocess import PIPE
import datetime
from bot import Var, bot_loop, ffpids_cache, LOGS, ani_cache
from .func_utils import mediainfo, convertBytes, convertTime, sendMessage, editMessage
from .reporter import rep
from .text_utils import TextEditor
from bot.core.database import db

# Global process-wide cap: no more than two FFmpeg processes may run at once.
FFENCODE_SEMAPHORE = asyncio.Semaphore(2)

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
    def __init__(
        self, message, path, name, qual, is_movie=False,
        force_default=False, force_encode=False, bypass_pause=False,
        quiet_errors=False, fallback_mode=0, size_target_mib=None,
    ):
        self.force_default = bool(force_default)
        self.fallback_mode = max(0, min(2, int(fallback_mode or 0)))
        # None means use the normal target for the requested resolution.
        # Explicit values are reserved for oversized-file recovery retries.
        self.size_target_mib = (
            max(650, min(1700, int(size_target_mib)))
            if size_target_mib is not None else None
        )
        self.force_encode = bool(force_encode)
        self.bypass_pause = bool(bypass_pause)
        self.quiet_errors = bool(quiet_errors)
        self.last_error = None
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
        # Unique temp paths prevent concurrent anime jobs from clobbering
        # another encode's input, progress file, or output.
        self._run_token = uuid.uuid4().hex[:12]
        self.__prog_file = f"prog_{self._run_token}.txt"
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
            if ani_cache.get('global_pause') and not self.bypass_pause:
                self.is_cancelled = True
                if self.__proc is not None and self.__proc.returncode is None:
                    await self._terminate_process_group()
                break
            if self.__proc is not None and getattr(self.__proc, 'returncode', None) is not None:
                break
            try:
                async with aiopen(self.__prog_file, 'r+') as p:
                    text = await p.read()
            except (FileNotFoundError, OSError) as exc:
                LOGS.warning(f"FFmpeg progress file temporarily unavailable: {exc}")
                await asleep(8)
                continue

            if text:
                time_done = int(t[-1]) / 1_000_000 if (t := findall(r"out_time_ms=(\d+)", text)) else 0.0
                ensize = int(s[-1]) if (s := findall(r"total_size=(\d+)", text)) else 0

                diff = time() - self.__start_time
                speed = ensize / diff if diff else 1

                percent = (time_done / self.__total_time) * 100
                percent = min(round(percent, 2), 100)

                # Estimate remaining encode time from media-time throughput.
                # Output bitrate varies by scene, so extrapolating total file size
                # from current bytes/percent produces misleading "real size" figures.
                encode_speed = (time_done / diff) if diff > 0 and time_done > 0 else 0
                remaining_media = max(0, self.__total_time - time_done)
                eta = remaining_media / encode_speed if encode_speed > 0 else 0
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
<blockquote>   ‣ <b>Generated so far :</b> {convertBytes(ensize)}
    ‣ <b>Speed :</b> {convertBytes(speed)}/s
    ‣ <b>Time Took :</b> {convertTime(diff)}
    ‣ <b>Time Left :</b> {convertTime(eta)}
    ‣ <b>Quality:</b> {self.__qual}p</blockquote>
<blockquote>‣ <b>File(s) Encoded:</b> <code>{qual_index} / {total_quals}</code></blockquote>"""

                try:
                    await editMessage(self.message, progress_str)
                except Exception as status_error:
                    # A failed Telegram heartbeat must not stop FFmpeg or lose a valid output.
                    LOGS.warning(
                        f"FFmpeg progress edit failed for {self.__name} ({self.__qual}p): "
                        f"{type(status_error).__name__}: {status_error}"
                    )

                if (prog := findall(r"progress=(\w+)", text)) and prog[-1] == 'end':
                    break


            await asleep(2)

    async def get_ffmpeg_command(self, dl_npath, out_npath):
        try:
            if self.fallback_mode >= 2:
                # Last-resort profile: drop subtitle/data streams and transcode
                # the first audio stream to AAC. This avoids failures caused by
                # unsupported subtitle codecs, copied audio codecs, or MKV
                # attachment/data streams in custom templates.
                resolution = self.resolution_map.get(self.__qual, "1920x1080")
                return (
                    "ffmpeg -hide_banner -y "
                    "-filter_threads 1 -filter_complex_threads 1 "
                    f"-i {shlex.quote(dl_npath)} "
                    "-map 0:v:0 -map 0:a:0? "
                    "-map_metadata 0 -map_chapters 0 "
                    "-c:v libx264 -preset ultrafast "
                    f"-s {resolution} -pix_fmt yuv420p -crf 32 "
                    "-c:a aac -b:a 96k -ac 2 -sn -dn "
                    "-threads 1 -max_muxing_queue_size 4096 "
                    f"-progress {shlex.quote(self.__prog_file)} -nostats "
                    f"{shlex.quote(out_npath)}"
                )
            if self.force_default:
                # First fallback: exclude attachments/data and map only
                # ordinary video, audio and optional subtitle streams.
                resolution = self.resolution_map.get(self.__qual, "1920x1080")
                safe_command = (
                    "ffmpeg -hide_banner -y "
                    "-filter_threads 1 -filter_complex_threads 1 "
                    f"-i {shlex.quote(dl_npath)} "
                    "-map 0:v:0 -map 0:a? -map 0:s? "
                    "-map_metadata 0 -map_chapters 0 "
                    "-c:v libx264 -preset veryfast "
                    f"-s {resolution} -pix_fmt yuv420p -crf 30 "
                    "-c:a libopus -b:a 48k -ac 2 -c:s copy "
                    "-threads 1 -max_muxing_queue_size 2048 "
                    f"-progress {shlex.quote(self.__prog_file)} -nostats "
                    f"{shlex.quote(out_npath)}"
                )
                LOGS.warning(
                    f"Using FFmpeg fallback 1/2 for {self.__name} "
                    f"({self.__qual}p)."
                )
                return safe_command
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
        # Prefer the host's FFmpeg, but fall back to the self-contained binary
        # bundled by imageio-ffmpeg on Python-buildpack deployments.
        # Resolve and verify the actual executable before building any command.
        # Some buildpack deployments do not install system packages from Dockerfile.
        # Ignore the old temporary shim directory: earlier releases could
        # accidentally resolve ffmpeg to /tmp/.../ffmpeg and then symlink that
        # path back to itself, creating ELOOP ("Too many levels of symlinks").
        shim_dir = "/tmp/autoanime-ffmpeg-bin"
        discovered_ffmpeg = shutil.which("ffmpeg")
        candidates = [discovered_ffmpeg] if discovered_ffmpeg else []
        try:
            bundled_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            if bundled_ffmpeg not in candidates:
                candidates.append(bundled_ffmpeg)
        except Exception as exc:
            LOGS.warning(f"Could not locate bundled imageio-ffmpeg binary: {exc}")

        verified_ffmpeg = None
        ffmpeg_probe_errors = []
        seen_realpaths = set()
        for candidate in candidates:
            if not candidate:
                continue
            candidate = ospath.realpath(ospath.abspath(candidate))
            if candidate.startswith(ospath.realpath(shim_dir) + os.sep):
                ffmpeg_probe_errors.append(f"{candidate!r} points inside the temporary shim directory")
                continue
            if candidate in seen_realpaths:
                continue
            seen_realpaths.add(candidate)
            if not ospath.isfile(candidate) or not os.access(candidate, os.X_OK):
                ffmpeg_probe_errors.append(f"{candidate!r} is missing or not executable")
                continue
            try:
                probe = await asyncio.create_subprocess_exec(
                    candidate, "-version",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                probe_code = await probe.wait()
                if probe_code == 0:
                    verified_ffmpeg = candidate
                    break
                ffmpeg_probe_errors.append(f"{candidate!r} returned exit code {probe_code}")
            except Exception as exc:
                ffmpeg_probe_errors.append(f"{candidate!r}: {type(exc).__name__}: {exc}")

        if not verified_ffmpeg:
            self.last_error = "FFmpeg is unavailable or cannot run: " + "; ".join(ffmpeg_probe_errors)
            LOGS.error(self.last_error)
            if not self.quiet_errors:
                await rep.report(
                    "FFmpeg could not be started in this deployment. Install the system "
                    "ffmpeg package or redeploy with the repository's FFmpeg-enabled "
                    "Docker/Nixpacks configuration. Encoding was not started.",
                    "error",
                )
            return None
        ffmpeg_executable = verified_ffmpeg

        # Use the real, probed executable directly. Do not create a symlink
        # shim: a stale shim can be selected by shutil.which() and link to itself.
        # Adding the binary's actual directory to PATH still supports custom
        # templates that invoke the bare command "ffmpeg".
        try:
            ffmpeg_executable = ospath.realpath(verified_ffmpeg)
            if not ospath.isfile(ffmpeg_executable) or not os.access(ffmpeg_executable, os.X_OK):
                raise RuntimeError(f"Resolved FFmpeg is not executable: {ffmpeg_executable}")
            os.environ["PATH"] = (
                ospath.dirname(ffmpeg_executable) + os.pathsep
                + os.environ.get("PATH", "")
            )
            final_probe = await asyncio.create_subprocess_exec(
                ffmpeg_executable, "-version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            if await final_probe.wait() != 0:
                raise RuntimeError(f"Resolved FFmpeg failed its final probe: {ffmpeg_executable}")
        except Exception as exc:
            self.last_error = f"Could not resolve a safe FFmpeg executable: {type(exc).__name__}: {exc}"
            LOGS.exception(self.last_error)
            if not self.quiet_errors:
                await rep.report(self.last_error, "error")
            return None

        if ani_cache.get('global_pause') and not self.bypass_pause:
            self.last_error = "Encoding skipped because /pause is active."
            LOGS.info(self.last_error)
            return None
        encoding_enabled = await db.get_encoding()
        if not encoding_enabled and not self.force_encode:
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

        dl_npath = ospath.join("encode", f"ffanimeadvin_{self._run_token}.mkv")
        out_npath = ospath.join("encode", f"ffanimeadvout_{self._run_token}.mkv")
        # Remove only this attempt's temporary output; never reuse a partial MKV.
        try:
            if ospath.exists(out_npath):
                await aioremove(out_npath)
        except FileNotFoundError:
            pass
        await aiorename(self.dl_path, dl_npath)

        ffcode = await self.get_ffmpeg_command(dl_npath, out_npath)

        # Fast-encode profile: ultrafast reduces CPU time. Two encoder
        # threads per job are a modest speed-up while limiting CPU/RAM pressure
        # when the archive worker runs two FFmpeg processes concurrently.
        if ffcode:
            ffcode, preset_count = re.subn(
                r"(?i)(-preset\s+)(ultrafast|superfast|veryfast|faster|fast|medium|slow|slower|veryslow)",
                r"\1ultrafast",
                ffcode,
            )
            ffcode, thread_count = re.subn(
                r"(?i)(?<![\w-])-threads\s+\d+(?!\d)",
                "-threads 2",
                ffcode,
            )
            LOGS.info(
                f"Fast encode profile applied for {self.__name} ({self.__qual}p): "
                f"ultrafast preset; 2 encoder threads per job "
                f"(presets updated: {preset_count}, thread settings updated: {thread_count})."
            )

        # Anime-specific HDRip templates sometimes contain an empty -map
        # argument or require a subtitle stream that the source does not have.
        # Drop empty map arguments and make subtitle maps optional so a
        # subtitle-less episode can still produce its HDRip file.
        if self.__qual == "HDRi" and ffcode:
            # HDRi templates historically use "-map 0", which also selects
            # MKV attachments/data streams. Those streams frequently make
            # FFmpeg fail even though the video itself is valid. Map only
            # video, optional audio and optional subtitles instead.
            ffcode = re.sub(
                r"""(?i)(?:^|\s)-map\s+(?:''|"")(?=\s|$)""",
                " ",
                ffcode,
            )
            ffcode = re.sub(
                r"(?i)(-map\s+)0(?=\s|$)",
                r"\g<1>0:v:0 -map 0:a? -map 0:s?",
                ffcode,
            )
            ffcode = re.sub(
                r"(?i)(-map\s+)(0:s(?::\d+)?)(?=\s|$)",
                r"\1\2?",
                ffcode,
            )
            # Explicitly disable data streams even if a custom template
            # contains a broad mapping option.
            if not re.search(r"(?i)(?:^|\s)-dn(?:\s|$)", ffcode):
                ffcode = ffcode.replace(" -y", " -dn -y", 1) if " -y" in ffcode else ffcode + " -dn"

        # Limit FFmpeg memory usage on small Heroku dynos.
        if ffcode and ffcode.startswith("ffmpeg "):
            ffcode = ffcode.replace(
                "ffmpeg ",
                "ffmpeg -filter_threads 1 -filter_complex_threads 1 ",
                1,
            )

        if ffcode:
            output_name = f"'{out_npath}'"
            double_output_name = f'"{out_npath}"'

            if output_name in ffcode:
                ffcode = ffcode.replace(
                    output_name, f"-threads 1 {output_name}", 1
                )
            elif double_output_name in ffcode:
                ffcode = ffcode.replace(
                    double_output_name,
                    f"-threads 1 {double_output_name}",
                    1,
                )
            elif out_npath in ffcode:
                ffcode = ffcode.replace(
                    out_npath, f"-threads 1 {out_npath}", 1
                )
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

        # Commands use the generic ffmpeg executable name. Replace it with
        # the resolved binary, including imageio-ffmpeg's bundled executable.
        if ffcode:
            ffcode = re.sub(
                r"^ffmpeg(?=\s)",
                shlex.quote(ffmpeg_executable),
                ffcode,
                count=1,
            )

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

        # Enforce the requested dimensions for downscaled tiers only.
        # HDRip is a source-resolution master: forcing 1920x1080 here made it
        # a duplicate of the 1080p encode and could upscale lower-resolution
        # sources while falsely labelling them HDRip.
        if ffcode and self.__qual == "HDRi":
            ffcode, removed_scale = re.subn(
                r"(?i)(?<!\S)-s\s+\\d{2,5}x\\d{2,5}(?=\s|$)",
                "",
                ffcode,
            )
            if removed_scale:
                LOGS.info(
                    f"Removed forced output scaling for HDRip {self.__name}; "
                    "retaining source dimensions."
                )
        elif ffcode:
            target_resolution = self.resolution_map.get(self.__qual)
            if not target_resolution:
                raise RuntimeError(f"Unsupported output quality: {self.__qual}")
            output_name = f"'{out_npath}'"
            double_output_name = f'"{out_npath}"'
            if output_name in ffcode:
                ffcode = ffcode.replace(output_name, f"-s {target_resolution} {output_name}", 1)
            elif double_output_name in ffcode:
                ffcode = ffcode.replace(double_output_name, f"-s {target_resolution} {double_output_name}", 1)
            elif out_npath in ffcode:
                ffcode = ffcode.replace(out_npath, f"-s {target_resolution} {out_npath}", 1)
            else:
                raise RuntimeError(
                    f"Cannot enforce {self.__qual}p dimensions: output path is missing from FFmpeg command."
                )
            LOGS.info(
                f"Enforced {self.__qual}p output dimensions {target_resolution} "
                f"for {self.__name}."
            )

        # Enforce distinct quality profiles AFTER saved/global FFmpeg templates.
        # Previously all three profiles commonly used CRF 30 and 32k audio, so
        # 720p and 1080p outputs could end up almost the same size. The ultrafast
        # preset is retained for the requested faster encoding; CRF now differs
        # by output tier and audio bitrate is no longer starved.
        quality_profile = {
            "1080": {"crf": 23, "audio_bitrate": "128k"},
            "720": {"crf": 27, "audio_bitrate": "96k"},
            "480": {"crf": 30, "audio_bitrate": "64k"},
            "360": {"crf": 32, "audio_bitrate": "48k"},
            "240": {"crf": 34, "audio_bitrate": "32k"},
            "144": {"crf": 35, "audio_bitrate": "24k"},
            "HDRi": {"crf": 18, "audio_bitrate": "160k"},
        }
        profile = quality_profile.get(self.__qual)
        if ffcode and profile:
            crf_value = str(profile["crf"])
            if re.search(r"(?i)(?<![\w-])-crf\s+\d+", ffcode):
                ffcode = re.sub(
                    r"(?i)(?<![\w-])-crf\s+\d+",
                    f"-crf {crf_value}",
                    ffcode,
                )
            else:
                # Add CRF before the output filename so it applies to this output.
                output_tokens = (f"'{out_npath}'", f'"{out_npath}"', out_npath)
                for output_token in output_tokens:
                    if output_token in ffcode:
                        ffcode = ffcode.replace(
                            output_token,
                            f"-crf {crf_value} {output_token}",
                            1,
                        )
                        break

            audio_bitrate = profile["audio_bitrate"]
            if re.search(r"(?i)(?<![\w-])-b:a\s+\S+", ffcode):
                ffcode = re.sub(
                    r"(?i)(?<![\w-])-b:a\s+\S+",
                    f"-b:a {audio_bitrate}",
                    ffcode,
                )
            elif re.search(r"(?i)(?<![\w-])-ab\s+\S+", ffcode):
                ffcode = re.sub(
                    r"(?i)(?<![\w-])-ab\s+\S+",
                    f"-ab {audio_bitrate}",
                    ffcode,
                )
            LOGS.info(
                f"Enforced {self.__qual}p encode profile for {self.__name}: "
                f"CRF {crf_value}, audio {audio_bitrate}."
            )

        # Enforce a resolution-aware average bitrate budget.
        # A single 1,400 MiB target for every tier made 480p files unnecessarily
        # large (e.g. ~900 MB). Normal encodes now use per-quality size budgets;
        # explicit size_target_mib is used only by oversized-file recovery.
        try:
            source_duration = float(await mediainfo(dl_npath, get_duration=True))
            if source_duration <= 0:
                raise ValueError(f"Invalid source duration: {source_duration!r}")

            # Conservative per-resolution file-size budgets. These are
            # total output targets (video + audio + container overhead), not
            # Telegram's maximum upload limit.
            quality_size_targets = {
                "1080": 600,
                "720": 320,
                "480": 180,
                "360": 130,
                "240": 90,
                "144": 60,
                "HDRi": 800,
            }
            target_mib = self.size_target_mib or quality_size_targets.get(self.__qual, 800)
            target_bytes = target_mib * 1024 * 1024

            # Reserve the selected tier's audio bitrate plus muxing overhead.
            audio_bps = {
                "1080": 128_000, "720": 96_000, "480": 64_000,
                "360": 48_000, "240": 32_000, "144": 24_000,
                "HDRi": 160_000,
            }.get(self.__qual, 96_000)
            # Budget for up to two audio tracks; copied source audio previously
            # bypassed this estimate and could make the final file much larger.
            total_budget_bps = int((target_bytes * 8 / source_duration) * 0.90)
            video_bitrate = max(150_000, total_budget_bps - (audio_bps * 2) - 100_000)
            video_maxrate = max(video_bitrate, int(video_bitrate * 1.15))
            video_bufsize = video_maxrate * 2

            # Force a predictable software encoder. Keep audio/subtitle mappings.
            codec_pattern = r"(?i)(?<![\w-])-(?:c:v|codec:v)\s+\S+"
            ffcode, codec_count = re.subn(codec_pattern, "-c:v libx264", ffcode)
            if not codec_count:
                output_token = next(
                    (token for token in (shlex.quote(out_npath), f"'{out_npath}'", f'"{out_npath}"', out_npath)
                     if token in ffcode),
                    None,
                )
                if not output_token:
                    raise RuntimeError("Cannot select video encoder: output path not found.")
                ffcode = ffcode.replace(output_token, f"-c:v libx264 {output_token}", 1)

            # Force audio to AAC at the tier bitrate. A saved template using
            # "-c:a copy" can otherwise preserve a high-bitrate source track and
            # defeat the file-size budget even when video bitrate is constrained.
            ffcode = re.sub(
                r"(?i)(?<![\\w-])-(?:c:a|codec:a)\\s+\\S+",
                "",
                ffcode,
            )
            for option in ("-b:a", "-ab", "-ac"):
                ffcode = re.sub(
                    rf"(?i)(?<![\\w-]){re.escape(option)}\\s+\\S+",
                    "",
                    ffcode,
                )

            # Remove conflicting rate-control options before applying the
            # final bitrate budget. This prevents a template CRF from overriding it.
            for option in ("-crf", "-qp", "-b:v", "-vb", "-maxrate", "-bufsize", "-minrate"):
                ffcode = re.sub(
                    rf"(?i)(?<![\w-]){re.escape(option)}\s+\S+",
                    "",
                    ffcode,
                )

            output_token = next(
                (token for token in (shlex.quote(out_npath), f"'{out_npath}'", f'"{out_npath}"', out_npath)
                 if token in ffcode),
                None,
            )
            if not output_token:
                raise RuntimeError("Cannot insert Telegram bitrate budget: output path not found.")
            enforced_options = (
                f"-preset ultrafast -threads 2 -b:v {video_bitrate} "
                f"-maxrate {video_maxrate} -bufsize {video_bufsize} "
                f"-c:a aac -b:a {audio_bps} -ac 2"
            )
            ffcode = ffcode.replace(output_token, f"{enforced_options} {output_token}", 1)

            LOGS.info(
                f"Enforced {self.__qual}p size profile for {self.__name}: "
                f"duration={source_duration:.1f}s, video bitrate={video_bitrate} bps, "
                f"target output~{target_mib} MiB."
            )
        except Exception as size_profile_error:
            self.last_error = (
                f"Could not apply Telegram-safe average bitrate profile: "
                f"{type(size_profile_error).__name__}: {size_profile_error}"
            )
            LOGS.exception(f"Could not apply Telegram-safe bitrate for {self.__name} ({self.__qual}p)")
            if not self.quiet_errors:
                await rep.report(self.last_error, "error")
            return None

        # /pause may be pressed while the FFmpeg command is being prepared.
        # Restore the source file and do not start a new encode in that case.
        if ani_cache.get('global_pause') and not self.bypass_pause:
            self.is_cancelled = True
            self.last_error = "Encoding cancelled because /pause is active."
            if ospath.exists(dl_npath):
                await aiorename(dl_npath, self.dl_path)
            LOGS.info("Encoding cancelled before FFmpeg process start because /pause is active.")
            return None

        # Replace the command name in both default and saved custom templates.
        # shlex.quote keeps bundled executable paths with spaces safe for the shell.
        ffmpeg_command = shlex.quote(ffmpeg_executable)
        ffcode = re.sub(
            r"(?<![A-Za-z0-9_./-])ffmpeg(?=\s|$)",
            lambda _match: ffmpeg_command,
            ffcode,
            flags=re.IGNORECASE,
        )
        LOGS.info(f"Resolved FFmpeg executable: {ffmpeg_executable}")

        # Capture FFmpeg stderr so failed encodes show the real cause in Anime Logs.
        await FFENCODE_SEMAPHORE.acquire()
        try:
            self.__proc = await create_subprocess_shell(
                ffcode,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=PIPE,
                # FFmpeg is launched through a shell because saved templates
                # may contain shell syntax. A new session lets cancellation
                # terminate the shell AND its FFmpeg child process reliably.
                start_new_session=True,
            )
        except Exception as exc:
            FFENCODE_SEMAPHORE.release()
            self.last_error = f"Could not start FFmpeg: {type(exc).__name__}: {exc}"
            LOGS.exception(f"Could not start FFmpeg for {self.__name} ({self.__qual}p)")
            try:
                if ospath.exists(dl_npath):
                    await aiorename(dl_npath, self.dl_path)
            except Exception as restore_exc:
                LOGS.exception(f"Could not restore source after FFmpeg launch failure: {restore_exc}")
            if not self.quiet_errors:
                await rep.report(
                    f"FFmpeg did not start for {self.__name} ({self.__qual}p): {exc}",
                    "error",
                )
            try:
                await editMessage(
                    self.message,
                    f"<b>Encoding failed to start:</b> {self.__name} — {self.__qual}p\\n<pre>{str(exc)[:1500]}</pre>",
                )
            except Exception:
                pass
            return None

        proc_pid = self.__proc.pid
        try:
            await editMessage(
                self.message,
                f"<b>Encoding started:</b> {self.__name} — {self.__qual}p\\n"
                "FFmpeg process is running; waiting for progress data...",
            )
        except Exception as status_exc:
            LOGS.warning(f"Could not update encoding-start status: {status_exc}")
        ffpids_cache.append(proc_pid)

        progress_task = create_task(self.progress())
        stderr_output = b""

        try:
            _, stderr_output = await self.__proc.communicate()
        except asyncio.CancelledError:
            # A cancelled worker must not leave an orphaned FFmpeg child,
            # hold a semaphore slot, or strand the renamed source file.
            self.is_cancelled = True
            try:
                await self._terminate_process_group()
                await asyncio.wait_for(self.__proc.communicate(), timeout=10)
            except Exception as cleanup_error:
                LOGS.warning(
                    f"Cancellation cleanup for {self.__name} ({self.__qual}p) "
                    f"was incomplete: {type(cleanup_error).__name__}: {cleanup_error}"
                )
            try:
                ffpids_cache.remove(proc_pid)
            except ValueError:
                pass
            try:
                if ospath.exists(dl_npath) and not ospath.exists(self.dl_path):
                    await aiorename(dl_npath, self.dl_path)
                if ospath.exists(out_npath):
                    await aioremove(out_npath)
            except Exception as restore_error:
                LOGS.exception(
                    f"Could not restore files after cancelled encode for {self.__name}: {restore_error}"
                )
            LOGS.warning(
                f"Encoding task cancelled for {self.__name} ({self.__qual}p); "
                "FFmpeg process group stopped and source restored."
            )
            raise
        finally:
            FFENCODE_SEMAPHORE.release()
            try:
                progress_task.cancel()
                await progress_task
            except asyncio.CancelledError:
                pass
            except Exception as progress_error:
                LOGS.warning(
                    f"FFmpeg progress task ended for {self.__name} ({self.__qual}p): "
                    f"{type(progress_error).__name__}: {progress_error}"
                )

        return_code = self.__proc.returncode
        if isinstance(stderr_output, bytes):
            stderr_text = stderr_output.decode("utf-8", errors="replace")
        else:
            stderr_text = str(stderr_output or "")
        stderr_tail = "\\n".join(stderr_text.splitlines()[-35:]).strip()

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
            if not ospath.exists(out_npath) or ospath.getsize(out_npath) <= 0:
                LOGS.error(f"FFmpeg exited successfully but produced no output for {self.__name} ({self.__qual}p)")
                await rep.report(
                    f"FFmpeg reported success but produced no output file for {self.__name} ({self.__qual}p).",
                    "error",
                )
                return None
            await aiorename(out_npath, self.out_path)
            LOGS.info(f"Encoding successful! Output file: {self.out_path}")
            try:
                await editMessage(
                    self.message,
                    f"<b>Encoding completed:</b> {self.__name} — {self.__qual}p\\n"
                    f"Output: {convertBytes(ospath.getsize(self.out_path))}",
                )
            except Exception as status_exc:
                LOGS.warning(f"Could not update encoding-complete status: {status_exc}")
            return self.out_path
        else:
            error_message = f"FFmpeg exited with return code {return_code}"
            if stderr_tail:
                # Keep reports readable while preserving the useful final diagnostics.
                diagnostic = stderr_tail[-3500:]
                self.last_error = f"{error_message}: {diagnostic}"
                LOGS.error(
                    f"Encoding failed for {self.__name} ({self.__qual}p): "
                    f"{error_message}\\n{diagnostic}"
                )
                if not self.quiet_errors:
                    await rep.report(
                        f"{self.__qual}p encoding failed for {self.__name}. "
                        f"{error_message}\\n<pre>{diagnostic}</pre>",
                        "error",
                    )
            else:
                self.last_error = f"{error_message}; FFmpeg returned no stderr output."
                LOGS.error(
                    f"Encoding failed for {self.__name} ({self.__qual}p): "
                    f"{self.last_error}"
                )
                if not self.quiet_errors:
                    await rep.report(
                        f"{self.__qual}p encoding failed for {self.__name}: "
                        f"{error_message}. FFmpeg returned no diagnostic output.",
                        "error",
                    )
            return None
    
    async def _terminate_process_group(self):
        """Stop FFmpeg and any shell descendants without leaving orphan workers."""
        proc = self.__proc
        if proc is None or proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, __import__("signal").SIGTERM)
        except ProcessLookupError:
            return
        except Exception as exc:
            LOGS.warning(f"Could not SIGTERM FFmpeg process group {proc.pid}: {exc}")
            try:
                proc.terminate()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        try:
            os.killpg(proc.pid, __import__("signal").SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            LOGS.error(f"FFmpeg process group {proc.pid} did not exit after SIGKILL.")

    async def cancel_encode(self):
        self.is_cancelled = True
        await self._terminate_process_group()
