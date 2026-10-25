# Optional network tool image; build the default ctf-agent:local image first.
FROM ctf-agent:local
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl nmap \
    && rm -rf /var/lib/apt/lists/*
USER 65534:65534
