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
GRAPH_SEEDS = 10     # 그래프 확장(개선계획 ⑥) 시드 = 융합 상위 N
GRAPH_MAX_ADD = 15   # 후보 풀에 추가하는 위임 이웃 상한 (rerank 비용 억제)
RERANK_BLEND = 2.0   # 최종 점수 = z(rerank) + w·z(RRF) — rerank 단독 심판 금지.
                     # 47문항 스윕(2026-07-26): 플래토 w∈[1.5,3.0] r@5 0.730, 중앙값 채택.


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


def graph_neighbors(fused: list[repo.Hit]) -> list[repo.Hit]:
    """융합 상위 시드의 DELEGATES 이웃을 rerank 후보로 추가 (개선계획 ⑥).

    골드 정답이 법+영+규칙 세트인 문항에서, 검색이 법 조문만 찾아도 위임
    이웃(시행령·규칙의 상세 조문)이 후보 풀에 들어와 rerank 가 건질 수 있다.
    이웃은 검색 점수가 없어(0.0, 꼬리 배치) rerank 없이는 limit 밖 — use_graph
    는 use_rerank 와 함께 쓰는 것이 전제다.
    """
    neigh = repo.delegation_neighbors([h.article_pk for h in fused[:GRAPH_SEEDS]])
    have = {h.article_pk for h in fused}
    added: list[repo.Hit] = []
    for n in neigh:
        if n["article_pk"] in have:
            continue
        added.append(repo.Hit(n["article_pk"], 0.0, n["content"]))
        if len(added) >= GRAPH_MAX_ADD:
            break
    return added


def search(query: str, limit: int = 10, depth: int = 30,
           use_rerank: bool = True, use_rewrite: bool = False,
           use_graph: bool = False, as_of=None) -> list[repo.Hit]:
    """dense+bm25 융합 검색. depth = arm 당 회수/rerank 후보 폭.

    use_rewrite=True 면 쿼리 재작성(개선계획 ④) 변형들도 arm 별로 검색해
    가중 RRF 로 함께 융합한다. rerank 는 항상 원 쿼리 기준.
    use_graph=True 면 융합 상위의 위임(DELEGATES) 이웃을 후보 풀에 추가한다
    (개선계획 ⑥ — 그래프는 현행 스냅샷이라 as_of 지정 시 자동 생략).
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
    if use_graph and as_of is None and fused:
        fused = fused + graph_neighbors(fused)
    if use_rerank:
        return _blend_rerank(query, fused, limit)
    return fused[:limit]


def _zscores(vals: list[float]) -> list[float]:
    import statistics

    if len(vals) < 2:
        return [0.0] * len(vals)
    m = statistics.mean(vals)
    s = statistics.pstdev(vals) or 1.0
    return [(v - m) / s for v in vals]


def _blend_rerank(query: str, pool: list[repo.Hit], limit: int) -> list[repo.Hit]:
    """rerank 점수와 융합(RRF) 점수의 z-score 가중합으로 최종 정렬.

    rerank 를 단독 심판으로 쓰면 dense+bm25 합의로 상위에 온 조문(특히 절차
    상세를 담은 시행령 조문)을 표면 관련성만으로 밀어낸다 — 47문항 스윕에서
    순수 rerank r@5 0.633 / 순수 융합 0.670 / 혼합(w=2.0) 0.730 으로 혼합이
    양 극단을 모두 이겼다(내부 최적, 플래토 w∈[1.5,3.0]). 그래프 이웃처럼
    융합 점수가 없는(0.0) 후보는 z 에서 자연 페널티를 받되 rerank 가 충분히
    높으면 진입할 수 있다.
    """
    reranked = rerank(query, pool, limit=len(pool))
    zr = dict(zip((h.article_pk for h in reranked),
                  _zscores([h.score for h in reranked])))
    zf = dict(zip((h.article_pk for h in pool),
                  _zscores([h.score for h in pool])))
    blended = sorted(
        pool, key=lambda h: -(zr[h.article_pk] + RERANK_BLEND * zf[h.article_pk])
    )
    return [
        repo.Hit(h.article_pk, zr[h.article_pk] + RERANK_BLEND * zf[h.article_pk], h.text)
        for h in blended[:limit]
    ]
