# Twenty CRM PoC 결과 (PRD M3, FR-CRM-06)

- 시점: 2026-10-24 · 대상: `twentycrm/twenty:v2.46.0` (2026-10-12 릴리스, 고정 태그)
- 환경: RTX 5080 개발 PC, Docker Desktop, `twenty/docker-compose.yml`(공식 compose, 프로젝트명 `aicc-twenty`)
- 데이터: 가상 고객·직원·상담만 사용

## 연계 구조 (구현됨: `aicc/twenty_sync.py`)

- **원본 데이터는 AICC PostgreSQL**이다. Twenty에는 **공개 REST API로 AICC → Twenty 단방향 push**만 한다. Twenty 내부 DB에 직접 접근하지 않는다.

| AICC | Twenty | 비고 |
|---|---|---|
| 고객(customers) | Company (표준) | 고객이 연결된 세션만 동기화 |
| 상담 세션 | `aiccConsultation` (**사용자 정의 객체**) | 세션 ID·주제·검토 상태·요약·시작 시각, Company 관계(N:1) |
| 후속 업무 | Task (표준) + TaskTarget(Company) | 상태 매핑 open→TODO, in_progress·blocked→IN_PROGRESS, completed·cancelled→DONE(본문에 원래 상태 표기) |

- **보내지 않는 것**: 전사 원문. 요약은 직원 확정 요약을 우선하고, 없으면 `[AI 요약·미검토]` 표시를 붙여 보낸다.
- **동기화 시점**: 상담 종료, 직원 검토, 후속 업무 생성·변경 직후 백그라운드로 실행한다. CRM 상세 화면의 **지금 동기화** 버튼으로도 실행할 수 있다(수동 재시도 겸용).
- **중복 방지**: `crm_sync_links(entity_type, aicc_id)` 유일키를 둔다. 링크가 있으면 PATCH, Twenty 쪽에서 삭제됐으면(404) 다시 생성한다. 같은 세션의 동시 동기화는 잠금으로 직렬화한다(동시 4회 호출에도 생성 1회, 테스트로 확인).
- **실패 처리**: `last_error`와 trace(`service=crm`)에 기록한다. 상담 기능은 막지 않는다. 자동 재시도는 없고 수동 재시도만 있다.
- **필드 소유권**: 위 매핑 필드는 AICC가 소유하므로 Twenty에서 고쳐도 다음 동기화 때 덮어쓴다. Twenty에서 수정이 필요한 정보는 Twenty 전용 필드(노트 등)에 둔다.

## PoC에서 실제로 확인한 것

| 기준 | 결과 |
|---|---|
| 자체 호스팅 | 공식 compose(server·worker·postgres·redis)로 단일 PC에서 기동 확인 |
| API 인증 | 설정 > API에서 API 키를 발급한다(역할 지정, 만료일 있음). PoC 키는 Admin 역할, 180일 만료이며 `twenty/.env`에만 보관한다. |
| 사용자 정의 객체·필드·관계 | metadata GraphQL API로 객체 1개, 필드 5개, Company 관계를 코드에서 생성했다. 생성 직후 REST `/rest/aiccConsultations`가 바로 노출되었다. |
| 쓰기·수정·조회·삭제 | REST create(201)·PATCH(200)·GET·DELETE(200) 확인. 응답 형식은 `data.create<Object>.id` |
| 오류 동작 | 잘못된 UUID는 400이다. 연결 실패와 404는 어댑터에서 처리하며, 테스트로 검증했다. |
| 고객 이력 표현 | Company 상세에서 연결된 AICC 상담·작업이 보인다(관계 필드). |
| 후속 업무 배정·기한·상태 | Task의 상태·기한은 확인했다. 담당자(`assigneeId`)는 Twenty 워크스페이스 멤버 ID라서 AICC 가상 직원과 매핑하지 않았다(미연계). |

## 확인하지 못한 것 / 위험

- **라이선스**: 저장소 LICENSE(main 브랜치 기준)는 대부분 **AGPLv3**이다. `/* @license Enterprise */` 표시 파일은 상용 라이선스이고, SDK·UI 패키지 일부는 MIT이다. 공개 HTTP API·웹훅으로 연동하는 애플리케이션에는 **AGPLv3 Section 7 추가 허가(Application Exception)** 조항이 있다. 다만 v2.46.0 태그 시점의 문구와 공공기관 내부 배포·수정 조건은 **법무 검토가 필요하다.**
- **권한·감사·SSO**: 역할(Admin 등)은 있다. 그러나 행 단위 권한, 감사 로그, SSO가 어느 플랜에 포함되는지는 확인하지 않았다. 해당 기능이 무료로 제공된다고 가정하지 않는다.
- **웹훅**: Twenty → AICC 방향은 필요하지 않아 구현하지 않았다. 재시도·중복 전달 동작도 검증하지 않았다.
- **폐쇄망 배포**: 이미지 반입(`docker save`/`load`)은 가능할 것으로 보이지만 해 보지 않았다. 사용 중 외부 호출(텔레메트리, 아바타 등) 여부도 검토하지 않았다.
- **백업·복구**: Twenty DB 볼륨(`aicc-twenty_db-data`)의 백업·복구 절차는 검증하지 않았다.
- **다중 프로세스**: 동기화 잠금은 프로세스 내부에서만 동작한다. 백엔드를 여러 프로세스로 띄우면 DB advisory lock으로 바꿔야 한다.

## 판단

- PoC 범위에서 Twenty는 고객·상담·후속 업무를 **표준 객체와 사용자 정의 객체로 자연스럽게 표현**한다.
- 공개 API만으로 단방향 연계가 동작한다.
- 상담원 실시간 화면과 AICC 원본 데이터는 그대로 AICC에 두고, Twenty는 **업무 관리용 사본**으로 쓰는 구조가 PRD 원칙과 맞는다.
- **최종 채택 전 남은 관문**:
  - 라이선스 법무 검토
  - 권한·감사 기능의 플랜 확인
  - 폐쇄망 반입 검증
- 관문을 통과하지 못하면 현재의 자체 CRM 화면(`aicc/crm.py` + UI)을 유지한다. 자체 CRM은 이미 PRD의 CRM 필수(P0) 기능을 충족한다.

## 재현

```powershell
cd twenty
# .env: TAG=v2.46.0, PG_DATABASE_PASSWORD, ENCRYPTION_KEY(base64 32B), SERVER_URL=http://localhost:3000
docker compose up -d          # 프로젝트명 aicc-twenty (기존 twenty_* 볼륨과 분리)
# http://localhost:3000 에서 워크스페이스·API 키 생성 → 백엔드 환경변수
$env:TWENTY_ENABLED="true"; $env:TWENTY_API_KEY="<키>"; python -m uvicorn aicc.main:app --port 8000
```

`aiccConsultation` 사용자 정의 객체를 처음 한 번 만드는 스크립트는 `twenty/create_schema.py`에 있다.
