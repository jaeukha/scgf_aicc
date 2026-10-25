# AICC 상담원 보조 시연

서울신용보증재단 보증 상담용 AICC 시연 시스템이다. FastAPI 백엔드 하나가 상담 화면, CRM, 시스템 모니터링 UI(`aicc/static/index.html`)를 서빙한다.
모델은 별도 프로세스인 `model_server.py`(GPU venv)가 HTTP로 제공한다. 모델 없이도 Mock으로 전체 흐름을 시연할 수 있다.
기존 Gradio 앱(`app.py`)은 그대로 유지되며, 신규 코드는 그 안의 라우팅·Coach 검증·ASR·Decider 런타임을 import해 재사용한다.

## 실행

### 1) Mock (모델 없음)
```powershell
python -m pip install -r requirements.txt
python -m alembic upgrade head          # 스키마 (앱은 테이블을 자동 생성·삭제하지 않음)
python -m uvicorn aicc.main:app --port 8000      # http://127.0.0.1:8000
```

### 2) 실제 모델 (RTX 5080, Windows)
```powershell
# GPU 전용 venv (최초 1회, 수 GB 다운로드)
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe app.py --setup
.\.venv\Scripts\python.exe app.py --setup-coach          # Coach 로컬 실행 시
.\.venv\Scripts\python.exe app.py --download-only --coach local   # models/가 없을 때

# 모델 서버. Coach까지 쓰려면 ASR을 0.6B로 바꿔야 16GB GPU에서 느려지지 않는다(아래 실측 참고)
.\scripts\setup_whisperx.ps1
.\.venv\Scripts\python.exe model_server.py --offline --port 8101 --coach --diarize
# Coach 없이: model_server.py --offline --port 8101   (ASR 1.7B)

# 백엔드 (다른 터미널)
$env:PROVIDER_MODE="local"; $env:COACH_MODE="internal"   # 모델 서버를 --coach 없이 띄웠다면 off
python -m uvicorn aicc.main:app --port 8000
```
URL은 `localhost`가 아닌 `127.0.0.1`로 지정한다. Windows에서는 `localhost`가 IPv6를 먼저 시도해 요청마다 약 2초씩 지연된다.

### 2-1) 실제 마이크 자동 화자 분리 (선택)
```powershell
# pyannote/speaker-diarization-community-1 접근을 승인한 새 Hugging Face read 토큰만 현재 셸에 설정한다.
$env:HF_TOKEN = "새_토큰"
.\.venv\Scripts\python.exe app.py --setup-diarization
# 최초 모델 다운로드는 --offline 없이 실행한다. 이후 모델이 캐시되면 --offline을 붙일 수 있다.
.\.venv\Scripts\python.exe model_server.py --port 8101 --diarize
```
화자 분리는 최근 45초 음성을 메모리에만 유지해 `화자 1`, `화자 2`를 붙인다. 고객·상담원 역할은 단정하지 않으므로, 역할이 필요한 업무 판단 전에는 상담원이 매핑을 확인해야 한다. 음성 원본은 디스크·DB에 저장하지 않는다.

### 2-2) DGX Spark (Linux ARM64) 실행

DGX Spark에서는 Windows용 `app.py --setup` 및 `scripts/setup_whisperx.ps1`를 실행하지 마세요. DGX Spark는 ARM64 기반 Ubuntu 24.04와 NVIDIA Container Toolkit 환경이므로, 모델 서버는 Linux용 Python 환경에서 별도로 실행합니다.

```bash
# 0. 호스트 확인: 모두 성공한 뒤에만 다음 단계로 진행
uname -m                         # aarch64
nvidia-smi
docker --version
nvidia-ctk --version

# 1. 저장소와 백엔드 환경
git clone https://github.com/jaeukha/scgf_aicc.git
cd scgf_aicc
python3 -m venv .venv-app
. .venv-app/bin/activate
pip install -r requirements.txt
python -m alembic upgrade head
```

