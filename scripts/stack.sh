#!/usr/bin/env bash
# The device's Compose stack (~/percept-kit), driven by percept-kit.sh. Runs as root.
#   stack.sh stage              reads "sha256 path" lines (the PC's files), starts .incoming/ with the
#                               ones the stack already has, and prints the paths it still needs
#   stack.sh receive            unpacks a tar of those paths (from stdin) into .incoming/
#   stack.sh plan               checks the upload's configuration; prints what apply will build
#                               ("build=<services>") and whether the Eye's build base is in Docker
#                               ("eyebase=present|missing")
#   stack.sh apply              host setup; the upload into place; build what changed; start everything
#   stack.sh rebuild <service>  build one image from scratch even if its source didn't change, then restart it
#   stack.sh health [SECONDS]   check every component, retrying for up to SECONDS; exit 1 if one fails
#   stack.sh status             the containers, then one health pass
# The kit passes PK_HOST, the address your PC uses for the device.
set -euo pipefail

PROJECT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
INCOMING=$PROJECT/.incoming
# What the kit uploads. Everything else in the project (.env, .env.d/, .deploy-baseline, scripts/)
# belongs to the device.
MANAGED="compose.yaml modules samples services config"
LIB=/usr/local/lib/percept-kit
LABEL=org.percept-kit.source
cd "$PROJECT"

log() { echo "### $*"; }
warn() { echo "WARNING: $*" >&2; }
die() { echo "ERROR: $*" >&2; exit 1; }

# Hash of a file or directory tree (names and contents), 16 hex digits; "-" if it doesn't exist.
# Documentation (*.md) doesn't count: no image copies it, so editing it shouldn't rebuild one.
tree_hash() {
  if [ ! -e "$1" ]; then echo -; return; fi
  (cd "$(dirname "$1")" && find "$(basename "$1")" -type f ! -name '*.md' -print0 | LC_ALL=C sort -z | xargs -0r sha256sum) |
    sha256sum | cut -c1-16
}

# The Eye's build base (Microsoft's azureeyemodule image), from the Dockerfile in directory $1.
eye_base() { sed -n 's/^ARG BASE=//p' "$1/modules/eye/Dockerfile"; }

INFO_PY='
import json, os, sys
root = sys.argv[1].rstrip("/") + "/"
config = json.load(sys.stdin)
here = lambda p: p if p.startswith("/") else os.path.join(root, p)
for name, s in sorted(config.get("services", {}).items()):
    if s.get("build"):
        # Compose names a built image <project>-<service> unless the service sets one.
        print("build", name, here(s["build"]["context"]), s.get("image") or config["name"] + "-" + name)
    else:
        image = s["image"]
        if ":" not in image.rsplit("/", 1)[-1] and "@" not in image:
            image += ":latest"
        print("pull", name, image)
    print("container", name, s.get("container_name", name))
    for v in s.get("volumes", []):
        if v.get("type") == "bind" and (here(v["source"]).rstrip("/") + "/").startswith(root):
            print("bind", name, here(v["source"]))
for n in config.get("networks", {}).values():
    if n.get("external"):
        print("network", n["name"])
for v in config.get("volumes", {}).values():
    if v.get("external"):
        print("volume", v["name"])
'
# info [--all] [DIR]: facts from the compose configuration in DIR (default: the project), one per
# line: "build <service> <context> <image>", "pull <service> <image>", "container <service> <name>",
# "bind <service> <path in DIR>", "network <name>", "volume <name>". Active services only, unless --all.
info() {
  local all=() dir json
  if [ "${1:-}" = --all ]; then all=(--profile '*'); shift; fi
  dir=${1:-$PROJECT}
  json=$(cd "$dir" && docker compose "${all[@]}" config --format json) || return 1
  python3 -c "$INFO_PY" "$dir" <<< "$json"
}

# needs_build <context> <image>: prints the context's hash when the image wasn't built from it.
needs_build() {
  local want have
  want=$(tree_hash "$1")
  have=$(docker image inspect -f "{{index .Config.Labels \"$LABEL\"}}" "$2" 2>/dev/null || true)
  if [ "$want" != "$have" ]; then echo "$want"; fi
}

# ---------------------------------------------------------------- upload

