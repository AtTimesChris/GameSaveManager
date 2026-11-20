#!/bin/bash
# Double-click launcher for the Steam Deck (choose "Execute in Terminal").
# Needs to be marked executable once:  chmod +x GameSaveManager.sh
cd "$(dirname "$(readlink -f "$0")")" || exit 1
python3 savemanager.py
echo
read -r -p "Press Enter to close..."
