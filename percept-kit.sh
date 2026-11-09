#!/usr/bin/env bash
# percept-kit: run an Azure Percept DK standalone on Ubuntu 22.04, and back again.
# Runs on your PC: Git Bash on Windows, or bash 4+ on Linux/macOS. See README.md.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONF=$ROOT/.percept-kit.conf
BACKUPS=$ROOT/backups
REPORTS=$ROOT/reports
HOST= DEVUSER= KEY=$HOME/.ssh/id_ed25519_percept
if [ -f "$CONF" ]; then . "$CONF"; fi
YES=0 KDIR=

SSH_OPTS=(-o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=15
          -o ServerAliveCountMax=4 -o ConnectTimeout=10 -o LogLevel=ERROR)

log()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
confirm() {
  if [ "$YES" = 1 ]; then return 0; fi
  local a; printf '\033[1;33m%s\033[0m\n' "$1"
  read -r -p "Type 'yes' to continue: " a < /dev/tty
  [ "$a" = yes ] || die "aborted"
}

need_conf() { if [ -z "$HOST" ] || [ -z "$DEVUSER" ]; then die "not connected; run: $0 connect <host> <user>"; fi; }
dssh() { ssh -i "$KEY" "${SSH_OPTS[@]}" -o BatchMode=yes "$DEVUSER@$HOST" "$@"; }
# Run one of scripts/kit/*.sh on the device without uploading it.
dscript() { local f=$1; shift; tr -d '\r' < "$ROOT/scripts/kit/$f" | dssh "bash -s -- $*"; }

# Rewrites HOST/DEVUSER/KEY and keeps any other lines.
save_conf() {
  local extra=
  if [ -f "$CONF" ]; then extra=$(grep -vE '^(HOST|DEVUSER|KEY)=' "$CONF" || true); fi
  { printf 'HOST=%q\nDEVUSER=%q\nKEY=%q\n' "$HOST" "$DEVUSER" "$KEY"
    if [ -n "$extra" ]; then printf '%s\n' "$extra"; fi; } > "$CONF"
}

# Device facts from scripts/kit/state.sh become D_<key> variables. The same connection
# first exempts this PC from Mariner's SSH rate limit (ssh-exempt.sh), so call this
# before any burst of dssh calls after connecting or rebooting.
load_state() {
  local line k
  unset "${!D_@}"
  while IFS= read -r line; do
    k=${line%%=*}
    if [[ $k =~ ^[a-z0-9]+$ ]]; then printf -v "D_$k" '%s' "${line#*=}"; fi
  done < <({ cat "$ROOT/scripts/kit/ssh-exempt.sh"; echo; cat "$ROOT/scripts/kit/state.sh"; } | tr -d '\r' | dssh "bash -s")
  [ -n "${D_state:-}" ] || die "could not read device state from $HOST"
  KDIR=$D_home/percept-kit/scripts
}

show_state() {
  echo "Device:  $DEVUSER@$HOST  ($D_model)"
  echo "OS:      $D_os  (root $D_root, /var $D_var)"
  case $D_state in
    stock)      echo "State:   stock Azure Percept OS. Next: '$0 install' (it takes a backup first)." ;;
    rescue)     echo "State:   rescue Mariner (/var on p4); p5: ${D_p5label:-${D_p5type:-empty}}. Next: '$0 install' or '$0 restore'." ;;
    ubuntu)     echo "State:   Ubuntu on eMMC p5. Update with '$0 deploy'; see '$0 help'." ;;
    usb-ubuntu) echo "State:   Ubuntu booted from USB (development setup); install/restore need Mariner." ;;
    *)          echo "State:   unrecognised layout" ;;
  esac
}

