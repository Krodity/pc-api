#!/bin/sh
# Install pc-api as a systemd *user* service. Re-run after moving the repo.
set -eu
REPO="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s|%REPO%|$REPO|g" "$REPO/systemd/pc-api.service" > "$UNIT_DIR/pc-api.service"
cp "$REPO/systemd/pc-api-failed.service" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable pc-api
systemctl --user restart pc-api

# Connectors: yk (key input, falls back to the ydotool original) and pcctl.
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
if [ -f "$BIN/yk" ] && [ ! -L "$BIN/yk" ]; then cp "$BIN/yk" "$BIN/yk.bak.$(date +%s)"; fi
ln -sfn "$REPO/connectors/yk/yk" "$BIN/yk"
ln -sfn "$REPO/connectors/yk/yk-ydotool" "$BIN/yk-ydotool"
ln -sfn "$REPO/connectors/pcctl" "$BIN/pcctl"
for t in hyprctl playerctl pactl notify-send grim jq curl; do
  command -v "$t" >/dev/null 2>&1 || echo "note: '$t' not found - its routes will return 502"
done
python3 -c "import os; os.close(os.open('/dev/uinput', os.O_WRONLY))" 2>/dev/null || {
  echo "note: /dev/uinput isn't writable - input routes will fail. Fix:"
  echo "  echo 'KERNEL==\"uinput\", TAG+=\"uaccess\"' | sudo tee /etc/udev/rules.d/50-uinput.rules"
  echo "  sudo udevadm control --reload && sudo udevadm trigger   (then log out and back in)"
}
sleep 1
echo "pc-api is $(systemctl --user is-active pc-api). Token: ~/.config/pc-api/token"
[ -e /usr/local/bin/pcctl ] || echo "note: non-interactive SSH may not have ~/.local/bin on PATH: sudo ln -s $BIN/pcctl /usr/local/bin/pcctl"
