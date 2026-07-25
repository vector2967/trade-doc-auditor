"""골드셋 recall@k 자동 평가 — 통관 DB 테스트 기록지 47문항 (정확도 개선계획 ①).

data/goldset.json 의 (질문 → 정답 조문) 쌍으로 repository.search() 의 dense/bm25
각 arm 재현율을 측정한다. 모든 검색 튜닝(융합·rerank·쿼리재작성·토크나이저)의
전/후 비교 기준선이 된다.

- 조문 매칭 단위: (law_id, article_no). article_no 는 조*100+가지 (lawgo.jo_code 규약).
  분할 청크(항 단위)는 부모 조문과 article_no 가 같아 자동으로 조문 단위로 접힌다.
- recall@k (covered): 코퍼스에 적재된 법령의 정답 조문만 분모로 사용.
- recall@k (strict) : 미적재 법령(고시 등)의 정답도 분모에 포함 — 코퍼스 갭 비용까지 반영.
- gold_optional 은 분모에 넣지 않고 적중만 집계.

실행: law_repository/ 에서  python scripts/eval_recall.py
      [--limit 10] [--k 5 10] [--arms dense bm25] [--out-dir data/eval]
사전조건: Qdrant(검색)·Postgres(laws/law_articles 매핑) 접속 가능(.env 또는 환경변수),
첫 dense 검색은 bge-m3 로딩으로 수십 초.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # agent 레이어(리포 루트)
from src import repository as repo  # noqa: E402
from src.db.postgres import connect  # noqa: E402

# 융합/rerank 는 에이전트 레이어 소유 — arm 이름으로 디스패치
AGENT_ARMS = ("rrf", "rrf_rerank")


def run_search(question: str, arm: str, depth: int):
    if arm in AGENT_ARMS:
        from agent import retrieval

        return retrieval.search(question, limit=depth, use_rerank=(arm == "rrf_rerank"))
    return repo.search(question, arm=arm, limit=depth)

# 리포트 표시용 축약 (없으면 원명 그대로)
SHORT = {
    "관세법": "법",
    "관세법 시행령": "영",
    "관세법 시행규칙": "규칙",
    "자유무역협정의 이행을 위한 관세법의 특례에 관한 법률": "FTA법",
    "자유무역협정의 이행을 위한 관세법의 특례에 관한 법률 시행령": "FTA영",
    "자유무역협정의 이행을 위한 관세법의 특례에 관한 법률 시행규칙": "FTA규칙",
    "수출용 원재료에 대한 관세 등 환급에 관한 특례법": "환급법",
    "수출용 원재료에 대한 관세 등 환급에 관한 특례법 시행령": "환급영",
    "수출용 원재료에 대한 관세 등 환급에 관한 특례법 시행규칙": "환급규칙",
    "대외무역법": "대무법",
    "대외무역법 시행령": "대무영",
    "전기용품 및 생활용품 안전관리법": "전기용품법",
    "수입식품안전관리 특별법": "수입식품법",
    "관세법 제226조의 규정에 의한 세관장확인물품 및 확인방법 지정고시": "세관장확인고시",
    "지식재산권 보호를 위한 수출입통관 사무처리에 관한 고시": "지재권고시",
    "수입통관 사무처리에 관한 고시": "수입통관고시",
}


def jo_code(article: str) -> int:
    """'38-3' → 3803, '106' → 10600 (lawgo.jo_code 와 동일 인코딩)."""
    if "-" in article:
        no, branch = article.split("-", 1)
        return int(no) * 100 + int(branch)
    return int(article) * 100


def jo_label(code: int) -> str:
    return f"{code // 100}" + (f"의{code % 100}" if code % 100 else "")


def pair_label(law_name: str, code: int) -> str:
    return f"{SHORT.get(law_name, law_name)}{jo_label(code)}"


def load_law_map() -> dict[str, str]:
    """laws.law_name → law_id (코퍼스에 실재하는 법령만)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT law_name, law_id FROM laws")
        return dict(cur.fetchall())


def resolve_pks(pks: list[int]) -> dict[int, tuple[str, int]]:
    """article_pk → (law_id, article_no). 분할 자식도 부모와 같은 article_no."""
    if not pks:
        return {}
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, law_id, article_no FROM law_articles WHERE id = ANY(%s)",
            (pks,),
        )
        return {pk: (law_id, art_no) for pk, law_id, art_no in cur.fetchall()}


