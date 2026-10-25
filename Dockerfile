FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# app.py는 라우팅/Coach 검증 로직 재사용을 위해 포함(모델 패키지는 설치하지 않음)
COPY app.py alembic.ini pytest.ini ./
COPY aicc aicc
COPY config config
COPY data/knowledge data/knowledge
COPY data/mock_transcripts.json data/
COPY migrations migrations
COPY tests tests
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn aicc.main:app --host 0.0.0.0 --port 8000"]