upload_kit() {
  (cd "$ROOT" && tar -cf - scripts) | dssh 'set -e; rm -rf ~/percept-kit/scripts; mkdir -p ~/percept-kit
    tar -C ~/percept-kit -xf - --no-same-owner
    find ~/percept-kit/scripts -type f ! -name "*.dtb" -exec sed -i "s/\r$//" {} +
    chmod +x ~/percept-kit/scripts/*.sh ~/percept-kit/scripts/kit/*.sh'
}

# Run a long command on the device as a root systemd unit (survives SSH drops) and
# stream its log until it finishes. Usage: job <name> <shell command>
job() {
  local name=$1 cmd=$2 seen=0 out last n rc
  # umask 022: Mariner's default 077 would hide the log and rc from the polling user.
  printf 'umask 022\n(%s) > /var/tmp/pk-%s.log 2>&1\necho $? > /var/tmp/pk-%s.rc\n' "$cmd" "$name" "$name" \
    | dssh "cat > /var/tmp/pk-$name.sh"
  dssh "sudo -n rm -f /var/tmp/pk-$name.log /var/tmp/pk-$name.rc; sudo -n systemctl reset-failed pk-$name 2>/dev/null;
        sudo -n systemd-run --quiet --collect --unit=pk-$name --setenv=SUDO_USER=$DEVUSER \
          --setenv=PK_HOSTNAME=${PK_HOSTNAME:-} /bin/bash /var/tmp/pk-$name.sh"
  local poll='f=/var/tmp/pk-NAME.log; n=0; if [ -f $f ]; then n=$(wc -l < $f); fi
    if [ "$n" -gt SEEN ]; then sed -n "$((SEEN+1)),${n}p" $f; fi; echo "@@ $n $(cat /var/tmp/pk-NAME.rc 2>/dev/null)"'
  while :; do
    sleep 5
    if ! out=$(dssh "$(printf '%s' "$poll" | sed "s/NAME/$name/g; s/SEEN/$seen/g")" 2>/dev/null); then
      echo "   (connection lost, retrying)"; sleep 10; continue
    fi
    last=${out##*$'\n'}
    if [ "$last" != "$out" ]; then printf '%s\n' "${out%$'\n'*}" | sed 's/^/   /'; fi
    read -r _ n rc <<< "$last"
    seen=$n
    if [ -n "$rc" ]; then
      [ "$rc" = 0 ] || die "device job '$name' failed (rc=$rc); log: /var/tmp/pk-$name.log"
      return 0
    fi
  done
}

reboot_wait() {
  local old=$D_bootid out i
  log "Rebooting device (this takes 1-3 minutes)"
  dssh "sudo -n systemd-run --quiet --on-active=2 systemctl reboot"
  sleep 30
  # Poll every 15 s: Mariner's SSH rate limit allows 5 new connections per minute
  # until load_state re-adds this boot's exemption.
  for i in $(seq 60); do
    if out=$(dssh "cat /proc/sys/kernel/random/boot_id" 2>/dev/null) && [ "$out" != "$old" ]; then
      sleep 5; load_state; show_state; return 0
    fi
    sleep 15
  done
  die "device did not come back after 15 minutes; check it on HDMI or the serial console"
}

# Newest complete stock backup of this device (matched by eMMC CID).
find_backup() {
  local d best=
  for d in "$BACKUPS"/*/; do
    d=${d%/}
    if [[ $d == *.partial ]] || [ ! -f "$d/kit-backup.conf" ]; then continue; fi
    if grep -qx "cid=$D_cid" "$d/kit-backup.conf" && grep -qx "state=stock" "$d/kit-backup.conf"; then best=$d; fi
  done
  [ -n "$best" ] || die "no stock backup of this device (CID $D_cid) in $BACKUPS; run: $0 backup"
  echo "$best"
}

verify_backup() {
  local bk=$1
  grep -qx "cid=$D_cid" "$bk/kit-backup.conf" || die "$bk is not a backup of this device"
  log "Verifying backup checksums ($(basename "$bk"))"
  (cd "$bk" && sha256sum -c --quiet SHA256SUMS) || die "backup checksum mismatch in $bk"
}

# The Eye's build base (Microsoft's azureeyemodule image): only the Eye's build stage uses it. Every
# stock DK has it in its IoT Edge cache, and the backup keeps the device's own copy in case MCR stops
# serving it.
eye_base() { sed -n 's/^ARG BASE=//p' "$ROOT/modules/eye/Dockerfile"; }

# save_eye_base <backup dir>: device's Docker -> <dir>/eye-base.tar.zst (listed in SHA256SUMS).
save_eye_base() {
  local dir=$1 base f; base=$(eye_base); f=$dir/eye-base.tar.zst
  if ! dssh "sudo -n docker image inspect $base >/dev/null 2>&1"; then
    warn "$base is not in the device's Docker; the backup won't include the Eye base image"
    return 1
  fi
  log "Saving $base from the device to $(basename "$dir") (3-10 minutes)"
  dssh "set -o pipefail; sudo -n docker save $base | zstd -3 -T0 -q -c" > "$f.tmp" ||
    { rm -f "$f.tmp"; warn "saving $base failed"; return 1; }
  mv "$f.tmp" "$f"
  touch "$dir/SHA256SUMS"
  sed -i '/[ *]eye-base\.tar\.zst$/d' "$dir/SHA256SUMS"
  echo "$(sha256sum "$f" | cut -c1-64)  eye-base.tar.zst" >> "$dir/SHA256SUMS"
  printf '   %-14s%6s\n' eye-base "$(du -h "$f" | cut -f1)"
}

# backup_verified <backup dir> <file>: true when the backup holds <file> and its SHA256SUMS line matches.
backup_verified() {
  [ -n "${1:-}" ] && [ -f "$1/$2" ] &&
    (cd "$1" && grep "[ *]${2//./\\.}\$" SHA256SUMS | sha256sum -c --quiet - > /dev/null 2>&1)
}

