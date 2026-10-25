# PRD.md — 서울신용보증재단 AICC 상담원 보조 시연 시스템

- 문서 버전: 0.3
- 상태: AICC 코어 + 상담 데이터 영속화 + CRM 관리 기능 포함 구현 명세
- 우선 목표: 가정용 RTX 5080 PC에서 핵심 기능 검증 후, 사무실 DGX Spark 환경에 시연용으로 배포
- 제품 유형: 상담원 보조형 AICC 웹 애플리케이션
- 구현 원칙: 로컬 우선, 모델 어댑터 분리, API 간 통신 관측 가능성 확보, 실제 고객정보 없이도 완전한 시연 가능

---

## 1. 제품 개요

### 1.1 배경

서울신용보증재단의 보증 상담 시나리오를 대상으로, 통화 중 음성인식 결과를 바탕으로 상담 주제와 진행 단계를 판단하고 상담원에게 다음 질문·확인사항·안내 문구를 실시간에 가깝게 제안하는 AICC 상담원 보조 시연 시스템을 구축한다.

초기 버전은 실제 전화교환기(CTI), 실제 고객정보, 운영 업무시스템과 연계하지 않는다. 집의 RTX 5080 개발 PC에서 음성 파일 또는 브라우저 마이크 입력과 가상 고객 데이터를 사용해 핵심 기능을 검증한다. 검증 후 사무실 DGX Spark 환경에 시연 배포한다.

### 1.2 제품 목표

1. 상담 음성을 텍스트로 변환하고 대화 내용을 화면에 표시한다.
2. 전사 내용을 기반으로 상담 주제, 현재 상담 단계, 다음 확인 질문을 구조화한다.
3. 상담원에게 다음 질문, 참고 지식, 주의사항, 권장 후속 조치를 제공한다.
4. 상담원 화면에서 통화 흐름과 AI 판단의 변경 과정을 실시간으로 확인한다.
5. 프런트엔드와 백엔드, 백엔드와 각 모델 API 사이의 요청·응답·지연·오류를 모니터링한다.
6. 상담 세션, 전사, AI 분석, 요약, 상담원 메모, 후속 업무, API trace를 PostgreSQL에 영속 저장하고 세션 종료 후에도 조회할 수 있게 한다.
7. 직원이 고객별 상담 이력과 상담 결과를 조회·보정하고 후속 조치를 배정·관리할 수 있는 CRM 화면을 제공한다.
8. Twenty CRM을 우선 검증 대상 오픈소스 CRM으로 평가하고, AICC와의 연계 가능성을 PoC로 확인한다. 상담원용 AICC 실시간 화면은 별도 React 화면으로 유지한다.
9. Twenty가 요구사항을 충족하지 못하는 경우에만 자체 React CRM과의 비교 결과를 근거로 최종 선택한다. NocoDB는 일반 상담원 CRM 대체재로 전제하지 않으며, 관리자용 데이터 도구 필요성이 확인될 때 별도 검토한다.
10. 동일한 코드베이스를 로컬 개발 PC와 DGX Spark 시연 환경에서 환경설정만 바꾸어 실행할 수 있게 한다.

### 1.3 비목표(초기 버전에서 구현하지 않음)

- 실제 전화망, SIP/SIPREC, CTI 연동
- 실제 재단 고객·보증 데이터 조회 및 업무시스템 쓰기
- 보증 승인 여부, 보증 가능 여부, 법률적 판단의 자동 확정
- 모델 학습·파인튜닝 파이프라인
- 멀티테넌시, 고가용성 클러스터, 운영 수준의 수평 자동 확장
- 실제 상담 녹음의 장기 보관
- 운영 고객정보·실제 재단 CRM/보증시스템과의 연계
- 운영 수준의 SSO/인사시스템 연계 및 복잡한 조직 권한 자동 동기화
- 초기 버전에서의 외부 인터넷 의존 서비스
- Twenty 또는 NocoDB의 내부 DB 테이블을 AICC가 직접 수정하거나, CRM 제품의 내부 스키마에 직접 업무 테이블을 추가하는 방식
- Twenty PoC 및 라이선스 검토가 끝나기 전에 CRM 제품을 최종 확정하는 방식

---


### 1.4 기존 Gradio 소스 분석 및 마이그레이션 원칙 (중요)

현재 기준 소스는 업로드된 `app.py`(약 3,245줄)이다. 코딩 에이전트는 구현 전에 실제 파일을 읽고 아래 기능과 현재 동작을 확인해야 한다. PRD와 소스가 충돌하면 기존 구현을 무조건 복사하거나 폐기하지 말고, 코어 AICC 범위와 마이그레이션 계획을 먼저 제시한다.

**기존 소스에 이미 있는 기능**
- `Qwen/Qwen3-ASR-1.7B-hf`를 Transformers 네이티브 방식으로 실행하는 ASR 런타임
- `perplexity-ai/pplx-decider-flash-v1.2`를 `pplx_decider.Decider`로 호출하는 구조화 판단 런타임
- Decider를 활용한 발화 텍스트 감정 분류 및 부정 표현 강도 점수화
- 확정 발화마다 최근 최대 30개 전사를 사용해 보증 상담 주제를 분류하고 조건 분기를 진행하는 상담 라우팅
- `Qwen/Qwen3.5-2B` 직원 안내 LLM: 로컬 NF4 4비트 실행 또는 OpenAI 호환 API 호출 옵션
- 직원 안내 JSON 파싱/검증, 취소된/오래된 생성 결과 폐기, 생성 실패 시 전사·Decider 결과 유지
- 마이크 입력, Windows WASAPI loopback 시스템 오디오 입력, 음성 파일 업로드 및 재생 입력
- RMS 에너지 기반 음성 구간 분할, 중간 전사와 확정 전사, 마지막 구간 flush 처리
- 상황 기반 의사결정 시연 탭과 일반 텍스트/PDF/이미지 기반 Decider Playground
- 모델 다운로드/로드/해제, 오프라인 모델 반입 및 SHA-256 검증, 선택적 기본 인증, JSON 결과 내보내기

**마이그레이션 방침**
- 기존 `app.py` 전체를 새 FastAPI 앱에 통째로 복사하지 않는다. 모델 로딩·전처리·추론·출력 검증·상담 라우팅을 먼저 식별하고, 순수 로직을 `providers/` 또는 `services/` 모듈로 분리한다.
- Gradio 이벤트 콜백, `gr.State`, UI HTML 렌더링, 컴포넌트 업데이트 코드는 새 React UI로 옮기지 않는다. UI와 독립적인 처리 로직만 재사용한다.
- 기존의 일반 Decision Playground(PDF/이미지 다중 질문 판정)는 AICC 코어 범위가 아니다. 기존 소스는 보존하되, 신규 AICC 웹의 첫 구현에서는 음성 상담 흐름·상담 라우팅·직원 안내에 집중한다. 해당 탭의 이식은 별도 승인 후 진행한다.
- 기존 기능의 의미를 임의로 바꾸지 않는다. 특히 Decider는 별도의 감정 전용 모델이 아니라 동일한 Decider 런타임으로 정의된 선택지/점수 질문을 판정한다.
- 모델 ID와 revision은 현재 소스의 고정값을 기본 기준으로 삼되, 설정 파일로 관리하고 배포 산출물에 실제 사용 버전을 기록한다.

## 2. 사용자와 핵심 사용 흐름

### 2.1 주요 사용자

- **상담원(시연 사용자):** 상담 전사, AI 제안, 다음 단계와 상담 요약을 확인한다.
- **시연 관리자/개발자:** 모델 연결 상태, API 지연·오류, 세션 이벤트와 로그를 확인한다.

### 2.2 대표 시나리오

1. 사용자가 웹 화면에서 새 상담 세션을 시작한다.
2. 음성 파일을 업로드하거나 브라우저 마이크 입력을 시작한다.
3. ASR 서비스가 음성을 전사한다. 지원되는 경우 중간 전사와 확정 전사를 구분한다.
4. 확정된 전사 문장을 세션 대화 기록에 추가한다.
5. Decider 어댑터가 대화 맥락을 분석해 상담 주제·단계·필요한 확인사항을 반환한다.
6. 선택적으로 업무지식 검색 어댑터가 관련 FAQ·업무지침을 검색한다. 초기 버전은 샘플 지식 또는 정적 지식 파일을 사용할 수 있다.
7. Coach LLM 어댑터가 판단 결과와 검색 근거를 입력받아 상담원용 제안을 생성한다.
8. 웹 화면이 전사, 주제, 진행 단계, 신뢰도(모델이 제공하는 경우), 다음 질문, 확인사항, 근거를 갱신한다.
9. 개발자는 모니터링 화면에서 각 API 호출의 상태와 소요시간을 확인한다.
10. 상담 종료 시 상담 요약과 이벤트 이력을 표시한다.
11. 세션·전사·AI 분석·요약·상담원 수정 내역·후속 업무를 PostgreSQL에 저장한다.
12. CRM에서 고객별 상담 타임라인, 상담 상세, 담당 직원, 메모, 후속 업무를 조회·관리한다.
13. Twenty CRM PoC 환경에서 고객·활동·업무 객체와 API 연동을 검증한다. NocoDB는 PoC 결과상 별도 데이터 관리 화면이 필요한 경우에만 선택적으로 검토한다.

### 2.3 제공할 시연용 예시

