FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STUDY_DATA_DIR=/data \
    STUDY_BANK_DIR=/bank

# ONNX Runtime（本地 OCR）在 slim 镜像中需要 libgomp1；ffmpeg 供课堂视频导出使用（无则能力降级）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY deploy/requirements.txt requirements-deploy.txt
RUN pip install --no-cache-dir -r requirements.txt -r requirements-deploy.txt

COPY app ./app
COPY web ./web
COPY eval ./eval
COPY deploy ./deploy
COPY scripts ./scripts

RUN useradd -m -u 10001 zhixu && mkdir -p /data /bank && chown -R zhixu:zhixu /app /data
USER zhixu

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health/live', timeout=3)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
