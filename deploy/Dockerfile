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

# 可选引擎构建层：默认 0，不把 fastembed / sherpa-onnx / pdfplumber / rdkit 装进生产镜像。
# 构建时用 --build-arg INSTALL_OSS=1（或 compose 覆盖文件 deploy/docker-compose.oss.yml）。
# 公式/表格引擎会钉住 numpy<2，不在本层安装；需要时用独立镜像/进程，见 requirements-oss-formula.txt。
ARG INSTALL_OSS=0
COPY requirements-oss.txt constraints-oss.txt requirements-embeddings.txt requirements-speech.txt requirements-pdf.txt requirements-chem.txt ./
RUN if [ "$INSTALL_OSS" = "1" ]; then \
      pip install --no-cache-dir -r requirements-oss.txt -c constraints-oss.txt; \
    fi

COPY app ./app
COPY web ./web
COPY eval ./eval
COPY deploy ./deploy
COPY scripts ./scripts
# 分发治理（方案 §7「分发与运维」）：构建上下文由根 `.dockerignore` 收敛（排除 data/、
# backups/、node_modules/、.git/ 与验收产物）；镜像随包携带三方声明与 vendored 编辑器源码，
# 便于溯源与许可证核对。web/editor 是构建产物，已随 `COPY web` 进入镜像。
COPY THIRD_PARTY_NOTICES.md ./THIRD_PARTY_NOTICES.md
COPY editor/vendor ./editor/vendor

RUN useradd -m -u 10001 zhixu && mkdir -p /data /bank && chown -R zhixu:zhixu /app /data
USER zhixu

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health/live', timeout=3)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--proxy-headers"]
