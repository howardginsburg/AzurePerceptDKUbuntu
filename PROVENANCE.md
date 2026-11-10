# Provenance: how each part of the board keeps working

The Azure Percept DK was built for Azure: Microsoft's own OS, Azure IoT Edge modules, and cloud
services. This document goes through the board's features one at a time:
- what stood in the way of each on a standalone Ubuntu device;
- what percept-kit does about it, and why that way;
- which files do it.

It ends with where every component comes from, and the licences. How the pieces fit together is
in [ARCHITECTURE.md](ARCHITECTURE.md).

Every piece of percept-kit is one of three kinds:

| Kind | Meaning |
|---|---|
| **From the device** | Copied from the Percept DK's own Mariner OS (Microsoft binaries). It never leaves the device, except inside *your* backups |
| **Downloaded** | Fetched from a public upstream at a pinned version, and checked by SHA-256 or by the upstream's signed checksums |
| **Authored** | Written for this project |

## Contents

- [Booting Ubuntu on a locked board](#booting-ubuntu-on-a-locked-board)
- [Making room: rescue Mariner](#making-room-rescue-mariner)
- [Ubuntu on Mariner's kernel](#ubuntu-on-mariners-kernel)
- [Wi-Fi, network and your login](#wi-fi-network-and-your-login)
- [HDMI: the console and the camera](#hdmi-the-console-and-the-camera)
- [Vision: the Eye SoM](#vision-the-eye-som)
- [Microphones and buttons: the Ear SoM](#microphones-and-buttons-the-ear-som)
- [Sound out: the mouth and Bluetooth](#sound-out-the-mouth-and-bluetooth)
- [Voice commands: the sample](#voice-commands-the-sample)
- [Dashboards and video in a browser](#dashboards-and-video-in-a-browser)
- [Device Settings](#device-settings)
- [Driving the device from a PC](#driving-the-device-from-a-pc)
- [Where every component comes from](#where-every-component-comes-from)
- [Licences and what must not be shared](#licences-and-what-must-not-be-shared)
- [Verify it yourself](#verify-it-yourself)

---

## Booting Ubuntu on a locked board

**Files**: `scripts/grub.cfg.template`, `scripts/install-grub-cfg.sh`. Authored.

**In the way.** The assessment ([ASSESSMENT.md](ASSESSMENT.md)) read the boot configuration from
the running Mariner:

| Finding | How it was read |
|---|---|
| i.MX High Assurance Boot is closed: only signed SPL, ATF and UEFI can run | OCOTP fuse Bank 1 Word 3 bit 25; the firmware's `fw-version` reports `uboot-secure 1`, `uefi-secure 1` |
| UEFI Secure Boot is on, not in setup mode | EFI variables `SecureBoot=1`, `SetupMode=0` |
| `db` trusts only *SantaCruz SecureBoot Production Signing* | Certificates parsed out of the `db`, `PK` and `KEK` EFI variables |
| Boot chain: Project Mu UEFI → shim → GRUB 2.06 → `/boot/vmlinuz` on p3, no initrd | The ESP's contents, the GRUB binary's embedded prefix and module list, `/proc/cmdline` |

Ubuntu's own shim, GRUB and kernel are signed by the Microsoft UEFI CA, which `db` doesn't trust,
and there's no screen or serial port to reach UEFI setup and turn Secure Boot off.

**The opening.** `grub.cfg` is plain text that nothing checks. GRUB has `load_env`, `save_env`,
`devicetree`, `search` and `ext2` built in. Only the kernel is verified (through shim's
`shim_lock`), and the root filesystem never is.

**What percept-kit does.** Microsoft's signed shim, GRUB and kernel stay as they are; only
`grub.cfg` changes. Its first entry, Mariner, copies the stock file's `linux` and `devicetree`
lines exactly, so Mariner boots as before. The two Ubuntu entries boot the same kernel with these
changes to Mariner's command line:

| Parameter | Why |
|---|---|
| `root=PARTUUID=<p5>` | Stable whatever the order of USB and eMMC devices. Filled in from `blkid` |
| `rootwait` | Wait for the root device rather than panic |
| `net.ifnames=0` | Keep the names `wlan0` and `eth0`, which Mariner's Wi-Fi configuration uses |
| `systemd.unified_cgroup_hierarchy=0` | cgroup v1: see [Ubuntu on Mariner's kernel](#ubuntu-on-mariners-kernel) |
| `panic=10` | Reboot after a panic, so a bad trial boot falls back to the default entry |
| `console=ttymxc0,115200 console=tty0` | Kernel messages on the internal serial port and on HDMI |
| `ima_policy=tcb` dropped | Mariner's IMA measurement policy has no use on Ubuntu |

**Never a boot you can't get back from:**
- Every new entry is first tried as a **one-shot boot**. GRUB's standard `next_entry` pattern
  would retry a hanging entry on every boot if clearing the variable failed, so the template boots
  `next_entry` only if saving the cleared value succeeded. `load_env` reads only `next_entry`, so
  nothing else in `grubenv` can change the menu.
- The rendered file is checked with `grub-script-check` and renamed into place; the original stays
  as `grub.cfg.orig`.
- Mariner has no `grub2-editenv`, so the kit writes `grubenv` itself: exactly 1024 bytes,
  `key=value` lines padded with `#`.
- The kit's scripts live in `~/percept-kit/scripts`, not `/tmp`: `/tmp` is a tmpfs, and a one-shot
  boot armed from it wouldn't survive the reboot.

## Making room: rescue Mariner

**Files**: `scripts/rescue-prep.sh`, `scripts/restore-stock-p3.sh`. Authored.

**In the way.** Only p5, Mariner's 10.5 GB `/var`, is big enough for Ubuntu and its container
images. Mariner can't simply be deleted: p3 holds the only kernel the board will boot, and Mariner
is the only OS left to repair Ubuntu or restore the eMMC.

**What percept-kit does.** Mariner's `/var` moves to p4, the unused second root slot: about 60 MB
once the container images, the journal and `tmp` are left out (measured with `du`). These services
are kept from starting:

| Unit | Why |
|---|---|
| `iotedge*`, `docker-watcher*` | They (re)start IoT Edge and Docker |
| `preloadedimgs` | Re-imports container images into Docker |
| `do-client-lite`, `adu-agent` | Delivery Optimization and Device Update. An A/B update would write the second root slot, now `/var` |
| `containerd` | Not needed in rescue mode |
| `resizefs` | Runs `resize2fs` on p5, which becomes Ubuntu's |

- Their `multi-user.target.wants` links are moved to `/etc/percept-kit/wants-disabled/` rather than
  disabled: several are links to unit files elsewhere, which `systemctl disable` would delete and
  `enable` couldn't recreate identically. Moving them back is exact.
- `/etc/fstab` is saved to `/etc/percept-kit/fstab.orig`, its `/var` line pointed at p4, and a
  marker file records the rescue state. The copy on p4 is checked before `fstab` changes.
- On Mariner, `/home` and `/etc/sudoers.d` live under `/var/custom-configs`, so your user, SSH key
  and Wi-Fi settings come along.

`restore-stock-p3.sh` undoes exactly these changes.

## Ubuntu on Mariner's kernel

**File**: `scripts/build-ubuntu-rootfs.sh`. Authored. It runs on the device, from rescue Mariner,
and writes straight to p5 (or to a USB disk, for experiments).

**What percept-kit does.**
- **Ubuntu 22.04**: the newest LTS whose systemd, Docker and glibc are happy on a 5.4 kernel that
  will never be updated.
- **The base** is Ubuntu's official `ubuntu-base` tarball, the one Ubuntu's own container images
  are built from, checked against Ubuntu's `SHA256SUMS`. It's completed in a chroot with
  `ubuntu-minimal` and the packages listed in the script.
- **From Mariner**: the kernel's modules and firmware (Wi-Fi, Bluetooth, GPU, VPU drivers), the
  accessory udev rules, and your Wi-Fi network, user and SSH identity
  ([Where every component comes from](#where-every-component-comes-from)).
- **The same user and group IDs as Mariner.** Your account keeps uid 10000, the one Mariner's setup
  wizard gave it, so your files keep their owner on both systems. The accessory group
  `apdk_accessories` keeps gid 5000, which the Eye image's user depends on.
- **Left out**: the setup hotspot, IoT Edge and the Azure telemetry daemons.

**What the kernel doesn't have.** Each problem was found by booting a USB build and reading
`journalctl`, Docker's errors, or the kernel's `config`:

| Symptom | Cause | Fix |
|---|---|---|
| Docker fails to start: iptables errors | No `nf_tables`; Ubuntu 22.04's `iptables` defaults to it | The legacy `iptables` backend |
| Docker 28+ fails to create the `raw` table | No `CONFIG_IP_NF_RAW` | `DOCKER_INSECURE_NO_IPTABLES_RAW=1`, Docker's documented switch |
| Containers can't be given devices under cgroup v2 | No `CONFIG_BPF_SYSCALL`, which cgroup v2 device control needs | cgroup v1, on the kernel command line |
| Snaps can't run | No AppArmor | `snapd` pinned to priority -1, so nothing pulls it in |
| Boot stalls 2 minutes at `systemd-networkd-wait-online` | Unplugged Ethernet ports count as required | `RequiredForOnline=no` for Ethernet |
| Images take about 3× their size on disk | The containerd image store keeps the compressed layers as well as the unpacked ones | Docker's classic overlay2 store, and the size-capped `local` log driver |

Also:
- systemd drives the hardware watchdog (`RuntimeWatchdogSec=30`), so a hung Ubuntu reboots, back
  to the default entry if it was a trial boot.
- The journal is capped at 200 MB, because the eMMC is small.

## Wi-Fi, network and your login

**Files**: `scripts/build-ubuntu-rootfs.sh`, `scripts/stack.sh` (host setup),
`services/settings/server.py` (network changes). Authored.

**On the stock Percept**, the setup experience joined your Wi-Fi network from its own hotspot,
and the Wi-Fi chip (Realtek RTL8822CE) uses an out-of-tree driver, `88x2ce`.

**What percept-kit does.**
- The driver and its load options come from Mariner, with your network's
  `wpa_supplicant-wlan0.conf`. systemd-networkd gets an address by DHCP on `wlan0` and on any
  Ethernet port.
- At its default log level the driver logs every Wi-Fi power-save change: about 10,000 journal
  lines an hour, 800 MB in three days. It's set to errors only (`rtw_drv_log_level=2`), at boot and,
  through sysfs, straight away.
- Your user, password hash, `authorized_keys` and the device's SSH host keys are copied, so you log
  in as before and `known_hosts` still matches.
- Ubuntu's DHCP client identifies itself differently from Mariner's (a DUID-based client ID), so
  the router can hand Ubuntu a different address
  ([setup.md](setup.md#if-the-ip-address-changes)).
- **Changing the network from Device Settings** must not cut off a device with no screen. After a
  change, the device pings its gateway; if that doesn't answer within about a minute, the previous
  files are put back. A change cut short by a crash or a reboot is undone when the service starts.

## HDMI: the console and the camera

### The display

**Files**: `scripts/hdmi-dcss-nodes.dtsi`, `scripts/build-hdmi-dtb.sh`; NXP's reference sources in
`dtb/`. Authored, from NXP's device tree.

**On the stock Percept** the HDMI port shows nothing. Three findings showed that only the device
tree was missing:
1. **The kernel has the drivers built in**: its `config` has `DRM_IMX_DCSS=y`,
   `DRM_IMX_CDNS_MHDP=y`, `DRM_CDNS_HDMI=y`, `FB=y` and `FRAMEBUFFER_CONSOLE=y`.
2. **The firmware loads the HDMI controller's own signed firmware**: it's in the eMMC's hardware
   boot partition.
3. **The display nodes were deleted from Microsoft's tree.** `pe100.dtb` decompiles to NXP's
   i.MX8MQ EVK tree without its `hdmi` and `dcss` nodes. Its phandles run from 0x01 to 0x5f with
   exactly two gaps, 0x43 and 0x45, either side of the interrupt controller those nodes sit next to
   in NXP's order. So the tree was compiled with them and they were removed afterwards.

**What percept-kit does.** `pe100-hdmi.dtb` is Microsoft's `pe100.dtb` plus two nodes, the DCSS
display controller and the HDMI transmitter, and nothing else:
- Their values come from NXP's reference tree for the same SoC and the kernel's exact BSP version
  (`linux-imx`, tag `imx_5.4.3_2.0.0`). A decompiled tree has no labels or macros, so every clock
  ID and phandle was resolved to its number. The result, with comments, is
  `scripts/hdmi-dcss-nodes.dtsi`.
- Microsoft published no licence for `pe100.dtb`, so the kit never ships it or anything built from
  it. `build-hdmi-dtb.sh` builds `pe100-hdmi.dtb` on each device from that device's own `pe100.dtb`.
  It checks that the insertion point exists once and the phandles are the expected ones, and fails
  unless the tested input gives the tested output, byte for byte.
- GRUB entry 2 boots with the untouched tree, in case the HDMI one ever misbehaves.

**Tested**: the display controller binds (after one deferred probe), HDMI reports 1920×1080, the
console appears, and a USB keyboard logs in. Two versions of `dtc` (1.6.1 and 1.7.2) built
byte-identical files. Not added: HDMI audio.

### The camera on HDMI

**Files**: `services/settings/server.py` (`camera()`), `scripts/host/percept-camera.service`,
`scripts/stack.sh` (`apply_display`). Authored.

**The goal**: show the Eye's video on the monitor without a desktop, which with a browser would
need far more memory than the board has to spare with the demo running.

**What percept-kit does.** mpv draws straight to the display (DRM), on virtual terminal 7, under
`percept-camera.service`. Its supervisor waits for a monitor and for the Eye's stream, plays again
when the stream ends (the Eye restarted), and on a video output error gives the monitor back to the
console and shows the reason on the Device Settings page, rather than retrying for ever.

**In the way: display memory.** mpv first stopped with `Cannot create dumb buffer: Cannot allocate
memory`. Diagnosis, on the device:
- The display driver takes frame buffers from the kernel's contiguous memory (CMA): 320 MB.
- The Vivante GPU driver (`galcore`) reserves 256 MB of it at boot (its `contiguousSize`
  parameter), though nothing uses the GPU.
- The page cache filled most of the rest: 2 MB was free.
- Allocating frame buffers directly through DRM showed that 1920×1080 never fit, and 1280×720 got
  at most one of the two mpv needs. After the clean page cache was dropped and memory compacted,
  about 22 MB came free, and 1280×720 got both every time; 1920×1080 still got only one.

So the supervisor drops the page cache before it starts mpv, mpv keeps two frame buffers instead of
four, the output modes stop at 1280×720, and a video output error is retried twice before the
player gives up. The video is 816×616, and the monitor scales 720p to fill its screen.

**Cost**, measured with the demo running: mpv about 62% of one core and 62 MB of memory, and the
Eye about a fifth of a core more, to serve the extra stream.

**Not done**: freeing the GPU driver's reservation, which would allow 1080p. It would take a kernel
command-line change, `galcore.contiguousSize`, which is a change to the boot chain.

## Vision: the Eye SoM

**Files**: `modules/eye/` (`app/` is Microsoft's; `Dockerfile`, `collect-rootfs.sh`, `entrypoint.sh`
and `app/output/` are authored).

**On the stock Percept**, Microsoft's Eye module (`azureeyemodule`) ran under IoT Edge. It drove
the camera and the Myriad X, and sent every inference to Azure IoT Hub.

**In the way.**
- Without IoT Edge and IoT Hub, the results had nowhere to go.
- The Myriad X software stack exists only inside Microsoft's image, in the versions the app was
  built against: Intel's OpenCV G-API build with its Myriad X backend, `mxIf` and the Myriad
  firmware, OpenVINO 2021, onnxruntime, and a matching GStreamer.
- Microsoft's image is a 2.6 GB debug build (1.23 GB on disk), and its registry may stop serving it.

### The same app, from Microsoft's source

Almost all of the code (about 10,000 lines of C++ and 65 MB of model data) is Microsoft's
open-source Eye module, the program inside `azureeyemodule`:

| | |
|---|---|
| Repository | [microsoft/azure-percept-advanced-development](https://github.com/microsoft/azure-percept-advanced-development), archived read-only |
| Path | `azureeyemodule/app` |
| Commit | `b6782ded19c342e53a02b958fe38053d43e74507` (HEAD of `main`) |
| Last change to the app | "Add no-auth patch (#83)", 2023-01-20. This matches the `2301-…-noauth` image the device shipped with |
| Licence | MIT. Copied unchanged to `modules/eye/LICENSE.txt`, with the third-party `NOTICE.txt` |

The GitHub source is used, not the code in Microsoft's image, because the image holds only part of
it: no `main.cpp`, no `CMakeLists.txt`.

Our changes are in five files, +946 / −1 lines; everything else is byte-identical to upstream:

| File | Change |
|---|---|
| `output/output.hpp`, `output/output.cpp` | **New**: the HTTP, MQTT and stdout outputs, and the Prometheus metrics with the Myriad X telemetry |
| `main.cpp` | Starts the outputs just before the IoT client |
| `iot/iot_interface.cpp` | `send_message()` also hands every message to the outputs |
| `CMakeLists.txt` | Builds `output/` and links the two libraries below |

### Building it on the device

- **The build stage starts from Microsoft's image**, which holds the whole toolchain and Myriad X
  stack. Its cmake options are upstream's `Dockerfile.arm64v8`'s, plus Microsoft's no-auth switch
  (`AUTH_FIX`, so the app never waits for Azure device authentication) and a release build.
- **The final stage starts from nothing.** `collect-rootfs.sh` copies only the binary, its models,
  every library its files need, the Myriad firmware, OpenVINO (to compile `.xml` models at run
  time), the GStreamer plugins and a few tools the app calls. The image is about 240 MB.
- It runs as the same user and group as Microsoft's image (5001:5000), matching the device's udev
  rules.
- **Microsoft's image must stay available.** Every stock device has it in its IoT Edge cache, so
  `backup` saves the device's own copy. An Eye build uses Docker's copy, else your backup's, else
  Microsoft's registry. Loaded from a backup with the registry blocked, it had the identical image
  ID.

### Local outputs instead of IoT Hub

Upstream sends every result through one function, `send_message()`. Hooking it publishes every
message the app produces, in exactly the format IoT Hub would have received, with no change to any
model's parser. Because IoT Hub accepts plain MQTT, the same output can still send to it, without an
Azure SDK; the Eye image's system certificates include Azure's root, DigiCert Global Root G2
([customize.md](customize.md#send-the-results-to-azure-iot-hub)).

| Library considered | Outcome |
|---|---|
| Writing our own MQTT and HTTP code | Rejected, in favour of maintained libraries |
| `azure_umqtt_c`, already in the base image | Rejected: its TLS layer sends no SNI and doesn't check the host name, so `mqtts://` to cloud brokers would fail or be unsafe |
| Paho from Mariner's packages | Impossible: Mariner 1.0's package repositories now answer 403 |
| **Eclipse Paho MQTT C**, built from source, static | Chosen: `mqtt://`, `mqtts://`, `ws://` and `wss://`, with SNI and host-name checks |
| **cpp-httplib** (one header) | Chosen for HTTP. Used without TLS, which would need OpenSSL 3 |

A slow HTTP or MQTT consumer must never stall inference: publishing only copies into memory, MQTT
has its own thread and a bounded queue, and HTTP streams skip ahead if they fall behind. A broker
outage only means reconnecting, with backoff, while everything else carries on.

**Myriad X telemetry.** Upstream includes Microsoft's `VPUTempRead` header but never calls it.
Disassembling `libmxIf.so` showed that its boot already starts the temperature and memory
telemetry services and that nothing reads them. A probe program confirmed both answer; `/metrics`
now reports them.

### Obstacles

| Obstacle | Fix |
|---|---|
| No video: `no element "appsrc"`. GStreamer's plugin path is compiled in, and in the base image part of it is a symlink | The collector recreates the base's `/lib`, `/lib64` and `/usr/lib64` symlinks first |
| The Myriad X changes USB device number when its firmware loads, so passing one device breaks | The whole `/dev/bus/usb` is mounted, with a cgroup rule allowing USB devices |
| Upstream compiles with `-Werror` | Third-party headers included as system headers |
| The image contains Microsoft binaries that the source's MIT licence doesn't cover | Built on your device from Microsoft's image, never pushed ([Licences](#licences-and-what-must-not-be-shared)) |
| Inference latency can't be measured | The Myriad X stamps frames with its own clock, which isn't synchronised with the board's. Not exported |
| A stalled pipeline would leave old values in the metrics | `/metrics` leaves out values more than 15 s old, so a stall shows as missing data |

## Microphones and buttons: the Ear SoM

**Files**: `modules/ear/` (`ear.py`, `Dockerfile`), `scripts/host/81-percept-ear.rules`,
`scripts/host/percept-ear-audio.service`. Authored; nothing from Microsoft.

**On the stock Percept**, Microsoft's speech module (`azureearspeechclientmodule`) ran an Azure
Speech voice assistant. Its keyword model downloads from Azure, so on a standalone device it
fails. It also drove the Ear's LEDs.

**What percept-kit does.**
- **A plain microphone service.** `lsusb -v` and `arecord --dump-hw-params` show that the Ear SoM is
  a standard USB Audio Class device: a 4-mic linear array and a line-out. It needs no Microsoft
  software. `ear.py` is a Python standard-library HTTP service around ALSA's `arecord`, in an
  Alpine image of about 60 MB. It only listens: the mouth owns the line-out.
- **The voice assistant became an application**: the voice sample, which runs offline
  ([Voice commands](#voice-commands-the-sample)).

**In the way, and fixed:**

| Obstacle | Diagnosis | Fix |
|---|---|---|
| The Ear is listed by `lsusb` but often has no sound card after boot | Its controller re-enumerates the audio function late in boot, after `snd-usb-audio` has probed | A udev rule starts `percept-ear-audio.service`, which waits 5 s and reloads the driver if no "Azure Ear SoM" card appeared. (A udev rule can't run anything long itself) |
| How to read the Mute and PTT/PTS buttons | Documented only for Microsoft's speech module. `lsusb -t`: interface 4 is HID, which Linux exposes on `/dev/input`. Recording events while pressing each button: bit 0 = Mute state, bit 1 toggles at each PTT press | `ear` reads that input device, read-only |
| Every mic read pure zeros though `arecord` ran | The Mute button: the SoM's own firmware zeroes the capture, and its LED doesn't show it under Ubuntu | `ear` reports `mute`, and the voice sample announces changes |
| The LEDs | Microsoft drove them over a serial port with an undocumented protocol | Not used |
| Now and then, the Ear and Bluetooth are missing after a reboot | The second USB controller's power domain didn't switch on (`failed to command PGC`); a kernel or hardware issue, not fixable from user space | Reboot ([setup.md](setup.md#the-ear-or-bluetooth-is-missing-after-a-reboot)) |

## Sound out: the mouth and Bluetooth

**Files**: `modules/mouth/` (`mouth.py`, `bt.sh`, `Dockerfile`, `requirements.txt`), and the `bt`
command in `percept-kit.sh`. Authored; nothing from Microsoft.

**On the stock Percept**, the Ear's line-out played the voice assistant's replies. The board's
Realtek combo chip also has Bluetooth, and the kernel supports Classic Bluetooth audio (A2DP).

**What percept-kit does.**
- **One owner for every sound.** The mouth plays everything the device says, on a connected
  Bluetooth speaker or else the line-out. Its requests return only when the sound has finished,
  which is what lets the voice sample stop listening while the device talks.
- **Bluetooth audio without a desktop sound server.** BlueALSA, a small A2DP daemon from Ubuntu,
  exposes speakers as ALSA devices; PulseAudio and PipeWire are per-user session daemons and much
  larger. The mouth image is Ubuntu 22.04, not Alpine, so that its BlueALSA plugin speaks the same
  D-Bus API as the host's daemon.

| Obstacle | Diagnosis | Fix |
|---|---|---|
| A Bluetooth scan crashed the kernel, and the device rebooted | A bug in the kernel's Bluetooth LE background scan, when discovery stops. The kernel can't be patched | BlueZ set to Classic only (`ControllerMode = bredr`), which is all A2DP speakers need |
| The container couldn't reach BlueALSA | D-Bus resolves a caller's groups from the **host's** user database, and BlueALSA's policy admits only group `audio` | A host account `percept-mouth` (uid 5003, in `audio`); the container runs as that uid with the host's D-Bus socket |
| Pairings weren't remembered | With the adapter not pairable, BlueZ pairs without storing a link key | `bt.sh pair` makes the adapter pairable first |
| Replies almost silent after a reboot | BlueALSA's software volume comes back at about 25% | The mouth sets the speaker's volume (100% by default) whenever it connects |
| Short sounds lost on Bluetooth | The speaker's amplifier wakes on the first audio | 1 s of silence first after 4 s of quiet |

**The neural voice: Piper.**
- **Why Piper**: it's the offline neural text-to-speech that runs well on small ARM CPUs (VITS
  models through ONNX Runtime, no GPU). Cloud speech was out, and larger models (Coqui XTTS, Bark)
  are far too slow for a Cortex-A53.
- **The package**, `piper-tts`, is GPL-3.0-or-later, like espeak-ng, which the image already had.
  Only three wheels are needed: piper-tts, onnxruntime and numpy. onnxruntime's last release with a
  wheel for Ubuntu 22.04's Python 3.10 is pinned, and numpy comes from PyPI because Ubuntu's is too
  old for it.
- **Voices, by licence.** The `rhasspy/piper-voices` repository is MIT, but each voice is trained
  on its own dataset, named in its `MODEL_CARD`:
  - usable: ljspeech, kristin, norman, john (public domain), joe, kathleen (CC0), libritts_r
    (CC BY 4.0);
  - excluded: lessac (Blizzard Challenge licence), amy (unclear), ryan, hfc_female and hfc_male
    (CC BY-NC-SA, non-commercial).
- **Choice**: the usable US voices were compared by ear on a Bluetooth speaker, and
  `en_US-kristin-medium` sounded the most natural.
- **Speed**: Piper's command line loads the voice for every sentence, which takes 14–22 s. The
  mouth loads it once, in the background, then caches what it renders, and the voice sample
  pre-renders its fixed replies.

## Voice commands: the sample

**Files**: `samples/voice/` (`voice.py`, `Dockerfile`, `requirements.txt`). Authored.

**The idea**: an offline replacement for Microsoft's cloud voice assistant, built only from what
the modules offer: the Ear's audio, the Eye's inferences over MQTT, and the mouth's voice.

**What it does, and why:**
- **Speech recognition**: Vosk, which runs on the Cortex-A53 with a 40 MB English model. It uses a
  fixed grammar, the command phrases plus "unknown", which is fast and accurate and rejects
  ordinary conversation. Rejected: openWakeWord's pre-trained models (non-commercial licence),
  Porcupine (needs a proprietary key), Azure Speech (cloud).
- **What the Eye sees** arrives over MQTT, through the stack's own broker, instead of new code.
- **Snapshots** are one frame of the Eye's `result` stream, read with ffmpeg.

| Obstacle | Fix |
|---|---|
| `import vosk` failed: no module `srt` | vosk imports it at load time; it's installed, hash-pinned |
| `libvosk.so` wouldn't load: `libatomic.so.1` | `libatomic1` added to the image |
| `useradd: group voice exists` | Ubuntu images have a `voice` group; the user is `percept-voice` |
| The small model has no word list to check phrases against | `vosk_model_find_word()` checks each word; phrases with unknown words are skipped, and logged |
| ffmpeg over TCP: `461 Unsupported transport` | The Eye's `/result` stream offers only UDP; ffmpeg uses its default |

## Dashboards and video in a browser

**Files**: `services/dashboard/` (`prometheus/`, `grafana/`, `homepage/`, `go2rtc/`); the metrics in
`modules/eye/app/output/output.cpp`. Authored configuration; the six images are the projects' own.

**On the stock Percept**, monitoring went through Azure: IoT Hub and Azure Percept Studio.

**What percept-kit does, and why:**
- **Standard collectors.** node_exporter (the board) and cAdvisor (the containers) are the standard
  Prometheus exporters; Prometheus pulls and stores; Grafana draws. A custom collector would have
  re-implemented them, and Grafana Alloy would still need Prometheus to store the data.
- **The Myriad X's metrics come from the Eye.** Only one process can talk to the Myriad X, so the
  Eye's existing HTTP server got a `/metrics` route.
- **One definition per headline number.** Recording rules feed both Grafana's top row and the home
  page, so the two can't disagree.
- **Video without transcoding.** go2rtc relays the Eye's RTSP streams to browsers. The Cortex-A53
  can't decode the 8-megapixel H.264 stream in real time, so transcoding was out.
- **No sign-in** on Grafana: every visitor is an anonymous Admin, and Grafana's own login is off,
  so the default `admin` account can't be used either. Grafana 13 still shows a *Sign in* link that
  no setting hides; it leads to an empty page.

| Obstacle | Fix |
|---|---|
| go2rtc can't read `/result` and `/raw` | They offer only UDP, and go2rtc's RTSP client speaks only TCP. The Eye's `/resultTCP` and `/rawTCP` are the same streams over TCP |
| The full-resolution H.264 stream froze in the browser | The player's fixed 2 MB buffer overflows at about 25 Mbit/s. That stream goes by WebRTC instead, on its own port, offering only the address the kit uses |
| Homepage refuses unknown `Host` headers | `deploy` lists the device's own addresses and names |
| Grafana's memory grows (the board has no swap) | `GOMEMLIMIT`, a soft cap for its garbage collector |

## Device Settings

**Files**: `services/settings/` (`server.py`, `configuration.py`, `index.html`, `app.js`,
`style.css`, `packages.txt`), `scripts/host/percept-settings.service`, `scripts/kit/config_guard.py`,
`scripts/stack.sh` (`configure`, `install_settings`). Authored. Tests in `tests/`.

**Why on the host, not in a container**: it changes the host itself (systemd services, the network,
the host name, power) as well as running `docker compose`.

**What it does, and why:**
- **One settings file.** The page offers every setting documented in
  `config.example/percept.env`, with the description and default read from that file, and writes
  the device's `config/percept.env`. `configuration.py` holds each setting's allowed values or
  range, and refuses values the services couldn't use, as well as characters Compose's `.env`
  reader would misread.
- **Only what changed.** `stack.sh configure` recreates only the containers the changed settings
  belong to, and builds nothing. If Compose rejects the result, the previous file is put back.
- **The PC can't silently overwrite it.** A change made on the page marks the device.
  `config_guard.py` then stops a `deploy` whose `config/` doesn't match the device's, until
  `settings pull`. Deploys and settings changes share one lock, so they never overlap.
- **Network changes are checked**: see [Wi-Fi, network and your login](#wi-fi-network-and-your-login).
- **No sign-in**, like the other pages: this is a development device on a trusted network
  ([dashboard.md](dashboard.md#security)).

## Driving the device from a PC

**Files**: `percept-kit.sh`, `scripts/kit/`. Authored. These obstacles shaped how the kit talks to
the device:

| Obstacle | Diagnosis | What the kit does |
|---|---|---|
| SSH timed out for minutes at a time; it looked like Wi-Fi dropping | A Mariner firewall rule drops a source address that opens 6 new SSH connections within 60 s, and every retry restarts the 60 s. Windows OpenSSH can't reuse connections | Each time it reads the device state, the kit adds a runtime-only exemption for the PC's address ahead of that rule. After a reboot it polls only every 15 s until the exemption is back |
| Long jobs died when SSH dropped | `systemd-logind` kills a closed session's processes | Long jobs run as transient systemd units, logging to `/var/tmp`; the kit reconnects and tails the log |
| Backup images were unreadable | Mariner's default `umask 077` | `umask 022` for the dump, read through `sudo` |
| No `zstd` on the PC | | Images are compressed on the device and streamed; SHA-256 is compared at both ends |
| Scripts failed with `set: illegal option -` | Windows line endings | Removed from every text file uploaded; `.gitattributes` keeps scripts LF |
| Mariner has no `grub2-editenv`, `xxd`, `strings` or `sgdisk` | | `grubenv` written in bash; the partition table saved with `dd` |
| A `syntax error` after `install` finished, once the repository had been updated meanwhile | bash reads a script as it runs it | The script's last line, `{ main "$@"; exit; }`, is parsed whole before it runs |

## Where every component comes from

### From the device

Microsoft binaries and your own settings, copied during `install`. They never leave the device,
except in your backups.

| Item | Where it goes | Why |
|---|---|---|
| Signed shim, GRUB and the Mariner kernel | Left in place on the ESP and p3, never written | The only binaries Secure Boot accepts |
| The kernel's modules and firmware | Ubuntu's root | Drivers matching that kernel: Wi-Fi, Bluetooth, GPU, VPU |
| `pe100.dtb` | Left on the ESP | Microsoft's board device tree, the input to `pe100-hdmi.dtb` |
| Accessory udev rules | Ubuntu, same path | Give group `apdk_accessories` (gid 5000) access to the Eye and Ear SoMs |
| Wi-Fi driver options and `wpa_supplicant-wlan0.conf` | Ubuntu | The Realtek driver's settings, and your Wi-Fi network |
| SSH host keys, your password hash and `authorized_keys` | Ubuntu | The same login, and the same host identity |
| The Eye's build base, Microsoft's `azureeyemodule` image | Your backup, then Docker on the device | The Myriad X software stack ([Vision](#vision-the-eye-som)) |

### Downloaded

Each is fetched at a version pinned in one place, named in the third column, and checked against a
SHA-256 there or by the upstream's signed checksums.

| Component | Source | Pinned in | Licence | Used for |
|---|---|---|---|---|
| Ubuntu base | cdimage.ubuntu.com `ubuntu-base` | `scripts/build-ubuntu-rootfs.sh` (checked against Ubuntu's `SHA256SUMS`) | Ubuntu (various OSS) | The Ubuntu root |
| Ubuntu packages | Ubuntu's `jammy` archive | apt's signed `Release` files | Various OSS | The Ubuntu root; BlueALSA and mpv on the host; the mouth and voice images |
| NXP device-tree sources | `github.com/nxp-imx/linux-imx`, tag `imx_5.4.3_2.0.0` | `dtb/` (copies) | `(GPL-2.0+ OR MIT)`, `(GPL-2.0 OR MIT)`, `GPL-2.0` | The HDMI nodes' values |
| Eye app source | Microsoft's `azure-percept-advanced-development` | The commit in [Vision](#the-same-app-from-microsofts-source) | MIT | `modules/eye/app` |
| Eclipse Paho MQTT C | `github.com/eclipse-paho/paho.mqtt.c` | `modules/eye/Dockerfile` | EPL-2.0 / EDL-1.0 | The Eye's MQTT output |
| cpp-httplib | `github.com/yhirose/cpp-httplib` | `modules/eye/Dockerfile` | MIT | The Eye's HTTP output |
| Alpine | Docker Hub `alpine` | `modules/ear/Dockerfile` | Various OSS | The Ear image |
| piper-tts, onnxruntime, numpy | PyPI | `modules/mouth/requirements.txt` | GPL-3.0-or-later, MIT, BSD-3-Clause | The mouth's neural voice |
| Piper voice | `huggingface.co/rhasspy/piper-voices` | `modules/mouth/Dockerfile` | MIT repository; public-domain training data (its `MODEL_CARD`) | The mouth's default voice |
| vosk, srt | PyPI | `samples/voice/requirements.txt` | Apache-2.0, MIT | Speech recognition |
| Vosk speech model | `alphacephei.com/vosk/models` | `samples/voice/Dockerfile` | Apache-2.0 | Speech recognition |
| Mosquitto, Homepage, Grafana, Prometheus, node_exporter, cAdvisor, go2rtc | The projects' own images | `compose.yaml` | EPL-2.0/EDL-1.0, GPL-3.0, AGPL-3.0, Apache-2.0, Apache-2.0, Apache-2.0, MIT | The broker and the dashboard |

### Authored

Everything else: `percept-kit.sh`, `scripts/` (including `hdmi-dcss-nodes.dtsi`, whose node
definitions are NXP's), `compose.yaml`, `config.example/`, the Eye's `Dockerfile`,
`collect-rootfs.sh`, `entrypoint.sh` and `app/output/` with the four small hooks, `modules/ear/`,
`modules/mouth/`, `samples/voice/`, `services/` and `tests/`.

## Licences and what must not be shared

Everything that can't be redistributed comes from **your own Percept DK** while the kit runs.
Nothing from a device is ever committed: `.gitignore` keeps out `backups/`, `reports/`,
`.percept-kit.conf`, your `config/`, any other `*.env` file, every `*.dtb`, and the decompiled
Microsoft trees in `dtb/`.

| Component | Licence | OK to share |
|---|---|---|
| The kit's scripts, documents, `compose.yaml`, `config.example/`, the Eye's `output/`, the ear and mouth modules, `samples/`, `services/` | Written for this project | Yes |
| `modules/eye/app` (the upstream part) | MIT, © Microsoft. Keep `LICENSE.txt` and `NOTICE.txt` with it | Yes, with the licence files |
| Paho MQTT C and cpp-httplib (in the Eye image) | EPL-2.0 / EDL-1.0; MIT. The texts are in the image's `/usr/share/licenses/` | Yes, with the licence texts |
| NXP's sources in `dtb/`, and `scripts/hdmi-dcss-nodes.dtsi` | NXP's dual licences, in each file's SPDX header. The phandle numbers in the `.dtsi` are facts read from `pe100.dtb` | Yes, with their SPDX headers |
| The pulled images (Mosquitto and the dashboard) | Their projects' licences | Not committed or rebuilt: the device pulls the projects' own images, and the kit only mounts its configuration into them |
| What the mouth and voice images download (piper-tts, espeak-ng, ffmpeg, onnxruntime, numpy, the Piper voice, vosk, srt, the Vosk model) | GPL-3.0-or-later, GPL-3.0, LGPL/GPL, MIT, BSD-3-Clause, MIT (public-domain data), Apache-2.0, MIT, Apache-2.0 | Not committed. The images contain no Microsoft binaries, but if you redistribute them, the GPL and LGPL obligations of piper-tts, espeak-ng and ffmpeg apply |
| `dtb/pe100.dtb`, and `pe100-hdmi.dtb` and `.dts` derived from it | Microsoft's board file; no licence published | **Keep local.** Each device builds its own from its ESP |
| The `percept/eye` image | Contains binaries from Microsoft's image (the Myriad stack, OpenVINO, the firmware, libraries) | **Never push or redistribute.** Build it on your own device |
| Microsoft's base image, and `eye-base.tar.zst` in your backup | Microsoft's image | **Never push or redistribute.** Keep it in your private backup |
| The Mariner kernel, modules and firmware copied into Ubuntu; your `backups/` | Microsoft and vendor binaries. Backups also hold your Wi-Fi password, SSH keys and password hash | **Keep local and private** |

## Verify it yourself

```bash
# 1. pe100-hdmi.dtb is pe100.dtb plus scripts/hdmi-dcss-nodes.dtsi, byte for byte (on the device):
findmnt -q /boot/efi || sudo mount /boot/efi
sudo sh ~/percept-kit/scripts/build-hdmi-dtb.sh /boot/efi/pe100.dtb /tmp/rebuilt.dtb && sudo cmp /tmp/rebuilt.dtb /boot/efi/pe100-hdmi.dtb && echo identical
#    The script also compares the input and output with the tested hashes it holds (KNOWN_IN, KNOWN_OUT).

# 2. The Eye app differs from upstream only in the five files listed under "Vision" (on your PC):
git clone -q --depth 1 --filter=blob:none --sparse https://github.com/microsoft/azure-percept-advanced-development.git /tmp/apad
git -C /tmp/apad sparse-checkout set azureeyemodule/app
git diff --no-index --stat /tmp/apad/azureeyemodule/app modules/eye/app

# 3. Downloads: each Dockerfile's `sha256sum -c`, and pip's --require-hashes, fail the build on any mismatch.
```
