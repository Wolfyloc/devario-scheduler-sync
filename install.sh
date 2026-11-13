#!/usr/bin/env bash
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/var/lib/coolercontrol/plugins/devario_scheduler_sync
if [[ $EUID -ne 0 ]]; then echo 'Run with sudo: sudo ./install.sh' >&2; exit 1; fi
if ! id cc-plugin-user &>/dev/null; then echo 'cc-plugin-user is missing; check CoolerControl installation' >&2; exit 1; fi
if [[ ! -f "$DEST/manifest.toml" ]]; then echo 'v0.1 installation not found; this installer upgrades an existing plugin.' >&2; exit 1; fi
BACKUP="$DEST/backup-v0.1-$(date +%Y%m%d-%H%M%S)"
install -d -m 700 -o root -g root "$BACKUP"
for f in manifest.toml sync.py config.json; do [[ ! -e "$DEST/$f" ]] || cp -p "$DEST/$f" "$BACKUP/$f"; done
[[ ! -e "$DEST/ui/index.html" ]] || cp -p "$DEST/ui/index.html" "$BACKUP/index.html"
install -m 755 "$SRC/sync.py" "$DEST/sync.py"
install -m 644 "$SRC/ui/index.html" "$DEST/ui/index.html"
install -m 644 "$SRC/manifest.toml" "$DEST/manifest.toml"
# Keep the installed config.json and api_token unchanged.
chown cc-plugin-user:cc-plugin-user "$DEST/sync.py" "$DEST/ui/index.html"
chmod 600 "$DEST/api_token" "$DEST/config.json"
if command -v selinuxenabled >/dev/null && selinuxenabled; then
  echo 'SELinux enabled: retain or reapply the executable bin_t label for sync.py.'
fi
systemctl restart coolercontrold.service
echo "Upgraded to v0.2. Previous files backed up at $BACKUP"