STAGE_PY='
import hashlib, os, sys
project, incoming, managed = sys.argv[1], sys.argv[2], sys.argv[3].split()
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
for line in sys.stdin.read().splitlines():
    digest, path = line[:64], line[66:]  # sha256sum: "<hash> <space or *><path>"
    parts = path.split("/")
    if (len(line) < 67 or line[64:65] != " " or line[65:66] not in " *" or
            not all(c in "0123456789abcdef" for c in digest) or
            parts[0] not in managed or ".." in parts or "" in parts):
        sys.exit("unexpected line in the file list: " + line)
    have, want = os.path.join(project, path), os.path.join(incoming, path)
    if os.path.isfile(have) and not os.path.islink(have) and sha256(have) == digest:
        os.makedirs(os.path.dirname(want), exist_ok=True)
        os.link(have, want)
    else:
        print(path)
'
# Starts .incoming/ with hard links to the stack's files that the PC's list (stdin) says are unchanged,
# and prints the paths the PC must send.
stage() {
  rm -rf "$INCOMING"
  install -d -m 700 "$INCOMING"
  python3 -c "$STAGE_PY" "$PROJECT" "$INCOMING" "$MANAGED"
}

# Unpacks the PC's tar (stdin) into .incoming/. Files edited on Windows may have CRLF line ends: the
# received text files lose their CRs (the linked ones, from earlier deploys, already have).
receive() {
  [ -d "$INCOMING" ] || die "run stack.sh stage first"
  tar -C "$INCOMING" -xf - --no-same-owner
  find "$INCOMING" -type f -links 1 \( -name Dockerfile -o -name '*.sh' -o -name '*.py' -o -name '*.conf' \
    -o -name '*.txt' -o -name '*.env' -o -name '*.yaml' -o -name '*.yml' -o -name '*.json' -o -name '*.js' \
    -o -name '*.css' \) -exec sed -i 's/\r$//' {} +
}

# Checks that the upload's configuration loads, with its .env; prints what apply will build.
plan() {
  local facts list='' svc ctx img
  [ -f "$INCOMING/compose.yaml" ] || die "nothing uploaded to $INCOMING"
  write_env "$INCOMING"
  facts=$(info "$INCOMING") || die "the configuration doesn't load (see above): fix it on your PC, then deploy again"
  while read -r _ svc ctx img; do
    if [ -n "$(needs_build "$ctx" "$img")" ]; then list="$list $svc"; fi
  done < <(grep '^build ' <<< "$facts" || true)
  echo "build=${list# }"
  if docker image inspect "$(eye_base "$INCOMING")" > /dev/null 2>&1; then echo eyebase=present; else echo eyebase=missing; fi
}

# ---------------------------------------------------------------- apply

host_setup() {
  local missing='' p conf level
  for p in docker-compose-v2 docker-buildx rsync; do
    dpkg -s "$p" > /dev/null 2>&1 || missing="$missing $p"
  done
  if [ -n "$missing" ]; then
    log "Installing$missing"
    apt-get update -q > /dev/null
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $missing > /dev/null
  fi
  # Cap the journal: the eMMC is small.
  conf=/etc/systemd/journald.conf.d/percept-kit.conf
  if [ "$(cat "$conf" 2> /dev/null)" != "$(printf '[Journal]\nSystemMaxUse=200M')" ]; then
    mkdir -p "${conf%/*}"
    printf '[Journal]\nSystemMaxUse=200M\n' > "$conf"
    systemctl restart systemd-journald
    journalctl --vacuum-size=200M > /dev/null 2>&1 || true
  fi
  # The Realtek driver logs every Wi-Fi power-save change at its default level (4, info): about
  # 10,000 journal lines an hour. Errors only, now and at every boot.
  conf=/etc/modprobe.d/percept-kit-wifi.conf
  if [ "$(cat "$conf" 2> /dev/null)" != 'options 88x2ce rtw_drv_log_level=2' ]; then
    echo 'options 88x2ce rtw_drv_log_level=2' > "$conf"
  fi
  level=/sys/module/88x2ce/parameters/rtw_drv_log_level
  if [ -w "$level" ] && [ "$(cat "$level")" != 2 ]; then echo 2 > "$level"; fi
}

