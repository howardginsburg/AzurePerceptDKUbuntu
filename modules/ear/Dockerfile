# Azure Percept Ear module - slim standalone audio service.
# Needs: --device /dev/snd --group-add <host audio gid>
# Buttons (optional): -v /dev/input:/dev/input:ro --device-cgroup-rule='c 13:* r' --group-add <host input gid>
FROM alpine:3.24
RUN apk add --no-cache python3 alsa-utils && \
    adduser -D -H -u 5002 -h /app ear
COPY ear.py /app/ear.py
USER 5002
EXPOSE 8000
ENTRYPOINT ["python3", "-u", "/app/ear.py"]
