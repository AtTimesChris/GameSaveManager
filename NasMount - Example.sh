#!/bin/bash
# NasMount.sh - checks Tailscale, then (re)mounts the NAS "Media" share on the Steam Deck.
# Double-click it and choose "Execute in Terminal" (or: bash NasMount.sh).
# Safe to run as often as you like: if everything is already fine it just says so.

# ---- change these if anything changes -----------------------------------
NAS_IP="tailscaleIP"          # the NAS's Tailscale IP (NOT the Deck's)
SHARE="Media"                    # the shared folder name on the NAS
MOUNT_DIR="$HOME/nas"            # where it appears on the Deck
CRED_FILE="$HOME/.nas-credentials"
# --------------------------------------------------------------------------

# Run with --reset to throw away the saved NAS login and type it again.
if [ "$1" = "--reset" ]; then
    rm -f "$CRED_FILE"
    echo "Saved NAS login removed - you will be asked for it again."
fi

ok()   { echo "  [ OK ]  $*"; }
bad()  { echo "  [FAIL]  $*"; }
info() { echo "          $*"; }
stop() { echo; echo "Stopped: $*"; echo; read -r -p "Press Enter to close..."; exit 1; }

echo
echo "NAS mount helper"
echo "================"
echo

# 1. Is Tailscale installed? (a SteamOS update can remove it)
[ -f /etc/profile.d/tailscale.sh ] && source /etc/profile.d/tailscale.sh

if ! command -v tailscale >/dev/null 2>&1; then
    bad "Tailscale is not installed on this Deck."
    info "Re-run the deck-tailscale install in a terminal:"
    info "  cd ~/deck-tailscale"
    info "  sudo bash tailscale.sh"
    info "  tailscale up --qr --operator=deck --ssh"
    stop "Tailscale missing"
fi

ok "Tailscale is installed"

# 2. Is it running and logged in?
if ! tailscale status >/dev/null 2>&1; then
    info "Tailscale is not running - trying to start it..."
    sudo systemctl start tailscaled
    sleep 3

    if ! tailscale status >/dev/null 2>&1; then
        bad "Tailscale would not start."
        info "Try:  sudo tailscale up --operator=deck --ssh"
        stop "Tailscale not running"
    fi
fi

ok "Tailscale is running"

# 3. Can we reach the NAS through Tailscale?
if tailscale ping --c 1 --timeout 8s "$NAS_IP" >/dev/null 2>&1; then
    ok "NAS answers over Tailscale ($NAS_IP)"
else
    bad "The NAS does not answer over Tailscale ($NAS_IP)."
    info "Check the NAS is on and that Tailscale is running on it."
    stop "NAS unreachable"
fi

# 4. Is the file-sharing port open?
if timeout 5 bash -c "echo > /dev/tcp/$NAS_IP/445" 2>/dev/null; then
    ok "NAS file sharing port (445) is open"
else
    bad "Port 445 is closed - the NAS firewall is probably blocking Tailscale."
    info "DSM > Control Panel > Security > Firewall: allow 'Windows file server'"
    info "from source IP 100.64.0.0 with subnet mask 255.192.0.0"
    stop "Port 445 blocked"
fi

# 5. Already mounted?
sudo -v || stop "sudo password needed"

if mountpoint -q "$MOUNT_DIR"; then
    SOURCE=$(findmnt -n -o SOURCE --target "$MOUNT_DIR")

    if [ "$SOURCE" = "//$NAS_IP/$SHARE" ] && timeout 5 ls "$MOUNT_DIR" >/dev/null 2>&1; then
        ok "Already mounted and working: $MOUNT_DIR"
        echo
        info "Nothing to do. In Games.json use:  $MOUNT_DIR/Games/Saves"
        echo
        read -r -p "Press Enter to close..."
        exit 0
    fi

    info "Existing mount ($SOURCE) is stale or points somewhere else - unmounting it..."
    sudo umount -l "$MOUNT_DIR"
    ok "Old mount removed"
fi

# 6. NAS login
TEMP_CRED=0
CRED="$CRED_FILE"

if [ -f "$CRED_FILE" ]; then
    info "Using the saved NAS login for user: $(grep '^username=' "$CRED_FILE" | cut -d= -f2-)"
    info "(Wrong? run again with:  bash NasMount.sh --reset)"
fi

if [ ! -f "$CRED_FILE" ]; then
    read -r -p "NAS username: " NAS_USER
    read -r -s -p "NAS password: " NAS_PASS
    echo
    read -r -p "Save this login on the Deck so you are not asked again? [y/N] " SAVE

    if [ "$SAVE" != "y" ] && [ "$SAVE" != "Y" ]; then
        CRED=$(mktemp)
        TEMP_CRED=1
    fi

    (umask 077; printf 'username=%s\npassword=%s\n' "$NAS_USER" "$NAS_PASS" > "$CRED")
fi

# 7. Mount (tries the normal way first, then older/newer SMB versions)
mkdir -p "$MOUNT_DIR"
ERRFILE=$(mktemp)
MOUNTED=0

for VERS in "" ",vers=3.0" ",vers=2.1"; do
    if sudo mount -t cifs "//$NAS_IP/$SHARE" "$MOUNT_DIR" \
        -o "credentials=$CRED,soft,uid=$(id -u),gid=$(id -g),file_mode=0664,dir_mode=0775$VERS" 2>"$ERRFILE"; then
        MOUNTED=1
        ok "Mounted //$NAS_IP/$SHARE at $MOUNT_DIR ${VERS:+(using ${VERS#,})}"
        break
    fi
done

if [ "$MOUNTED" = "0" ]; then
    bad "Mount failed:"
    sed 's/^/          /' "$ERRFILE"
    echo
    info "What the system says about it (look for LOGON_FAILURE or ACCESS_DENIED):"
    sudo dmesg 2>/dev/null | grep -i cifs | tail -n 4 | sed 's/^/          /'
    echo
    info "error(13) = the NAS refused the login: wrong user name or password,"
    info "            or that account may not use this share."
    info "error(115)/(110) = NAS not reachable."
    info "'unknown filesystem type cifs' = this SteamOS has no CIFS support - tell me."
    info "To type the login again:  bash NasMount.sh --reset"
    [ "$TEMP_CRED" = "1" ] && rm -f "$CRED"
    rm -f "$ERRFILE"
    stop "mount failed"
fi

[ "$TEMP_CRED" = "1" ] && rm -f "$CRED"
rm -f "$ERRFILE"

# 8. Check we can really see the games folder
if timeout 5 ls "$MOUNT_DIR" >/dev/null 2>&1; then
    ok "Folder is readable"
else
    bad "Mounted, but the folder cannot be read."
    stop "cannot read mount"
fi

if [ -d "$MOUNT_DIR/Games" ]; then
    ok "Found the Games folder"
else
    bad "No 'Games' folder inside - check the SHARE name at the top of this script."
fi

echo
echo "All done. In Games.json use:  $MOUNT_DIR/Games/Saves"
echo
read -r -p "Press Enter to close..."