모델 서버는 별도 가상환경을 권장합니다. 먼저 DGX Spark에서 지원하는 Linux ARM64 CUDA PyTorch를 NVIDIA 문서 또는 사내 표준 이미지 기준으로 설치하고, `python -c "import torch; print(torch.cuda.is_available())"`가 `True`인지 확인하세요. 그 뒤 WhisperX와 프로젝트 의존성을 설치합니다.

```bash
python3 -m venv .venv-model
. .venv-model/bin/activate
pip install -U pip

# DGX Spark용 CUDA PyTorch를 먼저 설치한다. Windows/cu128 wheel을 쓰지 않는다.
# 이후 프로젝트 모델 서버 의존성 설치
pip install -r requirements.txt
pip install transformers==5.17.0 safetensors==0.8.0 pillow==12.3.0 soundfile>=0.13 \
  huggingface-hub==1.31.0 pyannote.audio==4.0.7 whisperx==3.8.6
# --coach를 쓸 때만 추가. ARM64 bitsandbytes가 지원되지 않으면 --coach 없이 시작하거나 COACH_MODE=api를 쓴다.
pip install accelerate==1.15.0 bitsandbytes==0.50.2
# WhisperX 설치가 Transformers를 낮췄다면 Decider 호환 버전으로 복구
pip install --force-reinstall --no-deps transformers==5.17.0 tokenizers==0.23.3 huggingface-hub==1.31.0

# 첫 실행은 --offline 없이 모델을 받는다. 토큰은 셸에만 설정하고 .env/커밋하지 않는다.
export HF_TOKEN='Hugging_Face_read_token'
python model_server.py --host 0.0.0.0 --port 8101 --coach --diarize
```

`0.0.0.0:8101`은 인증 없는 내부 모델 API입니다. Docker 백엔드가 필요할 때만 사설망·방화벽 범위에서 열고, 호스트 백엔드 실행에서는 `--host 127.0.0.1`을 사용하세요.

모델 서버와 백엔드를 모두 호스트에서 실행하는 가장 단순한 경로입니다.

```bash
# 별도 터미널
. .venv-app/bin/activate
export PROVIDER_MODE=local COACH_MODE=internal
export ASR_BASE_URL=http://127.0.0.1:8101
export DECIDER_BASE_URL=http://127.0.0.1:8101
export COACH_BASE_URL=http://127.0.0.1:8101
uvicorn aicc.main:app --host 0.0.0.0 --port 8000

curl http://127.0.0.1:8101/health
curl http://127.0.0.1:8000/health/ready
```

백엔드와 PostgreSQL만 Docker Compose로 실행할 때는 `.env`에 위 `*_BASE_URL`을 `http://host.docker.internal:8101`로 설정하고 다음처럼 DGX Spark 오버라이드를 함께 사용합니다.

```bash
docker compose -f docker-compose.yml -f docker-compose.dgx.yml up --build
```

운영 전에는 샘플 음성으로 ASR·화자 분리·Coach·모니터링을 모두 확인하고, `docs/dgx_spark_checklist.md`의 ARM64 의존성 및 VRAM 점검 항목을 완료하세요. DGX Spark 환경에서의 전체 모델 조합은 아직 실기기 검증 전입니다.

### 3) Docker Compose (PostgreSQL)
```powershell
copy .env.example .env
docker compose up --build          # 백엔드 + Postgres(볼륨 pgdata)
# 모델 서버를 함께 쓰려면: model_server.py --host 0.0.0.0, .env에 *_BASE_URL=http://host.docker.internal:8101
# Twenty 연동: .env에 TWENTY_ENABLED=true, TWENTY_BASE_URL=http://host.docker.internal:3000, TWENTY_API_KEY=<twenty/.env의 키>
# 주의: 이미 만든 pgdata 볼륨이 있으면 .env에 POSTGRES_PASSWORD를 새로 넣지 말 것(기존 비밀번호와 달라져 접속 실패)
```

