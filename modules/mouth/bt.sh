#!/usr/bin/env bash
# Bluetooth speaker helper for the Percept DK (BlueZ + BlueALSA).
# One script, two users: percept-kit.sh runs it on the host (as root, installed at
# /usr/local/lib/percept-kit/bt.sh), and the mouth container runs it for its web page,
# talking to the host's bluetoothd over the mounted system D-Bus socket.
#
# Usage: bt.sh [--table] <command>
#   scan [SECONDS]      discover nearby devices (default 15 s), then list them
#   devices             list the devices BlueZ knows (paired + recently seen)
#   status              list paired devices and the BlueALSA playback PCMs
#   pair MAC            pair + trust + connect (keeps scanning so the device stays visible)
#   connect MAC | disconnect MAC | forget MAC
#   autoconnect [SECONDS]  retry connecting trusted, paired audio devices (boot service)
#   setup               host only, as root: install BlueALSA, the container user, the boot service
# Lists are tab-separated: mac, name, rssi, paired, trusted, connected, audio.
# --table aligns them for people.
set -uo pipefail

LIB=/usr/local/lib/percept-kit
MOUTH_UID=5003
TABLE=0
if [ "${1:-}" = --table ]; then TABLE=1; shift; fi

die() { echo "bt: $*" >&2; exit 1; }
mac_ok() { [[ ${1:-} =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] || die "bad MAC address '${1:-}'"; }
# bluetoothctl colours its output and may print agent chatter; keep plain text only.
btc() { bluetoothctl "$@" 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g; s/\r//g; s/\x01\|\x02//g'; }
out() {
  if [ "$TABLE" = 1 ]; then
    awk -F'\t' '{ for (i = 1; i <= NF; i++) { c[NR,i] = $i; if (length($i) > w[i]) w[i] = length($i) } n = NF > n ? NF : n }
      END { for (r = 1; r <= NR; r++) { s = ""; for (i = 1; i <= n; i++) s = s sprintf("%-" w[i] + 2 "s", c[r,i]); sub(/ +$/, "", s); print s } }'
  else cat; fi
}
field() { sed -n "s/^[[:space:]]*$1: //p" <<< "$2" | head -n1; }

row() {
  local mac=$1 info name rssi cls audio=no
  info=$(btc info "$mac")
  grep -q "^Device $mac" <<< "$info" || return 0
  name=$(field Name "$info"); [ -n "$name" ] || name=$(field Alias "$info")
  rssi=$(grep -E '^[[:space:]]*RSSI:' <<< "$info" | grep -oE -- '-?[0-9]+\)?$' | tr -d ')' | tail -n1)
  cls=$(field Class "$info")
  if grep -qiE 'Icon: audio|0000110b-' <<< "$info"; then audio=yes
  elif [[ $cls =~ ^0x[0-9a-fA-F]+$ ]] && [ $(( (cls >> 8) & 0x1f )) = 4 ]; then audio=yes; fi
  local p t c
  p=$(field Paired "$info") t=$(field Trusted "$info") c=$(field Connected "$info")
  # Tabs are IFS whitespace, so empty fields would collapse when read back: never emit one.
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$mac" "${name:-?}" "${rssi:--}" "${p:-?}" "${t:-?}" "${c:-?}" "$audio"
}

macs() { btc "$@" | sed -n 's/^Device \([0-9A-Fa-f:]\{17\}\) .*/\1/p'; }
paired_macs() { { macs paired-devices; macs devices Paired; } | sort -u; }
header() { printf 'mac\tname\trssi\tpaired\ttrusted\tconnected\taudio\n'; }
list() { { header; for m in "$@"; do row "$m"; done; } | out; }

connected() { btc info "$1" | grep -q 'Connected: yes'; }

pair() {
  local mac=$1 scan i
  btc power on > /dev/null
  # Without "pairable on" BlueZ pairs without bonding: no link key is stored and the
  # speaker is forgotten as soon as it disconnects.
  btc pairable on > /dev/null
  # BlueZ forgets unpaired devices ~30 s after discovery stops, so scan during pairing.
  bluetoothctl --timeout 60 scan on > /dev/null 2>&1 & scan=$!
  for i in $(seq 30); do btc info "$mac" | grep -q "^Device $mac" && break; sleep 1; done
  if ! btc info "$mac" | grep -q "^Device $mac"; then
    kill $scan 2> /dev/null; die "$mac not found; is the speaker in pairing mode and nearby?"
  fi
  if ! btc info "$mac" | grep -q 'Paired: yes'; then
    timeout 40 bluetoothctl --agent NoInputNoOutput pair "$mac" 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -E 'Pairing|Failed|Error' || true
  fi
  kill $scan 2> /dev/null; wait $scan 2> /dev/null
  btc info "$mac" | grep -q 'Paired: yes' || die "pairing with $mac failed"
  btc trust "$mac" > /dev/null
  connect "$mac"
}

connect() {
  local mac=$1 i
  for i in 1 2 3; do
    connected "$mac" && break
    timeout 25 bluetoothctl connect "$mac" > /dev/null 2>&1
    connected "$mac" && break
    sleep 2
  done
  connected "$mac" || die "could not connect to $mac (is it on and in range?)"
  echo "connected $mac"
}

autoconnect() {
  local end=$(( $(date +%s) + ${1:-120} )) m info todo
  # At boot the USB Bluetooth adapter can appear after this service starts.
  until btc show | grep -q 'Powered: yes'; do
    [ "$(date +%s)" -lt "$end" ] || { echo "bt: no powered Bluetooth controller" >&2; return 0; }
    btc power on > /dev/null; sleep 3
  done
  while :; do
    todo=0
    for m in $(paired_macs); do
      info=$(row "$m")
      [ -n "$info" ] || continue
      IFS=$'\t' read -r _ _ _ _ trusted conn audio <<< "$info"
      if [ "$trusted" = yes ] && [ "$audio" = yes ] && [ "$conn" != yes ]; then
        todo=1
        timeout 15 bluetoothctl connect "$m" > /dev/null 2>&1 && connected "$m" && echo "connected $m"
      fi
    done
    [ "$todo" = 1 ] && [ "$(date +%s)" -lt "$end" ] || break
    sleep 10
  done
}

setup() {
  [ "$(id -u)" = 0 ] || die "setup needs root"
  if ! dpkg -s bluez-alsa-utils > /dev/null 2>&1; then
    echo "bt: installing bluez-alsa-utils"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends bluez-alsa-utils > /dev/null ||
      { apt-get update -q > /dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends bluez-alsa-utils > /dev/null; } ||
      die "could not install bluez-alsa-utils"
  fi
  # Classic (BR/EDR) only. The Percept's 5.4.3 kernel oopses in the LE background-scan
  # worker (__hci_update_background_scan -> kfree) when discovery stops; with LE off the
  # kernel never runs that code. A2DP speakers are classic Bluetooth anyway.
  local conf=/etc/bluetooth/main.conf
  if ! grep -qx 'ControllerMode = bredr' $conf; then
    if grep -qE '^#?ControllerMode *=' $conf; then
      sed -i -E 's/^#?ControllerMode *=.*/ControllerMode = bredr/' $conf
    else
      sed -i 's/^\[General\]$/[General]\nControllerMode = bredr/' $conf
    fi
    systemctl restart bluetooth.service
  fi
  systemctl enable --now bluetooth.service bluez-alsa.service > /dev/null 2>&1 || die "bluez-alsa did not start"
  # The system D-Bus checks the caller's uid against the HOST's user database, so the
  # mouth container's uid needs a host account in group audio (BlueALSA's D-Bus policy).
  local u
  u=$(getent passwd $MOUTH_UID | cut -d: -f1)
  if [ -z "$u" ]; then
    useradd --system --uid $MOUTH_UID --no-create-home --home-dir /nonexistent \
      --shell /usr/sbin/nologin --groups audio percept-mouth
  elif ! id -nG "$u" | grep -qw audio; then
    usermod -aG audio "$u"
  fi
  local unit=/etc/systemd/system/percept-bt-connect.service new
  new="[Unit]
Description=Reconnect trusted Bluetooth speakers (percept-kit)
After=bluetooth.service bluez-alsa.service
Wants=bluetooth.service bluez-alsa.service

[Service]
Type=exec
ExecStart=$LIB/bt.sh autoconnect 120

[Install]
WantedBy=multi-user.target"
  if [ "$(cat $unit 2> /dev/null)" != "$new" ]; then
    printf '%s\n' "$new" > $unit
    systemctl daemon-reload
    systemctl enable percept-bt-connect.service > /dev/null 2>&1
  fi
}

cmd=${1:-status}; shift || true
case $cmd in
  scan)
    secs=${1:-15}; [[ $secs =~ ^[0-9]+$ ]] && [ "$secs" -ge 1 ] && [ "$secs" -le 120 ] || die "scan seconds must be 1-120"
    btc power on > /dev/null
    bluetoothctl --timeout "$secs" scan on > /dev/null 2>&1
    list $(macs devices) ;;
  devices) list $(macs devices) ;;
  status)
    list $(paired_macs)
    if command -v bluealsa-aplay > /dev/null; then echo; echo "BlueALSA playback PCMs:"; bluealsa-aplay -L 2>/dev/null | grep -A1 PROFILE=a2dp || echo "  (none)"; fi ;;
  pair) mac_ok "${1:-}"; pair "${1^^}" ;;
  connect) mac_ok "${1:-}"; connect "${1^^}" ;;
  disconnect) mac_ok "${1:-}"; btc disconnect "${1^^}" | grep -E 'Successful|Failed|not available' ;;
  forget) mac_ok "${1:-}"; btc remove "${1^^}" | grep -E 'removed|not available' ;;
  autoconnect) autoconnect "${1:-120}" ;;
  setup) setup ;;
  *) sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 2 ;;
esac