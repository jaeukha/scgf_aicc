# DGX Spark 이전 체크리스트 (PRD §6.2, M7)

RTX 5080(Windows x64)에서 검증한 구성을 DGX Spark로 옮길 때 확인할 항목이다. **아직 DGX Spark에서는 아무것도 실행하지 않았다.**

## 1. 대상 환경 확인 (먼저)
- [ ] OS·아키텍처(ARM64 여부), CUDA·드라이버 버전, 컨테이너 런타임(NVIDIA Container Toolkit), GPU 메모리
- [ ] 사내 네트워크 정책: 외부 인터넷 차단 여부, 허용 포트, 시연 접속 범위
- [ ] 이미지 레지스트리 또는 `docker save`/`load` 반입 경로

## 2. 그대로 쓰는 것
- [ ] AICC 백엔드 이미지(`Dockerfile`, python:3.12-slim, 순수 Python). ARM64면 `docker buildx --platform linux/arm64`로 다시 빌드
- [ ] PostgreSQL 16, Alembic 마이그레이션(`alembic upgrade head`), 시드(가상 직원·고객)
- [ ] 설정은 환경변수만 바꾼다: `PROVIDER_MODE=demo`, `*_BASE_URL`, `DATABASE_URL`, `TWENTY_*`
- [ ] Mock 대체: 모델 서버를 쓸 수 없으면 `PROVIDER_MODE=mock`, `COACH_MODE=mock`으로 바로 시연

## 3. 다시 준비할 것 (Windows 산출물은 재사용 불가)
- [ ] `model_server.py`용 Linux(ARM64) Python 환경. app.py의 `--setup`은 Windows용 cu130 wheel과 PyAudioWPatch를 설치하므로 쓸 수 없다. 대상 플랫폼에 맞는 PyTorch·transformers 5.17·soundfile 조합을 별도로 검증한다.
- [ ] 모델 반입: `models/` 3종(Decider, Qwen3-ASR-1.7B, Qwen3.5-2B). revision을 고정하고 `app.py --verify-model`로 SHA-256을 검증한다.
- [ ] Coach NF4는 bitsandbytes의 ARM64·CUDA 지원 여부를 확인한다. 지원되지 않으면 BF16으로 로드하거나 별도 OpenAI 호환 서버(`COACH_MODE=api`)를 쓴다.
- [ ] 시스템 오디오(WASAPI)는 Windows 전용이라 이전 대상이 아니다. 브라우저 마이크는 HTTPS(또는 SSH 터널 localhost)가 필요하다.

## 4. 측정 (RTX 5080 기준값과 비교)
- [ ] 세 모델을 함께 띄웠을 때의 VRAM. RTX 5080 기준: 15.8/16.3GB로 여유가 거의 없다.
- [ ] 단계별 지연(모니터링 화면 p50/p95). RTX 5080 기준: Decider 1회 0.27초, 라우팅 0.5초, Coach 2.0초, ASR 16초 음성 1.7초
- [ ] 동시 세션 수에 따른 대기. GPU 작업은 모델 서버에서 잠금으로 직렬화된다.

## 5. 시연 전 점검
- [ ] `/health/ready`, 모니터링 화면에서 모든 서비스 healthy
- [ ] 샘플 시나리오 3종(extension·progress·documents)과 TTS 음성 파일로 E2E 확인
- [ ] 오류 주입(Coach 타임아웃 등)으로 장애 시연 확인
- [ ] 로그·trace에 전사 원문·식별정보가 없는지 확인(`api_call_traces` 컬럼 점검)
- [ ] Twenty를 쓴다면 라이선스 법무 검토 완료 여부(`docs/twenty_poc.md`)