### 테스트
```powershell
python -m pytest -q                       # Mock 프로필 E2E 9개 (SQLite 임시 DB)
python -m pytest -q -k twenty             # 단일 테스트
# Postgres에서 실행: 빈 DB를 DATABASE_URL로 지정
docker compose run --rm -e DATABASE_URL=postgresql+psycopg://aicc:aicc_dev_only@postgres:5432/aicc_test backend python -m pytest -q
```

## 사내 지식베이스 RAG

```
문서 폴더(data/kb_docs) ──색인──▶ 파싱·조각(약 700자, 섹션·페이지 유지) ──▶ embedder(EmbeddingGemma 2) ──▶ Qdrant
상담 발화 ──▶ 질의(최근 고객 발화 3개 + 파악된 주제) ──▶ embedder ──▶ Qdrant 검색(상위 4, 점수 하한·격차 필터)
         ──▶ 화면 "참고 자료"(문서·섹션·출처·유사도) + Coach 입력(문서 ID 포함)
```

```powershell
docker compose up -d qdrant embedder      # 첫 기동 때 모델 파일(약 1.4GB)을 models/hf-embed에 받음
$env:PYTHONUTF8="1"
.\.venv-app\Scripts\python.exe -m aicc.knowledge ingest data/kb_docs     # 바뀐 문서만 다시 색인, 지운 문서는 제거
.\.venv-app\Scripts\python.exe -m aicc.knowledge search "만기 연장 서류"  # 검색 확인
# 백엔드에서 사용: .env에 KNOWLEDGE_MODE=qdrant
```

- **임베딩**: `google/embeddinggemma-2`(revision `914f7f8`, Apache 2.0)의 텍스트 인코더만 쓴다(270M). 768차원이고, 문서 앞에는 `title: … | text: …`, 질의 앞에는 `task: search result | query: …`를 붙인다.
  - CPU에서 fp32로 돌린다. GPU에서는 bf16을 쓰고 fp16은 쓰지 않는다(NaN이 난다).
  - 별도 컨테이너로 둔 이유: 모델 서버의 transformers 5.17은 이 모델을 지원하지 않고(5.19부터 지원), GPU 메모리도 가득 차 있다.
- **지원 형식**: PDF(페이지), DOCX(제목 스타일 = 섹션), HWPX, Markdown/TXT(`#` 제목), CSV/XLSX(첫 행이 머리글, 한 행이 한 조각). 바이너리 `.hwp`는 HWPX나 PDF로 저장해 넣는다.
- **실측(시연 문서 8조각)**: 색인 2.8초, 발화당 검색 70ms(질의 임베딩 60ms + Qdrant 3ms). 의미 검색 상위 1위가 4개 질의 모두 맞았다(예: "재단이 대신 갚았다는 우편" → 대위변제 이후).
- **실제 문서가 오면 할 일**:
  - 조각 크기(`CHUNK_CHARS`)와 점수 기준(`KNOWLEDGE_MIN_SCORE`, `KNOWLEDGE_MAX_GAP`)을 질의 평가 세트로 조정한다.
  - 키워드가 중요한 문서(상품명, 조항 번호)가 많으면 Qdrant sparse 벡터로 하이브리드 검색을 붙인다.
  - Coach 근거 인용 검증을 붙인다.
- 실제 사내 문서는 `data/kb_docs/`에 두며, 이 폴더는 git에서 제외한다. 시연 샘플은 `data/kb_samples/`에 있다.

