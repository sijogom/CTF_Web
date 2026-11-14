FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip==26.2
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY alembic.ini .
COPY migrations/ ./migrations/
COPY app/ ./app/
COPY docker-entrypoint.sh /usr/local/bin/ctf-auth-entrypoint

# JWT key는 Docker image에 복사하지 않고 runtime Docker secret으로만 주입한다.
RUN find /app/app /app/migrations -type d -exec chmod 0755 {} + \
    && adduser --disabled-password --gecos "" --uid 1001 appuser \
    && chmod 0555 /usr/local/bin/ctf-auth-entrypoint

EXPOSE 8020

ENTRYPOINT ["/usr/local/bin/ctf-auth-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8020", "--workers", "2"]
