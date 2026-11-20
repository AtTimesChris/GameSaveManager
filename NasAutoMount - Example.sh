#!/bin/bash
# NasAutoMount.sh - make the NAS folder connect BY ITSELF after every restart (Steam Deck).
#
#   bash NasAutoMount.sh             set it up (needs your Deck password once)
#   bash NasAutoMount.sh --status    show what is set up and whether it works
#   bash NasAutoMount.sh --remove    undo everything it did
#
# First run NasMount.sh once and answer  y  to "Save this login", so the NAS login is on the Deck.
# After this, the first time anything opens the folder after a restart, Linux connects it
# (works in gaming mode too - no desktop mode needed). If Tailscale is not connected yet, the
# first try fails after ~15 seconds and the next try works. Not tested on every SteamOS version:
# --status and the Health check (menu K) tell you whether it is working.

# ---- change these if anything changes -----------------------------------
NAS_IP="tailscaleIP"          # the NAS's Tailscale address (tailscale status)
SHARE="Media"                    # the shared folder name on the NAS
SUBPATH=""                       # a folder inside the share to mount instead of the whole share
MOUNT_DIR="$HOME/nas"            # where it appears (Games.json BackupRoots.SteamDeck = this + sub folders)
CRED_FILE="$HOME/.nas-credentials"
# --------------------------------------------------------------------------

MARK="# game-save-manager NAS mount (do not edit this line or the next)"
ok()   { echo "  [ OK ]  $*"; }
bad()  { echo "  [FAIL]  $*"; }
info() { echo "          $*"; }
stop() { echo; echo "Stopped: $*"; echo; read -r -p "Press Enter to close..."; exit 1; }

UNIT=$(systemd-escape -p --suffix=automount "$MOUNT_DIR" 2>/dev/null)

remove_lines() {
    # deletes our marker line and the entry under it
    sudo sed -i "\|^# game-save-manager NAS mount|{N;d}" /etc/fstab
}

edit_fstab() {
    # $1 = "add" or "remove". Tries normally; if the system is read-only, unlocks it just for this edit.
    local mode="$1"
    local attempt
    for attempt in 1 2; do
        if [ "$mode" = "add" ]; then
            printf '%s\n%s\n' "$MARK" "$FSTAB_LINE" | sudo tee -a /etc/fstab >/dev/null 2>&1 && return 0
        else
            remove_lines 2>/dev/null && return 0
        fi
        if [ "$attempt" = "1" ] && command -v steamos-readonly >/dev/null 2>&1; then
            info "The system folder is read-only - unlocking it for a moment..."
            sudo steamos-readonly disable
            UNLOCKED=1
        fi
    done
    return 1
}

relock() {
    if [ "$UNLOCKED" = "1" ]; then
        sudo steamos-readonly enable
        info "System folder locked again."
    fi
}

echo
echo "NAS auto-mount helper"
echo "====================="
echo

if [ "$1" = "--status" ]; then
    grep -q "game-save-manager NAS mount" /etc/fstab 2>/dev/null && ok "The boot entry is in /etc/fstab" || bad "No boot entry (run this script without options)"
    systemctl is-active "$UNIT" >/dev/null 2>&1 && ok "The automount is active ($UNIT)" || bad "The automount is not active"
    if timeout 20 ls "$MOUNT_DIR" >/dev/null 2>&1; then ok "The folder opens: $MOUNT_DIR"; else bad "The folder does not open (NAS or Tailscale not reachable?)"; fi
    findmnt -n -o SOURCE,FSTYPE --target "$MOUNT_DIR" 2>/dev/null | sed 's/^/          mounted as: /'
    echo; read -r -p "Press Enter to close..."; exit 0
fi

sudo -v || stop "sudo password needed (the Deck password; run  passwd  once if you never set one)"
UNLOCKED=0

if [ "$1" = "--remove" ]; then
    sudo systemctl stop "$UNIT" 2>/dev/null
    sudo umount -l "$MOUNT_DIR" 2>/dev/null
    edit_fstab remove && ok "The boot entry was removed" || bad "Could not edit /etc/fstab"
    relock
    sudo systemctl daemon-reload
    info "You can still connect by hand with NasMount.sh."
    echo; read -r -p "Press Enter to close..."; exit 0
fi

command -v mount.cifs >/dev/null 2>&1 || stop "This system has no CIFS support (mount.cifs missing) - tell Chris"
[ -f "$CRED_FILE" ] || stop "No saved NAS login ($CRED_FILE). Run NasMount.sh first and answer y to 'Save this login'."
ok "CIFS support and the saved NAS login are present"

PREFIX_OPT=""
[ -n "$SUBPATH" ] && PREFIX_OPT=",prefixpath=$SUBPATH"
FSTAB_LINE="//$NAS_IP/$SHARE $MOUNT_DIR cifs credentials=$CRED_FILE,uid=$(id -u),gid=$(id -g),file_mode=0664,dir_mode=0775,soft,nofail,_netdev,x-systemd.automount,x-systemd.mount-timeout=15,x-systemd.idle-timeout=0,x-systemd.after=tailscaled.service$PREFIX_OPT 0 0"

# leave a normal mount alone no longer: systemd will manage it from now on
mountpoint -q "$MOUNT_DIR" && sudo umount -l "$MOUNT_DIR"
mkdir -p "$MOUNT_DIR"

sudo cp /etc/fstab "/etc/fstab.bak-$(date +%Y%m%d-%H%M%S)" && ok "Backed up /etc/fstab"
edit_fstab remove >/dev/null 2>&1
if edit_fstab add; then
    ok "Added the boot entry"
else
    relock
    stop "Could not write /etc/fstab"
fi
relock

sudo systemctl daemon-reload
sudo systemctl restart "$UNIT" 2>/dev/null || sudo systemctl start "$UNIT" 2>/dev/null

if timeout 25 ls "$MOUNT_DIR" >/dev/null 2>&1 && findmnt -n --target "$MOUNT_DIR" >/dev/null 2>&1; then
    ok "It works: $MOUNT_DIR opens and the NAS is connected"
else
    bad "The folder did not open yet."
    info "Is Tailscale connected?  Try:  tailscale status   then run:  bash NasAutoMount.sh --status"
fi

echo
info "After a restart, just use the manager - the first access connects the NAS."
info "Games.json BackupRoots.SteamDeck should be:  $MOUNT_DIR${SUBPATH:+  (the share is mounted at its top)}"
info "Tip: set \"NasReachableSeconds\": 8 in Settings so the first access has time to connect."
info "(A SteamOS update can remove this. If the Health check says it is gone, run this again.)"
echo
read -r -p "Press Enter to close..."
