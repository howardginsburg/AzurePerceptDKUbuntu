# "voice" sample: voice commands heard by the Ear, answered from what the Eye sees, spoken by
# the mouth module. Everything third party is downloaded here at build time and pinned by hash.
FROM ubuntu:22.04 AS fetch
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG MODEL=vosk-model-small-en-us-0.15
ARG MODEL_SHA256=30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3-pip python3-setuptools python3-wheel curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --no-deps --require-hashes --no-build-isolation \
      --index-url "$PIP_INDEX_URL" --target /site -r /tmp/requirements.txt
# Vosk speech model (Apache-2.0, https://alphacephei.com/vosk/models)
RUN curl -fsSL -o /tmp/model.zip "https://alphacephei.com/vosk/models/$MODEL.zip" && \
    echo "$MODEL_SHA256  /tmp/model.zip" | sha256sum -c - && \
    python3 -m zipfile -e /tmp/model.zip /tmp/m && mv "/tmp/m/$MODEL" /model && rm -rf /tmp/m /tmp/model.zip

FROM ubuntu:22.04
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      python3 python3-cffi-backend python3-requests python3-tqdm python3-paho-mqtt libatomic1 ffmpeg && \
    rm -rf /var/lib/apt/lists/* && \
    useradd --system --uid 5004 --no-create-home --home-dir /app percept-voice && \
    mkdir -p /data && chown 5004 /data
COPY --from=fetch /site /usr/local/lib/python3.10/dist-packages
COPY --from=fetch /model /opt/model
COPY voice.py /app/
USER 5004
EXPOSE 8080
VOLUME /data
ENTRYPOINT ["python3", "-u", "/app/voice.py"]