# backup_has_base <backup dir>: true when the backup holds a copy of the Eye's base whose checksum matches.
backup_has_base() { backup_verified "${1:-}" eye-base.tar.zst; }

# ensure_eye_base [MB]: make sure the device has the Eye's build base: already there, else load this
# device's backup copy, else pull it from MCR. MB: room needed after it, for the build that follows.
ensure_eye_base() {
  local base bk after=${1:-0} need free; base=$(eye_base)
  dssh "sudo -n docker image inspect $base >/dev/null 2>&1" && return 0
  bk=$(find_backup 2>/dev/null) || bk=
  # Unpacked, the base takes 1.3 GB; a pull from MCR needs about 3 GB while it downloads.
  if backup_has_base "$bk"; then need=$((1300 + after)); else need=$((3000 + after)); fi
  free=$(device_free_mb)
  [ "$free" -ge "$need" ] ||
    die "getting the Eye's build base$([ "$after" -gt 0 ] && echo ', then building the Eye,') needs about $need MB on the device; it has $free MB free. Make room first: customize.md, \"Free up disk space\""
  if backup_has_base "$bk"; then
    log "Loading the Eye's build base from $(basename "$bk") (5-10 minutes)"
    if dssh "set -o pipefail; zstd -dcq | sudo -n docker load -q" < "$bk/eye-base.tar.zst" &&
       dssh "sudo -n docker image inspect $base >/dev/null 2>&1"; then
      return 0
    fi
    warn "loading it from the backup failed; trying MCR"
  fi
  log "Pulling the Eye's build base $base from MCR (2.6 GB)"
  dssh "sudo -n docker pull -q $base" ||
    die "cannot get $base: no backup of this device holds it and MCR doesn't serve it"
}

# Bytes Docker uses for the Eye's build base on the device, or nothing when it isn't there.
eye_base_size() { dssh "sudo -n docker image inspect -f '{{.Size}}' $(eye_base) 2>/dev/null" || true; }
device_free_mb() { dssh "df --output=avail -BM /var/lib/docker | tail -n1 | tr -dc 0-9"; }

cmd_eye_base() {
  need_conf; load_state
  local sub=${1:-status} base size bk free
  base=$(eye_base)
  bk=$(find_backup 2>/dev/null) || bk=
  case $sub in
    status)
      size=$(eye_base_size)
      if [ -n "$size" ]; then echo "Device:  $base, $((size / 1048576)) MB in Docker"
      else echo "Device:  not in Docker (the next Eye build loads or pulls it)"; fi
      if backup_has_base "$bk"; then echo "Backup:  verified copy in $(basename "$bk") ($(du -h "$bk/eye-base.tar.zst" | cut -f1))"
      else echo "Backup:  no copy (add one with: $0 eye-base save)"; fi ;;
    save)
      [ -n "$bk" ] || die "no stock backup of this device (CID $D_cid) in $BACKUPS"
      save_eye_base "$bk" || die "nothing saved" ;;
    remove)
      require_ubuntu eye-base
      size=$(eye_base_size)
      [ -n "$size" ] || { log "$base isn't on the device"; return 0; }
      if ! backup_has_base "$bk"; then
        confirm "No backup of this device holds a verified copy of $base. The Eye keeps running, but
rebuilding it would then depend on MCR still serving the image. Remove it anyway?"
      fi
      free=$(device_free_mb)
      dssh "sudo -n docker rmi $base > /dev/null"
      log "Removed $base: $(( $(device_free_mb) - free )) MB freed; '$0 eye-base restore' puts it back" ;;
    restore)
      require_ubuntu eye-base
      ensure_eye_base
      log "$base is on the device" ;;
    *) die "usage: $0 eye-base status|save|remove|restore" ;;
  esac
}

