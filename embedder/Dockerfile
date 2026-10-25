# EmbeddingGemma 2 임베딩 서버 (텍스트 전용, CPU). GPU 환경(DGX)에서는 CUDA torch로 바꿔 빌드한다.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
ENV HF_HOME=/models HF_HUB_DISABLE_TELEMETRY=1
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
