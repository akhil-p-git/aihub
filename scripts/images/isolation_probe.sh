#!/usr/bin/env bash
# Standing check that the image engine host is isolated and hardened. Run it after installing or changing
# aihub-images.socket and aihub-images.service, and after a systemd upgrade:
#
#   bash scripts/images/isolation_probe.sh
#
# It asks the launcher for its status, which starts the launcher through socket activation but never an
# engine, then looks at the running unit from outside through /proc/<MainPID>/ (a user may read the /proc
# entries of its own processes, including their view of /sys, /dev and /proc). It reads no process
# environment and starts nothing but the launcher. Prints PASS and exits 0, or FAIL and exits 1.
set -u

unit=aihub-images.service
socket=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/aihub-images/engine.sock
port=18234 # the engine's port inside the unit's network namespace (systemd/images/models.json)
failures=0

ok() { printf 'ok    %s\n' "$*"; }
fail() {
    printf 'FAIL  %s\n' "$*"
    failures=$((failures + 1))
}
manager() { systemctl --user "$@"; }
property() { manager show -p "$1" --value "$unit"; }
# expect DESCRIPTION ACTUAL EXPECTED
expect() {
    if [ "$2" = "$3" ]; then ok "$1: $2"; else fail "$1: got '${2:0:200}', want '$3'"; fi
}

# --- the launcher, started by the first connection --------------------------------------------------------
if ! status=$(curl -sS --max-time 10 --unix-socket "$socket" http://engine/launcher/status 2>&1); then
    fail "no answer from $socket ($status); is aihub-images.socket started?"
    echo FAIL
    exit 1
fi
ok "launcher answers: $status"
pid=0
for _ in $(seq 1 50); do
    pid=$(property MainPID)
    [ "${pid:-0}" -gt 0 ] && break
    sleep 0.1
done
if [ "${pid:-0}" -le 0 ]; then
    fail "$unit has no main process"
    echo FAIL
    exit 1
fi
inside=/proc/$pid/root # the unit's own root: its /sys, /dev, /proc and home as the launcher sees them

# --- no network but lo, inside the unit and on the host ---------------------------------------------------
interfaces=$(awk 'NR > 2 { sub(/:.*/, ""); gsub(/ /, ""); printf "%s ", $0 }' "/proc/$pid/net/dev")
expect "network interfaces in the unit's namespace" "$interfaces" "lo "
expect "/sys/class/net inside the unit" "$(ls "$inside/sys/class/net" | tr '\n' ' ')" "lo "
expect "host listeners on port $port" "$(ss -ltnH "sport = :$port" | wc -l)" "0"
curl -sS --max-time 3 -o /dev/null "http://127.0.0.1:$port/" 2>/dev/null
expect "curl to 127.0.0.1:$port on the host (7 is connection refused)" "$?" "7"

# --- devices: the GPU and the harmless basics -------------------------------------------------------------
allowed=" char core fd full hugepages log mqueue null nvidia-uvm nvidia-uvm-tools nvidia0 nvidiactl ptmx pts random shm stderr stdin stdout tty urandom zero "
unexpected=""
for node in $(ls -A "$inside/dev"); do
    case "$allowed" in *" $node "*) ;; *) unexpected="$unexpected $node" ;; esac
done
expect "device nodes in the unit that are not on the list" "${unexpected# }" ""

# --- other processes, and the user's files, are out of sight ----------------------------------------------
seen=""
for other in $$ "$(manager show -p MainPID --value aihub-web.service 2>/dev/null)"; do
    # Only whether the entry exists is tested; nothing of another process is ever read.
    [ "${other:-0}" -gt 0 ] && [ -e "$inside/proc/$other/environ" ] && seen="$seen $other"
done
expect "host processes whose /proc/<pid>/environ the unit can see" "${seen# }" ""
expect "entries of \$HOME in the unit" "$(ls -A "$inside$HOME" | tr '\n' ' ')" "work "
secrets=""
for path in .config .ssh .gnupg .secrets.env .claude; do
    [ -e "$inside$HOME/$path" ] && secrets="$secrets $path"
done
expect "files of the user's that the unit can see" "${secrets# }" ""

# --- the process itself: no capabilities, no new privileges ------------------------------------------------
proc_field() { awk -F'\t' -v key="$1:" '$1 == key { print $2 }' "/proc/$pid/status"; }
expect "effective capabilities" "$(proc_field CapEff)" "0000000000000000"
expect "bounding capabilities" "$(proc_field CapBnd)" "0000000000000000"
expect "NoNewPrivs" "$(proc_field NoNewPrivs)" "1"

# --- the unit as the manager loaded it --------------------------------------------------------------------
for line in PrivateNetwork=yes PrivateDevices=yes PrivateUsers=yes PrivatePIDs=yes PrivateIPC=yes \
    NoNewPrivileges=yes ProtectSystem=strict ProtectHome=tmpfs; do
    expect "loaded $unit ${line%%=*}" "$(property "${line%%=*}")" "${line#*=}"
done
# A hook runs outside the private network namespace (docs/adr/0001-image-engine.md).
expect "loaded ExecStartPre and ExecStartPost" "$(property ExecStartPre)$(property ExecStartPost)" ""
expect "loaded JoinsNamespaceOf" "$(property JoinsNamespaceOf)" ""
dropins=$(property DropInPaths)
[ -n "$dropins" ] && printf 'note  drop-ins change the unit: %s\n' "$dropins"

# --- the score systemd-analyze gives the unit file --------------------------------------------------------
score=$(systemd-analyze --user security --offline=yes --no-pager "$(property FragmentPath)" 2>&1 |
    sed -n 's/.*Overall exposure level for [^:]*: \([0-9.]*\) .*/\1/p')
if [ -n "$score" ] && awk -v s="$score" 'BEGIN { exit !(s <= 3.0) }'; then
    ok "systemd-analyze exposure level $score (3.0 or lower)"
else
    fail "systemd-analyze exposure level '${score:-unknown}' (want 3.0 or lower)"
fi

if [ "$failures" -eq 0 ]; then
    echo PASS
else
    echo "FAIL ($failures)"
    exit 1
fi