# Puts the upload in place. rsync --inplace rewrites changed files where they are, so the directories
# and files containers have mounted stay the same ones and running containers see the new content.
sync_upload() {
  local p
  mkdir -p "$INCOMING/config/files"
  # Settings and the files they refer to may hold credentials: only the modules' group (5000) and the
  # device user read them. The rest of config/ (sources of your own containers) stays readable.
  chmod -R u=rwX,go=rX "$INCOMING/config"
  chgrp -R 5000 "$INCOMING/config/files"
  chmod -R u=rwX,g=rX,o= "$INCOMING/config/files"
  for p in percept.env compose.override.yaml; do
    if [ -f "$INCOMING/config/$p" ]; then chgrp 5000 "$INCOMING/config/$p"; chmod 640 "$INCOMING/config/$p"; fi
  done
  for p in $MANAGED; do
    if [ -e "$INCOMING/$p" ]; then
      rsync -a --delete --inplace --checksum "$INCOMING/$p" "$PROJECT/"
    else
      rm -rf "${PROJECT:?}/$p"
    fi
  done
}

# setting DIR KEY: the value of KEY in DIR/config/percept.env (its last definition), without quotes.
setting() {
  if [ -f "$1/config/percept.env" ]; then
    sed -n "s/^[[:space:]]*$2[[:space:]]*=//p" "$1/config/percept.env" | tail -n1 |
      sed "s/^[[:space:]]*//; s/[[:space:]]*\$//; s/^\"\\(.*\\)\"\$/\\1/; s/^'\\(.*\\)'\$/\\1/"
  fi
}

# The device's IPv4 addresses (not Docker's bridges), sorted, so the list only changes when they do.
device_addresses() {
  ip -4 -o addr show scope global | awk '$2 !~ /^(docker|br-|veth)/ {split($4, a, "/"); print a[1]}' | sort -uV
}

# Every IPv4 address and name the device answers to: Homepage refuses other Host headers.
allowed_hosts() {
  local h out=''
  for h in "$1" $(device_addresses) "$(hostname)" "$(hostname).local"; do
    [[ $h =~ ^[0-9]+(\.[0-9]+){3}$ || $h =~ ^[A-Za-z0-9.-]+$ ]] || continue
    case ",$out," in *",$h,"*) ;; *) out=${out:+$out,}$h ;; esac
  done
  echo "$out"
}

# The modules whose settings deploy hands over, each with its own prefix in config/percept.env.
MODULES="eye ear mouth voice"

# write_env DIR: DIR/.env, the device's facts (read by compose.yaml), then DIR/config/percept.env;
# and DIR/.env.d/<module>.env, each module's own settings, so a change recreates only that module.
write_env() {
  local dir=$1 host=${PK_HOST:-} profiles='' files=compose.yaml owner m
  owner=$(stat -c %U "$PROJECT")
  if [ -z "$host" ] && [ -f "$PROJECT/.env" ]; then host=$(sed -n 's/^PK_HOST=//p' "$PROJECT/.env" | tail -n1); fi
  if [ -z "$host" ]; then host=$(device_addresses | head -n1); fi
  if [ -z "$(setting "$dir" MQTT_URL)" ]; then profiles=local-mqtt; fi
  if [ -f "$dir/config/compose.override.yaml" ]; then files=$files:config/compose.override.yaml; fi
  {
    echo "# Written by percept-kit deploy from the device's facts and config/percept.env."
    echo "# Change config/percept.env on your PC and deploy, rather than this file."
    echo "PK_HOST=$host"
    echo "PK_ALLOWED_HOSTS=$(allowed_hosts "$host")"
    echo "PK_AUDIO_GID=$(getent group audio | cut -d: -f3)"
    echo "PK_INPUT_GID=$(getent group input | cut -d: -f3)"
    echo "COMPOSE_PROFILES=$profiles"
    echo "COMPOSE_FILE=$files"
    if [ -f "$dir/config/percept.env" ]; then echo; cat "$dir/config/percept.env"; echo; fi
  } > "$dir/.env.new"
  chown "$owner" "$dir/.env.new"
  chmod 600 "$dir/.env.new"
  mv "$dir/.env.new" "$dir/.env"
  rm -rf "$dir/.env.d"
  install -d -m 700 -o "$owner" "$dir/.env.d"
  for m in $MODULES; do
    if [ -f "$dir/config/percept.env" ]; then
      grep -E "^[[:space:]]*${m^^}_[A-Za-z0-9_]*[[:space:]]*=" "$dir/config/percept.env" || true
    fi > "$dir/.env.d/$m.env"
    chown "$owner" "$dir/.env.d/$m.env"
    chmod 600 "$dir/.env.d/$m.env"
  done
}

