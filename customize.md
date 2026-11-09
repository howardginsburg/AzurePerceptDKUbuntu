# Customize the device

Every change is made on your PC and applied with one command:

```bash
./percept-kit.sh deploy
```

`deploy` sends the device only the files that changed. It then:
- rebuilds the images whose source changed: the Eye in 15–30 minutes, the others in a few minutes;
- recreates the containers whose settings changed, and restarts those whose configuration files
  changed;
- checks every component and prints its address.

If the new configuration doesn't load, `deploy` stops with Compose's message and the running stack
is left as it was.

## Your settings: `config/`

Start from the examples (once):

```bash
cp -r config.example config
```

| Path | What goes in it |
|---|---|
| `config/percept.env` | Every setting, each documented where it's defined. Commented out or empty, a setting keeps the default shown |
| `config/files/` | Models, label files, certificates, keys and password files. The containers see `config/files/<name>` as `/config/files/<name>`, read-only |
| `config/compose.override.yaml` | Optional. Changes to [`compose.yaml`](compose.yaml): turning a component off, adding your own container, other ports. Compose merges it over `compose.yaml` |

`config/` is git-ignored. On the device only your user and the modules' group can read it, because
it can hold credentials.

Each recipe below is: edit `config/`, run `deploy`, check. To undo one, put the file back as it
was and deploy again.

## Use another model

The Eye runs SSD MobileNet v2 (COCO, 90 classes) unless `EYE_MODEL` names another model.