- 고객: “작년에 받은 보증이 있는데, 이제 만기가 다가와서 연장하고 싶어요.”
- 상담원: “보증 기한연장 관련 문의시군요. 성함과 사업자등록번호를 확인해 주시겠습니까?”
- 고객: “사업자등록번호는 비공개입니다.”
- AI 제안: “현재 보증 만기일 확인 → 기존 보증 이용 내역 확인 → 연장 신청 가능 시점 확인 → 필요 서류 안내”
- 안전 문구: “AI 제안은 상담 참고용이며 실제 자격·한도·승인 여부는 기존 업무시스템과 담당 직원의 확인이 필요합니다.”

---

## 3. 기능 요구사항

우선순위는 P0(코어 필수), P1(가능하면 구현), P2(후속 확장)로 구분한다.

### 3.1 상담원 웹 화면

#### FR-UI-01 — AICC 상담 화면 (P0)
- 어두운 네이비 계열의 전문적인 데스크톱 대시보드를 제공한다.
- 기본 레이아웃은 다음 3개 영역으로 구성한다.
  1. **실시간 상담 전사:** 고객/상담원 발화, 타임스탬프, 중간/확정 전사 표시
  2. **상담 주제 및 의사결정:** 상담 주제, 상담 경로, 현재 단계, 단계별 진행 상태
  3. **직원 안내:** 다음 질문, 관련 지식, 유사 사례(샘플), 주의사항, 후속 조치
- 하단 또는 상단에 세션 상태, 음성 입력 상태, API/모델 상태를 표시한다.
- 화면은 첨부된 AICC 시연 이미지의 정보 구조를 참고하되, 이미지에만 의존하지 않고 반응형 컴포넌트로 구현한다.

#### FR-UI-02 — 상담 세션 제어 (P0)
- 새 상담 시작, 일시정지/재개, 상담 종료를 제공한다.
- 입력 모드를 선택할 수 있다: 음성 파일 / 브라우저 마이크 / 시뮬레이션 대화.
- 초기 코어 검증에서 마이크 권한 또는 브라우저별 음성 입력이 불안정할 경우 음성 파일 및 시뮬레이션 모드만으로도 핵심 기능을 테스트할 수 있어야 한다.
- 세션 종료 후 요약과 이벤트 목록을 볼 수 있다.

#### FR-UI-03 — 상담 단계 시각화 (P0)
- 기본 상담 흐름 예시: `보증 상담 → 기한연장 → 만기 확인 → 필요 서류 확인 → 신청 절차 안내`.
- 현재 단계, 완료 단계, 진행 중 단계, 보류/확인 필요 단계를 구분한다.
- 상담 주제나 단계는 모델 응답으로 갱신되지만, 미확정 또는 저신뢰 결과를 확정 사실처럼 표시하지 않는다.

#### FR-UI-04 — 직원 안내 카드 (P0)
- `다음 질문`, `추가 확인사항`, `관련 지식`, `주의사항`, `권장 후속 조치`를 구분해 표시한다.
- 생성 결과가 없거나 실패하면 빈 화면 대신 “안내 생성 대기 중”, “일시적으로 안내를 생성하지 못했습니다” 등의 상태를 표시한다.
- 각 안내에 근거 문서가 있으면 문서 제목/섹션을 표시한다. 근거가 없으면 근거가 확인되지 않았음을 명시한다.

### 3.2 음성 입력과 전사

#### FR-ASR-01 — ASR 어댑터 (P0)
- ASR 모델 호출을 UI 코드와 분리하고 내부 `ASRProvider` 인터페이스로 추상화한다.
- 최소 인터페이스:
  - `transcribe_file(...)`
  - `transcribe_chunk(...)` 또는 동등한 청크 처리 인터페이스
  - `healthcheck()`
- 기존 구현은 짧은 오디오 청크를 반복 재전사하는 `chunked_retranscription` 방식이다. 중간 전사는 수정될 수 있고, 무음 감지·최대 발화 길이·녹음 중지 시 확정한다. 진정한 프레임/토큰 단위 스트리밍이라고 표현하지 않는다.
- 기존 기본값과 동작을 우선 재사용한다: 약 0.5초 단위 입력 처리, 설정 가능한 RMS 에너지 기반 구간 분할, 중간 전사 간격 기본 약 1.2초(최소값은 구현 검토), 마지막 오디오 구간 flush. 수치는 실제 코드의 Segmenter와 UI 설정을 확인해 별도 설정값으로 만든다.
- GPU 추론 작업은 입력 수집과 분리하고, 중간 전사는 대기열에 최신 snapshot 하나만 유지한다. 확정 발화는 순서 보존을 우선하며, 대기열 한도 초과 시 녹음을 명시적으로 중단하고 화면에 이유를 표시한다.
- 모델을 사용할 수 없는 개발 환경에서도 샘플 전사 결과를 재생하는 Mock ASR 모드를 제공한다.
- 허용 파일 형식과 최대 파일 크기는 환경설정으로 지정한다. 기본 권장 형식은 WAV이며, 지원하지 않는 코덱은 명확한 오류로 반환한다.

#### FR-ASR-02 — 전사 이벤트 (P0)
- 전사 결과는 `partial` 또는 `final` 유형을 가진다.
- 각 이벤트에 세션 ID, 이벤트 ID, 생성 시각, 화자(확인된 경우), 텍스트, 처리 상태를 포함한다.
- 화자 분리 모델이 실제로 연결되지 않은 경우 화자를 임의로 추정하지 않는다. 파일 입력에서 화자를 알 수 없으면 `unknown` 또는 단일 입력으로 표시한다.
- 원본 음성은 기본 설정에서 영구 저장하지 않는다.

#### FR-AUDIO-03 — 입력 소스 및 파일 재생 (P0/P1)
- P0: 브라우저 마이크 또는 사전 녹음 파일 중 최소 하나로 전체 코어 흐름을 테스트할 수 있어야 한다. 개발 초기는 파일 입력과 시뮬레이션을 필수로 하고, 마이크는 환경별 검증 후 활성화한다.
- P1: Windows 개발 PC에서는 기존 소스의 WASAPI loopback 기반 시스템 오디오 입력을 별도 옵션으로 재구현할 수 있다. 이는 Windows 전용이며 DGX Spark/Linux로 그대로 이식할 수 있다고 가정하지 않는다.
- 기존 파일 재생은 30분/250MB 이내, 8–192kHz, 1–8채널 검증 및 0.5초 단위 입력 방식이다. 새 웹에서는 업로드 크기/길이/코덱을 환경설정으로 관리하고 서버 자원 보호를 위해 검증한다.
- 빠른 분석과 실시간 속도 재생 모드를 둘 수 있다. 빠른 분석에서도 확정 전사 순서와 마지막 구간 처리를 보장한다.
- 화자 분리 기능은 기존 소스에 없다. 현재 `speaker_role`은 사용자가 지정하는 역할 메타데이터일 뿐 자동 화자분리 결과가 아니다. 자동 분리 없이 고객/상담원 화자를 추정해 표시하지 않는다.

### 3.3 상담 판단(Decider)

#### FR-DEC-01 — Decider 어댑터 (P0)
- Decider 호출을 별도 서비스 어댑터로 분리한다.
- 내부 공통 입력은 상담 세션 ID, 최근 전사 문장, 필요한 대화 맥락, 선택 가능한 상담 주제/단계 목록으로 구성한다.
- 기존 상담 라우팅은 고정된 JSON 설정(`ROUTING_CONFIG`)을 기반으로 주제를 분류한 뒤 선택된 주제의 노드를 단계적으로 판정한다. 현재 샘플 주제는 보증 신청, 만기·연장, 서류 문의, 진행상태 문의이며 업무 기준이 아닌 시연용 초안이다.
- 기존 기본 판정 기준은 `min_probability=0.70`, `min_margin=0.15`다. 이를 재단 공식 업무 기준으로 표현하지 말고 설정값으로 관리하며, 불확실할 때는 다음 확인 질문/직원 확인 상태로 멈춘다.
- 기존 상담 라우팅 내부 공통 출력 스키마:
  - `topic`: 상담 주제
  - `stage`: 현재 단계
  - `next_step`: 다음 단계 또는 행동
  - `questions`: 추가 확인 질문 배열
  - `required_checks`: 확인사항 배열
  - `confidence`: 모델이 실제로 제공하는 경우에만 기록
  - `needs_human_review`: 직원 확인 필요 여부
  - `reasoning_summary`: 내부적으로 허용된 짧은 판단 근거. 민감한 내부 추론 전문을 요구하지 않음.
- 모델 출력의 스키마를 검증하고, 파싱 실패·타임아웃·저신뢰 응답을 안전하게 처리한다.
- Mock Decider 모드를 제공한다.
- 기존 음성 파이프라인은 같은 Decider 모델에 `emotion` 선택 질문과 `negative_intensity` 점수 질문을 전달한다. 감정은 텍스트에 표현된 감정이며 음성 억양·목소리 특성 분석이 아니다. 이 기능을 신규 UI에 유지할 경우 `텍스트 기반 감정 추정`으로 표시하고, 업무상 판단 근거로 과신하지 않도록 한다.
- 모델이 확률이나 신뢰도를 제공하지 않으면 임의의 점수를 만들어 표시하지 않는다.

### 3.4 업무지식 검색

#### FR-KNOW-01 — 지식 제공 인터페이스 (P0)
- `KnowledgeProvider` 인터페이스를 정의한다.
- 초기 버전은 로컬 Markdown/JSON 파일 기반의 간단한 키워드 검색 또는 정적 샘플 응답으로 시작할 수 있다.
- 향후 벡터 검색/RAG 서비스로 교체할 수 있도록 UI와 Coach 로직에서 검색 구현 세부사항을 분리한다.
- 검색 결과에 `document_id`, `title`, `section`, `snippet`, `source`를 포함한다.
- 검색 결과가 없으면 “관련 근거를 찾지 못함”을 반환한다. 존재하지 않는 규정·조항·문서명을 생성하지 않는다.