## 시연 시나리오
1. **시뮬레이션**: 새 상담 → 샘플 대화(만기·연장, 진행상태, 처리 지연·긴급 자금, 연체·분할상환, 신청 서류) → 전체 재생. 시연 도구에서 직접 입력과 장애 시연(`APP_ENV=local`에서만)도 할 수 있다.
2. **음성 파일**: WAV/MP3/FLAC/OGG 업로드 → 실제 ASR이 RMS 구간을 나눠 확정 발화를 순서대로 처리한다(Mock에서는 시나리오 전사).
3. **브라우저 마이크**: 녹음 시작 → 브라우저가 20ms RMS 에너지 기준으로 무음 0.7초 또는 최대 12초에서 구간을 확정하고 WAV로 업로드한다. 화자 역할은 사용자가 지정하는 값이며 자동 화자 분리가 아니다. 중간(partial) 전사는 없다.
4. **CRM**: 종료 → AI 요약(미검토) → 검토·정정, 배정, 메모, 후속 업무 → Twenty 동기화(켜져 있을 때).
5. **모니터링**: 서비스 상태(모델 서버·Twenty healthcheck 포함), 요청/실패/타임아웃, p50/p95, trace 단계별 소요 시간. 실제 HTTP 호출과 내부 단계를 구분해 보여 준다.

## 구조

| 파일 | 역할 |
|---|---|
| `aicc/main.py` | 세션 API·WebSocket·모니터링, 확정 발화 오케스트레이션(`process_utterance`), 마이크 구간 API |
| `aicc/providers.py` | Mock / HTTP Provider(ASR·Decider·Coach), 지식 검색. 라우팅은 `app.run_routing` 재사용 |
| `aicc/crm.py` | CRM API. AI 원본과 직원 확정값(`final_*`)을 분리하고 변경을 `audit_events`에 남김 |
| `aicc/twenty_sync.py` | Twenty 단방향 연계(공개 REST, 멱등 upsert, `crm_sync_links`) |
| `aicc/telemetry.py` | `span()` 공통 래퍼, 오류 코드 정규화, 상태 판정·집계 |
| `aicc/db.py`, `migrations/` | PRD §8 스키마 + 동기화 링크, 가상 시드 |
| `model_server.py` | `/internal/asr/transcribe`, `/internal/decider/evaluate`, `/internal/coach/generate`, `/health` |
| `config/routing.json` | 상담 분기 설정(시연용 기준) |
| `twenty/` | Twenty 공식 compose(v2.46.0 고정, 프로젝트명 `aicc-twenty`), 사용자 정의 객체 생성 스크립트 |
| `docs/` | Twenty PoC 결과, DGX Spark 이전 체크리스트 |

## RTX 5080 실측 (2026-10-24, TTS 한국어 16초 통화)
- **16GB GPU 모델 배치**:

  | 구성 | VRAM | Decider 1회(9개 주제) | ASR(16초 음성) |
  |---|---|---|---|
  | Decider + ASR 1.7B + Coach | 15.8GB | 11.9초 (메모리 넘침) | 1.7초 |
  | Decider + Coach, ASR 1.7B는 CPU | 12.3GB | 0.27초 | 15.9초 |
  | **Decider + Coach + ASR 0.6B (권장)** | **13.6GB** | **0.27~0.34초** | **1.8초** (4문장 모두 정확) |

  Coach(Qwen3.5-2B NF4)의 응답 형식 성공률은 샘플 22발화 기준 82%이고 1회 약 2~3초다. 고객 질문 칸에 고객의 말을 옮겨 쓰는 화자 혼동도 여전하다. 그래서 화면에서는 접힌 "AI 메모"로만 보여 준다. 형식 보정(앞뒤 설명문 제거, 쪼개진 JSON 객체 합치기, 추가 키 정리)은 `model_server.tolerant_parse`가 맡는다. 프롬프트에 규칙을 덧붙이는 시도는 성공률이 82%에서 73%로 떨어져 되돌렸다.
- **VRAM(참고)**: Decider + ASR 1.7B는 약 13.4GB다. Coach NF4까지 올리면 15.8 / 16.3GB가 되고, 주제 판정 입력이 약 1,900토큰을 넘으면 메모리가 넘쳐 **Decider 1회가 0.3초에서 11.9초로 느려진다**(9개 주제 실측). 그래서 RTX 5080에서는 Coach를 끈다(`COACH_MODE=off`). Coach가 필요하면 별도 GPU·서버(`COACH_MODE=api`)를 쓴다.
- **단계별 지연(평균)**:

