# NetMover im Container (Alternative zum systemd-Dienst, siehe README).
# Braucht zur Laufzeit CAP_SYS_ADMIN und ein Mount-Verzeichnis mit
# Propagation „rshared“ – sonst sieht der Host die Mounts nicht.

FROM python:3.12-slim-trixie AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim-trixie
RUN apt-get update \
 && apt-get install -y --no-install-recommends cifs-utils nfs-common \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

ENV NETMOVER_HOST=0.0.0.0 \
    NETMOVER_PORT=8080 \
    NETMOVER_DATA_DIR=/var/lib/netmover \
    NETMOVER_RUN_DIR=/run/netmover \
    NETMOVER_MOUNT_BASE=/mnt/netmover \
    PYTHONUNBUFFERED=1

VOLUME ["/var/lib/netmover"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"
CMD ["netmover", "serve"]