# The network and volumes compose.yaml declares external: kept outside the project.
ensure_shared() {
  local kind name
  while read -r kind name; do
    if [ "$kind" = network ]; then
      docker network inspect "$name" > /dev/null 2>&1 || docker network create "$name" > /dev/null
    else
      docker volume inspect "$name" > /dev/null 2>&1 || docker volume create "$name" > /dev/null
    fi
  done < <(info --all | grep -E '^(network|volume) ')
}

# free_mb: MB free where Docker keeps its images.
free_mb() { df --output=avail -BM "$(docker info -f '{{.DockerRootDir}}')" | tail -n1 | tr -dc 0-9; }
# need_space <MB> <what>: stops unless that much is free. Removing the Eye's build base is suggested
# only when the Eye isn't what needs it.
need_space() {
  local free hint=" (for example './percept-kit.sh eye-base remove')"; free=$(free_mb)
  if [ "$2" = "building eye" ]; then hint=; fi
  if [ "$free" -lt "$1" ]; then
    die "only $free MB free for Docker, $2 needs about $1 MB. Free space first: see customize.md, \"Free up disk space\"$hint"
  fi
}

# build_one <service> <source hash> [build options]: one image, labelled with its source's hash.
build_one() {
  local svc=$1 hash=$2; shift 2
  # Only the Eye's build stage uses Microsoft's base image; the kit loads it beforehand.
  if [ "$svc" = eye ] && ! docker image inspect "$(eye_base "$PROJECT")" > /dev/null 2>&1; then
    die "the Eye's build base $(eye_base "$PROJECT") isn't in Docker"
  fi
  # The Eye compiles in a large build stage.
  if [ "$svc" = eye ]; then need_space 1500 "building eye"; else need_space 1000 "building $svc"; fi
  log "Building $svc ($( [ "$svc" = eye ] && echo '15-30 minutes' || echo 'a few minutes'))"
  PK_SOURCE=$hash docker compose --progress plain build "$@" "$svc"
  # The build cache would take space the next build needs.
  docker builder prune -af > /dev/null
}

declare -A BEFORE=() CID=()

# Remembers the content of every project path a container mounts, and which container runs each
# service. Kept in .deploy-baseline until an apply finishes, so a deploy that fails after updating
# files still restarts the right services next time.
BASELINE=$PROJECT/.deploy-baseline
snapshot() {
  local kind key value svc path
  if [ -f "$BASELINE" ]; then
    while read -r kind key value; do
      case $kind in
        bind) BEFORE[$key]=$value ;;
        cid) CID[$key]=${value#-} ;;
      esac
    done < "$BASELINE"
    return 0
  fi
  if [ ! -f compose.yaml ] || [ ! -f .env ]; then return 0; fi
  while read -r _ svc path; do BEFORE[$path]=$(tree_hash "$path"); done < <(info | grep '^bind ' || true)
  for svc in $(docker compose config --services); do CID[$svc]=$(docker compose ps -q "$svc" 2> /dev/null || true); done
  {
    for path in "${!BEFORE[@]}"; do echo "bind $path ${BEFORE[$path]}"; done
    for svc in "${!CID[@]}"; do echo "cid $svc ${CID[$svc]:--}"; done
  } > "$BASELINE"
}

# Services that kept their container through "up" but whose mounted project files changed: they need
# a restart to read them.
stale_services() {
  local svc path out=''
  while read -r _ svc path; do
    [ -n "${CID[$svc]:-}" ] || continue
    [ "$(docker compose ps -q "$svc" 2> /dev/null || true)" = "${CID[$svc]}" ] || continue
    if [ "${BEFORE[$path]:-}" != "$(tree_hash "$path")" ]; then
      case " $out " in *" $svc "*) ;; *) out="$out $svc" ;; esac
    fi
  done < <(info | grep '^bind ' || true)
  echo "${out# }"
}

# Containers that use one of the stack's names but weren't created by this project are replaced.
adopt() {
  local svc name project
  while read -r _ svc name; do
    project=$(docker container inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$name" 2> /dev/null) || continue
    if [ "$project" != percept ]; then
      log "Replacing container $name (not created by Compose)"
      docker rm -f "$name" > /dev/null
    fi
  done < <(info --all | grep '^container ')
}

# Other tags of the images Compose pulls, left behind when a version pin changes.
remove_old_pins() {
  local svc img repo other
  while read -r _ svc img; do
    case $img in *@*) continue ;; esac
    repo=${img%:*}
    for other in $(docker images --format '{{.Repository}}:{{.Tag}}' "$repo"); do
      [ "$other" = "$img" ] || docker rmi "$other" > /dev/null 2>&1 || true
    done
  done < <(info --all | grep '^pull ')
}