def gold_pairs(entries: list[dict], law_map: dict[str, str]):
    """골드 항목 → (covered pairs, uncovered [(law, arts)]). 조문 없는 항목(고시)은 법령 단위."""
    covered: list[tuple[str, int, str]] = []  # (law_id, art_code, law_name)
    uncovered: list[tuple[str, list[str]]] = []
    for e in entries:
        law_id = law_map.get(e["law"])
        if law_id is None:
            uncovered.append((e["law"], e["articles"]))
            continue
        for a in e["articles"]:
            covered.append((law_id, jo_code(a), e["law"]))
    return covered, uncovered


def evaluate(goldset: dict, arms: list[str], ks: list[int], limit: int) -> dict:
    law_map = load_law_map()
    id_to_name = {v: k for k, v in law_map.items()}
    depth = max(limit, max(ks))
    results: list[dict] = []
    skipped: list[dict] = []

    questions = [q for q in goldset["questions"]]
    # 1) 검색 (arm × 질문) — pk 수집
    searches: dict[tuple[int, str], list] = {}
    all_pks: set[int] = set()
    for q in questions:
        if q.get("skip"):
            skipped.append({"id": q["id"], "reason": q.get("skip_reason", "")})
            continue
        for arm in arms:
            hits = run_search(q["question"], arm, depth)
            searches[(q["id"], arm)] = hits
            all_pks.update(h.article_pk for h in hits)
    pk_map = resolve_pks(sorted(all_pks))

    # 2) 채점
    for q in questions:
        if q.get("skip"):
            continue
        covered, uncovered = gold_pairs(q["gold"], law_map)
        opt_covered, _ = gold_pairs(q.get("gold_optional", []), law_map)
        gold_set = {(lid, code) for lid, code, _ in covered}
        opt_set = {(lid, code) for lid, code, _ in opt_covered}
        n_strict = len(gold_set) + sum(max(len(arts), 1) for _, arts in uncovered)

        row: dict = {
            "id": q["id"],
            "question": q["question"],
            "n_gold_covered": len(gold_set),
            "n_gold_strict": n_strict,
            "uncovered_laws": [law for law, _ in uncovered],
            "arms": {},
        }
        for arm in arms:
            hits = searches[(q["id"], arm)]
            # 조문 단위로 접기 (rank 순서 유지, 미해석 pk 는 건너뜀)
            ranked: list[tuple[str, int]] = []
            for h in hits:
                pair = pk_map.get(h.article_pk)
                if pair and pair not in ranked:
                    ranked.append(pair)
            found_rank = {
                pair: i + 1 for i, pair in enumerate(ranked) if pair in gold_set
            }
            opt_rank = {pair: i + 1 for i, pair in enumerate(ranked) if pair in opt_set}
            arm_res = {
                "retrieved": [
                    pair_label(id_to_name.get(lid, lid), code) for lid, code in ranked
                ],
                "gold_hits": {
                    pair_label(id_to_name.get(lid, lid), code): rank
                    for (lid, code), rank in sorted(found_rank.items(), key=lambda x: x[1])
                },
                "gold_misses": [
                    pair_label(name, code)
                    for lid, code, name in covered
                    if (lid, code) not in found_rank
                ],
                "optional_hits": {
                    pair_label(id_to_name.get(lid, lid), code): rank
                    for (lid, code), rank in opt_rank.items()
                },
            }
            for k in ks:
                n_hit = sum(1 for r in found_rank.values() if r <= k)
                arm_res[f"recall@{k}"] = n_hit / len(gold_set) if gold_set else None
                arm_res[f"recall@{k}_strict"] = n_hit / n_strict if n_strict else None
                arm_res[f"hit@{k}"] = bool(n_hit)
            row["arms"][arm] = arm_res
        results.append(row)

    # 3) 집계
    summary: dict = {"n_questions": len(results), "n_skipped": len(skipped), "arms": {}}
    for arm in arms:
        agg: dict = {}
        for k in ks:
            cov = [r["arms"][arm][f"recall@{k}"] for r in results
                   if r["arms"][arm][f"recall@{k}"] is not None]
            strict = [r["arms"][arm][f"recall@{k}_strict"] for r in results
                      if r["arms"][arm][f"recall@{k}_strict"] is not None]
            hits = [r["arms"][arm][f"hit@{k}"] for r in results]
            agg[f"mean_recall@{k}"] = sum(cov) / len(cov) if cov else 0.0
            agg[f"mean_recall@{k}_strict"] = sum(strict) / len(strict) if strict else 0.0
            agg[f"hit_rate@{k}"] = sum(hits) / len(hits) if hits else 0.0
        summary["arms"][arm] = agg

    # 코퍼스 갭: 골드에 등장하지만 미적재인 법령
    gap: dict[str, list[int]] = {}
    for r in results:
        for law in r["uncovered_laws"]:
            gap.setdefault(law, []).append(r["id"])
    summary["coverage_gap"] = gap

    return {
        "meta": {
            "measured_at": datetime.now().isoformat(timespec="seconds"),
            "limit": limit, "ks": ks, "arms": arms,
            "goldset": goldset["meta"]["source"],
            "corpus_laws": sorted(law_map),
        },
        "summary": summary,
        "skipped": skipped,
        "questions": results,
    }