# dump_part <device> <tag> <backup dir>: device -> <tag>.img.zst, verified end to end. The image gets
# its name, and its hashes replace the tag's lines in SHA256SUMS and RAW-SHA256SUMS, only once it has
# verified, so an interrupted dump leaves nothing that looks complete.
dump_part() {
  local dev=$1 tag=$2 dir=$3 t0=$SECONDS h raw z lz f=$3/$2.img.zst
  printf '   %-14s' "$tag"
  dssh "sudo -n $KDIR/kit/dump.sh $dev $tag" > "$f.part" || { rm -f "$f.part"; die "reading $dev failed"; }
  h=$(dssh "sudo -n cat /run/pk-$tag.raw /run/pk-$tag.zst")
  raw=${h%%$'\n'*} z=${h##*$'\n'}
  lz=$(sha256sum "$f.part" | cut -c1-64)
  [ "$lz" = "$z" ] || { rm -f "$f.part"; die "transfer of $tag was corrupted"; }
  touch "$dir/SHA256SUMS" "$dir/RAW-SHA256SUMS"
  sed -i "/[ *]$tag\.img\.zst\$/d" "$dir/SHA256SUMS"
  sed -i "/[ *]$tag\.img\$/d" "$dir/RAW-SHA256SUMS"
  mv "$f.part" "$f"
  echo "$z  $tag.img.zst" >> "$dir/SHA256SUMS"
  echo "$raw  $tag.img" >> "$dir/RAW-SHA256SUMS"
  printf '%6s  %4ds\n' "$(du -h "$f" | cut -f1)" $((SECONDS - t0))
}

# write_part <device> <tag> <backup dir>: <tag>.img.zst -> device, verified by read-back.
write_part() {
  local dev=$1 tag=$2 bk=$3 raw
  raw=$(awk -v f="$tag.img" '$2==f {print $1}' "$bk/RAW-SHA256SUMS")
  [ -n "$raw" ] || die "$tag.img not listed in $bk/RAW-SHA256SUMS"
  log "Writing $tag to $dev"
  dssh "sudo -n $KDIR/kit/write.sh $dev $raw" < "$bk/$tag.img.zst"
}

# ---------------------------------------------------------------- commands

cmd_connect() {
  HOST=${1:-$HOST} DEVUSER=${2:-$DEVUSER}
  if [ -z "$HOST" ]; then read -r -p "Device IP or hostname: " HOST; fi
  if [ -z "$DEVUSER" ]; then read -r -p "Device user name: " DEVUSER; fi
  [[ $HOST =~ ^[A-Za-z0-9.-]+$ ]] || die "'$HOST' isn't an IPv4 address or a host name"
  [[ $DEVUSER =~ ^[a-z_][a-z0-9_.-]*$ ]] || die "'$DEVUSER' isn't a Linux user name"
  if [ ! -f "$KEY" ]; then
    log "Creating SSH key $KEY"
    mkdir -p "$(dirname "$KEY")"; ssh-keygen -q -t ed25519 -N "" -C percept-kit -f "$KEY"
  fi
  # Stock Mariner blocks a source IP for 60 s after 6 new SSH connections in a minute,
  # so this uses at most 5 before load_state exempts the PC.
  # The probe prints yes or no and exits 0, so a failure means that SSH itself failed.
  local pub sudo_ok probe='if sudo -n test -e /etc/sudoers.d/90-percept-kit 2>/dev/null; then echo yes; else echo no; fi'
  pub=$(cat "$KEY.pub")
  if ! sudo_ok=$(dssh "$probe" 2>/dev/null); then
    log "Installing the SSH key on the device (enter the device password)"
    ssh "${SSH_OPTS[@]}" -o PubkeyAuthentication=no "$DEVUSER@$HOST" \
      "umask 077; mkdir -p ~/.ssh; grep -qxF '$pub' ~/.ssh/authorized_keys 2>/dev/null || echo '$pub' >> ~/.ssh/authorized_keys"
    sudo_ok=$(dssh "$probe") || die "key login still fails"
  fi
  if [ "$sudo_ok" != yes ]; then
    log "Enabling passwordless sudo for $DEVUSER (enter the device password if asked)"
    ssh -t -i "$KEY" "${SSH_OPTS[@]}" "$DEVUSER@$HOST" \
      "echo '$DEVUSER ALL=(ALL) NOPASSWD: ALL' > /tmp/pk-sudoers && sudo visudo -cqf /tmp/pk-sudoers &&
       sudo install -m 440 -o root -g root /tmp/pk-sudoers /etc/sudoers.d/90-percept-kit; rm -f /tmp/pk-sudoers"
  fi
  save_conf
  load_state
  [ "$D_kitsudo" = yes ] && [ "$D_sudo" = yes ] || die "passwordless sudo is not working"
  show_state
}

cmd_status() {
  need_conf; load_state; show_state
  case $D_state in
    ubuntu|usb-ubuntu)
      echo
      dssh "if [ -f ~/percept-kit/.env ]; then sudo -n bash ~/percept-kit/scripts/stack.sh status
            else echo 'The stack is not deployed: run $0 deploy'; fi" || true ;;
  esac
}

cmd_assess() {
  need_conf; load_state
  mkdir -p "$REPORTS"
  local f; f=$REPORTS/assess-$(date +%Y%m%d-%H%M%S).txt
  dscript assess.sh | tee "$f"
  log "Saved $f"
}

