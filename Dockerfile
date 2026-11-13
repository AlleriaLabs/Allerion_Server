FROM python:3.12-slim
RUN pip install --no-cache-dir psutil tzdata tinytuya cryptography speedtest-cli python-kasa
# Official Ookla Speedtest CLI (replaces the pip "speedtest" command). If this step fails the build
# continues and the server falls back to the python speedtest-cli package.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates iproute2 iputils-ping \
 && (curl -fsSL "https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-$(uname -m).tgz" \
     | tar -xz -C /usr/local/bin speedtest || echo "Ookla CLI not installed, using fallback") \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server_monitor.py .
CMD ["python3", "-u", "server_monitor.py"]
