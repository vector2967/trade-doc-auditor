"""Phase 5+ — 조문 위임/인용 그래프 (설계 §4.3(1), 로드맵 Phase 5 'Neo4j 위임/인용 확장').

현행 조문을 :Article 노드로 만들고, 조문 본문에서 상호참조를 추출해 두 종류의 엣지를 건다.
Phase 5 `repository.expand_article()` 가 이 그래프 위에서 동작한다.

노드
  (:Article {article_pk, law_id, law_name, hierarchy, article_no, title})
    article_pk = PG law_articles.id (현행 whole-article 행). PG·Qdrant 와 동일 키.

엣지 (설계 §4.3(1))
  (:Article)-[:CITES     {basis, count}]->(:Article)   -- 동일 법령 내 조문 상호참조
  (:Article)-[:DELEGATES {basis, count}]->(:Article)   -- 법령 위계를 가로지르는 참조
      방향은 항상 상위법령→하위법령(법률→시행령→시행규칙). 인용 방향과 무관하게
      구조적 위임/시행 관계로 본다. 상위 조문이 하위 조문으로 세부를 위임한 링크.

참조 추출 (실측 기반)
  - 헤딩 라인 `[법령명 라벨(제목)]` 은 자기 라벨이라 제거(관세'법 제1조' 꼬리 오탐 방지).
  - 「」 낫표 제거, "관세법 시행령/시행규칙" 은 단일 토큰으로 정규화.
  - 한정어(법/영/규칙/시행령/시행규칙/관세법…) + 제N조[의M] → 대상 법령 해석.
    · 코퍼스 16종(FAMILIES)만 노드 대상. bare 법/영/규칙 은 출발 조문의 패밀리 내에서 해석.
    · 이 법·같은 법 → 출발 조문의 법령. 한정어 없는 bare 제N조 → 동일 법령(self).
    · 외부 법령(외국환거래법 등) → 노드 없음 → 스킵. self 참조도 스킵.
    · DELEGATES 는 동일 패밀리(법률→시행령→시행규칙) 내에서만. 패밀리 밖 참조는 CITES.

DETAILED_IN(확인법령↔조문)은 확인법령(약사법 등)이 코퍼스에 없어 현재 데이터로 도출
불가 — 확인법령 본문 적재 후 별도 단계. 여기서는 만들지 않는다.

실행: law_repository/ 에서  python -m src.ingest.graph  [--nodes-only|--edges-only|--keep]
"""
from __future__ import annotations

import re
import sys

from src.db.neo4j import driver as make_driver
from src.db.postgres import connect
from src.lawgo import jo_code

# 코퍼스 16종 — 패밀리별 위계. rank = 1 법률 / 2 시행령 / 3 시행규칙 (작을수록 상위).
# DELEGATES 는 동일 패밀리 안에서만 상위→하위. TARGET_LAWS(src/ingest/laws.py)와 정합 유지.
FAMILIES = {
    "관세법": {1: "001556", 2: "002421", 3: "006392"},
    "FTA관세특례법": {1: "010097", 2: "010175", 3: "010172"},
    "관세환급특례법": {1: "000592", 2: "004043", 3: "007591"},
    "대외무역법": {1: "001467", 2: "003313"},
    "식물방역법": {1: "001513"},
    "수입식품특별법": {1: "012247"},
    "약사법": {1: "001783"},
    "화장품법": {1: "002015"},
    "전기용품법": {1: "001459"},
}
LAW_RANK = {law_id: rank for fam in FAMILIES.values() for rank, law_id in fam.items()}
LAW_FAMILY = {law_id: name for name, fam in FAMILIES.items() for law_id in fam.values()}


def family_of(law_id: str) -> str:
    """FAMILIES 미등록 법령(특수물자 개별법 등)은 자기 자신이 단독 패밀리(법률)."""
    return LAW_FAMILY.get(law_id, law_id)


def rank_of(law_id: str) -> int:
    return LAW_RANK.get(law_id, 1)


def family_maps(law_ids) -> tuple[dict[str, str], dict[str, int]]:
    """Cypher 파라미터용 전체 (family, rank) 맵 — 미등록 법령 포함."""
    ids = set(law_ids) | set(LAW_FAMILY)
    return ({lid: family_of(lid) for lid in ids}, {lid: rank_of(lid) for lid in ids})

# 본문에 등장하는 법령명 → 패밀리. 긴 이름 먼저 매칭(endswith).
_NAMED_FAMILY = [
    ("수입식품안전관리특별법", "수입식품특별법"),
    ("전기용품및생활용품안전관리법", "전기용품법"),
    ("대외무역법", "대외무역법"),
    ("식물방역법", "식물방역법"),
    ("약사법", "약사법"),
    ("화장품법", "화장품법"),
    ("관세법", "관세법"),
]