apply() {
  local svc ctx img hash builds=() missing='' need inactive stale active
  [ -f "$INCOMING/compose.yaml" ] || die "nothing uploaded to $INCOMING"
  host_setup
  # The upload's configuration must load before anything changes.
  write_env "$INCOMING"
  info "$INCOMING" > /dev/null || die "the configuration doesn't load (see above): fix it on your PC, then deploy again"
  snapshot
  log "Updating the stack in $PROJECT"
  sync_upload
  mv "$INCOMING/.env" "$PROJECT/.env"
  rm -rf "$PROJECT/.env.d"
  mv "$INCOMING/.env.d" "$PROJECT/.env.d"
  install -D -m 755 "$PROJECT/modules/mouth/bt.sh" "$LIB/bt.sh"
  "$LIB/bt.sh" setup
  ensure_shared

  while read -r _ svc ctx img; do
    hash=$(needs_build "$ctx" "$img")
    if [ -n "$hash" ]; then builds+=("$svc:$hash"); fi
  done < <(info | grep '^build ' || true)
  for svc in "${builds[@]}"; do build_one "${svc%%:*}" "${svc#*:}"; done

  while read -r _ svc img; do
    docker image inspect "$img" > /dev/null 2>&1 || missing="$missing $img"
  done < <(info | grep '^pull ' || true)
  if [ -n "$missing" ]; then
    # Room to unpack them: Grafana's image is about 1.4 GB, the others together about 0.8 GB.
    need=1000; case "$missing " in *" grafana/"*) need=3000 ;; esac
    need_space "$need" "pulling$missing"
    log "Pulling$missing"
    docker compose pull --policy missing --ignore-buildable --quiet
  fi

  adopt
  inactive=$(comm -23 <(docker compose --profile '*' config --services | sort) <(docker compose config --services | sort))
  for svc in $inactive; do
    if [ -n "$(docker compose --profile '*' ps -aq "$svc" 2> /dev/null)" ]; then
      log "Stopping $svc (turned off)"
      docker compose --profile '*' rm -sf "$svc" > /dev/null
    fi
  done
  log "Starting the stack"
  docker compose up -d --no-build --remove-orphans
  stale=$(stale_services)
  if [ -n "$stale" ]; then
    log "Restarting $stale (configuration changed)"
    # shellcheck disable=SC2086
    docker compose restart $stale
  fi
  rm -f "$BASELINE"

  docker image prune -f > /dev/null
  # Anonymous volumes of replaced containers (the label keeps named volumes out, on any Docker version).
  docker volume prune -f --filter label=com.docker.volume.anonymous > /dev/null
  remove_old_pins
  rm -rf "$INCOMING"
  active=$(docker compose config --services | tr '\n' ' ')
  log "Running: ${active% }"
  if [ ${#builds[@]} -gt 0 ]; then log "Built: ${builds[*]%%:*}"; fi
  log "Docker has $(free_mb) MB free"
}

rebuild() {
  local svc=${1:?usage: stack.sh rebuild <service>} ctx img
  read -r _ _ ctx img < <(info | grep "^build $svc " || true) || true
  [ -n "${ctx:-}" ] || die "$svc isn't built from source (services built here: $(info | awk '/^build /{print $2}' | tr '\n' ' '))"
  build_one "$svc" "$(tree_hash "$ctx")" --no-cache
  docker compose up -d --no-build "$svc"
  docker image prune -f > /dev/null
}

# ---------------------------------------------------------------- health

http_ok() { curl -fsS -o /dev/null -m 5 "$@" 2> /dev/null; }
mqtt_ok() { docker exec mosquitto mosquitto_sub -t '$SYS/broker/version' -C 1 -W 5 > /dev/null 2>&1; }
targets_up() {
  # The kit's scrape jobs count only while the service they scrape runs; any other job always counts.
  curl -fsS -m 5 http://127.0.0.1:9090/api/v1/targets 2> /dev/null | python3 -c '
import json, sys
service = {"board": "node-exporter", "containers": "cadvisor", "eye": "eye"}
active = sys.argv[1].split()
t = [x for x in json.load(sys.stdin)["data"]["activeTargets"]
     if x["labels"].get("job") not in service or service[x["labels"]["job"]] in active]
sys.exit(0 if t and all(x["health"] == "up" for x in t) else 1)' "$ACTIVE"
}

# check <service> <label> <address shown> <test command...>: one row of the health table.
ROWS=() FAILS=0
check() {
  local svc=$1 label=$2 address=$3 state=ok
  shift 3
  case " $ACTIVE " in *" $svc "*) ;; *) return 0 ;; esac
  "$@" || { state=failed; FAILS=$((FAILS + 1)); }
  ROWS+=("$label|$state|$address")
}

