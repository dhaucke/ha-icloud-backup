FROM ghcr.io/home-assistant/base-python:3.13-alpine3.24-2026.08.0

ARG RCLONE_VERSION=1.75.2
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64) arch=amd64; sum=349ac8fba6ff65d6247043f1750cdcb518ec5d500ef91463a10d37c0ccdf3702 ;; \
      aarch64) arch=arm64; sum=7e1e8d69654941b7b7df84ee74c5f7fb09cce7d5496947fb7bf16b55ff427d10 ;; \
      *) echo "Nicht unterstützte Architektur: $(uname -m)"; exit 1 ;; \
    esac; \
    zip="rclone-v${RCLONE_VERSION}-linux-${arch}.zip"; \
    cd /tmp; \
    curl -fsSLO "https://downloads.rclone.org/v${RCLONE_VERSION}/${zip}"; \
    echo "${sum}  ${zip}" | sha256sum -c -; \
    unzip -q "${zip}"; \
    install -m 755 "rclone-v${RCLONE_VERSION}-linux-${arch}/rclone" /usr/local/bin/rclone; \
    rm -rf /tmp/rclone-*

COPY app/ /app/
COPY run.sh /
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
