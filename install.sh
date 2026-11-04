#!/bin/sh
# One-time setup on the alarm host (run as the normal user, no sudo needed).
set -e
cd "$(dirname "$0")"

# yt-dlp: prefer a current single-file build in ~/.local/bin
# (distro packages are often too old for current YouTube).
mkdir -p "$HOME/.local/bin"
if [ ! -x "$HOME/.local/bin/yt-dlp" ]; then
  echo "downloading yt-dlp..."
  curl -L -o "$HOME/.local/bin/yt-dlp" \
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"
  chmod +x "$HOME/.local/bin/yt-dlp"
fi
"$HOME/.local/bin/yt-dlp" --version || true

for c in python3 mpv ffmpeg pactl paplay; do
  command -v "$c" >/dev/null 2>&1 || echo "WARNING: $c not found in PATH"
done

chmod +x start.sh stop.sh

# Start at boot through the user's crontab (no linger / sudo required).
# NOTE: `|| true` matters under `set -e`: grep exits 1 when there was no
# previous crontab, which would otherwise abort the subshell before echo.
LINE="@reboot $PWD/start.sh"
( crontab -l 2>/dev/null | grep -vF "$PWD/start.sh" || true; echo "$LINE" ) | crontab -
echo "crontab @reboot entry installed"

./start.sh
echo "done. Open http://<this-host>:8767/"
