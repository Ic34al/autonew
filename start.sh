#!/usr/bin/env bash
set -euo pipefail

# Add all library directories under Heroku Apt and system prefixes.
SEARCH_ROOTS=()
[[ -d /app/.apt ]] && SEARCH_ROOTS+=(/app/.apt)
[[ -d /usr/lib ]] && SEARCH_ROOTS+=(/usr/lib)
[[ -d /lib ]] && SEARCH_ROOTS+=(/lib)

if ((${#SEARCH_ROOTS[@]})); then
  while IFS= read -r lib_dir; do
    [[ -n "$lib_dir" ]] || continue
    export LD_LIBRARY_PATH="$lib_dir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  done < <(find "${SEARCH_ROOTS[@]}" -type f -name '*.so*' -print 2>/dev/null | sed 's|/[^/]*$||' | sort -u)
fi

FFMPEG_BIN="$(command -v ffmpeg || true)"
if [[ -z "$FFMPEG_BIN" && -x /app/.apt/usr/bin/ffmpeg ]]; then
  FFMPEG_BIN=/app/.apt/usr/bin/ffmpeg
fi
if [[ -z "$FFMPEG_BIN" ]]; then
  echo "[STARTUP ERROR] ffmpeg is not installed or not on PATH. Check Aptfile/buildpacks." >&2
  exit 1
fi

echo "[STARTUP CHECK] FFmpeg binary: $FFMPEG_BIN"
if command -v ldd >/dev/null 2>&1; then
  MISSING="$(ldd "$FFMPEG_BIN" 2>&1 | grep 'not found' || true)"
  if [[ -n "$MISSING" ]]; then
    echo "[STARTUP ERROR] FFmpeg has unresolved shared libraries:" >&2
    echo "$MISSING" >&2
    exit 1
  fi
fi
if ! "$FFMPEG_BIN" -version >/dev/null 2>&1; then
  echo "[STARTUP ERROR] FFmpeg cannot load runtime libraries. LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}" >&2
  "$FFMPEG_BIN" -version || true
  exit 1
fi
echo "[STARTUP CHECK] FFmpeg runtime dependencies are resolved."

exec python3 -m bot