# 한정어(law qualifier) 후보. 긴/구체적인 것 먼저. 「」 는 사전 정규화됨.
_Q = (
    r"[가-힣]{2,15}법\s?시행규칙|[가-힣]{2,15}법\s?시행령|이\s?법|같은\s?법|이\s?영|같은\s?영|"
    r"시행규칙|시행령|[가-힣]{2,15}법|법|영|규칙"
)
_REF = re.compile(rf"(?:(?P<q>{_Q})\s*)?제(?P<no>\d+)조(?:의(?P<b>\d+))?")


def _resolve_qualifier(q: str | None, source_law_id: str) -> str | None:
    """한정어 → 대상 법령ID. 코퍼스 밖이면 None(스킵). None 한정어 = 동일 법령."""
    if q is None:
        return source_law_id
    qn = q.replace(" ", "")
    if qn in ("이법", "같은법", "이영", "같은영"):
        return source_law_id
    # 법령명 명시 → 해당 패밀리, 접미(시행령/시행규칙)로 rank 결정
    rank = 1
    if qn.endswith("시행규칙"):
        rank, qn = 3, qn[: -len("시행규칙")]
    elif qn.endswith("시행령"):
        rank, qn = 2, qn[: -len("시행령")]
    for name, fam in _NAMED_FAMILY:
        if qn.endswith(name):
            return FAMILIES[fam].get(rank)
    # bare 법/영/규칙 → 출발 조문의 패밀리 내 위계로 해석 (미등록 법령은 자기 자신=법률만)
    src_fam = FAMILIES.get(family_of(source_law_id), {1: source_law_id})
    if qn in ("규칙", ""):  # ""=시행규칙/시행령 단독(위에서 접미 제거됨)
        return src_fam.get(rank if rank > 1 else 3)
    if qn == "영":
        return src_fam.get(2)
    if qn == "법":
        return src_fam.get(1)
    return None  # 외국환거래법 등 외부 법령


def _strip_heading(content: str) -> str:
    body = content
    if body.startswith("["):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    body = body.replace("「", "").replace("」", "")
    return body.replace("관세법 시행규칙", "관세법시행규칙").replace("관세법 시행령", "관세법시행령")


def extract_refs(content: str, source_law_id: str, source_art_no: int):
    """조문 본문 → {(대상 law_id, 대상 article_no): basis} 참조 집합. self/외부는 제외."""
    body = _strip_heading(content)
    out: dict[tuple[str, int], str] = {}
    for m in _REF.finditer(body):
        t_law = _resolve_qualifier(m.group("q"), source_law_id)
        if t_law is None:
            continue
        t_art = jo_code(m.group("no"), m.group("b") or 0)
        if (t_law, t_art) == (source_law_id, source_art_no):
            continue  # self
        out.setdefault((t_law, t_art), m.group().strip())
    return out


# ------------------------------------------------------------ Neo4j 적재

_NODE_CYPHER = """
UNWIND $rows AS r
MERGE (a:Article {article_pk: r.article_pk})
SET a.law_id = r.law_id, a.law_name = r.law_name, a.hierarchy = r.hierarchy,
    a.article_no = r.article_no, a.title = r.title
"""

_EDGE_CYPHER = """
UNWIND $rows AS r
MATCH (s:Article {article_pk: r.s}), (t:Article {article_pk: r.t})
MERGE (s)-[e:%s]->(t)
SET e.basis = r.basis, e.count = r.count
"""


def _fetch_current_articles():
    """현행 whole-article 행(분할 자식 제외). 반환: node dict + (law_id,art_no)->pk 맵 + 본문."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.law_id, l.law_name, l.hierarchy, a.article_no, a.title, a.content
            FROM law_articles a JOIN laws l USING (law_id)
            WHERE a.is_current AND a.paragraph_no IS NULL
            ORDER BY a.id
            """
        )
        return cur.fetchall()


