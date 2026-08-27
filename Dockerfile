# syntax=docker/dockerfile:1.7

FROM python:3.12.12-slim@sha256:f3fa41d74a768c2fce8016b98c191ae8c1bacd8f1152870a3f9f87d350920b7c

ARG SOURCE_DATE_EPOCH=0
# Optional regional download mirror for uv (e.g. a PyPI mirror); the default
# stays PyPI.  Lockfile hashes still gate every downloaded wheel, so mirroring
# only changes where bytes come from, never what gets installed.
ARG UV_DEFAULT_INDEX=""

COPY --from=ghcr.io/astral-sh/uv:0.9.24@sha256:816fdce3387ed2142e37d2e56e1b1b97ccc1ea87731ba199dc8a25c04e4997c5 /uv /uvx /bin/

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOURCE_DATE_EPOCH=0 \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH="/app/.venv/bin:$PATH"

RUN addgroup --system grove \
    && adduser --system --ingroup grove grove \
    && touch -h -d "@${SOURCE_DATE_EPOCH}" \
      /etc /etc/.pwd.lock /etc/group /etc/group- /etc/gshadow /etc/gshadow- \
      /etc/passwd /etc/passwd- /etc/shadow /etc/shadow- /run /run/adduser /usr/bin

RUN --mount=type=bind,target=/context,ro \
    tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --numeric-owner --owner=0 --group=0 \
      -C /context -cf - pyproject.toml uv.lock .python-version README.md | tar -C /app -xf - \
    && mkdir -p /app/app /app/alembic /app/scripts \
    && tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --numeric-owner --owner=0 --group=0 \
      --exclude='__pycache__' --exclude='*.pyc' -C /context/app -cf - . | tar -C /app/app -xf - \
    && tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --numeric-owner --owner=0 --group=0 \
      --exclude='__pycache__' --exclude='*.pyc' -C /context/alembic -cf - . | tar -C /app/alembic -xf - \
    && tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --numeric-owner --owner=0 --group=0 \
      -C /context -cf - alembic.ini | tar -C /app -xf - \
    && tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --numeric-owner --owner=0 --group=0 \
      -C /context/scripts -cf - migration_report.py ws3_downgrade.py | tar -C /app/scripts -xf - \
    && find /app -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +

RUN --mount=type=cache,target=/tmp/uv-cache \
    if [ -n "$UV_DEFAULT_INDEX" ]; then export UV_DEFAULT_INDEX; fi \
    && UV_HTTP_TIMEOUT=120 uv sync --frozen --no-dev --no-editable --no-cache \
    && find /app/.venv -name uv_cache.json -type f -delete \
    && find /app/.venv -name RECORD -type f -exec sed -i '/uv_cache.json/d' {} + \
    && chown -R root:root /app \
    && chmod -R a-w /app \
    && find /app -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +

USER grove

EXPOSE 8000
CMD ["python", "-m", "app.main"]
