#!/usr/bin/env bash

source non_sudo_check.sh

sudo rm -f /usr/bin/asus-tablet-mode-unstick
rm -f "$HOME/.local/share/applications/asus-tablet-mode-unstick.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null

echo "Break-glass launcher removed"