def build(keep: bool = False, nodes: bool = True, edges: bool = True) -> None:
    rows = _fetch_current_articles()
    node_rows = [
        {"article_pk": r[0], "law_id": r[1], "law_name": r[2], "hierarchy": r[3],
         "article_no": r[4], "title": r[5]}
        for r in rows
    ]
    pk_by_key = {(r[1], r[4]): r[0] for r in rows}

    drv = make_driver()
    try:
        with drv.session() as s:
            if not keep and nodes:
                s.run("MATCH (a:Article) DETACH DELETE a")  # 결정적 재구축
            s.run("CREATE CONSTRAINT article_pk IF NOT EXISTS "
                  "FOR (a:Article) REQUIRE a.article_pk IS UNIQUE")
            if nodes:
                for i in range(0, len(node_rows), 500):
                    s.run(_NODE_CYPHER, rows=node_rows[i : i + 500])
                print(f"[graph] :Article 노드 {len(node_rows)}건")

            if edges:
                cites, delegates = [], []
                for r in rows:
                    pk, law_id, _, _, art_no, _, content = r
                    for (t_law, t_art), basis in extract_refs(content, law_id, art_no).items():
                        t_pk = pk_by_key.get((t_law, t_art))
                        if t_pk is None:  # 대상 조문이 현행 노드에 없음(폐지/미적재)
                            continue
                        if family_of(law_id) != family_of(t_law) or law_id == t_law:
                            cites.append({"s": pk, "t": t_pk, "basis": basis})
                        else:  # 동일 패밀리 위계 교차 → 상위→하위 위임
                            hi, lo = (pk, t_pk) if rank_of(law_id) < rank_of(t_law) else (t_pk, pk)
                            delegates.append({"s": hi, "t": lo, "basis": basis})
                _write_edges(s, "CITES", _dedupe(cites))
                _write_edges(s, "DELEGATES", _dedupe(delegates))
                print(f"[graph] CITES {len(_dedupe(cites))} · DELEGATES {len(_dedupe(delegates))}")
    finally:
        drv.close()


def _dedupe(edges: list[dict]) -> list[dict]:
    """(s,t) 별 중복 병합 — count 누적, basis 는 첫 표현."""
    agg: dict[tuple[int, int], dict] = {}
    for e in edges:
        key = (e["s"], e["t"])
        if key in agg:
            agg[key]["count"] += 1
        else:
            agg[key] = {**e, "count": 1}
    return list(agg.values())


def _write_edges(s, rel: str, rows: list[dict]) -> None:
    for i in range(0, len(rows), 500):
        s.run(_EDGE_CYPHER % rel, rows=rows[i : i + 500])


# ------------------------------------------------------------ 검증

def verify() -> None:
    drv = make_driver()
    try:
        with drv.session() as s:
            n = s.run("MATCH (a:Article) RETURN count(a) AS c").single()["c"]
            c = s.run("MATCH ()-[e:CITES]->() RETURN count(e) AS c").single()["c"]
            d = s.run("MATCH ()-[e:DELEGATES]->() RETURN count(e) AS c").single()["c"]
            print(f"[verify] :Article {n} | CITES {c} | DELEGATES {d}")
            # 위임 방향 불변식: 동일 패밀리 + 시작이 끝보다 상위 위계여야
            law_ids = [r["lid"] for r in s.run(
                "MATCH (a:Article) RETURN DISTINCT a.law_id AS lid")]
            fam, rank = family_maps(law_ids)
            bad = s.run(
                """
                MATCH (hi:Article)-[:DELEGATES]->(lo:Article)
                WHERE $fam[hi.law_id] <> $fam[lo.law_id]
                   OR $rank[hi.law_id] >= $rank[lo.law_id]
                RETURN count(*) AS bad
                """,
                fam=fam, rank=rank,
            ).single()["bad"]
            print(f"[verify] DELEGATES 방향 위반(상위→하위 아님): {bad}")
            print("\n[sample] 관세법 제226조가 위임한 하위 조문:")
            for r in s.run(
                """
                MATCH (:Article {law_id:'001556', article_no:22600})-[:DELEGATES]->(t:Article)
                RETURN t.law_name AS ln, t.article_no AS ano, t.title AS ti LIMIT 5
                """
            ):
                print(f"  → {r['ln']} 제{r['ano']//100}조({r['ti']})")
            print("[sample] 관세법 제226조가 인용한 동일법령 조문:")
            for r in s.run(
                """
                MATCH (:Article {law_id:'001556', article_no:22600})-[:CITES]->(t:Article)
                RETURN t.article_no AS ano, t.title AS ti LIMIT 5
                """
            ):
                print(f"  → 제{r['ano']//100}조({r['ti']})")
    finally:
        drv.close()


def main(argv: list[str]) -> int:
    for _stream in (sys.stdout, sys.stderr):  # cp949 콘솔에서 em-dash 등 크래시 방지
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    keep = "--keep" in argv
    nodes = "--edges-only" not in argv
    edges = "--nodes-only" not in argv
    build(keep=keep, nodes=nodes, edges=edges)
    verify()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