# ---------------------------------------------------------------- 리포트

def to_markdown(report: dict) -> str:
    m, s = report["meta"], report["summary"]
    ks = m["ks"]
    lines = [
        "# 골드셋 recall 평가 리포트",
        "",
        f"- 측정 시각: {m['measured_at']}  |  검색 depth: {m['limit']}  |  채점 문항: "
        f"{s['n_questions']} (skip {s['n_skipped']})",
        f"- 코퍼스 법령 {len(m['corpus_laws'])}종: {', '.join(SHORT.get(x, x) for x in m['corpus_laws'])}",
        "",
        "## 요약",
        "",
        "| arm | " + " | ".join(
            f"recall@{k} | recall@{k}(strict) | hit@{k}" for k in ks) + " |",
        "|---|" + "---|" * (3 * len(ks)),
    ]
    for arm, agg in s["arms"].items():
        cells = []
        for k in ks:
            cells += [f"{agg[f'mean_recall@{k}']:.3f}",
                      f"{agg[f'mean_recall@{k}_strict']:.3f}",
                      f"{agg[f'hit_rate@{k}']:.3f}"]
        lines.append(f"| {arm} | " + " | ".join(cells) + " |")

    if s["coverage_gap"]:
        lines += ["", "## 코퍼스 갭 (골드에 있으나 미적재)", ""]
        for law, qids in sorted(s["coverage_gap"].items()):
            lines.append(f"- {law} — 문항 {', '.join(map(str, qids))}")

    for arm in m["arms"]:
        lines += ["", f"## 문항별 ({arm})", "",
                  "| # | recall@" + str(ks[0]) + " | 적중(순위) | 미적중 |", "|---|---|---|---|"]
        for r in report["questions"]:
            a = r["arms"][arm]
            rec = a[f"recall@{ks[0]}"]
            hits = ", ".join(f"{lab}@{rank}" for lab, rank in a["gold_hits"].items()) or "—"
            misses = ", ".join(a["gold_misses"]) or "—"
            opt = "".join(f" (+옵션 {lab}@{rank})" for lab, rank in a["optional_hits"].items())
            lines.append(
                f"| {r['id']} | {'' if rec is None else f'{rec:.2f}'} | {hits}{opt} | {misses} |")

    if report["skipped"]:
        lines += ["", "## 채점 제외 (skip)", ""]
        for sk in report["skipped"]:
            lines.append(f"- #{sk['id']}: {sk['reason']}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    # cp949 콘솔 유니코드 방어 (webapp/ccct 와 동일)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    ap = argparse.ArgumentParser(description="골드셋 recall@k 평가")
    ap.add_argument("--goldset", default="data/goldset.json")
    ap.add_argument("--limit", type=int, default=10, help="arm 당 검색 depth")
    ap.add_argument("--k", type=int, nargs="+", default=[5, 10])
    ap.add_argument("--arms", nargs="+", default=["dense", "bm25", "rrf"],
                    help="dense|bm25|rrf|rrf_rerank (rrf_rerank 는 reranker 모델 필요)")
    ap.add_argument("--out-dir", default="data/eval")
    args = ap.parse_args()

    goldset = json.loads(Path(args.goldset).read_text(encoding="utf-8"))
    report = evaluate(goldset, args.arms, sorted(args.k), args.limit)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "recall_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    md = to_markdown(report)
    (out_dir / "recall_report.md").write_text(md, encoding="utf-8")

    print(md)
    print(f"[eval] 리포트 저장: {out_dir / 'recall_report.md'} / recall_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
