FROM python:3.12-slim@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data STATIC_DIR=/app/web/dist
WORKDIR /app
COPY server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 observatory \
    && useradd --uid 10001 --gid 10001 --no-create-home observatory \
    && mkdir /data && chown 10001:10001 /data
COPY . .
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "server.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "32", "--no-access-log"]
