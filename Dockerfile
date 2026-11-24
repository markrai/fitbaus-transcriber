FROM python:3.12-slim AS qualification-audio
RUN apt-get update && apt-get install -y --no-install-recommends espeak-ng ffmpeg && rm -rf /var/lib/apt/lists/*
COPY scripts/build-qualification-sample.py /tmp/build-qualification-sample.py
RUN python /tmp/build-qualification-sample.py /sample.wav

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TRANSCRIBER_BIND=0.0.0.0 TRANSCRIBER_DATA_DIR=/data
ENV HF_HOME=/data/huggingface HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_IMPLICIT_TOKEN=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 HOME=/tmp
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements-cpu.txt /app/requirements-cpu.txt
COPY LICENSE /app/LICENSE
RUN chmod a+r /app/LICENSE
RUN pip install --no-cache-dir --only-binary=:all: -r /app/requirements-cpu.txt
RUN groupadd --gid 10001 transcriber && useradd --uid 10001 --gid 10001 --no-create-home transcriber && mkdir /data && chown 10001:10001 /data
COPY transcriber/model.py /app/transcriber/model.py
RUN HF_HOME=/tmp/model-build python -c 'from transcriber.model import bundle_default; bundle_default("/opt/fitbaus-models")' && rm -rf /tmp/model-build && chmod -R a+rX /opt/fitbaus-models
COPY licenses/whisper-MIT.txt /opt/fitbaus-models/LICENSE
COPY transcriber /app/transcriber
COPY --from=qualification-audio /sample.wav /opt/fitbaus-qualification/sample.wav
RUN chmod -R a+rX /app/transcriber /opt/fitbaus-models
USER 10001:10001
EXPOSE 8765
ENTRYPOINT ["python", "-m", "transcriber"]
