"""EmbeddingGemma 2 임베딩 서버. 텍스트 인코더만 적재(270M)하고 검색 질의/문서 접두어를 모델 규칙대로 붙인다.

POST /embed {"texts": [...], "kind": "query"|"document", "titles": [...]?} → {"vectors": [[...]], "dim": 768, "model": "..."}
GET  /health
모델 파일은 HF_HOME(/models, 호스트 볼륨)에 고정 revision으로 받는다. 오프라인 반입 시 그 폴더를 옮기고 HF_HUB_OFFLINE=1.
"""
import os
import time

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

MODEL_ID = os.environ.get("EMBED_MODEL_ID", "google/embeddinggemma-2")
REVISION = os.environ.get("EMBED_REVISION", "914f7f89142e33e77833254d9c9b90c3cef7303b")
DIM = int(os.environ.get("EMBED_DIM", "768"))  # MRL: 768/512/256/128
assert DIM in (768, 512, 256, 128), "EMBED_DIM은 768/512/256/128 중 하나"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
# fp16은 활성값 범위를 넘어 NaN이 나므로 금지(모델 카드). GPU는 bf16, CPU는 fp32
DTYPE = torch.bfloat16 if DEVICE == "cuda" and torch.cuda.is_bf16_supported() else torch.float32
MAX_TEXTS, MAX_CHARS = 64, 8000

model = SentenceTransformer(MODEL_ID, revision=REVISION, device=DEVICE,
                            config_kwargs={"vision_config": None, "audio_config": None},  # 텍스트 전용
                            model_kwargs={"torch_dtype": DTYPE})
app = FastAPI(title="embedder")


class EmbedIn(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=MAX_TEXTS)
    kind: str = Field(pattern="^(query|document)$")
    titles: list[str | None] | None = None


def prefixed(body):
    if body.kind == "query":
        return [f"task: search result | query: {t}" for t in body.texts]
    titles = body.titles or [None] * len(body.texts)
    if len(titles) != len(body.texts):
        raise HTTPException(422, "titles 길이가 texts와 다릅니다.")
    return [f"title: {(ti or 'none').strip() or 'none'} | text: {t}" for ti, t in zip(titles, body.texts)]


@app.post("/embed")
def embed(body: EmbedIn):
    if any(len(t) > MAX_CHARS or not t.strip() for t in body.texts):
        raise HTTPException(422, f"각 텍스트는 1~{MAX_CHARS}자여야 합니다.")
    started = time.perf_counter()
    vectors = model.encode(prefixed(body), truncate_dim=DIM, normalize_embeddings=True,
                           batch_size=16, convert_to_numpy=True)
    return {"vectors": vectors.tolist(), "dim": DIM, "model": f"{MODEL_ID}@{REVISION}",
            "seconds": round(time.perf_counter() - started, 3)}


@app.get("/health")
def health():
    return {"ok": True, "model": f"{MODEL_ID}@{REVISION}", "dim": DIM, "device": DEVICE, "dtype": str(DTYPE)}
