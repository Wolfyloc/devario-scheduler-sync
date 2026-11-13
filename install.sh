
#!/usr/bin/env bash
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="/var/lib/coolercontrol/plugins/devario_scheduler_sync"
PLUGIN_USER="cc-plugin-user"
SERVICE="coolercontrold.service"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo ./install.sh" >&2
    exit 1
fi

if ! systemctl cat "$SERVICE" >/dev/null 2>&1; then
    echo "CoolerControl daemon service not found." >&2
    exit 1
fi

for file in manifest.toml sync.py config.json ui/index.html; do
    if [[ ! -f "$SRC/$file" ]]; then
        echo "Missing source file: $file" >&2
        exit 1
    fi
done

echo "Installing Devario Scheduler Sync..."

# Preserve CoolerControl-managed directory permissions.
[[ -d "$DEST" ]] || install -d -m 755 "$DEST"
[[ -d "$DEST/ui" ]] || install -d -m 755 "$DEST/ui"

# Back up an existing installation.
if [[ -f "$DEST/manifest.toml" ]]; then
    BACKUP="$(mktemp -d \
        /var/lib/coolercontrol/devario_scheduler_sync-backup-XXXXXXXX)"

    chmod 700 "$BACKUP"

    for file in manifest.toml sync.py config.json api_token; do
        if [[ -f "$DEST/$file" ]]; then
            cp -p "$DEST/$file" "$BACKUP/$file"
        fi
    done

    if [[ -f "$DEST/ui/index.html" ]]; then
        cp -p "$DEST/ui/index.html" "$BACKUP/index.html"
    fi

    echo "Backup created at $BACKUP"
fi

# Install plugin source files.
install -m 644 "$SRC/manifest.toml" "$DEST/manifest.toml"
install -m 755 "$SRC/sync.py" "$DEST/sync.py"
install -m 644 "$SRC/ui/index.html" "$DEST/ui/index.html"

# Preserve existing scheduler mappings.
if [[ ! -f "$DEST/config.json" ]]; then
    install -m 600 "$SRC/config.json" "$DEST/config.json"
fi

# Preserve existing API credentials.
if [[ ! -f "$DEST/api_token" ]]; then
    echo "Enter a CoolerControl API token with write access:"
    read -r -s token
    echo

    if [[ -z "$token" ]]; then
        echo "Empty API token; installation aborted." >&2
        exit 1
    fi

    (umask 077; printf '%s\n' "$token" > "$DEST/api_token")
    unset token
fi

chmod 600 "$DEST/config.json" "$DEST/api_token"

# Allow CoolerControl to provision its service account if needed.
if ! getent passwd "$PLUGIN_USER" >/dev/null ||
   ! getent group "$PLUGIN_USER" >/dev/null; then

    echo "Waiting for CoolerControl to provision $PLUGIN_USER..."
    systemctl restart "$SERVICE"

    for attempt in {1..20}; do
        if getent passwd "$PLUGIN_USER" >/dev/null &&
           getent group "$PLUGIN_USER" >/dev/null; then
            break
        fi
        sleep 1
    done
fi

if ! getent passwd "$PLUGIN_USER" >/dev/null ||
   ! getent group "$PLUGIN_USER" >/dev/null; then
    echo "CoolerControl did not provision $PLUGIN_USER." >&2
    echo "Inspect: journalctl -u $SERVICE -n 50" >&2
    exit 1
fi

# Apply restrictive ownership and permissions.
chown "$PLUGIN_USER:$PLUGIN_USER" \
    "$DEST/config.json" \
    "$DEST/api_token"

chmod 600 "$DEST/config.json" "$DEST/api_token"
chmod 755 "$DEST/sync.py"
chmod 644 "$DEST/manifest.toml" "$DEST/ui/index.html"

# Load the completed plugin installation.
echo "Restarting CoolerControl..."
systemctl restart "$SERVICE"

echo "Installation complete."
echo "Open CoolerControl > Plugins > devario_scheduler_sync"
