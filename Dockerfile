FROM python:3.12-alpine

LABEL maintainer="adamdevlpmnt"
LABEL version="3.0.0-dev"
LABEL description="Vault-Tracker: Private tracker passkey protector for qBittorrent"

# Keeps Python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vault_tracker/ vault_tracker/

VOLUME /data

CMD ["python", "-m", "vault_tracker"]