cmd_backup() {
  need_conf; load_state
  case $D_state in
    stock) ;;
    rescue) warn "rescue layout: this backup is for reference only; restore uses a stock backup" ;;
    *) die "backup runs on Mariner (state: $D_state)" ;;
  esac
  upload_kit
  local dir; dir=$BACKUPS/$(date +%Y%m%d-%H%M%S)-${D_cid: -8}
  mkdir -p "$dir.partial"
  log "Backing up the whole eMMC to $dir (about 4 GB, 10-30 minutes)"
  printf 'cid=%s\nstate=%s\nmodel=%s\nos=%s\nhostname=%s\ncreated=%s\n' "$D_cid" "$D_state" "$D_model" \
    "$D_os" "$D_hostname" "$(date -u +%FT%TZ)" > "$dir.partial/kit-backup.conf"
  dscript assess.sh > "$dir.partial/assess.txt"
  dssh "sudo -n dd if=/dev/mmcblk0 bs=512 count=34 status=none" > "$dir.partial/gpt-primary.bin"
  dssh 'sudo -n sh -c "dd if=/dev/mmcblk0 bs=512 skip=\$((\$(blockdev --getsz /dev/mmcblk0) - 33)) count=33 status=none"' \
    > "$dir.partial/gpt-backup.bin"
  (cd "$dir.partial" && sha256sum gpt-primary.bin gpt-backup.bin > SHA256SUMS)
  dssh "sudo -n tar -C /var -czf - custom-configs" > "$dir.partial/custom-configs.tar.gz"
  save_eye_base "$dir.partial" || true
  local running=
  if [ "$D_state" = stock ]; then
    # Quiesce IoT Edge/Docker so p5 (/var) is as consistent as a live copy can be.
    running=$(dssh "for u in iotedge docker containerd; do systemctl -q is-active \$u && echo \$u; done" || true)
    if [ -n "$running" ]; then dssh "sudo -n systemctl stop iotedge docker.socket docker containerd; sync"; fi
  fi
  local p
  for p in boot0 boot1; do dump_part /dev/mmcblk0$p $p "$dir.partial"; done
  for p in 1 2 3 4 5; do dump_part /dev/mmcblk0p$p p$p "$dir.partial"; done
  if [ -n "$running" ]; then dssh "sudo -n systemctl start $(echo $running | tr '\n' ' ')"; fi
  mv "$dir.partial" "$dir"
  verify_backup "$dir"
  log "Backup complete: $dir"
}

install_ubuntu() {
  upload_kit
  log "Building Ubuntu 22.04 on p5 (downloads ~300 MB; 20-40 minutes)"
  job build "$KDIR/build-ubuntu-rootfs.sh /dev/mmcblk0p5"
  load_state
  [ "$D_p5label" = ubuntu-root ] || die "p5 does not hold the new Ubuntu rootfs"
  log "Installing the multi-boot GRUB menu (trial boot into Ubuntu once)"
  job grub "$KDIR/install-grub-cfg.sh $D_p5partuuid --default 0 --timeout 3 --next 1; rc=\$?; umount /mnt/ubuntu; exit \$rc"
  log "Trial boot into Ubuntu (a failed boot falls back to rescue Mariner)"
  reboot_wait
  [ "$D_state" = ubuntu ] || die "Ubuntu did not come up (state: $D_state); see /var/tmp/pk-build.log on the device"
}

# Once Ubuntu has booted: make it the default, build and start every component, then reboot to prove
# it all comes back by itself.
finish_install() {
  [ "$D_state" = ubuntu ] || die "expected Ubuntu (state: $D_state)"
  log "Ubuntu is running: making it the default boot entry"
  upload_kit
  dssh "sudo -n $KDIR/install-grub-cfg.sh $D_p5partuuid --default 1 --timeout 3"
  deploy
  log "Rebooting to check that everything comes back by itself"
  reboot_wait
  [ "$D_state" = ubuntu ] || die "expected Ubuntu after the reboot (state: $D_state)"
  health 600
  log "Done. Everything is linked from http://$HOST/"
  log "Next: pair a Bluetooth speaker at http://$HOST:8001/#bt (or '$0 bt scan', then '$0 bt pair <MAC>')"
}

# Room on this PC for a backup (every eMMC partition, compressed, and the Eye's base image) and the
# image of p5 that install adds: about 7 GB.
check_backup_space() {
  local free
  mkdir -p "$BACKUPS"
  free=$(df -Pm "$BACKUPS" | awk 'NR == 2 {print $4}')
  [ -z "$free" ] || [ "$free" -ge 8000 ] || die "the backup and install need about 7 GB on this PC; $BACKUPS has $free MB free"
}

