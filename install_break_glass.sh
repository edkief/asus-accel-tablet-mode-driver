#!/usr/bin/env bash

source non_sudo_check.sh

echo "Break-glass launcher"
echo

read -r -p "Do you want install the 'Unstick Tablet Mode' launcher? [y/N]" RESPONSE
case "$RESPONSE" in [yY][eE][sS]|[yY])

    sudo install -m 755 break_glass/asus-tablet-mode-unstick /usr/bin/asus-tablet-mode-unstick

    if [[ $? != 0 ]]; then
        echo "Something went wrong when installing /usr/bin/asus-tablet-mode-unstick"
        exit 1
    else
        echo "Helper /usr/bin/asus-tablet-mode-unstick installed"
    fi

    mkdir -p "$HOME/.local/share/applications"
    install -m 644 break_glass/asus-tablet-mode-unstick.desktop "$HOME/.local/share/applications/asus-tablet-mode-unstick.desktop"

    if [[ $? != 0 ]]; then
        echo "Something went wrong when installing the launcher"
        exit 1
    else
        echo "Launcher installed to $HOME/.local/share/applications"
    fi

    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null

    echo
    echo "Pin 'Unstick Tablet Mode' to the GNOME dash so it can be reached by touch:"
    echo "  Activities -> type or scroll to 'Unstick Tablet Mode' -> right click / long press -> Pin to Dash"
esac
