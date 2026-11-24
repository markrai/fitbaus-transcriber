FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TRANSCRIBER_BIND=0.0.0.0 TRANSCRIBER_DATA_DIR=/data
ENV HF_HOME=/data/huggingface HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_IMPLICIT_TOKEN=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 HOME=/tmp
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements-cpu.txt /app/requirements-cpu.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /app/requirements-cpu.txt
RUN groupadd --gid 10001 transcriber && useradd --uid 10001 --gid 10001 --no-create-home transcriber && mkdir /data && chown 10001:10001 /data
COPY transcriber /app/transcriber
USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["python", "-m", "transcriber"]