cmd_install() {
  need_conf; load_state
  local bk=
  case $D_state in
    stock)
      bk=$(find_backup 2>/dev/null) || bk=
      if [ -n "$bk" ]; then
        confirm "Install, using backup $(basename "$bk"):"$'\n'"$(install_summary)"
      else
        confirm "Install, starting with a full backup of the eMMC to backups/ (about 20 minutes; with the image of p5 that install adds, about 7 GB on this PC):"$'\n'"$(install_summary)"
        YES=1
        check_backup_space
        cmd_backup
        bk=$(find_backup)
      fi
      YES=1
      verify_backup "$bk"
      upload_kit
      log "Moving Mariner's /var to p4 (rescue layout)"
      job rescue-prep "$KDIR/rescue-prep.sh"
      reboot_wait
      [ "$D_state" = rescue ] || die "expected rescue Mariner after reboot (state: $D_state)"
      # A backup used before (install, restore, install) keeps its first clean image of p5.
      if backup_verified "$bk" p5-consistent.img.zst; then
        log "The backup already holds a clean image of Mariner's original /var (p5)"
      else
        log "Imaging Mariner's original /var (p5) now that it is unmounted"
        upload_kit
        dump_part /dev/mmcblk0p5 p5-consistent "$bk"
      fi
      install_ubuntu
      finish_install ;;
    rescue)
      if [ "$D_p5label" = ubuntu-root ]; then
        confirm "p5 already holds Ubuntu. Rebuild it from scratch (erases it), then install everything?"
      else
        bk=$(find_backup)
        if ! backup_verified "$bk" p5-consistent.img.zst; then
          log "Imaging Mariner's original /var (p5)"; upload_kit
          dump_part /dev/mmcblk0p5 p5-consistent "$bk"
        fi
      fi
      install_ubuntu
      finish_install ;;
    ubuntu) finish_install ;;
    *) die "install needs stock Mariner (state: $D_state)" ;;
  esac
}

install_summary() {
  cat <<'EOF'
- Mariner stays on the device as a rescue OS; its /var moves to p4.
- p5 (Mariner's /var: IoT Edge, Docker images, logs) is erased for Ubuntu 22.04.
- Ubuntu becomes the default once it has booted; then every component is built and started.
About 1-2.5 hours. The device reboots several times; keep it powered.
EOF
}

entry_of() {
  case $1 in
    mariner) echo 0 ;; ubuntu) echo 1 ;; ubuntu-nohdmi) echo 2 ;;
    *) die "unknown boot target '$1' (mariner | ubuntu | ubuntu-nohdmi)" ;;
  esac
}

cmd_commit() {
  need_conf; load_state
  local n; n=$(entry_of "${1:-ubuntu}")
  [ "$D_state" = ubuntu ] || [ "$D_state" = rescue ] || die "commit needs Ubuntu or rescue Mariner (state: $D_state)"
  [ "$D_p5label" = ubuntu-root ] || die "p5 does not hold Ubuntu"
  upload_kit
  dssh "sudo -n $KDIR/install-grub-cfg.sh $D_p5partuuid --default $n --timeout 3"
  log "Default boot entry is now '${1:-ubuntu}'"
}

cmd_boot() {
  need_conf; load_state
  local n; n=$(entry_of "${1:?usage: $0 boot mariner|ubuntu|ubuntu-nohdmi}")
  [ "$D_state" = ubuntu ] || [ "$D_state" = rescue ] || die "boot needs the kit's GRUB menu (state: $D_state)"
  upload_kit
  dssh "sudo -n $KDIR/install-grub-cfg.sh --next-only $n"
  reboot_wait
}

# ---------------------------------------------------------------- the stack (compose.yaml)

require_ubuntu() {
  case $D_state in ubuntu|usb-ubuntu) ;; *) die "$1 runs on Ubuntu (state: $D_state); see '$0 install'" ;; esac
}

# Service names from the command line reach a remote shell: letters, digits, '-' and '_' only.
check_names() {
  local n
  for n in "$@"; do [[ $n =~ ^[A-Za-z0-9_-]+$ ]] || die "not a service name: '$n'"; done
}

# What deploy uploads: compose.yaml, the modules and the sample, the services' configuration, and your
# config/ when there is one. Only files the device doesn't already have are sent; stack.sh on the
# device puts it all in place.
upload_stack() {
  local items=(compose.yaml modules samples services) files list n
  if [ -d "$ROOT/config" ]; then items+=(config); fi
  files=$(mktemp) list=$(mktemp)
  (cd "$ROOT" && find "${items[@]}" -type f ! -path '*/__pycache__/*' ! -name '*.pyc') > "$files"
  if ! (cd "$ROOT" && tr '\n' '\0' < "$files" | xargs -0 sha256sum) |
       dssh "sudo -n bash $KDIR/stack.sh stage" > "$list"; then
    rm -f "$files" "$list"; die "could not prepare the upload on the device"
  fi
  # Only files this PC offered are ever sent, whatever the device asks for.
  if grep -vxFf "$files" "$list" | grep -q .; then
    rm -f "$files" "$list"; die "the device asked for files outside the upload"
  fi
  n=$(grep -c . "$list" || true)
  if [ "$n" -gt 0 ]; then
    log "Uploading the stack ($n changed file$([ "$n" = 1 ] || echo s))"
    (cd "$ROOT" && tar -cf - -T "$list") | dssh "sudo -n bash $KDIR/stack.sh receive" ||
      { rm -f "$files" "$list"; die "the upload failed"; }
  else
    log "The device already has every file"
  fi
  rm -f "$files" "$list"
}

