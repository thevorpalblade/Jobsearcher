#!/bin/sh
# Install Jobsearcher as systemd *user* services (no root): the daily daemon and the
# web UI start at boot, restart if they crash, and log to the journal.
#
#   deploy/install-systemd.sh            install, enable and start both
#   deploy/install-systemd.sh --no-start install and enable only
#
# Logs:    journalctl --user -u jobsearcher-daemon -f   (or -u jobsearcher-web)
# Status:  systemctl --user status jobsearcher-daemon jobsearcher-web
# Remove:  systemctl --user disable --now jobsearcher-daemon jobsearcher-web
set -eu

repo=$(cd "$(dirname "$0")/.." && pwd)
dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$dest"

[ -x "$repo/.venv/bin/jobsearcher" ] || {
    echo "No $repo/.venv/bin/jobsearcher: create the virtualenv first (pip install -e .)" >&2
    exit 1
}
for unit in jobsearcher-daemon jobsearcher-web; do
    sed "s|@REPO@|$repo|g" "$repo/deploy/systemd/$unit.service" > "$dest/$unit.service"
done
systemctl --user daemon-reload
systemctl --user enable jobsearcher-daemon.service jobsearcher-web.service

# Without lingering, user services only run while the user is logged in.
if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != "yes" ]; then
    loginctl enable-linger "$(id -un)" ||
        echo "Couldn't enable lingering; run: sudo loginctl enable-linger $(id -un)" >&2
fi

if [ "${1:-}" != "--no-start" ]; then
    systemctl --user restart jobsearcher-daemon.service jobsearcher-web.service
fi
echo "Installed to $dest"
