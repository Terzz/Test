# glibc obligatoire : la wheel de claude-agent-sdk est une manylinux
# (x86_64 et aarch64) qui embarque le binaire Claude Code. Pas d'Alpine ici.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY fripe ./fripe
RUN pip install --no-cache-dir .

# Le jar de cookies Vinted et les fichiers temporaires vivent ici.
ENV DATA_DIR=/app/data
VOLUME ["/app/data"]

# Long polling : le bot n'ouvre aucun port entrant.
CMD ["python", "-m", "fripe.bot"]
