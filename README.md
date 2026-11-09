# percept-kit

percept-kit turns an Azure Percept DK (APDK-101) into a standalone Ubuntu 22.04 device, in one
command:
- No Azure, no IoT Edge.
- The Vision (Eye) and Audio (Ear) SoMs work, as containers, and so does Bluetooth audio.
- A home page links everything: Grafana dashboards of the board, the containers and the Myriad X,
  the Eye's video in a browser, and a voice sample that answers from what the Eye sees.
- Microsoft's Mariner OS stays on the device as a rescue system, and the stock image can be
  restored bit for bit.

![The home page](docs/images/home.png)

## Quick start

In Git Bash on Windows, or bash on Linux or macOS:

```bash
git clone https://github.com/howardginsburg/AzurePerceptDKUbuntu.git && cd AzurePerceptDKUbuntu
./percept-kit.sh connect <device-ip> <user>   # SSH key and passwordless sudo
./percept-kit.sh install                      # backup, Ubuntu, every component: 1-2.5 hours
```

Then pair a Bluetooth speaker from the home page. [setup.md](setup.md) walks through it.

## What you get

| Component | Address | What it is | Details |
|---|---|---|---|
| Home page | `http://<ip>/` | Links to everything below, with live numbers | [dashboard.md](dashboard.md#home-page) |
| Grafana | `http://<ip>:3000/` | Dashboards of the board, the containers, the Myriad X and inference. No sign-in | [dashboard.md](dashboard.md#grafana) |
| Video | `http://<ip>:1984/` | The Eye's three video streams in a browser | [dashboard.md](dashboard.md#video) |
| Eye | `rtsp://<ip>:8554/result`, `http://<ip>:8555/` | Camera and Myriad X inference: video, and detections as JSON over HTTP and MQTT | [eye.md](eye.md) |
| Ear | `http://<ip>:8000/` | The four microphones: levels, live audio, and the Mute and PTT/PTS buttons | [ear.md](ear.md) |
| Mouth | `http://<ip>:8001/` | Sound on a Bluetooth speaker or the line-out, offline neural text-to-speech, and Bluetooth pairing (`#bt`) | [mouth.md](mouth.md) |
| Voice sample | `http://<ip>:8080/` | Spoken commands answered from what the Eye sees | [samples/voice/README.md](samples/voice/README.md) |
| MQTT broker | `mosquitto:1883`, for the containers only | Carries the Eye's inferences to the voice sample. One setting swaps in your own broker | [customize.md](customize.md#use-your-own-mqtt-broker) |
| Prometheus | `http://<ip>:9090/` | The metrics store | [dashboard.md](dashboard.md#metrics) |
| node_exporter, cAdvisor | `http://<ip>:9100/metrics`, internal | The board's and the containers' metrics | [dashboard.md](dashboard.md#metrics) |

## Documents

| Document | Contents |
|---|---|
| [setup.md](setup.md) | From a stock device to Ubuntu with everything running; day-to-day commands; and back |
| [customize.md](customize.md) | Changing things: the model, the MQTT broker, the voice sample, the speaker, the dashboards, your own containers, disk space |
| [eye.md](eye.md) | The Eye: camera and Myriad X inference, its outputs, models and metrics |
| [ear.md](ear.md) | The Ear: the 4-mic array and its buttons |
| [mouth.md](mouth.md) | The mouth: line-out and Bluetooth speaker, offline neural text-to-speech |
| [dashboard.md](dashboard.md) | The home page, Grafana, Prometheus and the video page |
| [samples/voice/README.md](samples/voice/README.md) | Sample: spoken commands answered from what the Eye sees |
| [PROVENANCE.md](PROVENANCE.md) | Where every component came from, how each was derived, the constraints it works within, and licences |
| [ASSESSMENT.md](ASSESSMENT.md) | The stock device: hardware, software, eMMC and boot chain |

The rest of this document explains how it works.

---

## 1. The constraint: Secure Boot you can't turn off

```
i.MX8MQ ROM (HAB fused closed)
  -> signed SPL / ATF -> Microsoft Project Mu UEFI     (firmware: can't be replaced)
  -> shim  bootaa64.efi   (trusts only "SantaCruz SecureBoot Production Signing")
  -> GRUB  grubaa64.efi   (signed; reads a plain-text grub.cfg from the ESP)
  -> kernel /boot/vmlinuz (must be SantaCruz-signed: shim_lock verifies it)
  -> root filesystem      (NOT verified)
```

- UEFI Secure Boot is on, with Microsoft *test* PK/KEK keys. `db` holds only the SantaCruz
  certificate, so the Microsoft UEFI CA that signs Ubuntu's shim is **not** trusted.
- There's no way to reach the UEFI setup to disable Secure Boot, and new firmware can't be
  flashed.

Some links in that chain are **not** checked:
- `grub.cfg` is not signature-checked.
- GRUB has `load_env`/`save_env`, `devicetree`, `ext2`, `search` and `usb` built in.
- The root filesystem is never verified.

**The approach** follows from that: keep the signed shim, GRUB and **Mariner 5.4 kernel**, and
point the kernel at an Ubuntu 22.04 root filesystem by editing `grub.cfg`. Ubuntu userland gets
normal `apt` updates. The kernel stays at `5.4.3-26.cm1` forever, which is why 22.04 (the newest
LTS that is comfortable on a 5.4 kernel) was chosen.

## 2. eMMC layout

The eMMC is 16 GB (`/dev/mmcblk0`), plus two 4 MB hardware boot partitions holding the signed
firmware (`mmcblk0boot0/1`). The kit reads those for the backup and **never writes them**.

| Partition | Stock | After `install` |
|---|---|---|
| p1 ESP (11 MB) | `EFI/BOOT/bootaa64.efi` (shim), `grubaa64.efi`, `boot/grub2/grub.cfg`, `pe100.dtb` | Same binaries. The kit's `grub.cfg` (original kept as `grub.cfg.orig`), `boot/grub2/grubenv`, `pe100-hdmi.dtb` |
| p2 ESP B (11 MB) | Unused A/B update slot | Unchanged |
| p3 (2 GB) | Mariner root (`/boot/vmlinuz` lives here) | Mariner root, switched to **rescue** mode (section 4) |
| p4 (2 GB) | Mariner root B (unused update slot) | Mariner's `/var`, ext4 label `pk-mariner-var` |
| p5 (10.5 GB) | Mariner `/var`: IoT Edge, Docker images, logs | **Ubuntu 22.04 root**, ext4 label `ubuntu-root` |

Both Ubuntu and Mariner boot the same kernel file, `/boot/vmlinuz` on p3. Ubuntu carries a copy
of the matching modules and firmware in its own root.

## 3. Boot menu and one-shot boots

`scripts/install-grub-cfg.sh` renders `scripts/grub.cfg.template` onto the ESP:

| Entry | Name | Root | Device tree |
|---|---|---|---|
| 0 | Mariner | `/dev/mmcblk0p3` (stock command line) | `pe100.dtb` (stock) |
| 1 | Ubuntu | `PARTUUID=<p5>` | `pe100-hdmi.dtb` |
| 2 | Ubuntu (original device tree, no HDMI) | `PARTUUID=<p5>` | `pe100.dtb` |

`pe100-hdmi.dtb` is Microsoft's `pe100.dtb` with the i.MX8MQ DCSS display controller and HDMI
nodes, which Microsoft had removed, put back. With it Ubuntu has a console on HDMI. Entry 2
exists in case a future change to that tree misbehaves. It isn't shipped with the kit:
`install-grub-cfg.sh` builds it **on each device from that device's own `pe100.dtb`** with
`scripts/build-hdmi-dtb.sh`, which inserts `scripts/hdmi-dcss-nodes.dtsi` and checks the result
against the tested SHA-256; see [PROVENANCE.md](PROVENANCE.md#3-the-hdmi-device-tree-pe100-hdmidtb).

**One-shot boots** are how every risky boot is tried. The kit writes `next_entry=N` into
`grubenv`. On the next boot GRUB:
1. reads `next_entry`;
2. clears it;
3. **only if saving the cleared value succeeds**, boots entry N.

If that boot hangs, the watchdog or a power cycle lands on the *default* entry, never on N again.
While `install` builds Ubuntu, the default stays Mariner (entry 0), with a 3 s timeout, and Ubuntu
is tried once. When that trial boot comes up, `install` makes Ubuntu (entry 1) the default.
`commit` changes the default later.

Other details:
- `grubenv` is written by the kit in pure bash as a 1024-byte block padded with `#`, the same
  format as `grub-editenv`, because Mariner has no `grub-editenv`.
- The rendered `grub.cfg` is syntax-checked with `grub-script-check` before it's moved into
  place atomically.
- `--next-only` refuses to touch a `grub.cfg` that isn't marked "Managed by percept-kit".
- The GRUB menu itself appears only on the (internal) serial console. On HDMI you see Linux
  from the kernel onwards.

## 4. Rescue Mariner

p5 is the only partition big enough for Ubuntu, and it holds Mariner's `/var`. Mariner can't
simply be deleted:
- It owns the signed kernel.
- It's the only OS that can rewrite the eMMC if Ubuntu breaks.

So `install` first converts Mariner to a **rescue layout** (`scripts/rescue-prep.sh`):

1. Saves `/etc/fstab` to `/etc/percept-kit/fstab.orig`.
2. Stops IoT Edge, Docker, containerd and the update agents. "Parks" their
   `multi-user.target.wants` links in `/etc/percept-kit/wants-disabled/`, so they don't start
   at boot. Moving the links (rather than `systemctl disable`) matters because some of
   Mariner's units are linked unit files that `disable` would delete.
   - `resizefs.service` is parked because it would grow and `resize2fs` p5.
   - `adu-agent`/`do-client-lite` are parked because they perform A/B OTA updates, which
     write p4 (the B root slot).
3. Formats p4 (`pk-mariner-var`) and copies `/var` to it, **without** Docker/containerd images,
   the journal, or `tmp`: about 60 MB.
4. Points `/var` in `/etc/fstab` at p4 and writes the marker `/etc/percept-kit/rescue`.

After a reboot, Mariner runs from p3 with `/var` on p4, and p5 is free. `/home` and
`/etc/sudoers.d` live under `/var/custom-configs` on Mariner, so the user, SSH key and Wi-Fi
settings come along. `scripts/restore-stock-p3.sh` undoes exactly these p3 changes.

## 5. The Ubuntu root filesystem

`scripts/build-ubuntu-rootfs.sh` builds Ubuntu **on the device**, from rescue Mariner, straight
onto p5. It can also target a USB disk for experiments.

| Step | What and why |
|---|---|
| Base | Official `ubuntu-base-22.04.5-base-arm64.tar.gz` from cdimage.ubuntu.com, checked against Ubuntu's `SHA256SUMS`, unpacked and completed in a chroot |
| Packages | `ubuntu-minimal`, systemd-networkd/resolved/timesyncd, `openssh-server`, `wpasupplicant`, `bluez`, `docker.io` with `docker-compose-v2` and `docker-buildx`, `tpm2-tools`, `device-tree-compiler` (builds `pe100-hdmi.dtb`), `usbutils`, `rsync`, admin tools, then `dist-upgrade` |
| No snaps | `snapd` is pinned to -1: the Mariner kernel has no AppArmor, so snap confinement can't work |
| Kernel | `/lib/modules/5.4.3-26.cm1`, `/lib/firmware`, `vmlinuz`, `config` and `System.map` copied from Mariner, then `depmod`. Mariner's Realtek `88x2ce` Wi-Fi module options are copied too, plus `rtw_drv_log_level=2`: at its default level the driver logs every Wi-Fi power-save change, about 10,000 journal lines an hour |
| iptables | Legacy backend (`iptables-legacy`): the kernel has no nf_tables |
| Docker | `DOCKER_INSECURE_NO_IPTABLES_RAW=1` (no `raw` iptables table in this kernel); classic overlay2 store, because the containerd snapshotter roughly triples disk use; `local` log driver, so logs are size-capped |
| Journal | Capped at 200 MB (`/etc/systemd/journald.conf.d/percept-kit.conf`) |
| Network | systemd-networkd with DHCP on `wlan0` (via `wpa_supplicant@wlan0`, using Mariner's `wpa_supplicant-wlan0.conf`) and on any Ethernet port. Ethernet is not required for "online" |
| Identity | Your Mariner user is created with the same password hash and `authorized_keys`, in groups `sudo docker video plugdev apdk_accessories tss`. The Mariner SSH host keys are copied, so `known_hosts` still matches. Hostname defaults to `percept-ubuntu` |
| Devices | Mariner's accessory udev rules (Eye/Ear SoMs, group `apdk_accessories`, gid 5000) and TPM rules |
| Ear fix | The Ear SoM re-enumerates late in boot and `snd-usb-audio` can miss it. `81-percept-ear.rules` triggers `percept-ear-audio.service`, which reloads the driver if no "Azure Ear SoM" sound card appeared |
| Watchdog | systemd drives `imx2_wdt` (`RuntimeWatchdogSec=30`), so a hung Ubuntu reboots, back to the default entry if it was a trial boot |
| Kit | `/etc/sudoers.d/90-percept-kit` is copied so the kit keeps working on Ubuntu |

The OOBE hotspot (`apd-xxxx`), IoT Edge and the Azure telemetry daemons are not installed on
Ubuntu. The containers come next, from `deploy` (section 8).

## 6. Device states

`scripts/kit/state.sh` inspects the device read-only and reports one of these states. Every
command checks the state before acting.

| State | Meaning | Valid commands |
|---|---|---|
| `stock` | Mariner with `/var` on p5, ESP as shipped | `assess`, `backup`, `install`, `restore` (finishes a restore by rewriting p4) |
| `rescue` | Mariner with `/var` on p4 (`/etc/percept-kit/rescue` present) | `install` (carries on, or rebuilds p5), `commit`, `boot`, `restore` |
| `ubuntu` | Ubuntu running from p5 | `deploy` and the other stack commands (section 8), `install` (finishes the remaining steps), `commit`, `boot`, `restore` |
| `usb-ubuntu` | Ubuntu running from a USB disk (development) | `deploy` and the other stack commands, `assess` |

## 7. The PC side

`percept-kit.sh` runs on the PC (Git Bash on Windows, or bash on Linux/macOS) and drives the
device over SSH.

- **Config**: `.percept-kit.conf` holds `HOST`, `DEVUSER` and `KEY`
  (default `~/.ssh/id_ed25519_percept`). `connect` creates the key if needed, installs it with
  one password login, and adds `/etc/sudoers.d/90-percept-kit` (validated with `visudo`) for
  passwordless sudo.
- **Device scripts**: before each mutating command the kit uploads `scripts/` to
  `~/percept-kit/scripts` on the device, stripping Windows line endings. Read-only helpers
  (`state.sh`, `assess.sh`) are piped over SSH without uploading.
- **Long jobs**: rescue prep, the Ubuntu build, GRUB install, `deploy` and `rebuild` run as
  transient systemd units (`systemd-run --unit=pk-<name>`). They write `/var/tmp/pk-<name>.log`
  and `.rc`. The kit polls the log every 5 s and streams new lines, so a dropped Wi-Fi/SSH
  connection doesn't kill the job: the kit reconnects and carries on.
- **Reboots**: the reboot is scheduled 2 s ahead through systemd. The kit then polls every
  15 s, for up to 15 minutes, until the device reports a new `boot_id`, and re-reads the state.
- **Mariner's SSH rate limit**: stock and rescue Mariner have this firewall rule in the
  `mangle` table:

  ```
  -m recent --update --seconds 60 --hitcount 6 --name DEFAULT -j SSHBRUTEFORCE   (drop)
  ```

  A source IP that opens 6 new SSH connections within 60 s is dropped, and every dropped retry
  restarts the 60 s, so constant probing never gets back in. Windows OpenSSH can't reuse a
  connection (no ControlMaster), and the kit opens many short connections. So whenever it reads
  the device state it also runs `scripts/kit/ssh-exempt.sh`, which inserts a **runtime-only**
  `ACCEPT` for the PC's IP ahead of that rule. The exemption disappears at the next reboot. The
  post-reboot polling interval keeps the kit under the limit until the exemption is re-added.

## 8. The containers: one Compose project

Everything the device runs beyond the OS is one Docker Compose project, `percept`, defined in
[compose.yaml](compose.yaml) and kept in `~/percept-kit` on the device.

| Service | Image | Ports | Notes |
|---|---|---|---|
| `eye` | `percept/eye`, built on the device | 8554 (RTSP), 8555 (HTTP) | The USB bus is mounted, because the Myriad X re-enumerates ([eye.md](eye.md#how-it-runs)) |
| `ear` | `percept/ear`, built on the device | 8000 | `/dev/snd`; `/dev/input` read-only, for the buttons |
| `mouth` | `percept/mouth`, built on the device | 8001 | `/dev/snd`, and the host's D-Bus for Bluetooth |
| `voice` | `percept/sample-voice`, built on the device | 8080 | |
| `mosquitto` | Eclipse Mosquitto | none | Only while `MQTT_URL` is empty (profile `local-mqtt`) |
| `homepage`, `grafana`, `prometheus`, `node-exporter`, `cadvisor`, `go2rtc` | The projects' own images | 80, 3000, 9090, 9100, none, 1984 and 8556 | [dashboard.md](dashboard.md#how-it-works) |

- Image versions are pinned in `compose.yaml` and nowhere else. The four images built on the
  device never leave it.
- Every container restarts by itself (`unless-stopped`) and logs to Docker's `local` driver,
  capped at three files of 5 MB.
- Container names are the service names. The containers share the network `percept` and reach
  each other by name (`mosquitto:1883`, `eye:8555`).
- The kit creates the network and the data volumes (`mouth-cache`, `voice-data`,
  `prometheus-data`, `grafana-data`) outside the project, so `docker compose down -v` can't delete
  them.

**Settings.** On the PC, `config/percept.env` holds every setting, documented in
[config.example/percept.env](config.example/percept.env). `config/files/` holds the files settings
refer to, and `config/compose.override.yaml` any change to the services. On the device:
- `deploy` writes `~/percept-kit/.env`: the device's facts (the address the PC uses, the names
  Homepage must accept, the `audio` and `input` group IDs), `COMPOSE_PROFILES`, `COMPOSE_FILE`
  (with the override, when there is one), then `config/percept.env`. Plain `docker compose` in
  `~/percept-kit` therefore needs no flags.
- Each module gets only its own settings (`EYE_*`, `EAR_*`, `MOUTH_*`, `VOICE_*`), from an env
  file `deploy` writes to `.env.d/`, so changing a setting recreates only its module.
  `config/files/` is mounted at `/config/files`, read-only.
- `compose.yaml` maps the shared `MQTT_*` settings onto the Eye's and the voice sample's own.

**`deploy`** runs `scripts/stack.sh` on the device, as root:
1. **Upload.** The PC sends the SHA-256 hash of each of its files. The device stages the files it
   already has, as hard links, and asks for the rest, so an unchanged 65 MB model isn't sent again.
   Windows line endings are removed from the text files received.
2. **Check.** It writes the new `.env` and loads the configuration. If Compose rejects it, `deploy`
   stops there and nothing has changed.
3. **Host setup.** It installs anything missing (`docker-compose-v2`, `docker-buildx`, `rsync`),
   applies the journal cap and the Wi-Fi driver's log level, and sets up BlueALSA with
   `modules/mouth/bt.sh` ([mouth.md](mouth.md#how-it-works)).
4. **Files.** `rsync --inplace` rewrites each changed file where it is, so a container that mounts
   a folder sees the new content.
5. **Build.** Each image built on the device carries a label with the hash of its source folder
   (documentation aside). Only images whose folder changed are rebuilt, one at a time, each after
   a check that there's room. The Eye's build needs its base image in Docker; the kit loads it from
   the backup, or pulls it, beforehand.
6. **Pull** missing images, after a check that there's room to unpack them.
7. **Start.** Services switched off by a profile are removed. `docker compose up -d` creates or
   recreates what changed, and services whose mounted files changed are restarted.
8. **Tidy.** The build cache, dangling images, anonymous volumes and replaced image versions are
   deleted.
9. **Check.** `stack.sh health` checks every component over HTTP (MQTT for the broker), retrying
   for up to 5 minutes, and prints each one's address. It warns, rather than fails, when an SoM or
   the Bluetooth adapter is missing.

`install` runs `deploy` once Ubuntu is the default, then reboots and checks again, so the device is
known to come back by itself. The kit's `status`, `logs`, `restart`, `stop`, `start`, `rebuild` and
`compose` commands run `docker compose` on the device
([customize.md](customize.md#use-docker-compose-directly)).

## 9. Backups

`backup` (from stock Mariner) writes `backups/<yyyymmdd-hhmmss>-<last 8 of eMMC CID>/`:

| File | Contents |
|---|---|
| `kit-backup.conf` | CID, state, model, OS, hostname, timestamp. `install`/`restore` only use a backup whose CID matches the device |
| `assess.txt` | The `assess` report at backup time |
| `gpt-primary.bin`, `gpt-backup.bin` | Raw GPT headers and partition tables (first 34 / last 33 sectors) |
| `custom-configs.tar.gz` | Mariner's `/var/custom-configs`: users, SSH, Wi-Fi, sudoers, IoT Edge config |
| `boot0`, `boot1`, `p1`...`p5` `.img.zst` | Every partition, raw `dd` image compressed with zstd on the device |
| `p5-consistent.img.zst` | Added by `install`: p5 imaged from rescue Mariner while unmounted |
| `eye-base.tar.zst` | `docker save` of the Eye's build base (`azureeyemodule:2301-1-noauth`) from the device's own IoT Edge cache, about 420 MB. An Eye build loads it when Docker doesn't have the image, before trying MCR. `eye-base save` adds the device's copy to an existing backup |
| `SHA256SUMS` | Hashes of the files above, computed on the PC after transfer and matched against the device's |
| `RAW-SHA256SUMS` | Hashes of the uncompressed partitions, used to verify restores |

During the backup, IoT Edge and Docker are stopped so p5 is as quiet as a live copy can be. p3
and p5 are still mounted, so their images are only crash-consistent. That's why `install` also
saves `p5-consistent`, and why `restore` never writes p3 from an image. The folder is named
`.partial` until every file is written and verified. A backup is about 4 GB (7 GB once `install`
has added `p5-consistent`) and takes about 20 minutes over Wi-Fi.

## 10. Restore

`restore` walks the device back to stock:

1. **From Ubuntu**: one-shot boot into rescue Mariner (entry 0).
2. **From rescue**:
   - write p5 (`p5-consistent` if present, else `p5`) and p1 (stock ESP, which removes the kit's
     menu);
   - run `restore-stock-p3.sh` (original fstab, un-park services, delete `/etc/percept-kit`);
   - reboot into stock Mariner.
3. **From stock with p4 still labelled `pk-mariner-var`**: write p4 back.

`scripts/kit/write.sh` streams each image, then re-reads the partition and compares its SHA-256
with `RAW-SHA256SUMS`. It refuses to:
- write a mounted partition;
- write p5 outside rescue, or p4 unless `/var` is on p5;
- write anything other than p1–p5.

## 11. Limits and known issues

- The kernel never gets security updates. Treat the device as a lab/edge appliance, not an
  internet-facing server.
- No Ubuntu kernels, initramfs, snaps or AppArmor-dependent software.
- There's no USB-C reflash path any more (Microsoft retired it). Never modify
  `bootaa64.efi`/`grubaa64.efi` or `mmcblk0boot0/1`. The kit doesn't.
- On stock Mariner the OOBE hotspot (`apd-xxxx`) shares the Wi-Fi radio and stays on.
- Ubuntu's DHCP client (systemd-networkd, DUID-based client ID) can be handed a different IP
  address than Mariner was. See [setup.md](setup.md#if-the-ip-address-changes).
- The Mariner kernel crashes in its Bluetooth LE scanning code when discovery stops. The kit
  sets BlueZ to Classic-only (`ControllerMode = bredr`), which is all A2DP speakers need. See
  [mouth.md](mouth.md#how-it-works).
- Occasionally the second USB controller fails to power up at boot (`imx-pgc … failed to command
  PGC`), and the Ear and Bluetooth are missing until the next reboot. See
  [setup.md](setup.md#the-ear-or-bluetooth-is-missing-after-a-reboot).

## 12. What this repository does and doesn't contain

Everything that can't be redistributed comes from **the user's own Percept DK** while the kit
runs. Nothing from a device is ever committed.

| Needed | Where it comes from |
|---|---|
| Signed shim, GRUB, Mariner kernel | Stay on the device's ESP and p3; never copied off |
| Kernel modules, firmware, Wi-Fi driver options, accessory udev rules | Copied from the device's Mariner into Ubuntu during `install` |
| Wi-Fi settings, user, password hash, SSH keys | Copied from the device's Mariner into Ubuntu during `install` |
| `pe100-hdmi.dtb` | Built on the device from its own ESP `pe100.dtb` + `scripts/hdmi-dcss-nodes.dtsi` (section 3) |
| Ubuntu | Downloaded from Ubuntu, checked against `SHA256SUMS` |
| Eye base image (Myriad X stack, OpenVINO, toolchain) | Loaded from the device's own backup (section 9) before the Eye is built, or pulled from `mcr.microsoft.com` if the backup has no copy |
| Paho MQTT C, cpp-httplib | Downloaded at build time, pinned by SHA-256 |
| `percept/eye` image | Built on the device; never pushed to a registry |
| `percept/ear`, `percept/mouth`, `percept/sample-voice` images | Built on the device from Alpine/Ubuntu packages; the mouth's Piper wheels and voice, and the voice sample's Vosk wheel and speech model, are downloaded at build time, pinned by SHA-256 |
| Mosquitto and the dashboard's images (Homepage, Grafana, Prometheus, node_exporter, cAdvisor, go2rtc) | Pulled on the device from their projects' registries by `deploy`, at the versions pinned in `compose.yaml` |
| BlueALSA (Bluetooth audio on the host) | Installed from Ubuntu by `deploy` |

`.gitignore` keeps out `backups/`, `backup/`, `reports/`, `.percept-kit.conf`, your `config/`, any
other `*.env` file, every `*.dtb`, and the decompiled Microsoft trees in `dtb/`. The licence of each
component is in [PROVENANCE.md](PROVENANCE.md#12-licences-and-what-must-not-be-shared).

## 13. Files

```
percept-kit.sh                 PC-side orchestrator (all commands)
.percept-kit.conf              device address/user/key (written by connect; not committed)
compose.yaml                   the device's containers, how they run, and every image version, section 8
config.example/                percept.env (every setting, documented) and files/; copy it to config/
config/                        yours: percept.env, files/, compose.override.yaml (not committed); see customize.md
scripts/
  build-ubuntu-rootfs.sh       Ubuntu 22.04 root on p5 (or a USB disk), section 5
  install-grub-cfg.sh          ESP grub.cfg + grubenv + HDMI dtb; default and one-shot entries, section 3
  grub.cfg.template            the three-entry menu
  build-hdmi-dtb.sh            pe100.dtb + hdmi-dcss-nodes.dtsi -> pe100-hdmi.dtb (on the device)
  hdmi-dcss-nodes.dtsi         the DCSS + HDMI nodes added to Microsoft's device tree
  rescue-prep.sh               stock -> rescue Mariner, section 4
  restore-stock-p3.sh          rescue -> stock Mariner (p3 side)
  stack.sh                     deploy, rebuild, health and status on the device, section 8
  host/                        Ear audio udev rule + service installed into Ubuntu
  kit/state.sh                 device state report, section 6
  kit/assess.sh                hardware/software report
  kit/dump.sh, kit/write.sh    partition image out/in with hashes, sections 9-10
  kit/ssh-exempt.sh            runtime SSH rate-limit exemption, section 7
modules/eye/                   the Eye's image: Dockerfile, collect-rootfs.sh, entrypoint.sh, app/ (Microsoft's app); eye.md
  eye/app/output/              inference outputs (stdout / HTTP / MQTT) and Prometheus metrics, added to Microsoft's app
modules/ear/                   the Ear's image (ear.py); ear.md
modules/mouth/                 the audio output image (mouth.py) + bt.sh (host Bluetooth setup, pairing); mouth.md
samples/voice/                 voice + vision sample: voice.py, Dockerfile, requirements.txt (pinned)
services/mqtt/                 mosquitto.conf, the device's MQTT broker
services/dashboard/            prometheus/ (scrape config, recording rules), grafana/ (provisioning, percept-dk.json),
                               homepage/ (home page cards and bookmarks), go2rtc/ (the Eye's streams for browsers)
docs/images/                   the screenshots in these documents
dtb/                           NXP reference sources (imx8mq.dtsi, imx8mq-evk.dts, imx8mq-clock.h);
                               locally also pe100.dtb and its decompiled/derived files (not committed)
backups/, reports/             created by the kit (not committed)
```
