FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TRANSCRIBER_BIND=0.0.0.0 TRANSCRIBER_DATA_DIR=/data
WORKDIR /app
RUN groupadd --gid 10001 transcriber && useradd --uid 10001 --gid 10001 --no-create-home transcriber && mkdir /data && chown 10001:10001 /data
COPY transcriber /app/transcriber
USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["python", "-m", "transcriber"]