# Brings the device to this repository's state: builds what changed and starts everything. After load_state.
deploy() {
  local plan build
  [[ $HOST =~ ^[A-Za-z0-9.-]+$ ]] || die "HOST in $CONF must be an IPv4 address or a host name"
  upload_kit
  upload_stack
  plan=$(dssh "sudo -n env PK_HOST=$HOST bash $KDIR/stack.sh plan") || die "deploy stopped: the running stack is unchanged"
  build=$(sed -n 's/^build=//p' <<< "$plan")
  if [[ " $build " == *" eye "* ]]; then
    if grep -qx eyebase=missing <<< "$plan"; then ensure_eye_base 1500; fi
    log "To build: $build (the Eye takes 15-30 minutes)"
  elif [ -n "$build" ]; then
    log "To build: $build"
  fi
  job deploy "PK_HOST=$HOST bash $KDIR/stack.sh apply"
}

# health <seconds>: every component's state and address, waiting up to <seconds> for them to answer.
health() {
  log "Checking every component"
  dssh "sudo -n bash $KDIR/stack.sh health $1" || die "some components aren't working: see '$0 logs <service>'"
}

cmd_deploy() {
  need_conf; load_state; require_ubuntu deploy
  deploy
  health 300
}

cmd_logs() {
  need_conf; load_state; require_ubuntu logs
  local svc=${1:-} lines=${2:-50}
  if [ -n "$svc" ]; then check_names "$svc"; fi
  [[ $lines =~ ^[0-9]+$ ]] || die "usage: $0 logs [service] [lines]"
  dssh "cd ~/percept-kit && sudo -n docker compose logs --tail $lines $svc"
}

# restart|stop|start [service ...]: docker compose on the device. A stop lasts until the next deploy.
cmd_service() {
  local action=$1; shift
  need_conf; load_state; require_ubuntu "$action"
  check_names "$@"
  dssh "cd ~/percept-kit && sudo -n docker compose $action $*"
}

cmd_rebuild() {
  local svc=${1:-}
  [ -n "$svc" ] || die "usage: $0 rebuild <service>   (eye, ear, mouth or voice)"
  check_names "$svc"
  need_conf; load_state; require_ubuntu rebuild
  upload_kit
  if [ "$svc" = eye ]; then ensure_eye_base 1500; fi
  job rebuild "bash $KDIR/stack.sh rebuild $svc"
  health 300
}

# compose <args>: any docker compose command, in ~/percept-kit on the device (interactive).
cmd_compose() {
  need_conf; load_state; require_ubuntu compose
  [ $# -gt 0 ] || die "usage: $0 compose <docker compose arguments>   (e.g. $0 compose ps)"
  ssh -t -i "$KEY" "${SSH_OPTS[@]}" -o BatchMode=yes "$DEVUSER@$HOST" \
    "cd ~/percept-kit && sudo -n docker compose $(printf '%q ' "$@")"
}

BT=/usr/local/lib/percept-kit/bt.sh
# Install modules/mouth/bt.sh on the host and run its idempotent setup: BlueALSA, classic-only
# Bluetooth, the uid-5003 account the mouth container needs on D-Bus, the boot reconnect service.
ensure_bt_host() {
  tr -d '\r' < "$ROOT/modules/mouth/bt.sh" |
    dssh "sudo -n mkdir -p ${BT%/*} && sudo -n tee $BT > /dev/null && sudo -n chmod 755 $BT && sudo -n $BT setup"
}

cmd_bt() {
  need_conf; load_state; require_ubuntu bt
  local sub=${1:-status} arg=${2:-}
  case $sub in
    status|devices) arg= ;;
    scan) arg=${arg:-15}; [[ $arg =~ ^[0-9]+$ ]] || die "usage: $0 bt scan [seconds]" ;;
    pair|connect|disconnect|forget)
      [[ $arg =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] || die "usage: $0 bt $sub <MAC>  (find it with '$0 bt scan')" ;;
    *) die "usage: $0 bt scan [seconds] | pair <MAC> | connect <MAC> | disconnect <MAC> | forget <MAC> | status" ;;
  esac
  ensure_bt_host
  case $sub in
    scan) log "Scanning for $arg s (put the speaker in pairing mode)" ;;
    pair) log "Pairing with $arg (keep the speaker in pairing mode; up to a minute)" ;;
  esac
  dssh "sudo -n $BT --table $sub $arg"
}