| 단계 | 평균 지연 |
|---|---|
| ASR (16초 음성 파일 전체) | 1.7초 |
| Decider 호출 1회 | 0.27초 |
| 감정 추정 | 0.23초 |
| 상담 라우팅 (Decider 2~3회) | 0.5초 |
| Coach | 2.0초 |

  확정 발화 하나당 화면 갱신까지 약 3초 걸린다.
- **정확도(이 샘플 기준)**: 전사 4문장 모두 정확했다. 라우팅은 `만기·연장 → 만기 전(0.98) → 연장 상담 준비`, 서류 질문에서 `서류 문의` 주제를 추가했다.

## 기존 app.py 기능 추출표

| 기능 | 분류 | 상태 |
|---|---|---|
| 상담 라우팅, Coach 스키마, 감정 질문 | 재사용 | import해 그대로 사용 |
| Decider·ASR·Coach 런타임, Segmenter | 어댑터로 감싸기 | `model_server.py`에서 `/internal/*`로 노출 |
| 파일 재생 / 마이크 | 신규 구현 | 서버측 Segmenter / 브라우저 VAD |
| Windows WASAPI 시스템 오디오 | 범위 제외(P1) | 기존 app.py에 유지 |
| PDF/이미지 Playground, 의사결정 시연 탭 | 범위 제외 | 기존 app.py에 보존 |

## 남은 것
- **Twenty 최종 채택**: 라이선스 법무 검토, 권한·감사 플랜 확인, 폐쇄망 반입 검증(`docs/twenty_poc.md`). 담당자(Twenty 멤버) 매핑과 Twenty → AICC 웹훅은 구현하지 않았다.
- **중간 전사(partial)**: 실시간 재전사 스트리밍은 없다. 확정 구간 단위로만 처리한다.
- **모니터링 확장(P1)**: healthcheck는 조회할 때 수행하며 주기 루프는 없다. OpenTelemetry·Prometheus·Grafana는 아직 붙이지 않았다.
- **인증·권한**: 없다(`X-Employee-Id`로 작업자만 기록). 같은 망의 누구나 API·WebSocket으로 상담 내용을 볼 수 있으므로 내부망에서만 시연한다. WebSocket은 다른 출처(Origin)의 접속만 막는다.

## 설계상 결정 (QA 반영)
- **지금 할 일은 분기 설정 문구만 사용**: Coach(Qwen3.5-2B)는 화자 혼동("…알려주세요"를 고객 질문으로 제시)과 근거 없는 약속("1주일 이내 진행")을 만들어, 화면에서는 접힌 "AI 메모(사실 확인 필요)"로만 보여 준다.
- **주제 판정 기준 0.80** (`topic_min_probability`, 단계 판정은 0.70): 실제 모델 42개 대화 QA에서 오탐 주제가 0.71~0.77, 정답 최저가 0.83이었다. 놓치면 "상담 목적 확인" 질문이 나오고, 잘못 잡으면 틀린 경로가 나오므로 놓치는 쪽을 택했다.
- **분기 설정 상한**: 주제 10개, 노드 96개(`app.validate_routing_config`). 현재 9개 주제, 70개 노드(`config/routing.json`, `seoul-consultation-demo-3`).
- **개인정보 가림**: 주민·사업자등록·전화·카드·계좌번호 형식은 저장 전에 `[사업자등록번호]` 등으로 바꾼다(`main.mask_pii`).
- **DGX Spark**: 실행하지 않았다. `docs/dgx_spark_checklist.md`를 참고한다.
- **UI**: React 대신 정적 HTML 하나로 만들었다(PRD 권장안과 다름). 화면이 커지면 이전한다.
