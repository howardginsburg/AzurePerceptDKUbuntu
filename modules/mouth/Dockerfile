# Azure Percept "mouth" module: the device's audio output (Bluetooth speaker or the Ear's
# line-out), text-to-speech and Bluetooth pairing page.
# Needs: --device /dev/snd --group-add <host audio gid>
#        -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket
# and, on the host, BlueALSA plus a uid-5003 account in group audio (bt.sh setup).
# Third-party downloads (Piper, its voice) are fetched here at build time and pinned by hash.
FROM ubuntu:22.04 AS fetch
ARG PIP_INDEX_URL=https://pypi.org/simple
# Piper voice from huggingface.co/rhasspy/piper-voices (repository MIT; this voice's recordings
# are public domain, from LibriVox: see its MODEL_CARD). Pinned to a repository revision.
ARG PIPER_VOICE=en_US-kristin-medium
ARG PIPER_VOICE_PATH=en/en_US/kristin/medium
ARG PIPER_VOICES_REV=e7229f7d33a10e145e109b886d661984201ac3c4
ARG PIPER_VOICE_SHA256=5849957f929cbf720c258f8458692d6103fff2f0e3d3b19c8259474bb06a18d4
ARG PIPER_VOICE_JSON_SHA256=5681426d4aead22195de70531eeeeddb46493cfaffc5764b2ea3db73428b651c
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3-pip python3-setuptools python3-wheel curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --no-deps --require-hashes --no-build-isolation \
      --index-url "$PIP_INDEX_URL" --target /site -r /tmp/requirements.txt
RUN mkdir /piper && cd /piper && \
    base="https://huggingface.co/rhasspy/piper-voices/resolve/$PIPER_VOICES_REV/$PIPER_VOICE_PATH" && \
    curl -fsSL -o "$PIPER_VOICE.onnx" "$base/$PIPER_VOICE.onnx" && \
    curl -fsSL -o "$PIPER_VOICE.onnx.json" "$base/$PIPER_VOICE.onnx.json" && \
    curl -fsSL -o "$PIPER_VOICE.MODEL_CARD" "$base/MODEL_CARD" && \
    printf '%s  %s\n' "$PIPER_VOICE_SHA256" "$PIPER_VOICE.onnx" \
      "$PIPER_VOICE_JSON_SHA256" "$PIPER_VOICE.onnx.json" | sha256sum -c -

# Ubuntu 22.04 so the BlueALSA ALSA plugin here matches the host's bluealsa 3.0 D-Bus API.
FROM ubuntu:22.04
ARG PIPER_VOICE=en_US-kristin-medium
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 alsa-utils espeak-ng libasound2-plugin-bluez bluez-alsa-utils bluez && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --system --uid 5003 --no-create-home --home-dir /app mouth && \
    mkdir /cache && chown 5003 /cache
# libasound2-plugin-bluez ships /etc/alsa/conf.d/20-bluealsa.conf, which defines the
# "bluealsa:DEV=<MAC>,PROFILE=a2dp" PCM (with format conversion), so no asound.conf is needed.
COPY --from=fetch /site /usr/local/lib/python3.10/dist-packages
COPY --from=fetch /piper /opt/piper
COPY mouth.py bt.sh /app/
ENV MOUTH_DEFAULT_VOICE=$PIPER_VOICE
USER 5003
EXPOSE 8001
VOLUME /cache
ENTRYPOINT ["python3", "-u", "/app/mouth.py"]