### 3.5 상담원 안내(LLM Coach)

#### FR-COACH-01 — 상담 안내 생성 (P0)
- Coach LLM 호출을 `CoachProvider` 인터페이스로 분리한다.
- 입력: 최근 확정 전사(기본 최대 30개를 라우팅에 사용), 최신 Decider 상담 경로/추가 질문/조치 후보, 관련 지식 검색 결과(있는 경우), 출력 스키마/안전 지침.
- 기존 Coach 프롬프트와 호환되는 초기 출력 JSON을 우선 지원한다:
  - `staff_note`: 직원용 설명
  - `customer_question`: 고객에게 할 다음 질문(없으면 빈 문자열)
  - `action_items[]`: 최대 2개의 후속 조치 후보
- 이후 UI가 필요로 하는 `required_checks`, `cautions`, `knowledge_references` 등은 별도 필드로 점진 확장한다. 기존 모델이 출력하지 않는 필드를 있다고 가정하지 않는다.
- 현재 코드의 직원 안내 기본값은 `--coach off`이며, `local`은 Qwen3.5-2B NF4 4비트 로컬 추론, `api`는 OpenAI 호환 `/chat/completions` 호출이다. 신규 서비스는 `off/mock/local/api` 모드를 명시적으로 지원한다.
- 기존 구현은 안내 요청마다 generation/version을 관리해 오래된 결과를 취소/폐기한다. 신규 구현도 확정 발화가 빠르게 연속될 때 오래된 안내가 최신 안내를 덮어쓰지 않도록 해야 한다.
- 가능하면 JSON Schema 또는 Pydantic 기반으로 결과를 검증한다.
- 생성 실패 시 제한된 재시도를 수행하고, 실패가 계속되면 사용자에게 오류 상태를 표시한다.
- 모델이 업무 규정에 없는 내용을 확정적으로 말하지 않도록 한다.
- 실제 고객에게 자동 음성으로 응답하지 않는다. 모든 생성 결과는 상담원 참고용이다.

### 3.6 상담 기록과 요약

#### 기존 소스의 상태 관리 제약
- 현재 Gradio 앱은 세션과 전사 이력을 주로 프로세스 메모리에 보관하고, 사용자가 선택하면 JSON 파일로 내보낸다. 영속 DB/복구 기능은 구현되어 있지 않다.
- 신규 웹에서는 MVP부터 세션·이벤트 저장을 명시적으로 구현하되, 모델이 실행 중인 프로세스 메모리와 기록 저장소를 분리한다. 세션 만료/삭제 정책을 정의한다. PostgreSQL을 기본 영속 저장소로 사용하고, DB 마이그레이션 및 시드 데이터를 함께 제공한다.
- 기존 소스는 확정 전사 최대 500개를 메모리에 유지하고 화면에는 최근 일부를 보여준다. 새 시스템에서는 보관 개수/기간을 설정화하고 UI 표시 제한과 서버 보존 정책을 분리한다.

#### FR-SESSION-01 — 세션 이벤트 기록 (P0)
- 세션 시작/종료, 전사 이벤트, Decider 결과, 지식 검색, Coach 생성, 오류를 공통 이벤트 형식으로 기록한다.
- 개발 편의를 위해 초기에는 SQLite 또는 PostgreSQL 중 하나를 선택한다. 기본 로컬 프로필은 SQLite로 시작해도 되며, DGX Spark 시연 프로필에서 PostgreSQL이 필요하면 환경변수로 전환한다.
- 원본 음성 저장은 기본 비활성화한다.
- 로그에는 고객 식별정보, 사업자등록번호, 전화번호, 인증정보, 원문 음성을 기록하지 않는다. 시연용 가상 데이터만 사용한다.

#### FR-SESSION-02 — 상담 요약 (P1)
- 상담 종료 시 확인된 주제, 주요 문의, 안내한 내용, 미완료 확인사항, 후속 조치를 요약한다.
- 요약은 AI 생성 결과임을 표시하고 상담원이 수정할 수 있게 한다.
- 상담 요약 실패가 상담 세션 종료를 막지 않아야 한다.

### 3.7 상담 데이터 저장 및 CRM

#### FR-DATA-01 — PostgreSQL 영속 저장 (P0)
- PostgreSQL을 상담·고객·직원·후속 업무 데이터의 기준 저장소(source of truth)로 사용한다. SQLite는 RTX 5080 초기 단독 실험에서만 선택적으로 허용하며, CRM/NocoDB 연계 및 DGX Spark 시연 프로필은 PostgreSQL을 기본값으로 한다.
- 상담 시작 시 세션 레코드를 생성하고, 확정 전사·Decider 결과·Coach 제안·요약·상담원 수정·후속 업무·trace를 발생 시점에 저장한다. 세션 종료 때 한 번에만 저장하는 구조로 만들지 않는다.
- `partial` 전사는 기본적으로 영속 저장하지 않는다. 필요 시 최신 중간 전사만 임시 상태로 관리하며, 확정 전사와 중복 저장하지 않는다.
- 저장 실패 시 사용자에게 저장 상태를 알리고, 메모리 상태와 DB 상태가 불일치한 채 조용히 성공 처리하지 않는다. 핵심 상담 이벤트는 재시도 가능한 저장 경로를 사용한다.
- 모든 레코드는 생성·수정 시각을 가지며, 필요한 엔티티는 생성자/수정자 필드를 포함한다. AI 초안과 직원이 확인·수정한 값을 분리 보존한다.

#### FR-CRM-01 — CRM 대시보드 (P0)
- 별도 React CRM 화면에서 최근 상담 목록, 상담 상태, 상담 주제, 담당 직원, 시작·종료 시각, 후속 조치 상태를 조회한다.
- 목록 검색·필터: 기간, 상담 상태, 상담 주제, 담당 직원, 후속 업무 상태. 정렬·페이지네이션을 지원한다.
- 상담 세션을 선택하면 상세 페이지에서 상담 타임라인, 확정 전사, AI 분석 결과, AI 요약, 직원 확정 요약, 메모, 후속 업무, 오류/처리 이력을 확인한다.
- 기본 화면은 가상 고객 데이터만 사용하며, 식별정보는 필요한 최소 필드만 표시한다.

#### FR-CRM-02 — 고객 및 상담 이력 (P0)
- 시연용 고객 레코드와 고객별 상담 이력을 연결할 수 있다. 고객 식별자는 내부 UUID를 사용하고, 실명·전화번호·사업자등록번호 등 실제 식별정보를 요구하지 않는다.
- 동일 고객의 여러 상담 세션을 시간순 타임라인으로 제공한다. 고객 레코드가 연결되지 않은 세션도 독립적으로 조회 가능해야 한다.
- 고객 메모와 상담별 메모를 구분한다. 메모 작성자와 작성 시각을 기록한다.

#### FR-CRM-03 — 직원 및 담당 배정 (P0)
- 시연용 직원 목록을 관리하고 상담 세션과 후속 업무에 담당 직원을 연결한다. 직원 정보는 내부 UUID, 표시명, 부서/팀, 활성 여부 정도로 제한한다.
- 상담 담당자 변경 및 후속 업무 담당자 변경 이력을 기록한다.
- 초기 버전은 별도 인사시스템 연동 없이 가상 직원 시드 데이터를 사용한다.

#### FR-CRM-04 — 후속 업무 관리 (P0)
- 상담원은 상담에서 후속 업무를 생성·수정·완료 처리할 수 있다. 필드: 제목, 설명, 담당자, 기한, 우선순위, 상태, 연결 상담 세션, 생성 출처(`ai_suggested` 또는 `manual`).
- Coach가 제안한 `action_items`는 자동으로 확정 업무가 되지 않는다. 상담원이 선택·확인한 경우에만 후속 업무 레코드로 생성한다.
- 상태는 `open`, `in_progress`, `blocked`, `completed`, `cancelled`로 관리하고, 기한 초과 항목을 별도 표시한다.

#### FR-CRM-05 — AI 결과의 검토 및 정정 (P0)
- AI가 생성한 상담 주제·요약·후속 조치와 직원이 확인·수정한 최종 값을 분리 저장한다. 직원 수정은 AI 원본 결과를 덮어쓰지 않는다.
- 상담원은 상담 주제, 요약, 메모, 후속 조치를 보정할 수 있다. 정정 전후 값, 수정자, 수정 시각을 감사 이력으로 남긴다.
- 직원이 확인하지 않은 AI 분석은 `unreviewed` 등 상태로 명시하며, AI 결과를 공식 업무 판단으로 표시하지 않는다.

