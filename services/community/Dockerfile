FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip==26.2
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY alembic.ini ./
COPY migrations/ ./migrations/

# JWT 공개키는 Docker image에 복사하지 않고 runtime Docker secret으로만 주입한다.
RUN find /app/app /app/migrations -type d -exec chmod 0755 {} + \
    && adduser --disabled-password --gecos "" --uid 1001 appuser
USER appuser

EXPOSE 8300

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300", "--workers", "2"]
