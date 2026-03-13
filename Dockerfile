FROM python:3.12-slim

LABEL maintainer="Vault-Tracker" \
      description="Private tracker passkey protector for qBittorrent" \
      org.opencontainers.image.source="https://github.com/adamdevlpmnt/Vault-Tracker" \
      org.opencontainers.image.description="Private tracker passkey protector for qBittorrent" \
      org.opencontainers.image.licenses="MIT"

# Prevent Python from buffering stdout/stderr (important for docker logs)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY vault_tracker/ vault_tracker/

# Persistent volume for the SQLite database
VOLUME /data

CMD ["python", "-m", "vault_tracker"]