#### FR-CRM-06 — Twenty CRM 연계 PoC (P0 검증 게이트, 제품 확정은 PoC 후)
- Twenty를 우선 검증 대상 CRM으로 사용한다. 공식 프로젝트는 자체 호스팅, 사용자 정의 객체/필드, 관계형 데이터, 활동·작업 관리 및 REST/GraphQL API를 제공한다고 설명한다. 실제 배포 버전에서 필요한 기능과 API를 직접 확인한다.
- PoC에서는 가상 고객·직원·상담·후속 업무 데이터를 사용한다. Twenty의 기본 People/Companies/Tasks/Notes 등 객체와 사용자 정의 객체/필드·관계를 조사해 상담 도메인을 어느 수준까지 자연스럽게 표현할 수 있는지 기록한다.
- AICC는 상담 세션·전사 이벤트·AI 분석·모델 추적의 원본 데이터를 자체 PostgreSQL 스키마에서 관리한다. Twenty 내부 테이블에 직접 SQL로 쓰거나 Twenty 내부 스키마에 AICC 전용 테이블을 임의로 추가하지 않는다.
- AICC와 Twenty 간 데이터 교환은 공식적으로 지원되는 API/webhook 등 공개 인터페이스를 우선 사용한다. 실제 API 인증 방식, 객체 생성·수정, 페이지네이션, 제한, webhook 재시도/중복 처리 동작을 PoC에서 확인한다.
- PoC 비교 기준: 고객/상담 이력 표현력, 사용자 정의 객체, 후속 업무 배정·기한·상태, API 쓰기/조회, 화면 커스터마이징, 권한, 감사 이력, 자체 호스팅, 백업/복구, 폐쇄망 배포, 라이선스 적합성, 유지보수 부담.
- Twenty를 선택하면 AICC 원본 상담 데이터와 CRM 데이터의 소유권·동기화 방향·재시도·중복 방지 정책을 문서화한다. 양쪽에서 같은 필드를 무분별하게 수정하는 양방향 동기화는 피한다.
- Twenty가 요구사항을 충족하지 못하면 동일한 평가표로 자체 React CRM 방안과 비교한다. PoC 종료 전에는 Twenty를 최종 아키텍처의 필수 런타임으로 간주하지 않는다.
- Twenty 저장소는 대부분 AGPLv3 기반이며 일부 패키지/파일에 MIT 또는 상용 라이선스가 적용될 수 있다. 사용할 버전과 기능별 라이선스를 확인하고, 공공기관의 배포·수정·사내 이용 조건에 대해 법무/라이선스 검토를 거친다. SSO, 행 단위 권한, 감사 로그 등은 특정 플랜에 포함된다고 가정하지 않는다.

#### FR-NOCODB-01 — NocoDB 선택적 관리자 도구 (P2, 보류)
- NocoDB는 초기 MVP의 필수 구성요소가 아니다. 개발자/관리자의 표 형식 데이터 점검 요구가 실제로 확인될 때만 별도 도입을 검토한다.
- 도입 시에도 CRM 대체재나 핵심 업무 규칙 엔진으로 사용하지 않는다. FastAPI가 데이터 검증·상태 전이·감사 필수 업무 규칙을 담당한다.
- NocoDB가 DB에 직접 접근하는 경우 별도 최소 권한 계정과 허용 테이블을 사용하고, 시연용 데이터만 연결한다. 감사 이력이 필요한 테이블의 직접 수정은 제한한다.
- 버전·라이선스·역할/행 단위 권한·감사로그·폐쇄망 배포 가능 여부를 확인하며, 필요한 보안 기능이 무료/커뮤니티 플랜에 포함된다고 가정하지 않는다.

---

## 4. API 간 통신 모니터링 요구사항

이 시스템의 핵심 요구사항이다. 단순히 모델 서버의 CPU/GPU 사용률만 보는 것이 아니라, **프런트엔드 → 백엔드 → ASR/Decider/Knowledge/Coach API 간 호출 흐름을 요청 단위로 추적**할 수 있어야 한다.

### 4.1 모니터링 범위

모니터링 대상:
1. 브라우저 ↔ 백엔드 REST API
2. 브라우저 ↔ 백엔드 WebSocket
3. 백엔드 ↔ ASR API
4. 백엔드 ↔ Decider API
5. 백엔드 ↔ Knowledge API
6. 백엔드 ↔ Coach LLM API
7. 백엔드 ↔ 데이터베이스

현재 소스에서는 ASR과 Decider가 같은 Python 프로세스에서 실행되고 Coach만 로컬 추론 또는 선택적 OpenAI 호환 HTTP API로 실행될 수 있다. 따라서 모니터링은 **물리적 HTTP 호출**과 **동일 프로세스 내부의 논리적 추론 단계**를 구분해야 한다. 로컬 ASR/Decider 호출을 실제 HTTP API 호출인 것처럼 표시하지 않는다. 모델을 별도 서비스로 분리한 경우에만 해당 구간을 upstream HTTP 호출로 기록한다.

### 4.2 API 호출 추적 (P0)

각 내부 API 호출마다 다음 필드를 기록한다.

- `trace_id`: 한 요청 흐름 전체를 식별
- `span_id`: 개별 API 호출 식별
- `parent_span_id`: 상위 호출 식별
- `session_id`: 상담 세션 ID(임의 UUID)
- `service_name`: 호출 서비스
- `operation`: 작업명(예: `asr.transcribe`, `decider.evaluate`)
- `provider`: 모델/제공자 식별자(비밀정보 제외)
- `started_at`, `ended_at`, `duration_ms`
- `status`: `success`, `error`, `timeout`, `cancelled`
- `http_status_code`: 해당하는 경우
- `error_code`: 정규화된 오류 코드
- `retry_count`
- `request_size_bytes`, `response_size_bytes`(측정 가능한 경우)
- `queue_wait_ms`(큐가 구현된 경우)
- `model_name` 및 `model_version`(설정된 경우)

요청/응답 본문 전체를 기본 로그에 남기지 않는다. 특히 전사 텍스트, 고객 개인정보, API 키, 인증 헤더는 수집하지 않는다. 디버그 모드에서 본문 로깅을 추가하더라도 기본 비활성화하고 민감정보 마스킹을 강제한다.

### 4.3 모니터링 화면 (P0)

개발자/관리자용 `시스템 모니터링` 화면을 제공한다.

- **서비스 상태 카드:** Backend, ASR, Decider, Knowledge, Coach, DB의 `healthy/degraded/unavailable/unknown` 상태
- **API 호출 현황:** 최근 1분/5분/세션별 요청 수, 성공 수, 실패 수, 타임아웃 수
- **지연시간:** 평균 및 p50/p95 지연시간. 표본이 부족하면 “데이터 부족” 표시
- **오류 현황:** 서비스, 작업명, 오류 코드, 발생 시각, trace ID, 재시도 횟수
- **요청 흐름 상세:** 하나의 상담 이벤트가 어느 API를 거쳤는지 시간순으로 표시하고, 각 구간의 소요시간과 결과를 보여준다.
- **실시간 업데이트:** WebSocket 또는 짧은 주기의 폴링으로 갱신한다. 업데이트가 중단되면 마지막 갱신 시각을 표시한다.
- **필터:** 시간 범위, 서비스, 상태, 세션 ID, trace ID
- **상세 보기:** 오류 메시지는 내부 구현 정보가 노출되지 않도록 정리하되 개발 모드에서는 안전한 범위의 예외 정보를 확인할 수 있다.

### 4.4 관측성 구현 (P0/P1)

P0:
- 백엔드의 구조화 JSON 로그
- 요청별 `trace_id` 생성 및 하위 API로 전파
- 모든 내부 API 호출에 공통 타이밍/오류 래퍼 적용
- `/health/live`, `/health/ready` 및 서비스별 healthcheck
- 모니터링 UI를 위한 내부 metrics API
- API 요청 수, 오류 수, 지연시간, 타임아웃 수 집계

P1:
- OpenTelemetry SDK 기반 trace/span 계측
- Prometheus metrics endpoint
- Grafana 대시보드
- OpenTelemetry Collector 및 trace 저장소(예: Jaeger 또는 Tempo)
- 컨테이너 로그 수집·검색 시스템

초기 코어 검증은 무거운 모니터링 스택을 한 번에 설치하지 않고, 구조화 로그 + 백엔드 집계 + UI로 완성한다. 다만 이후 OpenTelemetry/Prometheus/Grafana를 붙일 수 있도록 계측 코드를 서비스 로직과 분리한다.

### 4.5 API 통신 상태 모델

- `healthy`: 최근 healthcheck 및 호출 성공
- `degraded`: 서비스는 응답하지만 오류율/지연이 기준치를 넘거나 일부 기능이 실패
- `unavailable`: healthcheck 실패 또는 연속 호출 실패
- `unknown`: 아직 확인하지 않았거나 측정 데이터가 없음

상태 판정 기준은 환경변수로 설정 가능하게 한다. 예시 기본값:
- 내부 API 타임아웃: 서비스별 설정 가능, 기본 30초
- healthcheck 주기: 10초
- 연속 healthcheck 실패 3회 시 `unavailable`
- API 오류율/지연 경고 임계치는 시연 중 관측 후 조정 가능

임계치는 제품의 절대 성능 보장치가 아니라 초기 시연을 위한 기본 설정이다.

### 4.6 오류 코드 표준화

최소 오류 코드:
- `UPSTREAM_TIMEOUT`
- `UPSTREAM_UNAVAILABLE`
- `UPSTREAM_BAD_RESPONSE`
- `SCHEMA_VALIDATION_FAILED`
- `RATE_LIMITED`
- `REQUEST_CANCELLED`
- `AUDIO_FORMAT_UNSUPPORTED`
- `AUDIO_TOO_LARGE`
- `SESSION_NOT_FOUND`
- `INTERNAL_ERROR`

각 오류 응답은 `trace_id`, `error_code`, 안전한 사용자 메시지를 포함한다. 내부 스택 트레이스나 환경변수·키·내부 파일 경로는 브라우저에 노출하지 않는다.

### 4.7 모니터링 자체의 신뢰성 (P1)
- 모니터링 기능의 오류가 상담 기능을 중단시키지 않도록 한다.
- metrics 기록 실패는 상담 요청 실패로 전파하지 않는다.
- trace ID가 없는 요청은 백엔드에서 새로 생성한다.
- 시계는 UTC로 저장하고 UI에서는 브라우저 현지 시간으로 표시한다.
- 시스템 모니터링 API는 초기 시연 환경에서 관리자 화면으로만 노출하고, 향후 인증·권한 제어를 추가할 수 있도록 분리한다.

