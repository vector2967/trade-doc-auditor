"""검색 융합 + rerank (정확도 개선계획 ③) — 에이전트 레이어 소유.

repository.search() 의 dense/bm25 arm 결과를 RRF(Reciprocal Rank Fusion)로 합치고,
선택적으로 bge-reranker-v2-m3 cross-encoder 로 재정렬한다.

- RRF: 점수 스케일이 다른 arm(dense cosine vs bm25 idf)을 순위만으로 융합 — k=60 표준.
- rerank: 융합 상위 depth 개를 (질문, 조문) 쌍으로 채점해 재정렬. 모델은 lazy 싱글턴
  (bge-m3 임베더와 별개 모델, 첫 호출 시 다운로드/로딩 수십 초).

사용: from agent import retrieval; retrieval.search("질문", limit=10)
"""
from __future__ import annotations

import sys
from pathlib import Path

_LAW_REPO = Path(__file__).resolve().parents[1] / "law_repository"
if str(_LAW_REPO) not in sys.path:
    sys.path.insert(0, str(_LAW_REPO))

from src import repository as repo  # noqa: E402

RRF_K = 60          # 표준값 — 상위권 순위 차의 영향을 완만하게
ARMS = ("dense", "bm25")
VARIANT_WEIGHT = 0.6  # 재작성 변형 쿼리의 RRF 가중 (원 쿼리 1.0 대비)


def rrf_fuse(ranklists: dict[str, list[repo.Hit]], limit: int,
             weights: dict[str, float] | None = None) -> list[repo.Hit]:
    """arm 별 순위 리스트 → RRF 점수로 융합한 상위 limit. 점수 = Σ w/(k+rank)."""
    scores: dict[int, float] = {}
    first: dict[int, repo.Hit] = {}
    for key, hits in ranklists.items():
        w = (weights or {}).get(key, 1.0)
        for rank, h in enumerate(hits, start=1):
            scores[h.article_pk] = scores.get(h.article_pk, 0.0) + w / (RRF_K + rank)
            first.setdefault(h.article_pk, h)
    order = sorted(scores, key=lambda pk: (-scores[pk], pk))[:limit]
    return [repo.Hit(pk, scores[pk], first[pk].text) for pk in order]


_reranker = None

# FlagEmbedding.FlagReranker 는 현행 transformers 와 비호환(prepare_for_model 제거)
# → transformers 로 직접 로드. bge-reranker-v2-m3 = 표준 seq-classification 헤드.
_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"


def _get_reranker():
    global _reranker
    if _reranker is None:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        tok = AutoTokenizer.from_pretrained(_RERANK_MODEL)
        model = AutoModelForSequenceClassification.from_pretrained(_RERANK_MODEL)
        model.eval()
        _reranker = (tok, model)
    return _reranker


def rerank(query: str, hits: list[repo.Hit], limit: int,
           batch_size: int = 4, max_length: int = 2048) -> list[repo.Hit]:
    """cross-encoder 재정렬. 반환 score 는 reranker 점수(다른 arm 과 비교 불가)."""
    if not hits:
        return []
    import torch

    tok, model = _get_reranker()
    scores: list[float] = []
    with torch.no_grad():
        for i in range(0, len(hits), batch_size):
            batch = hits[i : i + batch_size]
            enc = tok([query] * len(batch), [h.text for h in batch],
                      padding=True, truncation=True, max_length=max_length,
                      return_tensors="pt")
            scores.extend(model(**enc).logits.squeeze(-1).tolist())
    order = sorted(zip(hits, scores), key=lambda t: -t[1])
    return [repo.Hit(h.article_pk, float(s), h.text) for h, s in order[:limit]]


def search(query: str, limit: int = 10, depth: int = 30,
           use_rerank: bool = True, use_rewrite: bool = False,
           as_of=None) -> list[repo.Hit]:
    """dense+bm25 융합 검색. depth = arm 당 회수/rerank 후보 폭.

    use_rewrite=True 면 쿼리 재작성(개선계획 ④) 변형들도 arm 별로 검색해
    가중 RRF 로 함께 융합한다. rerank 는 항상 원 쿼리 기준.
    """
    queries = [query]
    if use_rewrite:
        from agent import query_rewrite

        queries += query_rewrite.rewrite(query)
    ranklists: dict[str, list[repo.Hit]] = {}
    weights: dict[str, float] = {}
    for i, q in enumerate(queries):
        for arm in ARMS:
            key = f"{arm}:{i}"
            ranklists[key] = repo.search(q, arm=arm, limit=depth, as_of=as_of)
            weights[key] = 1.0 if i == 0 else VARIANT_WEIGHT
    fused = rrf_fuse(ranklists, limit=depth, weights=weights)
    if use_rerank:
        return rerank(query, fused, limit)
    return fused[:limit]
