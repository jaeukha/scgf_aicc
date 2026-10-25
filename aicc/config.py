"""환경변수 설정. 모든 값은 .env.example에 문서화한다."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
env = os.environ.get


def _f(name, default):
    return float(env(name, default))


APP_ENV = env("APP_ENV", "local")
PROVIDER_MODE = env("PROVIDER_MODE", "mock")          # mock | local | demo (local/demo = model_server.py HTTP 호출)
REAL_MODELS = PROVIDER_MODE in ("local", "demo")
ASR_BASE_URL = env("ASR_BASE_URL", "http://127.0.0.1:8101")
ASR_TIMEOUT_SECONDS = _f("ASR_TIMEOUT_SECONDS", 30)            # 마이크 구간
ASR_FILE_TIMEOUT_SECONDS = _f("ASR_FILE_TIMEOUT_SECONDS", 900)  # 파일 전체(최대 30분 음성)
DECIDER_BASE_URL = env("DECIDER_BASE_URL", "http://127.0.0.1:8101")
DECIDER_TIMEOUT_SECONDS = _f("DECIDER_TIMEOUT_SECONDS", 30)
DATABASE_URL = env("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'aicc.db'}")
COACH_MODE = env("COACH_MODE", "mock")                # off | mock | api(OpenAI 호환) | internal(model_server --coach)
COACH_BASE_URL = env("COACH_BASE_URL", "http://127.0.0.1:8103/v1")
COACH_MODEL_NAME = env("COACH_MODEL_NAME", "Qwen/Qwen3.5-2B")
COACH_TIMEOUT_SECONDS = _f("COACH_TIMEOUT_SECONDS", 45)
KNOWLEDGE_PATH = Path(env("KNOWLEDGE_PATH", ROOT / "data" / "knowledge"))
KNOWLEDGE_MODE = env("KNOWLEDGE_MODE", "local")          # local(JSON 키워드) | qdrant(사내 지식베이스 RAG)
QDRANT_URL = env("QDRANT_URL", "http://127.0.0.1:6333")  # ":memory:"는 테스트용
QDRANT_COLLECTION = env("QDRANT_COLLECTION", "aicc_kb")
EMBED_BASE_URL = env("EMBED_BASE_URL", "http://127.0.0.1:8104")  # embedder 서비스, "mock"은 테스트용
EMBED_MODEL = "google/embeddinggemma-2@914f7f89142e33e77833254d9c9b90c3cef7303b"
EMBED_DIM = int(env("EMBED_DIM", 768))                   # embedder와 같아야 함(바꾸면 재색인)
EMBED_TIMEOUT_SECONDS = _f("EMBED_TIMEOUT_SECONDS", 30)
KNOWLEDGE_TOP_K = int(env("KNOWLEDGE_TOP_K", 4))
# EmbeddingGemma 2 코사인 점수는 0.65~0.85에 몰린다(시연 문서 실측). 절대 하한 + 1위와의 격차로 거른다.
KNOWLEDGE_MIN_SCORE = _f("KNOWLEDGE_MIN_SCORE", 0.60)    # 실제 문서가 오면 검색 평가로 조정
KNOWLEDGE_MAX_GAP = _f("KNOWLEDGE_MAX_GAP", 0.06)
ROUTING_CONFIG_PATH = Path(env("ROUTING_CONFIG_PATH", ROOT / "config" / "routing.json"))
AUDIO_MAX_BYTES = int(env("AUDIO_MAX_BYTES", 250 * 1024 * 1024))
AUDIO_EXTENSIONS = tuple(env("AUDIO_EXTENSIONS", ".wav,.mp3,.m4a,.flac,.ogg").split(","))
ROUTING_WINDOW = int(env("ROUTING_WINDOW", 30))       # 기존 app.py: 최근 30개 확정 전사
MOCK_DELAY_SECONDS = _f("MOCK_DELAY_SECONDS", 0.6)    # 파일 재생 mock의 발화 간격
DEGRADED_ERROR_RATE = _f("DEGRADED_ERROR_RATE", 0.2)
UNAVAILABLE_CONSECUTIVE_FAILURES = int(env("UNAVAILABLE_CONSECUTIVE_FAILURES", 3))
CORS_ORIGINS = [o for o in env("CORS_ORIGINS", "").split(",") if o]
MODEL_VERSIONS = {  # app.py 고정 revision. 실제 Provider 연결 시 기록용
    "asr": "WhisperX/turbo@3.8.6",
    "decider": "perplexity-ai/pplx-decider-flash-v1.2@9ff7cf9838e59df461c276b7699d56b244d2af58",
    "coach": "Qwen/Qwen3.5-2B@15852e8c16360a2fea060d615a32b45270f8a8fc",
}
# Twenty CRM PoC (AICC → Twenty 단방향, 공개 REST API만 사용)
TWENTY_ENABLED = env("TWENTY_ENABLED", "false").lower() == "true"
TWENTY_BASE_URL = env("TWENTY_BASE_URL", "http://127.0.0.1:3000")
TWENTY_API_KEY = env("TWENTY_API_KEY", "")
TWENTY_TIMEOUT_SECONDS = _f("TWENTY_TIMEOUT_SECONDS", 15)
