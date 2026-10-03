# IGNGbot v3 runtime image.
#
# Built from the repository so the deployed code is exactly the reviewed
# source; the previous VM deployment drifted from git (its call_log_db.py had
# no site-AI mirroring at all), so importing an image built here is the only
# way to guarantee what actually runs.
#
# Build host notes: the NAS cannot reach deb.debian.org or pypi.org directly,
# so apt and pip are pinned to mirrors that are reachable from it.
FROM dockerproxy.net/library/python:3.12-slim

ENV IGNGBOT_RUNTIME=v3 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Swap in reachable Debian mirrors before the first apt-get update.
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
            /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
            /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        tzdata \
        ca-certificates \
    ; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo $TZ > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies are installed from the committed manifest only.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY prompts/ ./prompts/
COPY igngbot_v3/ ./igngbot_v3/
COPY igngbot_v4/ ./igngbot_v4/
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/

# Run unprivileged; the NAS share is owned by uid/gid 1000:1001 and is mounted
# read-write into the container, so the process must match that ownership.
RUN groupadd --gid 1001 botgroup 2>/dev/null || true; \
    useradd --uid 1000 --gid 1001 --no-create-home --shell /usr/sbin/nologin botuser 2>/dev/null || true; \
    mkdir -p /data/message_logs /data/runtime; \
    chown -R 1000:1001 /app /data

USER 1000:1001

ENTRYPOINT ["python", "/app/main.py"]