---

## 5. 아키텍처 요구사항

### 5.1 논리 아키텍처

```text
┌─────────────────────────────────────────────────────────────┐
│ Browser                                                     │
│ AICC 상담원 UI | CRM(Twenty PoC 또는 자체 UI) | 모니터링 UI │
└───────────────────────┬─────────────────────────────────────┘
                        │ REST + WebSocket
┌───────────────────────▼─────────────────────────────────────┐
│ Backend API (FastAPI)                                      │
│ Session Manager | Orchestrator | CRM Services | Telemetry   │
└───────┬──────────────┬──────────────┬──────────────┬────────┘
        │              │              │              │
   ┌────▼────┐    ┌────▼────┐    ┌────▼────┐    ┌────▼─────┐
   │ ASR API │    │ Decider │    │Knowledge│    │ Coach LLM│
   │ Adapter │    │ Adapter │    │ Provider│    │ Adapter  │
   └─────────┘    └─────────┘    └─────────┘    └──────────┘
        │              │              │              │
        └──────────────┴──────┬───────┴──────────────┘
                              │
                   ┌────────────────────┐       REST/GraphQL API
                   │ PostgreSQL         │                 │
                   │ AICC source data   │       ┌─────────▼────────┐
                   │ Session/Event/AI   │       │ Twenty CRM PoC   │
                   │ results/trace      │       │ Customers/Tasks  │
                   └────────────────────┘       └──────────────────┘

                   NocoDB는 필요성 확인 후 별도 선택적으로 검토
```

### 5.2 권장 기술 스택

- 프런트엔드: React + TypeScript + Vite
- UI: Tailwind CSS 또는 프로젝트에 적합한 컴포넌트 라이브러리
- 백엔드: Python 3.11+ / FastAPI / Pydantic
- 실시간 전송: WebSocket
- HTTP 클라이언트: `httpx` 비동기 클라이언트
- 데이터 저장: PostgreSQL 기본(세션·CRM·이벤트·trace); SQLite는 초기 단독 실험 프로필에 한정
- DB 스키마 변경: Alembic 등 버전 관리형 마이그레이션 도구를 사용하고 자동 시작 시 임의의 스키마 파괴/초기화를 금지
- CRM 우선 검증 대상: Twenty 자체 호스팅 PoC(공식 API/webhook 사용 가능 여부, 라이선스·권한·폐쇄망 배포 확인)
- CRM 대안: PoC 결과가 미흡할 경우 React + TypeScript 기반 자체 CRM 화면, FastAPI를 통해 읽기/쓰기
- NocoDB: P2 선택 사항. 관리자용 표 기반 데이터 점검 필요성이 확인된 경우에만 라이선스·권한 검증 후 도입
- 테스트: pytest, HTTPX TestClient 또는 동등 도구, 프런트엔드 단위 테스트
- 컨테이너: Docker 및 Docker Compose
- 로깅: Python 표준 logging 또는 structlog 기반 JSON 로그
- 관측성 확장: OpenTelemetry + Prometheus + Grafana

기술 선택은 코딩 에이전트가 기존 저장소를 먼저 확인한 후 조정할 수 있다. 기존 코드가 있다면 전체를 무조건 재작성하지 말고 기존 모델 호출부를 어댑터로 이동하는 최소 변경을 우선한다.

### 5.3 서비스 분리 원칙

- UI는 모델 서버를 직접 호출하지 않는다.
- 백엔드가 모든 모델/지식 API 호출을 오케스트레이션한다.
- 각 Provider는 공통 인터페이스를 구현한다.
- 실제 모델 연결과 Mock Provider를 환경설정으로 선택한다.
- 로컬 PC와 DGX Spark에서 서비스 URL, 모델 이름, 타임아웃, 저장소 설정만 변경하면 되도록 한다.
- 모델 서버의 구체적 API 형식이 다르면 어댑터 안에서 변환한다. 모든 모델이 OpenAI 호환 API를 제공한다고 가정하지 않는다.
- 모델을 로컬에서 프로세스로 실행하는 방식과 별도 HTTP 서버로 호출하는 방식을 모두 어댑터로 수용하되, 우선 구현은 HTTP API 방식과 Mock 방식에 집중한다.

---

## 6. 배포 단계와 환경별 요구사항

### 6.1 단계 A — 집의 RTX 5080 개발 환경

목표: 핵심 사용자 흐름과 모델 연동, API 모니터링을 검증한다.

- 단일 개발 PC에서 Docker Compose로 실행할 수 있어야 한다.
- 현재 소스 기준 모델은 Decider `perplexity-ai/pplx-decider-flash-v1.2`, ASR `Qwen/Qwen3-ASR-1.7B-hf`, 선택적 Coach `Qwen/Qwen3.5-2B`다. 현재 앱은 Windows 11 / Python 3.12 x64 / Gradio 6.30.0 / Transformers 5.17.0 / CUDA 13.0용 PyTorch 조합을 전제로 작성되어 있다. 이는 RTX 5080 개발 환경의 현 상태이지 DGX Spark에 그대로 적용 가능한 의존성 목록이 아니다.
- 서비스별 GPU 사용량을 고려해 모델을 한 번에 모두 로드하는 것을 기본 전제로 삼지 않는다.
- ASR, Decider, Coach 모델을 별도 프로세스/서비스로 실행하거나 외부 로컬 모델 서버에 연결할 수 있어야 한다.
- 모델 VRAM 요구량과 동시 로딩 가능 여부는 실제 모델 크기·정밀도·엔진 설정에 따라 달라지므로 설정과 실행 로그로 확인한다.
- Mock 모드만으로도 UI, 세션 흐름, API 추적, 오류 처리를 모두 테스트할 수 있어야 한다.
- 기본 실행 명령과 환경변수 예제를 README에 제공한다.

### 6.2 단계 B — 사무실 DGX Spark 시연 환경

목표: RTX 5080에서 검증된 코어 기능을 시연 환경으로 옮긴다.

- 같은 저장소와 컨테이너 이미지를 사용하고 환경변수로 엔드포인트와 모델 설정을 변경한다.
- DGX Spark의 실제 OS/아키텍처(ARM64 여부 포함), CUDA/드라이버, 컨테이너 런타임, GPU 메모리, 네트워크 정책을 먼저 확인한 뒤 모델 서빙 방식을 결정한다.
- 현재 소스의 Windows x64 wheelhouse/오프라인 번들은 Linux/ARM64 환경에서 그대로 재사용할 수 없다. DGX Spark 대상 의존성과 모델 아티팩트는 대상 플랫폼에 맞춰 별도 준비·검증해야 한다.
- 우선순위는 기존 모델을 반드시 같은 프로세스에서 실행하는 것이 아니라, 기존 로직과 모델 버전을 유지하면서 GPU/런타임별 Provider를 선택할 수 있게 하는 것이다.
- DGX Spark가 제공하는 GPU 메모리와 성능을 확인하기 전 특정 모델 동시 구동이나 처리량을 보장하지 않는다.
- 모델별 엔드포인트와 healthcheck를 분리한다.
- 데모용 가상 데이터만 사용한다.
- 외부 인터넷이 차단되거나 제한된 환경에서도 필요한 이미지·패키지를 사전에 반입할 수 있도록 컨테이너 이미지와 의존성을 고정한다.
- 시연 접속은 승인된 사내 네트워크 범위로 제한하고, 관리자 모니터링 화면도 접근 통제를 고려한다.
- 시연 전 모델 서버를 사용할 수 없는 경우 Mock 모드로 전환할 수 있어야 한다.

### 6.3 설정 프로필

다음 프로필을 제공한다.

- `mock`: 모든 모델 호출을 Mock Provider로 처리
- `local`: 로컬 모델 API를 호출
- `demo`: DGX Spark 또는 사내 시연용 모델 API를 호출

`.env.example`에 다음과 같은 설정을 문서화한다(실제 값은 예시/placeholder만 사용).

```dotenv
APP_ENV=local
PROVIDER_MODE=mock
BACKEND_HOST=0.0.0.0
BACKEND_PORT=8000
FRONTEND_PORT=5173

ASR_BASE_URL=http://localhost:8101
ASR_TIMEOUT_SECONDS=30
ASR_MODEL_NAME=local-asr

DECIDER_BASE_URL=http://localhost:8102
DECIDER_TIMEOUT_SECONDS=30
DECIDER_MODEL_NAME=local-decider

KNOWLEDGE_MODE=local
KNOWLEDGE_PATH=./data/knowledge

COACH_BASE_URL=http://localhost:8103
COACH_TIMEOUT_SECONDS=45
COACH_MODEL_NAME=local-coach

DATABASE_URL=postgresql+psycopg://aicc:aicc_dev_only@postgres:5432/aicc
# SQLite를 사용하는 단독 실험 프로필 예: sqlite:///./data/aicc.db
POSTGRES_DB=aicc
POSTGRES_USER=aicc
POSTGRES_PASSWORD=change-me
CRM_MODE=twenty_poc
TWENTY_BASE_URL=http://twenty:3000
TWENTY_API_KEY=
TWENTY_ENABLED=false
NOCODB_ENABLED=false
NOCODB_PORT=8080  # 선택적 도구이며 기본 비활성화
HEALTHCHECK_INTERVAL_SECONDS=10
LOG_LEVEL=INFO
ENABLE_OTEL=false
OTEL_EXPORTER_OTLP_ENDPOINT=
```