cmd_restore() {
  need_conf; load_state
  local bk=${1:-}
  case $D_state in ubuntu|rescue|stock) ;; *) die "restore is not possible from state '$D_state'" ;; esac
  if [ "$D_state" != stock ] || [ "$D_p4label" = pk-mariner-var ]; then
    [ -n "$bk" ] || bk=$(find_backup)
    grep -qx "state=stock" "$bk/kit-backup.conf" || die "$bk is not a stock backup"
    verify_backup "$bk"
  fi
  if [ "$D_state" = ubuntu ]; then
    confirm "Restore the stock Azure Percept OS from $(basename "$bk")? Ubuntu on p5 will be erased."
    YES=1
    upload_kit
    dssh "sudo -n $KDIR/install-grub-cfg.sh --next-only 0"
    reboot_wait
    [ "$D_state" = rescue ] || die "expected rescue Mariner (state: $D_state)"
  fi
  if [ "$D_state" = rescue ]; then
    confirm "Restore the stock Azure Percept OS from $(basename "$bk")? p5 will be overwritten."
    upload_kit
    local p5=p5; if backup_verified "$bk" p5-consistent.img.zst; then p5=p5-consistent; fi
    write_part /dev/mmcblk0p5 $p5 "$bk"
    write_part /dev/mmcblk0p1 p1 "$bk"
    dssh "sudo -n $KDIR/restore-stock-p3.sh"
    reboot_wait
    [ "$D_state" = stock ] || die "expected stock Mariner (state: $D_state)"
  fi
  if [ "$D_p4label" = pk-mariner-var ]; then
    upload_kit
    write_part /dev/mmcblk0p4 p4 "$bk"
  fi
  log "Device is back to stock. '$0 disconnect' removes the kit's sudo rule."
}

# The stack's own files in ~/percept-kit stay: the running containers use them.
cmd_disconnect() {
  need_conf
  local pub; pub=$(cat "$KEY.pub")
  dssh "sudo -n rm -rf ~/percept-kit/scripts ~/percept-kit/.incoming; rmdir ~/percept-kit 2>/dev/null
        sudo -n rm -f /etc/sudoers.d/90-percept-kit"
  log "Removed the kit's scripts and its sudo rule"
  if [ "${1:-}" = --remove-key ]; then
    dssh "grep -vxF '$pub' ~/.ssh/authorized_keys > ~/.ssh/ak.tmp; mv ~/.ssh/ak.tmp ~/.ssh/authorized_keys"
    log "Removed the SSH key"
  fi
}

usage() {
  cat <<EOF
Usage: $0 [-y] <command> [args]

Set up
  connect [host] [user]      set up SSH key login and passwordless sudo, and remember the device
  install [--hostname H]     stock Percept to Ubuntu with everything running: a backup (when there's
                             none), Ubuntu 22.04 on p5 with Mariner kept as a rescue OS, then deploy

Day to day (Ubuntu)
  status                     the device's layout, and every component's state and address
  deploy                     apply this repository and config/ to the device: build what changed,
                             start everything, check it
  logs [service] [lines]     recent log lines of one service, or of all
  restart|stop|start [service ...]  docker compose restart, stop or start on the device
  rebuild <service>          build eye, ear, mouth or voice from scratch, then restart it
  compose <args>             any docker compose command, in ~/percept-kit on the device
  bt <cmd>                   Bluetooth speaker: scan [seconds] | pair <MAC> | connect <MAC> |
                             disconnect <MAC> | forget <MAC> | status
  eye-base <cmd>             the Eye's build base (1.2 GB): status | save | remove | restore

Boot, backup and restore
  assess                     hardware and software report, saved under reports/
  backup                     full eMMC image to backups/ (stock Mariner), with the Eye's build base
  commit [target]            make ubuntu (or mariner) the default boot entry
  boot <target>              reboot once into mariner | ubuntu | ubuntu-nohdmi
  restore [backup-dir]       put the stock Azure Percept OS back, bit for bit
  disconnect [--remove-key]  remove the kit's scripts and sudo rule (and SSH key) from the device

  -y   don't ask for confirmation
EOF
}

main() {
  while [ $# -gt 0 ]; do case $1 in -y|--yes) YES=1; shift ;; *) break ;; esac; done
  local cmd=${1:-help}; shift || true
  case $cmd in
    connect) cmd_connect "$@" ;;
    status) cmd_status ;;
    assess) cmd_assess ;;
    backup) cmd_backup ;;
    install)
      if [ "${1:-}" = --hostname ]; then PK_HOSTNAME=${2:?hostname}; fi
      cmd_install ;;
    commit) cmd_commit "$@" ;;
    boot) cmd_boot "$@" ;;
    deploy) cmd_deploy ;;
    logs) cmd_logs "$@" ;;
    restart|stop|start) cmd_service "$cmd" "$@" ;;
    rebuild) cmd_rebuild "$@" ;;
    compose) cmd_compose "$@" ;;
    bt) cmd_bt "$@" ;;
    eye-base) cmd_eye_base "$@" ;;
    restore) cmd_restore "$@" ;;
    disconnect) cmd_disconnect "$@" ;;
    help|-h|--help) usage ;;
    *) usage; exit 2 ;;
  esac
}
# Parsed whole before it runs, so editing this file (a git pull, say) while a long command runs can't
# make bash read on into the changed text.
{ main "$@"; exit; }
