# Eye module: camera and Myriad X inference

The Eye SoM (Azure Percept Vision) is a camera plus an Intel Movidius Myriad X VPU on USB. The
`eye` container runs Microsoft's open-source Azure Percept inference app:
1. It loads a neural network onto the Myriad X.
2. It runs it on the camera feed.
3. It serves the video over RTSP, with and without detections drawn on it, and publishes every
   result as JSON.

It needs no Azure services. `install` builds and starts it; [customize.md](customize.md) covers
changing its model and outputs.

## Addresses

```
rtsp://<ip>:8554/result     video with the detections drawn in (MJPEG 816×616)
rtsp://<ip>:8554/raw        the camera's video (MJPEG 816×616)
rtsp://<ip>:8554/h264raw    the sensor's full resolution (H.264 3264×2464)
http://<ip>:8555/latest     the newest result; /inferences (NDJSON), /events (SSE), /metrics, / (help)
```

The home page's *Video* cards play the three streams in a browser ([dashboard.md](dashboard.md#video)).
VLC (*Media > Open Network Stream*) and `ffplay rtsp://<ip>:8554/result` play them too. The default
model is SSD MobileNet v2 (COCO, 90 classes: people, cars, cups...). The results also go to the MQTT
broker ([Inference results](#inference-results-http-mqtt-stdout)), and `/metrics` adds counters
and the Myriad X's temperatures and memory ([Prometheus metrics](#prometheus-metrics-and-myriad-x-telemetry)).

## How the image is built

`modules/eye/Dockerfile` is a two-stage build on the device:

1. **Build stage**:
   - Starts from Microsoft's last published module image,
     `mcr.microsoft.com/azureedgedevices/azureeyemodule:2301-1-noauth` (2.6 GB download, about
     1.2 GB in Docker's store). It holds the toolchain and the Myriad X stack: OpenCV G-API with
     the MX backend, mxIf, OpenVINO, onnxruntime and GStreamer.
   - Compiles the app in `modules/eye/app/` (upstream:
     [azure-percept-advanced-development/azureeyemodule/app](https://github.com/microsoft/azure-percept-advanced-development/tree/main/azureeyemodule/app),
     MIT, see `LICENSE.txt`/`NOTICE.txt`) with `AUTH_FIX`, so it never waits for Azure
     device authentication. The only addition is `app/output/` (inference outputs, below), built
     against Eclipse Paho MQTT C 1.3.16 and cpp-httplib 0.65.0. Both are downloaded at pinned
     versions and checked by SHA-256 during the build.
2. **Final stage** is `FROM scratch`. `collect-rootfs.sh` copies in only:
   - the app binary;
   - every shared library it loads;
   - the Myriad X firmware;
   - the GStreamer plugins;
   - OpenVINO's `myriad_compile` (for converting `.xml` models);
   - the default model data;
   - a handful of tools (`bash`, `unzip`, `wget`...);
   - the Paho and cpp-httplib licence texts, in `/usr/share/licenses/`;
   - `entrypoint.sh`, which turns the `EYE_*` model and video settings into the app's options.

The result is about 240 MB. It runs as `apdk_app` (uid 5001, gid 5000 = `apdk_accessories`,
the group the host udev rules give access to the Eye). The image contains binaries from
Microsoft's base image: build it on your device and never push it to a registry. Where every
part came from is in [PROVENANCE.md](PROVENANCE.md#vision-the-eye-som).

**The build base stays on the device.** Every build of the Eye needs it, so the kit keeps it in
Docker after the build. Every stock Percept DK has this exact image in its IoT Edge cache, and
`backup` saves that copy (`eye-base.tar.zst`). When Docker doesn't have it, the kit loads it from
the backup, or else pulls it from MCR, which may stop serving it one day.
`./percept-kit.sh eye-base remove` frees its 1.2 GB; [customize.md](customize.md#the-eyes-build-base)
explains when that's worth it.

## How it runs

The `eye` service in [compose.yaml](compose.yaml):

- The Myriad X **re-enumerates on USB** when its firmware is loaded: it changes device number.
  That's why the whole `/dev/bus/usb` tree is mounted and USB character devices (major 189) are
  allowed, rather than passing one device.
- Only one process can own the Myriad X. Stop the `eye` container before running anything else
  that uses it.
- `config/files/` is mounted at `/config/files`, read-only, for models, labels, certificates and
  password files.
- The HTTP outputs are on port 8555, and the RTSP server on 8554.

## Models

`EYE_MODEL`, `EYE_PARSER` and `EYE_LABELS` in `config/percept.env` choose the model
([customize.md](customize.md#use-another-model)). The model files go in `config/files/`, which the
Eye sees as `/config/files/`.

- **`.zip`** is unpacked to `/app/model`. It must contain one of:
  - a `config.json`: `{"DomainType": "<parser>", "ModelFileName": "model.xml", "LabelFileName": "labels.txt"}`;
  - a Custom Vision `cvexport.manifest` (an export of type *OpenVINO* / *Vision AI Dev Kit*);
  - or a `model.blob`.

  With a `config.json` or manifest, the parser and labels come from the package, so `EYE_PARSER`
  and `EYE_LABELS` aren't needed.
- **`.xml`** (OpenVINO IR, FP16, with its `.bin` beside it) is compiled to a Myriad blob at startup
  with `myriad_compile`. That takes a minute.
- **`.blob`** is used as-is: compile it for OpenVINO 2021.x / `MYRIAD`.
- **`.onnx`** works with the `onnxssd` parser, like the bundled
  `/app/data/ssd_mobilenet_v1_10.onnx`.

The parsers are `ssd100` (the default), `ssd200`, `yolo`, `classification`, `s1`, `openpose`,
`onnxssd`, `faster-rcnn-resnet50`, `unet` and `ocr`. Models from the Azure Percept model zoo and
Custom Vision "Vision AI Dev Kit" exports are of these kinds. If a model fails to load, the app
falls back to the default SSD and logs why; `EYE_STRICT_MODEL=1` makes it exit instead.

The app has a few options with no setting. Give them as the service's `command` in
`config/compose.override.yaml`; they're added after the ones the settings produce:

| Option | Meaning |
|---|---|
| `-i=uvc` | Use a USB webcam (`/dev/video0`) instead of the Eye's camera; also add `devices: ["/dev/video0"]` |
| `--h264_out=<file>` | Also write the raw H.264 stream to a file in the container |

## Inference results (HTTP, MQTT, stdout)

Every inference produces one JSON message, in the format the app used to send to IoT Hub, and the
MQTT output can still send it to Azure IoT Hub
([customize.md](customize.md#send-the-results-to-azure-iot-hub)). This
message was captured from `GET /latest` on the device with the default SSD model, with one
person in view:

```json
{"NEURAL_NETWORK": [{"bbox": [0.102, 0.227, 0.634, 0.989],"label": "person", "confidence": "0.934082", "timestamp": "1793936538424400822"}]}
```

`NEURAL_NETWORK` holds one entry per detected object. Formatted, a frame with two objects looks
like this:

```json
{
  "NEURAL_NETWORK": [
    {
      "bbox": [0.102, 0.227, 0.634, 0.989],
      "label": "person",
      "confidence": "0.934082",
      "timestamp": "1793936538424400822"
    },
    {
      "bbox": [0.655, 0.512, 0.871, 0.940],
      "label": "chair",
      "confidence": "0.612305",
      "timestamp": "1793936538424400822"
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `bbox` | 4 numbers | `[x0, y0, x1, y1]`: top-left and bottom-right corners, as fractions (0–1) of the frame's width and height, origin top-left, 3 decimals. Multiply by the frame size for pixels |
| `label` | string | Class name from the model's labels file (`app/data/labels.txt` for the default model: COCO classes) |
| `confidence` | **string** | Score 0–1, 6 decimals. Convert with `float()` / `parseFloat()` |
| `timestamp` | **string** | Time of the frame, nanoseconds since the Unix epoch (`1793936538424400822` is 2026-11-06 03:42:18 UTC). It's a string because it's too big for a JSON double. All entries from one frame share it |

Things to know when parsing:

- **No detections**: the source sends `{"NEURAL_NETWORK": []}`, so you can tell "nothing seen"
  apart from "no messages".
- **Rate**: one message per inference, about 11–12 a second with the default model. Use
  `EYE_OUTPUT_INTERVAL_MS` to slow it down.
- **Other model types** use the same `{"NEURAL_NETWORK": [...]}` wrapper but different
  entries. Classification models have `label`, `confidence` and `timestamp` with no `bbox`.
  OpenPose, OCR and segmentation models have their own fields: see `app/model/*.cpp`.
- **Per transport**: HTTP `/latest` and MQTT payloads are exactly the message above. `/inferences`
  and stdout add a newline after each message. `/events` sends `data: <message>` followed by a
  blank line.

The app sends each message to its outputs, added in `app/output/`:

| Output | When | What you get |
|---|---|---|
| HTTP | Always, on port 8555 | `GET /latest` returns the newest message (204 before the first). `GET /inferences` streams NDJSON (one message per line). `GET /events` streams Server-Sent Events. `GET /metrics` returns [Prometheus metrics](#prometheus-metrics-and-myriad-x-telemetry). `GET /` returns help |
| MQTT | Always; the device's own broker unless `MQTT_URL` names another | QoS 0 publish of each message to `percept/eye/inferences` (`MQTT_EYE_TOPIC`) |
| stdout | `EYE_STDOUT=1` | One JSON line per message in `./percept-kit.sh logs eye` |

`EYE_OUTPUT_INTERVAL_MS=N` caps every output at one message per N ms. 0 (the default) sends every
inference, about 12 a second. The settings are in `config/percept.env`; switching broker is a
[customize.md recipe](customize.md#use-your-own-mqtt-broker).

### MQTT

`compose.yaml` passes the shared `MQTT_*` settings to the app as `EYE_MQTT_URL`, `EYE_MQTT_TOPIC`,
`EYE_MQTT_USERNAME`, `EYE_MQTT_PASSWORD_FILE`, `EYE_MQTT_CA_FILE`, `EYE_MQTT_CERT_FILE`,
`EYE_MQTT_KEY_FILE` and `EYE_MQTT_INSECURE`. `EYE_MQTT_CLIENT_ID`, `EYE_MQTT_KEEPALIVE` and
`EYE_MQTT_RETAIN` are set directly.

- URLs can be `mqtt://host[:1883]`, `mqtts://host[:8883]`, `ws://host[:80]/path` or
  `wss://host[:443]/path`.
- The client id is `eye-<container id>` unless set; keep it to 23 characters or less.
- It publishes from its own thread, through a queue of 256 messages. When the broker is slow or
  down, the oldest messages are dropped, so inference and video never wait for the network.
- On a failed connection it logs the reason (`certificate verify failed`, `not authorized`...)
  and retries after 1, 2, 4... seconds, up to 60.
- With TLS, the broker's certificate and host name are checked, against the system CAs or
  `MQTT_CA_FILE`.

### Reading the results

```bash
curl http://<ip>:8555/latest                    # newest message
curl -N http://<ip>:8555/inferences             # stream, one JSON per line
curl -N http://<ip>:8555/events                 # Server-Sent Events (browser EventSource)
# on the device: one message from the device's broker
docker exec mosquitto mosquitto_sub -t percept/eye/inferences -C 1
```

Any MQTT client library (Paho for Python, C, Java, .NET; MQTT.js; MQTTnet...) or HTTP/SSE client
can read them. Each HTTP server accepts up to 16 streaming clients at a time; others get a 503.

## Prometheus metrics and Myriad X telemetry

`GET /metrics` returns Prometheus metrics (text format 0.0.4). The
[dashboard](dashboard.md) scrapes them every 15 s and charts them in Grafana.

| Metric | Type | Meaning |
|---|---|---|
| `percept_eye_inferences_total` | counter | Inference results from the model: one per inference, about 12 a second with the default SSD |
| `percept_eye_detections_total{label}` | counter | Objects detected, by label |
| `percept_eye_objects{label}` | gauge | Objects in the latest inference, by label. A label stays at 0 once it has been seen |
| `percept_eye_last_inference_timestamp_seconds` | gauge | Unix time of the latest inference |
| `percept_myriadx_temperature_celsius{sensor}` | gauge | The Myriad X's four on-die sensors: `css` CPU subsystem, `mss` media subsystem, `upa` SHAVE vector-processor array, `dss` DDR subsystem |
| `percept_myriadx_memory_used_bytes{pool}`, `percept_myriadx_memory_size_bytes{pool}` | gauge | Myriad X memory: `main` (the 1.5 GB DDR pool for frames and networks), `leonos_heap` and `leonrt_heap` (its two LEON cores' heaps) |
| `percept_myriadx_telemetry_timestamp_seconds` | gauge | When the temperatures and memory were last read |
| `percept_myriadx_usb_speed_bits_per_second` | gauge | The Myriad X's USB link: 5 Gbit/s is USB 3 |
| `process_start_time_seconds` | gauge | When the inference app started |

An excerpt, taken on the device with a laptop in view:

```
percept_eye_inferences_total 497
percept_eye_detections_total{label="laptop"} 407
percept_eye_detections_total{label="person"} 11
percept_myriadx_temperature_celsius{sensor="css"} 45.775
percept_myriadx_temperature_celsius{sensor="upa"} 44.413
percept_myriadx_memory_used_bytes{pool="main"} 214481252
percept_myriadx_memory_size_bytes{pool="main"} 1572864000
percept_myriadx_usb_speed_bits_per_second 5000000000
```

How it works:

- **Counters.** Every inference message is counted, before `EYE_OUTPUT_INTERVAL_MS` thins out the
  other outputs. Labels come from the message's `"label"` fields, which object-detection and
  classification models produce; for other model types only inferences are counted. At most 200
  labels get their own series; any more are counted as `other`.
- **Myriad X telemetry.** Only the process that owns the Myriad X can talk to it, so the Eye reads it
  itself. Microsoft's firmware (`/eyesom/mx.mvcmd`) includes the VPUAL `TempRead` and `Telemetry`
  services, the base image's `libmxIf` has their host side, and `mxIf::Boot()` initialises both when
  the pipeline starts. Every `EYE_VPU_TELEMETRY_MS` (default 5000; 0 turns it off) the inference
  thread calls `vpual::tempread::stub::Get()` and `vpual::telemetry::Memory::Get()`, about 1.5 ms
  each, as long as the Myriad X shows as booted on USB (`03e7:f63b`). Reading from the inference
  thread means a read can never overlap the pipeline being torn down.
- **USB speed** is read from sysfs on each request.
- **Stale values are left out.** If inference stops while the app keeps running, the Myriad X's
  temperatures and memory and `percept_eye_objects` disappear from `/metrics` after 15 s (or three
  `EYE_VPU_TELEMETRY_MS` intervals, if longer), so dashboards show missing data rather than frozen
  numbers. `percept_eye_last_inference_timestamp_seconds` stays, and shows how long ago it was.
- **Not measured**: inference latency and utilisation. Frame timestamps come from the Myriad X's
  clock, which isn't synchronised with the board's: "latency" measured against the board's clock came
  out between −0.5 s and +0.8 s, drifting minute to minute. The Myriad X reports no utilisation figure.

## Limits

- The app's video-file input isn't finished upstream; it supports the Eye camera and UVC only.
- The Azure Percept "secure AI" (encrypted model) path is compiled in but needs Azure; it isn't
  used here.
- The app's Azure IoT client is still compiled in. With no IoT Edge it only logs connection
  errors and sends nothing; results reach Azure IoT Hub through the MQTT output instead
  ([customize.md](customize.md#send-the-results-to-azure-iot-hub)).
- MQTT is QoS 0, MQTT 3.1.1. HTTP and RTSP have no TLS or authentication: keep the device on a
  trusted network.