주의: `localhost`는 각 컨테이너 내부를 가리킬 수 있으므로 Docker Compose 네트워크에서 서비스 간 통신 시에는 실제 서비스 이름을 사용하도록 문서화한다. API 키 등 비밀정보는 저장소에 커밋하지 않고 환경변수 또는 별도 secret으로 주입한다.

---

## 7. API 초안

경로와 필드명은 구현 과정에서 일관되게 유지한다.

### 7.1 세션 API

- `POST /api/v1/sessions`
  - 상담 세션 생성
  - 응답: `session_id`, `created_at`, `status`

- `GET /api/v1/sessions/{session_id}`
  - 세션 상태와 현재 상담 결과 조회

- `POST /api/v1/sessions/{session_id}/audio`
  - 음성 파일 업로드 및 처리 시작
  - 업로드 크기·형식 검증 필수

- `POST /api/v1/sessions/{session_id}/simulate`
  - 시연용 발화 텍스트를 주입해 판단·안내 흐름 테스트

- `WS /api/v1/sessions/{session_id}/stream`
  - 전사·판단·안내 이벤트 수신
  - 음성 청크 업로드를 지원하는 경우 메시지 프로토콜을 문서화

- `POST /api/v1/sessions/{session_id}/end`
  - 세션 종료 및 요약 생성 요청

- `GET /api/v1/sessions/{session_id}/events`
  - 세션 이벤트 이력 조회

### 7.2 CRM 및 데이터 API

- `GET /api/v1/crm/dashboard` — 상담 건수, 진행 중 후속 업무, 기한 초과 업무 등 요약
- `GET /api/v1/crm/customers` — 고객 검색·필터·페이지네이션
- `POST /api/v1/crm/customers` — 시연용 고객 생성
- `GET /api/v1/crm/customers/{customer_id}` — 고객 정보 및 상담 타임라인
- `PATCH /api/v1/crm/customers/{customer_id}` — 허용된 고객 필드 수정
- `GET /api/v1/crm/employees` — 시연용 직원 목록
- `GET /api/v1/crm/sessions` — CRM 상담 목록 검색·필터·페이지네이션
- `GET /api/v1/crm/sessions/{session_id}` — 상담 상세(전사, 분석, 요약, 메모, 후속 업무)
- `PATCH /api/v1/crm/sessions/{session_id}/assignment` — 상담 담당자 배정/변경
- `POST /api/v1/crm/sessions/{session_id}/notes` — 상담 메모 추가
- `PATCH /api/v1/crm/sessions/{session_id}/review` — 직원 검토 상태 및 확정 값 저장
- `GET /api/v1/crm/tasks` — 후속 업무 검색·필터
- `POST /api/v1/crm/tasks` — 후속 업무 생성(수동 또는 AI 제안 확인 후)
- `PATCH /api/v1/crm/tasks/{task_id}` — 담당자·기한·상태 변경
- `GET /api/v1/crm/audit-events` — 권한이 있는 사용자에 한해 정정/배정 이력 조회

모든 쓰기 API는 Pydantic 검증, 허용 필드 검증, 관계 무결성 확인, 작성자/시각 기록을 적용한다. 초기 시연에서 인증이 간소화되어도 서비스 계층은 향후 역할 기반 권한을 적용할 수 있게 구성한다.

### 7.3 모니터링 API

- `GET /api/v1/monitoring/overview`
  - 서비스 상태, 최근 요청량, 오류량, 지연시간 요약

- `GET /api/v1/monitoring/services`
  - 서비스별 health 상태와 마지막 확인 시각

- `GET /api/v1/monitoring/requests`
  - 필터 기반 API 호출 목록

- `GET /api/v1/monitoring/traces/{trace_id}`
  - 하나의 요청 흐름을 구성하는 span 목록과 시간순 호출 관계

- `GET /api/v1/monitoring/errors`
  - 최근 오류 목록

- `GET /health/live`
  - 백엔드 프로세스가 살아 있는지 확인

- `GET /health/ready`
  - 필수 의존성 초기화 여부 확인

- `GET /metrics` (P1)
  - Prometheus 형식 지표. 활성화된 경우에만 노출

### 7.4 내부 서비스 API(어댑터 계약)

- `POST /internal/asr/transcribe`
- `POST /internal/decider/evaluate`
- `POST /internal/knowledge/search`
- `POST /internal/coach/generate`

각 내부 API는 요청 ID/trace ID를 전달받고, 타임아웃·오류·스키마 검증을 일관되게 적용한다. 내부 API를 실제로 별도 컨테이너로 분리하지 않은 초기 구현에서는 Provider 인터페이스를 내부 함수로 구현해도 되지만, 모니터링 UI에는 논리적 호출 단위가 기록되어야 한다.

### 7.5 공통 응답 예시

```json
{
  "ok": false,
  "error": {
    "code": "UPSTREAM_TIMEOUT",
    "message": "상담 분석 서비스 응답 시간이 초과되었습니다."
  },
  "trace_id": "7fd3dce4-9e80-4b88-b0d9-4f40a1e5bb12"
}
```

---

## 8. 데이터 모델 및 관계 초안

PostgreSQL을 기준 데이터 저장소로 사용한다. UUID 기본키, UTC 타임스탬프, 외래키 및 필요한 인덱스를 적용한다. 실제 운영 데이터 연계는 범위 밖이며, 초기 데이터는 가상 데이터다. JSONB는 모델 응답의 부가 필드에 제한적으로 사용하고, 자주 검색·조인하는 핵심 필드는 정규화된 컬럼으로 둔다.

### 8.1 `customers` — 시연용 고객
- `customer_id` UUID PK
- `display_name` 가상 표시명(예: `시연 고객 001`), `customer_type` nullable
- `external_ref` nullable, 가상 데이터에 한해 사용하는 내부 참조값
- `created_at`, `updated_at`, `is_active`
- 실제 성명·전화번호·주민등록번호·사업자등록번호를 기본 필수 필드로 만들지 않는다.

### 8.2 `employees` — 시연용 직원
- `employee_id` UUID PK
- `display_name`, `department_name`, `team_name` nullable, `is_active`
- `created_at`, `updated_at`
- 인증 계정과 업무 직원 레코드는 개념적으로 분리한다. 초기 버전은 가상 직원 시드 데이터를 사용한다.

### 8.3 `consultation_sessions` — 상담 세션
- `session_id` UUID PK, `customer_id` nullable FK, `assigned_employee_id` nullable FK
- `status`: `created | active | paused | completed | failed`
- `input_mode`: `file | microphone | simulation`
- `started_at`, `ended_at`, `created_at`, `updated_at`
- `current_topic`, `current_stage`는 최신 화면 상태용 캐시이며, 분석 이력의 원본을 대체하지 않는다.
- `ai_summary` nullable, `final_summary` nullable, `review_status`: `unreviewed | reviewed | corrected`

### 8.4 `transcript_events` — 확정 전사 및 이벤트
- `event_id` UUID PK, `session_id` FK
- `speaker`: `customer | agent | unknown`; `text`; `transcript_type`: `partial | final`
- `occurred_at`, `source`: `asr | simulation | manual`, `trace_id` nullable
- `sequence_no`를 세션 내 순서 보장을 위해 둔다. `partial` 저장은 기본 비활성화하고 `final` 중복 저장을 방지한다.

### 8.5 `ai_analysis_results` — Decider/분석 결과
- `analysis_id` UUID PK, `session_id` FK, `source_event_id` nullable FK
- `analysis_type`(예: `routing`, `sentiment_text`), `topic`, `stage`, `next_step`
- `confidence` nullable(모델이 실제 제공한 경우만), `needs_human_review`
- `result_json` JSONB, `model_name`, `model_version`, `created_at`
- 결과는 append-only 이력으로 저장하고 최신 결과를 필요 시 별도로 조회한다.

### 8.6 `coach_suggestions` — AI 상담원 제안
- `suggestion_id` UUID PK, `session_id` FK, `trace_id` nullable
- `staff_note`, `customer_question`, `action_items` JSONB
- `required_checks`, `cautions`, `knowledge_references` JSONB nullable
- `generation_status`, `model_name`, `model_version`, `created_at`
- 기존 출력 필드(`staff_note`, `customer_question`, `action_items`)를 유지한다.

### 8.7 `consultation_notes` — 상담원 메모
- `note_id` UUID PK, `session_id` FK, `author_employee_id` FK
- `note_text`, `created_at`, `updated_at`, `is_deleted`
- 삭제는 초기에는 논리 삭제를 우선하며, 감사 목적의 수정 이력을 유지한다.

### 8.8 `follow_up_tasks` — 후속 업무
- `task_id` UUID PK, `session_id` FK, `customer_id` nullable FK
- `title`, `description`, `assigned_employee_id` nullable FK, `due_at` nullable
- `priority`: `low | normal | high`; `status`: `open | in_progress | blocked | completed | cancelled`
- `source`: `ai_suggested | manual`; `created_by_employee_id` nullable FK
- `created_at`, `updated_at`, `completed_at` nullable

### 8.9 `api_call_traces` — API 및 논리 단계 추적
- 기존 `ApiCallRecord` 필드: `span_id`, `trace_id`, `parent_span_id`, `session_id` nullable, `service_name`, `operation`, `provider`
- `started_at`, `ended_at`, `duration_ms`, `status`, `http_status_code`, `error_code`, `retry_count`, `request_size_bytes`, `response_size_bytes`, `model_name`, `model_version`
- 전사 원문·프롬프트·인증 헤더를 저장하지 않는다. 세션별 조회와 시간 범위 검색을 위한 인덱스를 둔다.