1. Put the model in `config/files/`, for example `config/files/my-model.zip`.
2. In `config/percept.env`:

   ```bash
   EYE_MODEL=/config/files/my-model.zip
   ```

   Add `EYE_PARSER` and `EYE_LABELS` when the model isn't a package that names them. The formats
   and parsers are in [eye.md](eye.md#models).
3. `./percept-kit.sh deploy`. The Eye restarts and loads the model; an `.xml` model is compiled
   first, which takes about a minute.
4. Check: the *Annotated* video on the home page draws the new model's labels, and
   `curl http://<ip>:8555/latest` returns its results. If the model doesn't load, the Eye falls back
   to the default SSD and says why in `./percept-kit.sh logs eye`.

Example, the ONNX SSD that comes with the Eye:

```bash
EYE_MODEL=/app/data/ssd_mobilenet_v1_10.onnx
EYE_PARSER=onnxssd
```

`EYE_MODEL` can also be an `http(s)://` URL to a `.zip` package, which the Eye downloads at each
start.

## Use your own MQTT broker

The Eye publishes every inference over MQTT, and the voice sample reads them from there. By
default both use the device's own broker, the `mosquitto` container, which only the device's
containers can reach. `MQTT_URL` moves both to your broker:

```bash
MQTT_URL=mqtts://broker.example.com:8883
MQTT_USERNAME=percept
MQTT_PASSWORD_FILE=/config/files/mqtt-password
# Only if your broker's certificate isn't from a public CA:
MQTT_CA_FILE=/config/files/ca.pem
# Only if your broker requires client certificates (mutual TLS):
MQTT_CERT_FILE=/config/files/client.pem
MQTT_KEY_FILE=/config/files/client.key
```

Put the password alone on the first line of `config/files/mqtt-password`. `mqtt://`, `mqtts://`,
`ws://` and `wss://` URLs work.

After `deploy`:
- the Eye and the voice sample connect to your broker; the device's own broker is stopped and
  removed;
- the Eye publishes to `percept/eye/inferences`, and the voice sample to `percept/voice/commands`
  and `percept/voice/events`. `MQTT_EYE_TOPIC` and `MQTT_VOICE_PREFIX` change them;
- check from any machine: `mosquitto_sub -h broker.example.com -p 8883 -u percept -P <password> -t 'percept/#' -v`.
  `./percept-kit.sh logs eye` shows the reason for any refused connection (`not authorized`,
  `certificate verify failed`), and the Eye retries every 1 to 60 s.

Remove `MQTT_URL` and deploy to go back to the device's broker. The message format, and how the
Eye behaves when the broker is slow or down, are in
[eye.md](eye.md#inference-results-http-mqtt-stdout).

To read the device's own broker from your network instead, publish its port in
`config/compose.override.yaml`. Anyone on the network can then publish and subscribe, because it
accepts anonymous clients:

```yaml
services:
  mosquitto:
    ports: ["1883:1883"]
```

## Change the video or the inference outputs

In `config/percept.env`:

| Setting | Effect |
|---|---|
| `EYE_RESOLUTION` | `native` (816×616 video), `1080p` or `720p` |
| `EYE_FPS` | Video frame rate (10) |
| `EYE_TIME_ALIGN=1` | Delays the video so each box lines up exactly with its frame |
| `EYE_OUTPUT_INTERVAL_MS` | At most one message every N ms on MQTT, HTTP and stdout. The default, 0, sends every inference, about 12 a second |
| `EYE_STDOUT=1` | One JSON line per inference in `./percept-kit.sh logs eye` |

The Prometheus metrics still count every inference. [eye.md](eye.md) has the details.

## Change the voice sample

| To | Do |
|---|---|
| Use another wake word | `VOICE_WAKE_WORD=computer` (one word the speech model knows; the sample checks at start-up and says which words it doesn't know) |
| Use no wake word | `VOICE_WAKE_WORD=none`: every command phrase is acted on |
| Listen with one microphone | `VOICE_MIC=0` (to `3`) instead of the average of all four |
| Hear quieter speech | Raise `VOICE_GAIN_DB` (12). Each `heard` event's `peak_dbfs` helps tune it |
| Reply in another voice | `VOICE_TTS_VOICE=espeak:en-us+f3`, or a Piper voice the mouth has |
| Watch for something else | `VOICE_WATCH_LABEL=cat` (a label of the Eye's model) |
| Add a command | Add its phrases to `COMMANDS` and a handler to `HANDLERS` in `samples/voice/voice.py`, then deploy: the voice image is rebuilt |

The other settings, with their defaults, are in `config/percept.env`. How the sample works is in
[samples/voice/README.md](samples/voice/README.md).

## Change the speech voice or the speaker

| To | Do |
|---|---|
| Pair a Bluetooth speaker | The home page's **Bluetooth speaker** card, or `http://<ip>:8001/#bt`; see [setup.md](setup.md#3-pair-a-bluetooth-speaker) |
| Never use Bluetooth, or only Bluetooth | `MOUTH_SINK=line-out`, or `MOUTH_SINK=bluetooth` |
| Keep the speaker's volume as it is | `MOUTH_BT_VOLUME=keep`. By default it's set to 100% whenever the speaker connects |
| Stop visitors pairing speakers | `MOUTH_BT_ADMIN=0` hides scan, connect and forget on the mouth page; pair with `./percept-kit.sh bt` |
| Speak faster or slower | `MOUTH_SPEED=190` (165) |
| Use the fast, robotic voice | `MOUTH_VOICE=espeak:en-us+f3` |
| Use another Piper voice | Change the `PIPER_VOICE*` build arguments in `modules/mouth/Dockerfile` (name, path and both SHA-256 sums), then deploy: the mouth image is rebuilt. Check the voice's licence first ([mouth.md](mouth.md#text-to-speech)) |

## Change the dashboard

- **Grafana.** There is no sign-in and everyone is an Admin, so you can change anything in the
  browser. The *Percept DK* dashboard itself is read-only: save your changes as a copy (*Save as*),
  which is kept in Grafana's volume. To change the kit's dashboard, edit
  `services/dashboard/grafana/dashboards/percept-dk.json` (or export your copy over it) and deploy.
- **Home page.** Its cards, widgets and bookmarks are in `services/dashboard/homepage/`
  (`services.yaml`, `widgets.yaml`, `bookmarks.yaml`, `settings.yaml`). Edit them and deploy;
  Homepage is restarted. The format is in [Homepage's documentation](https://gethomepage.dev/configs/).
- **Metrics history.** Prometheus keeps 30 days or 1 GB, whichever limit comes first:

  ```bash
  PROMETHEUS_RETENTION=90d
  PROMETHEUS_RETENTION_SIZE=2GB
  ```

  It stores about 185 samples a second, and a 2-hour block of them takes about 4 MB before
  Prometheus merges blocks, so the 1 GB limit may come first. That limit protects the eMMC
  whatever the time limit says.

## Turn a component off

In `config/compose.override.yaml`, give the service a profile that is never switched on:

```yaml
services:
  voice:
    profiles: [off]
```

`deploy` then stops and removes it. Its home page card shows a red dot, and its data volume is
kept. Delete the lines and deploy to bring it back. The whole dashboard, for example:

```yaml
services:
  homepage: {profiles: [off]}
  grafana: {profiles: [off]}
  prometheus: {profiles: [off]}
  node-exporter: {profiles: [off]}
  cadvisor: {profiles: [off]}
  go2rtc: {profiles: [off]}
```

`./percept-kit.sh stop <service>` is the temporary alternative: the container stays stopped, across
reboots too, until the next `deploy` or `./percept-kit.sh start <service>`.

## Add your own container

Add it to `config/compose.override.yaml`. It joins the network `percept`, so it reaches the other
containers by name: `mosquitto:1883`, `eye:8555`, `ear:8000`, `mouth:8001`, `prometheus:9090`.
For example, [Node-RED](https://nodered.org/) (about 600 MB on disk), to build flows on the Eye's
inferences:

```yaml
services:
  node-red:
    image: nodered/node-red:5.0.9
    restart: unless-stopped
    ports: ["1880:1880"]
    volumes: ["node-red-data:/data"]

volumes:
  node-red-data:
```

After `deploy`, open `http://<ip>:1880/`, add an *mqtt in* node with the server `mosquitto`, port
1883, and the topic `percept/eye/inferences`.

To build one from source, put its folder in `config/` (deploy only sends `config/` and the kit's
own folders), and add the label below so that `deploy` rebuilds it only when its files change:

```yaml
services:
  my-app:
    build:
      context: config/my-app
      labels:
        org.percept-kit.source: ${PK_SOURCE:-}
    restart: unless-stopped
    ports: ["8090:8090"]
```

`./percept-kit.sh status` lists your containers too. Settings from `config/percept.env` reach them
if you add `env_file: config/percept.env`.

## Change the code

Edit anything in `modules/`, `samples/voice/` or `services/`, then deploy. Only the images whose
folder changed are rebuilt; documentation (`*.md`) doesn't count. `./percept-kit.sh rebuild <service>`
rebuilds one from scratch, without Docker's cache, for example after a base image was updated.

## Free up disk space

Ubuntu's root partition, which also holds Docker, is 10.5 GB. With everything running and the
Eye's build base kept, 3.5 GB of it is free.

| What | Size | Notes |
|---|---|---|
| Ubuntu | 1.8 GB | Includes the system journal (capped at 200 MB) and apt's download cache (`sudo apt clean` empties it) |
| The Eye's build base | 1.2 GB | Below |
| Grafana's image | 1.4 GB | |
| The other images | 1.9 GB | The voice sample 513 MB, the mouth 366 MB, the Eye 238 MB, the rest under 300 MB each |
| Metrics history | Up to 1 GB | `PROMETHEUS_RETENTION_SIZE` |
| Container logs | Up to 15 MB per container | Three files of 5 MB |
| Snapshots | About 3 MB | `VOICE_MAX_SNAPSHOTS` (50) |

`deploy` checks for room before building or downloading: 1.5 GB free to build the Eye, 1 GB for
the other images, 3 GB when Grafana's image must be downloaded. When the Eye must be built and its
build base isn't on the device, it also needs room for the base: 1.3 GB loaded from your backup, or
3 GB downloaded. It stops with a message if there's less.

### The Eye's build base

The Eye is compiled on the device inside Microsoft's last published Azure Percept module image,
`mcr.microsoft.com/azureedgedevices/azureeyemodule:2301-1-noauth` ([eye.md](eye.md#how-the-image-is-built)).
The running Eye doesn't use it, but every Eye build does, so the kit keeps it on the device.

**Why you might remove it.** It's the largest single item, 1.2 GB. Removing it makes room for longer
metrics history, more snapshots, your own models and containers, or newer image versions.

**Why you might keep it.** Any build of the Eye needs it again: a change to `modules/eye/`, a
`deploy` that rebuilds the Eye, or `rebuild eye`. The kit then puts it back by itself, either by
loading this device's copy from your backup (423 MB from your PC, 5–10 minutes) or by downloading
it from Microsoft's registry (2.6 GB), if the registry still serves it. Microsoft retired Azure
Percept in 2023.

**Never remove it without a copy in your backup.** The Eye keeps running, but without a copy you
might never be able to build it again.

How:

```bash
./percept-kit.sh eye-base status     # on the device? a verified copy in your backup?
./percept-kit.sh eye-base save       # no copy yet: add the device's to your backup
./percept-kit.sh eye-base remove     # delete it from the device; prints the space freed
```

`remove` asks before going ahead when the backup holds no verified copy. To put it back before you
need it, `./percept-kit.sh eye-base restore` loads it from the backup, or else downloads it.

## Change the address, Wi-Fi or host name

- **The device's IP address changed.** The home page's links and the full-resolution video use the
  address the kit knows. Run `./percept-kit.sh connect <new-ip>`, then `./percept-kit.sh deploy`. A
  DHCP reservation for the device's Wi-Fi MAC address (printed by `./percept-kit.sh assess`) keeps it
  from changing.
- **Another Wi-Fi network.** On the device, edit `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf`,
  then `sudo systemctl restart wpa_supplicant@wlan0`. Then connect and deploy as above.
- **Another host name.** `sudo hostnamectl set-hostname <name>` on the device, then deploy: the home
  page accepts the device's names and addresses only.

## Use Docker Compose directly

The device's stack is an ordinary Compose project in `~/percept-kit`. Your user can run
`docker compose` there without `sudo`:

| On your PC | On the device, in `~/percept-kit` |
|---|---|
| `./percept-kit.sh status` | `docker compose ps` |
| `./percept-kit.sh logs eye 100` | `docker compose logs --tail 100 eye` |
| `./percept-kit.sh restart mouth` | `docker compose restart mouth` |
| `./percept-kit.sh stop voice` | `docker compose stop voice` |
| `./percept-kit.sh compose <args>` | `docker compose <args>` |

Change files on your PC, not on the device: each `deploy` replaces `compose.yaml`, `modules/`,
`samples/`, `services/`, `config/`, `.env` and `.env.d/` in `~/percept-kit` with what your PC has.
