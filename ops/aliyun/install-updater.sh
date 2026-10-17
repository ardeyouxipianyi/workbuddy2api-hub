#!/bin/sh
set -eu

APP_DIR=/opt/workbuddy2api-hub
APP_SERVICE=workbuddy2api-hub.service
UPDATER_NAME=workbuddy2api-hub-updater
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE=/etc/workbuddy2api-hub-updater.env
DROPIN_DIR=/etc/systemd/system/workbuddy2api-hub.service.d
DROPIN=$DROPIN_DIR/20-updater.conf
BACKUP_ROOT=/var/backups/workbuddy2api-hub
STAMP=$(date +%Y%m%d-%H%M%S)
BOOTSTRAP_BACKUP=$BACKUP_ROOT/bootstrap-$STAMP
ENV_CREATED=0

targets='
/usr/local/libexec/workbuddy2api-hub-updater.py
/etc/systemd/system/workbuddy2api-hub-updater.service
/etc/systemd/system/workbuddy2api-hub-updater.path
/etc/tmpfiles.d/workbuddy2api-hub-updater.conf
/etc/systemd/system/workbuddy2api-hub.service.d/20-updater.conf'

rollback_bootstrap(){
    systemctl disable --now workbuddy2api-hub-updater.path >/dev/null 2>&1 || true
    printf '%s\n' "$targets" | while IFS= read -r target; do
        [ -n "$target" ] || continue
        rel=$(printf '%s' "$target" | sed 's#^/##; s#/#_#g')
        saved=$BOOTSTRAP_BACKUP/$rel
        if [ -f "$saved" ]; then
            cp -a -- "$saved" "$target"
        else
            rm -f -- "$target"
        fi
    done
    if [ "$ENV_CREATED" -eq 1 ]; then rm -f -- "$ENV_FILE"; fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl restart "$APP_SERVICE" >/dev/null 2>&1 || true
}

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root." >&2
    exit 1
fi
if [ ! -d "$APP_DIR/accounts" ] || [ ! -d "$APP_DIR/usage" ]; then
    echo "Expected account and usage directories were not found; stopping." >&2
    exit 1
fi
if ! id wbproxy >/dev/null 2>&1; then
    echo "The wbproxy service user was not found; stopping." >&2
    exit 1
fi
for file in \
    workbuddy2api-hub-updater.py \
    workbuddy2api-hub-updater.service \
    workbuddy2api-hub-updater.path \
    workbuddy2api-hub-updater.conf \
    workbuddy2api-hub-updater-app.conf; do
    if [ ! -f "$SCRIPT_DIR/$file" ]; then
        echo "Missing updater asset: $file" >&2
        exit 1
    fi
done

install -d -o root -g root -m 0700 "$BACKUP_ROOT" "$BOOTSTRAP_BACKUP"
install -d -o root -g wbproxy -m 1730 /run/workbuddy2api-hub-updater
install -d -o root -g root -m 0700 \
    /var/lib/workbuddy2api-hub-updater \
    /var/tmp/workbuddy2api-hub-updater
install -d -o root -g root -m 0755 /usr/local/libexec /etc/tmpfiles.d "$DROPIN_DIR"

if [ -e "$ENV_FILE" ]; then
    if ! grep -qx 'WB_UPDATE_REPO=xybzljc/workbuddy2api-hub' "$ENV_FILE"; then
        echo "Existing updater repository differs; preserved $ENV_FILE." >&2
        exit 1
    fi
else
    env_tmp=$(mktemp /etc/workbuddy2api-hub-updater.env.XXXXXX)
    printf '%s\n' 'WB_UPDATE_REPO=xybzljc/workbuddy2api-hub' > "$env_tmp"
    chown root:root "$env_tmp"
    chmod 0600 "$env_tmp"
    mv -f "$env_tmp" "$ENV_FILE"
    ENV_CREATED=1
fi

for target in \
    /usr/local/libexec/workbuddy2api-hub-updater.py \
    /etc/systemd/system/workbuddy2api-hub-updater.service \
    /etc/systemd/system/workbuddy2api-hub-updater.path \
    /etc/tmpfiles.d/workbuddy2api-hub-updater.conf \
    "$DROPIN"; do
    if [ -e "$target" ]; then
        rel=$(printf '%s' "$target" | sed 's#^/##; s#/#_#g')
        cp -a -- "$target" "$BOOTSTRAP_BACKUP/$rel"
    fi
done

install -o root -g root -m 0750 "$SCRIPT_DIR/workbuddy2api-hub-updater.py" \
    /usr/local/libexec/workbuddy2api-hub-updater.py
install -o root -g root -m 0644 "$SCRIPT_DIR/workbuddy2api-hub-updater.service" \
    /etc/systemd/system/workbuddy2api-hub-updater.service
install -o root -g root -m 0644 "$SCRIPT_DIR/workbuddy2api-hub-updater.path" \
    /etc/systemd/system/workbuddy2api-hub-updater.path
install -o root -g root -m 0644 "$SCRIPT_DIR/workbuddy2api-hub-updater.conf" \
    /etc/tmpfiles.d/workbuddy2api-hub-updater.conf
install -o root -g root -m 0644 "$SCRIPT_DIR/workbuddy2api-hub-updater-app.conf" \
    "$DROPIN"

if ! systemd-tmpfiles --create /etc/tmpfiles.d/workbuddy2api-hub-updater.conf; then
    echo "Could not create the updater request directory; restoring previous files." >&2
    rollback_bootstrap
    exit 1
fi
if ! systemctl daemon-reload || ! systemctl enable --now "$UPDATER_NAME.path" \
        || ! systemctl restart "$APP_SERVICE"; then
    echo "Could not start the updater or restart the gateway; restoring previous files." >&2
    rollback_bootstrap
    exit 1
fi

if ! /usr/bin/python3 -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8788/health", timeout=5)); raise SystemExit(0 if d.get("ok") is True else 1)'; then
    echo "Gateway health check failed after enabling updater. Bootstrap files are backed up at $BOOTSTRAP_BACKUP." >&2
    rollback_bootstrap
    exit 1
fi

echo "Updater installed. Bootstrap backup: $BOOTSTRAP_BACKUP"
