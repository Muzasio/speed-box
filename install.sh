#!/usr/bin/env bash
# Set up SpeedBox after a clone: system packages, app menu entry and the root service.
# Usage:
#   ./install.sh                 install everything
#   ./install.sh --no-service    skip the root service (install it later from the window)
#   ./install.sh --uninstall     remove the service and the menu entry (your rules stay)
#   ./install.sh --uninstall --purge   also remove your rules (~/.config/speedbox)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/speedbox"
MENU="${XDG_DATA_HOME:-$HOME/.local/share}/applications/speedbox.desktop"

service=1 uninstall=0 purge=0
for arg in "$@"; do
    case "$arg" in
        --no-service) service=0 ;;
        --uninstall) uninstall=1 ;;
        --purge) purge=1 ;;
        -h|--help) sed -n 2,7p "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" -ne 0 ] || { echo "Run this as your normal user, not root. It asks for sudo when needed." >&2; exit 1; }

if [ "$uninstall" -eq 1 ]; then
    if [ -e /etc/systemd/system/speedbox.service ]; then
        sudo python3 -I "$HERE/speedboxd.py" uninstall
    fi
    rm -f "$MENU"
    echo "Menu entry removed."
    if [ "$purge" -eq 1 ]; then
        rm -rf "$CONF"
        echo "Rules removed: $CONF"
    else
        echo "Your rules stay in $CONF (remove with --uninstall --purge)."
    fi
    exit 0
fi

# 1. System packages: python3 with tkinter, iproute2 (tc), nftables, polkit (pkexec).
install_packages() {
    local id like
    # shellcheck disable=SC1091
    id="$(. /etc/os-release 2>/dev/null && echo "${ID:-}")"
    # shellcheck disable=SC1091
    like="$(. /etc/os-release 2>/dev/null && echo "${ID_LIKE:-}")"
    case " $id $like " in
        *" arch "*)
            sudo pacman -S --needed --noconfirm python tk iproute2 nftables polkit ;;
        *" debian "*|*" ubuntu "*)
            sudo apt-get update
            if ! sudo apt-get install -y python3 python3-tk iproute2 nftables polkitd pkexec; then
                sudo apt-get install -y python3 python3-tk iproute2 nftables policykit-1  # older Debian/Ubuntu
            fi ;;
        *" fedora "*|*" rhel "*)
            sudo dnf install -y python3 python3-tkinter iproute nftables polkit ;;
        *" suse "*|*" opensuse "*)
            sudo zypper --non-interactive install python3 python3-tk iproute2 nftables polkit ;;
        *)
            echo "Unknown distro '$id'. Install by hand: python3 with tkinter, iproute2, nftables, polkit." >&2 ;;
    esac
}

need_pkgs=0
python3 -c 'import tkinter' 2>/dev/null || need_pkgs=1
for cmd in tc nft pkexec systemctl; do
    command -v "$cmd" >/dev/null || need_pkgs=1
done
if [ "$need_pkgs" -eq 1 ]; then
    echo "Installing system packages..."
    install_packages
fi

# 2. Checks the service needs.
command -v systemctl >/dev/null || { echo "SpeedBox needs systemd." >&2; exit 1; }
[ -f /sys/fs/cgroup/cgroup.controllers ] || { echo "SpeedBox needs cgroup v2." >&2; exit 1; }
python3 -c 'import sys, tkinter; sys.exit(sys.version_info < (3, 6))' \
    || { echo "SpeedBox needs Python 3.6 or newer with tkinter." >&2; exit 1; }

# 3. Menu entry (no other dependencies: SpeedBox uses only the Python standard library).
python3 "$HERE/speedbox.py" --install-menu

# 4. Root service: applies the rules at boot and when they change.
if [ "$service" -eq 1 ]; then
    mkdir -p "$CONF" && chmod 700 "$CONF"
    echo "Installing the SpeedBox service (sudo)..."
    sudo python3 -I "$HERE/speedboxd.py" install --rules "$CONF/rules.json"
else
    echo "Service skipped. Install it later with 'Install / Update service' in the window."
fi

echo "Done. Start SpeedBox from the app menu, or: python3 $HERE/speedbox.py"