run_checks() {
  local h=$1 svc
  ROWS=() FAILS=0
  check homepage "Home page" "http://$h/" http_ok -H "Host: $h" http://127.0.0.1/
  check grafana "Grafana" "http://$h:3000/" http_ok http://127.0.0.1:3000/api/health
  check go2rtc "Video" "http://$h:1984/" http_ok http://127.0.0.1:1984/api
  check eye "Eye" "http://$h:8555/  rtsp://$h:8554/result" http_ok http://127.0.0.1:8555/
  check ear "Ear" "http://$h:8000/" http_ok http://127.0.0.1:8000/levels
  check mouth "Mouth" "http://$h:8001/" http_ok http://127.0.0.1:8001/status
  check mouth "Bluetooth speaker" "http://$h:8001/#bt" http_ok http://127.0.0.1:8001/bt/devices
  check voice "Voice sample" "http://$h:8080/" http_ok http://127.0.0.1:8080/state
  check mosquitto "MQTT broker" "mqtt://mosquitto:1883 (containers only)" mqtt_ok
  check prometheus "Prometheus" "http://$h:9090/" targets_up
  check node-exporter "Board metrics" "http://$h:9100/metrics" http_ok http://127.0.0.1:9100/metrics
  check cadvisor "Container metrics" "(Prometheus target)" test "$(docker inspect -f '{{.State.Health.Status}}' cadvisor 2> /dev/null)" = healthy
  # Services added in config/compose.override.yaml: running is enough.
  local known=" homepage grafana go2rtc eye ear mouth voice mosquitto prometheus node-exporter cadvisor "
  for svc in $ACTIVE; do
    case $known in
      *" $svc "*) ;;
      *) check "$svc" "$svc" "" test -n "$(docker compose ps -q --status running "$svc")" ;;
    esac
  done
}

health() {
  local wait=${1:-0} host end row
  [ -f .env ] || die "the stack isn't deployed yet: run ./percept-kit.sh deploy"
  host=$(sed -n 's/^PK_HOST=//p' .env | tail -n1)
  ACTIVE=" $(docker compose config --services | tr '\n' ' ')"
  end=$(( $(date +%s) + wait ))
  while :; do
    run_checks "$host"
    if [ "$FAILS" = 0 ] || [ "$(date +%s)" -ge "$end" ]; then break; fi
    sleep 10
  done
  printf '%-19s %-7s %s\n' Component State Address
  for row in "${ROWS[@]}"; do
    IFS='|' read -r a b c <<< "$row"
    printf '%-19s %-7s %s\n' "$a" "$b" "$c"
  done
  if [ -z "$(lsusb -d 03e7: 2> /dev/null)" ]; then
    warn "no Eye SoM on USB (Myriad X, 03e7): plug it in; the Eye waits for it"
  elif ! curl -fsS -m 5 http://127.0.0.1:8555/metrics 2> /dev/null | grep -q '^percept_myriadx_temperature_celsius'; then
    warn "the Eye isn't running a model yet (it takes up to a minute after a start)"
  fi
  if ! grep -q 'Azure Ear SoM' /proc/asound/cards 2> /dev/null; then
    warn "no Ear SoM sound card: plug it in, or reboot if it is (setup.md, Troubleshooting)"
  fi
  if ! ls /sys/class/bluetooth/hci* > /dev/null 2>&1; then
    warn "no Bluetooth adapter: reboot (setup.md, Troubleshooting)"
  fi
  [ "$FAILS" = 0 ]
}

status() {
  [ -f .env ] || die "the stack isn't deployed yet: run ./percept-kit.sh deploy"
  docker compose ps -a --format 'table {{.Service}}\t{{.Status}}\t{{.Image}}'
  echo
  health 0
}

cmd=${1:-status}; shift || true
case $cmd in
  stage) stage ;;
  receive) receive ;;
  plan) plan ;;
  apply) apply ;;
  rebuild) rebuild "$@" ;;
  health) health "${1:-0}" ;;
  status) status ;;
  *) die "usage: stack.sh stage | receive | plan | apply | rebuild <service> | health [SECONDS] | status" ;;
esac