### 8.10 `audit_events` — 직원 수정 및 배정 이력
- `audit_id` UUID PK, `entity_type`, `entity_id`, `action`
- `actor_employee_id` nullable FK, `occurred_at`, `trace_id` nullable
- `before_value` JSONB nullable, `after_value` JSONB nullable
- 민감한 원문/식별정보는 감사 JSON에 무분별하게 복제하지 않고 변경된 허용 필드만 기록한다.

### 8.11 관계 및 저장 규칙
- `customers 1:N consultation_sessions`; `employees 1:N consultation_sessions`(담당자 기준)
- `consultation_sessions 1:N transcript_events / ai_analysis_results / coach_suggestions / consultation_notes / follow_up_tasks`
- `employees 1:N follow_up_tasks`; 모든 관련 외래키는 참조 무결성을 보장한다.
- AI 생성 결과는 불변 이력으로 남기고, 직원 확정값은 별도 필드/검토 상태로 관리한다. AI 결과가 직원 수정값을 덮어쓰지 않도록 한다.
- 인덱스 권장: 세션 `started_at/status/customer_id/assigned_employee_id`, 전사 `(session_id, sequence_no)`, 후속 업무 `(status, due_at, assigned_employee_id)`, trace `(trace_id, started_at)`.

## 9. 보안·개인정보·안전 요구사항

1. 개발과 시연에서는 가상 고객 정보만 사용한다.
2. 사업자등록번호, 주민등록번호, 전화번호, 성명 등 직접 식별정보를 로그와 trace attribute에 기록하지 않는다.
3. 음성 원본 저장은 기본 비활성화하고 임시 파일은 처리 후 삭제한다.
4. 환경변수 파일과 API 비밀정보를 Git에 커밋하지 않는다.
5. 프런트엔드에 내부 모델 API 키를 넣지 않는다.
6. 모니터링 화면은 오류와 호출 메타데이터만 기본 표시하고 요청 본문은 기본 표시하지 않는다.
7. AI는 직원 보조 도구이며 보증 심사·승인·자격 판단을 자동 확정하지 않는다.
8. AI 결과와 모델 오류를 구분해 표시한다. 오류를 정상적인 판단 결과처럼 표시하지 않는다.
9. 프롬프트 인젝션 가능성을 고려해 검색된 문서는 지시사항이 아닌 참고자료로 취급한다.
10. 사용자 입력과 모델 출력은 화면에 렌더링하기 전에 적절히 검증·이스케이프한다.
11. CORS, 업로드 크기, 요청 빈도, 타임아웃을 설정으로 관리한다.
12. 상세 오류 로그는 개발자용으로 제한하고 사용자 메시지는 안전하게 정리한다.
13. CRM 연계 PoC 및 시연에는 가상 데이터만 사용한다. Twenty는 내부 시연망에서만 노출하고 기본적으로 외부 공개하지 않는다.
14. CRM이 AICC DB에 직접 관리자 권한으로 접근하지 않도록 한다. Twenty 연계는 공개 API/webhook을 우선 사용하고, 직접 DB 접근이 필요한 별도 도구에는 최소 권한 계정을 적용한다.
15. AI 분석 결과, 직원 검토 상태, 최종 확정값을 구분하고 수정 이력을 기록한다.
16. 데이터 보관 기간과 삭제 정책은 환경설정/운영정책으로 분리하며, 초기에는 세션 데이터 삭제·내보내기 절차를 문서화한다.
17. Twenty의 버전별 라이선스와 배포 조건, 필요한 SSO/행 단위 권한/감사 기능의 플랜 포함 여부를 확인한 후 사용한다. NocoDB도 도입 시 동일하게 검토한다.

---

## 10. 비기능 요구사항

### NFR-01 — 안정성
- 하나의 모델 API 실패가 백엔드 전체 프로세스를 종료시키지 않아야 한다.
- 한 단계의 실패를 trace ID로 확인할 수 있어야 한다.
- 재시도는 타임아웃/일시적 오류에 한해 제한적으로 적용하고, 무조건적인 재시도로 GPU 과부하를 유발하지 않는다.
- 상담 종료와 기록 조회는 Coach 생성 실패와 독립적으로 동작해야 한다.

### NFR-02 — 지연시간 관측
- ASR, Decider, Knowledge, Coach의 지연시간을 각각 측정한다.
- 총 처리시간과 각 단계 시간을 UI에서 확인할 수 있어야 한다.
- 초기 단계에서는 절대 지연시간 SLO를 보장하지 않고 실제 측정값을 수집해 최적화한다.

### NFR-03 — 재현성
- 동일한 샘플 시나리오를 반복 실행할 수 있어야 한다.
- Mock 모드에서 모델 서버 없이도 주요 기능 및 오류 상황을 재현할 수 있어야 한다.
- 로그에 앱 버전과 설정된 모델 이름을 남긴다. 비밀 설정값은 남기지 않는다.

### NFR-04 — 이식성
- 개발 PC와 DGX Spark 사이에서 애플리케이션 코드 변경을 최소화한다.
- OS/GPU/모델 서빙 엔진의 차이는 Provider와 배포 설정으로 흡수한다.
- 모델의 VRAM 사용량, 처리량, 동시 실행 가능성은 실제 환경에서 검증하고 문서화한다.

### NFR-05 — 사용성
- 모델 호출 중에는 진행 상태를 표시한다.
- 오류가 발생해도 전사 내용과 기존 상담 이벤트를 유지한다.
- 모니터링 화면에서 초보 개발자도 서비스별 오류와 병목 구간을 식별할 수 있어야 한다.

---

## 11. 테스트 및 완료 기준

### 11.1 기능 테스트 (P0)
- [ ] 새 상담 세션 생성 및 종료 가능
- [ ] Mock 모드에서 샘플 발화를 주입할 수 있음
- [ ] 발화가 전사 영역에 표시됨
- [ ] Decider 결과에 따라 주제와 단계가 갱신됨
- [ ] Coach 안내 카드가 생성됨
- [ ] 근거가 없을 때 근거가 없다고 표시됨
- [ ] API 실패 시 오류 상태가 화면에 표시되고 앱이 계속 동작함
- [ ] 상담 이벤트 이력을 조회할 수 있음
- [ ] 상담 세션·전사·AI 분석·요약이 PostgreSQL에 저장되고 앱 재시작 후에도 조회됨
- [ ] CRM에서 고객별 상담 타임라인과 상담 상세를 조회할 수 있음
- [ ] 직원 메모 추가 및 수정 이력 기록이 동작함
- [ ] AI 제안에서 후속 업무를 선택적으로 생성하고 담당자·기한·상태를 관리할 수 있음
- [ ] AI 생성 결과와 직원 확정/수정 결과가 분리 저장됨
- [ ] 고객·직원·상담·후속 업무의 관계 무결성이 테스트됨
- [ ] Twenty PoC에서 API 인증·조회·쓰기·오류/재시도 동작과 요구 데이터 모델이 검증됨
- [ ] CRM 선택 근거와 라이선스/권한/폐쇄망 배포 검토 결과가 문서화됨
- [ ] NocoDB를 도입하는 경우에만 최소 권한 DB 계정으로 지정된 테이블만 접근할 수 있음

### 11.2 API 모니터링 테스트 (P0)
- [ ] 각 내부 호출에 trace ID와 span ID가 부여됨
- [ ] trace ID가 백엔드와 하위 서비스에 전달됨
- [ ] 성공·실패·타임아웃이 구분되어 기록됨
- [ ] 서비스별 호출 횟수와 지연시간이 집계됨
- [ ] 모니터링 UI에서 호출 흐름과 병목 단계를 확인할 수 있음
- [ ] 서비스 중단 시 health 상태가 갱신됨
- [ ] 잘못된 JSON 응답과 스키마 검증 실패를 식별할 수 있음
- [ ] 로그/trace에 개인정보 및 API 키가 포함되지 않음
- [ ] 모니터링 저장 실패가 상담 기능을 중단시키지 않음

### 11.3 환경 테스트 (P0)
- [ ] 모델 서버 없이 `mock` 프로필로 실행 가능
- [ ] 로컬 모델 API를 `local` 프로필로 연결 가능
- [ ] 서비스 URL을 설정만 변경해 교체 가능
- [ ] Docker Compose로 AICC와 PostgreSQL 실행 가능하며, Twenty PoC 프로필은 선택적으로 실행 가능
- [ ] `.env.example`만으로 필요한 설정 항목을 이해할 수 있음
- [ ] RTX 5080에서 실제 연결할 모델별 VRAM과 지연시간을 기록할 수 있음

### 11.4 시연 준비 완료(Definition of Done)
- [ ] README에 설치·실행·중지·테스트 절차가 있음
- [ ] 아키텍처 및 API 계약 문서가 있음
- [ ] AICC 화면, CRM 화면, 시스템 모니터링 화면이 구현됨
- [ ] PostgreSQL 스키마/마이그레이션 및 가상 시드 데이터가 제공됨
- [ ] 앱 재시작/컨테이너 재생성 후에도 DB 데이터가 유지됨(볼륨 포함)
- [ ] CRM 고객 타임라인·상담 상세·메모·후속 업무 흐름이 end-to-end로 동작함
- [ ] Twenty PoC 결과, 최종 CRM 선택 근거, API 연계 방식 및 라이선스 검토 항목이 README에 기록됨
- [ ] NocoDB는 필요성이 입증된 경우에만 선택적으로 구동되며, 연결·권한·라이선스 조건이 README에 기록됨
- [ ] 샘플 상담 시나리오 2개 이상이 동작함
- [ ] ASR/Decider/Coach 중 일부를 Mock으로 바꾸어도 데모가 가능함
- [ ] API 타임아웃/오류/서비스 중단 시연이 가능함
- [ ] 가상 데이터만 사용하며 개인정보 로그 검사가 통과함
- [ ] DGX Spark 이전을 위한 배포 체크리스트가 작성됨

