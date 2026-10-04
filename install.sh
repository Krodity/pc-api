#!/bin/sh
# Install pc-api as a systemd *user* service. Re-run after moving the repo.
set -eu
REPO="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
sed "s|%REPO%|$REPO|g" "$REPO/systemd/pc-api.service" > "$UNIT_DIR/pc-api.service"
systemctl --user daemon-reload
systemctl --user enable --now pc-api
for t in hyprctl playerctl pactl notify-send grim; do
  command -v "$t" >/dev/null 2>&1 || echo "note: '$t' not found - its routes will return 502"
done
python3 -c "import os; os.close(os.open('/dev/uinput', os.O_WRONLY))" 2>/dev/null || {
  echo "note: /dev/uinput isn't writable - input routes will fail. Fix:"
  echo "  echo 'KERNEL==\"uinput\", TAG+=\"uaccess\"' | sudo tee /etc/udev/rules.d/50-uinput.rules"
  echo "  sudo udevadm control --reload && sudo udevadm trigger   (then log out and back in)"
}
sleep 1
echo "pc-api is $(systemctl --user is-active pc-api). Token: ~/.config/pc-api/token"