---

### 11.5 기존 소스 기준 추가 수용 기준

- [ ] 기존 상담 라우팅 JSON 설정을 외부 설정 파일로 분리하고, 시연용 기준임을 화면에 표시한다.
- [ ] 확정 발화와 중간 전사를 분리해 처리하고, 중간 전사 변경이 확정 이력에 중복 저장되지 않는다.
- [ ] 빠르게 연속된 발화에서 오래된 Coach 결과가 최신 발화의 안내를 덮어쓰지 않는다.
- [ ] Coach 실패/시간 초과 시 ASR 전사와 Decider 결과는 보존되고 화면에 독립적인 실패 상태가 표시된다.
- [ ] 감정 결과는 텍스트 기반 분석으로 명시하고, 음성 기반 감정·자동 화자 분리라고 표현하지 않는다.
- [ ] 기존 `staff_note`, `customer_question`, `action_items` 응답을 수용하거나 명시적인 버전 변환기를 제공한다.
- [ ] 모델 ID/revision, backend 모드, 추론 시간, 오류가 기록되며 민감한 프롬프트/전사 전문은 기본 로그에 남지 않는다.
- [ ] 기존 PDF/이미지 Decision Playground를 신규 코어 앱에 포함할지 여부는 별도 범위 결정으로 남기며, 음성 AICC 구현을 지연시키지 않는다.

## 12. 코딩 에이전트 작업 지시

코딩을 시작하기 전에 다음 순서를 따른다.

1. **저장소 점검:** 현재 디렉터리 구조, 기존 Gradio 시연 코드(`app.py`), 모델 로딩 방식, 의존성, 실행 방법을 확인한다. 기존 코드의 실제 모델 ID/revision, 음성 청크 처리, 라우팅 설정, Coach JSON 스키마를 코드에서 검증하고 아래 설계에 반영한다.
2. **기능 추출표 작성:** 기존 기능을 `재사용`, `어댑터로 감싸기`, `신규 구현`, `이번 범위 제외`로 분류한다. 적어도 ASR, Decider, 상담 라우팅, 감정 분석, Coach, 음성 입력, 파일 재생, PDF/이미지 Playground를 각각 분류한다.
3. **구현 계획 제시:** 재사용할 코드와 변경할 파일, 신규 모듈, API 계약, 위험요소를 짧게 제시한다.
4. **단계별 구현:** 한 번에 전체를 대규모 재작성하지 않는다. Mock 기반 end-to-end 흐름을 먼저 완성한 다음 실제 Provider를 연결한다.
5. **모니터링 우선 포함:** 처음부터 trace ID, API 호출 타이밍, 오류 기록을 공통 HTTP/Provider 계층에 구현한다. 나중에 덧붙이는 방식으로 미루지 않는다.
6. **실제 모델 연결:** 사용 중인 모델과 API 규격을 확인한 뒤 Provider별 어댑터를 작성한다. 존재하지 않는 endpoint나 지원하지 않는 스트리밍 기능을 가정하지 않는다.
7. **테스트:** 각 단계마다 자동 테스트를 실행하고 실패 원인을 수정한다.
8. **실행 문서:** `.env.example`, Docker Compose, README, API 예시, 시연 시나리오를 제공한다.
9. **완료 보고:** 구현 파일, 실행 명령, 테스트 결과, 알려진 제약, RTX 5080 실측 결과, DGX Spark 이전 시 확인할 사항을 요약한다.

### 구현 마일스톤

- **M1 — Mock MVP + PostgreSQL:** AICC 화면, 세션·전사 이벤트 저장, 스키마/마이그레이션, 가상 시드 데이터
- **M2 — Observability:** trace/span, API 모니터링 API 및 UI, 오류·타임아웃 시뮬레이션
- **M3 — CRM 비교 PoC:** Twenty 자체 호스팅, 가상 데이터 모델링, API 인증·조회·쓰기·webhook 검증, 권한/감사/라이선스/폐쇄망 적합성 평가
- **M4 — CRM 아키텍처 결정 및 MVP:** PoC 평가표에 따라 Twenty 연계 또는 자체 React CRM 선택. 고객/상담 목록, 타임라인, 메모, AI 결과 검토, 후속 업무 구현
- **M5 — Local model integration:** ASR·Decider·Coach Provider 연결 및 환경설정화
- **M6 — RTX 5080 검증:** 실제 음성·샘플 상담 테스트, 지연시간·VRAM·DB 복구·CRM 흐름 측정
- **M7 — DGX Spark demo packaging:** 이미지 빌드, 환경변수 프로필, 폐쇄/제한 네트워크 반입 절차, 시연 체크리스트
- **M8 — NocoDB 필요성 재평가(선택):** 관리자 데이터 점검 요구가 남아 있는 경우에만 최소 권한 PoC

### 코딩 에이전트가 지켜야 할 원칙

- 요구사항에 없는 복잡한 마이크로서비스를 무리하게 만들지 않는다. 초기에는 하나의 FastAPI 백엔드 안에서 Provider를 논리적으로 분리해도 된다.
- 모델별 API 호출은 추적 가능한 공통 래퍼를 통과시킨다.
- 샘플/Mock 모드는 실제 모델 모드와 명확히 구분해 UI에 표시한다.
- 모델의 confidence, 화자, 실시간 스트리밍 등 제공 여부를 확인하지 않고 값을 꾸며내지 않는다.
- 실제 고객정보 또는 운영 자격증명을 요구하지 않는다.
- 실행되지 않은 테스트를 통과했다고 보고하지 않는다.
- 사용자가 별도 승인하지 않는 한 기존 파일이나 데이터의 파괴적 변경을 하지 않는다.

---

## 13. 구현 범위 우선순위 및 후속 확장 후보

### 13.1 MVP 포함 범위
- PostgreSQL 영속 저장과 스키마 마이그레이션
- 상담 목록/상세/고객별 타임라인 CRM
- 직원 메모, AI 결과 검토·정정, 후속 업무 배정·상태 관리
- trace_id로 상담 세션과 모델/API 호출 이력 연결
- 가상 고객·직원·상담 데이터 시드

### 13.2 후속 확장 후보
- CTI/SIPREC 오디오 및 통화 메타데이터 연동
- 화자 분리 및 상담원/고객 채널 구분
- 재단 업무지침 RAG 및 문서 근거 표시
- 상담 결과와 실제 직원 조치의 비교·평가
- OpenTelemetry + Prometheus + Grafana + Tempo/Jaeger
- 사용자 인증, 역할 기반 권한, 감사로그
- DGX Spark에서의 모델별 자원 배치·동시성·큐 관리
- 상담 유형별 평가 데이터셋 및 회귀 테스트


## 부록 A. 기존 `app.py` 기준 구현 인벤토리

| 영역 | 현재 구현 | 신규 앱에서의 처리 |
|---|---|---|
| ASR | Qwen3-ASR-1.7B, Transformers 네이티브, CPU/CUDA 선택 | `ASRProvider`로 분리, 로컬 프로세스/API/Mock 선택 |
| Decider | pplx-decider 공식 라이브러리, Choice/Boolean/Score 구조화 판정 | `DeciderProvider`로 분리, 모델 출력 구조와 확률 의미 유지 |
| 감정 | Decider에 감정 Choice + 부정 강도 Score 질문을 전달 | 선택 기능으로 유지하되 텍스트 기반이라고 표시 |
| 상담 라우팅 | 최근 최대 30개 전사, 고정 JSON 노드/분기, 확률·margin 기준 | JSON/YAML 설정 분리, 기준 미확정 시 질문 단계에서 중지 |
| Coach | Qwen3.5-2B 로컬 NF4 또는 OpenAI 호환 API, JSON 검증 | `CoachProvider`, 타임아웃/취소/오래된 결과 폐기 |
| 음성 입력 | 마이크, Windows WASAPI 시스템 오디오, 파일 재생 | 플랫폼별 Input Adapter. Linux/DGX에서 WASAPI 기능을 전제하지 않음 |
| 구간 분리 | RMS 에너지 기반 Segmenter, 중간/확정 구간, flush | 독립 모듈로 추출하고 큐/백프레셔 계측 추가 |
| 세션 | 메모리 기반 상태, 세션 만료/정리, JSON 내보내기 | 영속 이벤트 저장과 메모리 상태를 분리 |
| 모니터링 | 일부 단계별 처리시간과 UI 오류 표시 | 신규: trace/span, API·논리 단계별 latency/error, 상태 대시보드 |
| PDF/이미지 | PDF/PNG/JPEG/WEBP 입력, 페이지 렌더링, 다중 질문 Decider | 초기 AICC 범위 제외. 별도 확장 기능으로 보존 |
| 배포 | Windows 전용 설치/다운로드/오프라인 반입 절차 | Docker Compose 개발 프로필과 DGX Spark 대상 별도 빌드/검증 |

### 부록 A.1 확인된 모델 식별자

- ASR: `Qwen/Qwen3-ASR-1.7B-hf`, revision `bcd2b5b7f32b480ab5790554cfa8347f246a14f3`
- Decider: `perplexity-ai/pplx-decider-flash-v1.2`, revision `9ff7cf9838e59df461c276b7699d56b244d2af58`
- Coach: `Qwen/Qwen3.5-2B`, revision `15852e8c16360a2fea060d615a32b45270f8a8fc`

이 식별자는 업로드 시점의 소스에 명시된 값이다. 모델 저장소의 현재 상태나 DGX Spark 호환성을 별도로 검증했다는 의미는 아니다.
