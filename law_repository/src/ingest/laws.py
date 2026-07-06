"""Phase 2 — 법령 초기 적재 (설계 §4.1/§4.2, 로드맵 Phase 2).

현행 조문을 law_articles(PG, 시간버전 원장)에 넣고 Qdrant customs_law_current 에
dense(bge-m3)+bm25(sparse, idf) 로 인덱싱한다.

- 초기 적재 시맨틱: 모든 현행 조문 valid_from=조문시행일자, valid_to=NULL,
  is_current=(valid_from<=today). 버전 닫기(valid_to)·폐지 tombstone 은 Phase 3.
- 멱등: 같은 (law_id, article_no, paragraph_no) 의 열린(valid_to IS NULL) 행 기준
  upsert. content_hash 동일하면 아무것도 안 함(재임베딩 skip — 불변식).
- 청킹: 조문 1청크. SPLIT_THRESHOLD 초과 조문만 항 단위 분할 + parent_article_pk.
  분할 시 부모 행은 Qdrant 미적재(자식이 검색 단위).

실행: law_repository/ 에서  python -m src.ingest.laws  [--no-cache] [--skip-embed]
"""
from __future__ import annotations

import hashlib
import sys
import uuid
from datetime import date, datetime

from src import lawgo
from src.db.postgres import connect
from src.db.qdrant import BM25_VECTOR, COLLECTION, DENSE_VECTOR, client as qdrant_client, ensure_collection
from src.embed import bm25

TARGET_LAWS = [
    {"law_id": "001556", "name": "관세법"},
    {"law_id": "002421", "name": "관세법 시행령"},
    {"law_id": "006392", "name": "관세법 시행규칙"},
]

SPLIT_THRESHOLD = 6000  # bge-m3 8192 토큰 대비 여유. 현행 관세 3법 최장 5,325자 → 분할 0건 예상

_POINT_NS = uuid.uuid5(uuid.NAMESPACE_URL, "trade-doc-auditor/law-article")


def _hierarchy(name: str) -> str:
    if name.endswith("시행령"):
        return "시행령"
    if name.endswith("시행규칙"):
        return "시행규칙"
    return "법률"


def _to_date(yyyymmdd: str) -> date:
    return datetime.strptime(yyyymmdd, "%Y%m%d").date()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def point_id(law_id: str, article_no: int, paragraph_no: int | None, mst: str) -> str:
    return str(uuid.uuid5(_POINT_NS, f"{law_id}:{article_no}:{paragraph_no or 0}:{mst}"))


# ---------------------------------------------------------------- 파싱/청킹

def resolve_current_mst(law_id: str, name: str) -> str:
    """현행법령 목록(lawSearch target=law)에서 법령일련번호(MST) 확인.

    본문 응답의 '법령키'는 법령ID+공포일자+공포번호 연결값이라 MST 가 아님 —
    Phase 3 변경이력(lsHstInf)의 법령일련번호와 조인하려면 진짜 MST 가 필요하다.
    """
    data = lawgo.get("lawSearch.do", target="law", query=name, display=100)
    for it in lawgo.as_list(data.get("LawSearch", {}).get("law")):
        if lawgo.squash(it.get("법령ID")) == law_id:
            return lawgo.squash(it.get("법령일련번호"))
    raise LookupError(f"현행법령 목록에서 법령ID {law_id}({name}) 미발견")


def build_meta(body: dict, mst: str) -> dict:
    info = body.get("법령", {}).get("기본정보", {})
    return {
        "law_id": (info.get("법령ID") or "").strip(),
        "law_name": (info.get("법령명_한글") or "").strip(),
        "mst": mst,
        "enforcement_date": lawgo.squash(info.get("시행일자")),
        "promulgation_date": lawgo.squash(info.get("공포일자")),
        "revision_type": lawgo.content_of(info.get("제개정구분")).strip() or "일부개정",
        "ministry": lawgo.content_of(info.get("소관부처")).strip(),
    }


def _assemble_text(unit: dict) -> str:
    """항/호/목 계층을 들여쓰기 평문으로 (프로토타입 검증 로직 이식)."""
    parts = []
    head = lawgo.clean_text(unit.get("조문내용"))
    if head:
        parts.append(head)
    for hang in lawgo.as_list(unit.get("항")):
        h = lawgo.clean_text(hang.get("항내용"))
        if h:
            parts.append(h)
        for ho in lawgo.as_list(hang.get("호")):
            t = lawgo.clean_text(ho.get("호내용"))
            if t:
                parts.append("  " + t)
            for mok in lawgo.as_list(ho.get("목")):
                m = lawgo.clean_text(mok.get("목내용"))
                if m:
                    parts.append("    " + m)
    return "\n".join(parts)


def parse_articles(body: dict, meta: dict) -> list[dict]:
    """조문단위 → 청크 행 목록. 임계 초과 조문은 항 단위 분할(부모+자식)."""
    units = lawgo.as_list(body.get("법령", {}).get("조문", {}).get("조문단위"))
    rows: list[dict] = []
    for u in units:
        if u.get("조문여부") and u.get("조문여부") != "조문":
            continue
        jo_no = lawgo.squash(u.get("조문번호"))
        jo_branch = lawgo.squash(u.get("조문가지번호")) or "0"
        text = _assemble_text(u)
        if not text:
            continue
        title = lawgo.clean_text(u.get("조문제목"))
        label = lawgo.jo_label(jo_no, jo_branch)
        heading = f"[{meta['law_name']} {label}({title})]" if title else f"[{meta['law_name']} {label}]"
        valid_from = lawgo.squash(u.get("조문시행일자")) or meta["enforcement_date"]
        base = {
            "article_no": lawgo.jo_code(jo_no, jo_branch),
            "title": title or None,
            "valid_from": _to_date(valid_from),
            "label": label,
        }
        full = f"{heading}\n{text}"
        if len(full) <= SPLIT_THRESHOLD:
            rows.append({**base, "paragraph_no": None, "content": full, "children": []})
        else:
            children = []
            for i, hang in enumerate(lawgo.as_list(u.get("항")), start=1):
                parts = [lawgo.clean_text(hang.get("항내용"))]
                for ho in lawgo.as_list(hang.get("호")):
                    parts.append("  " + lawgo.clean_text(ho.get("호내용")))
                    for mok in lawgo.as_list(ho.get("목")):
                        parts.append("    " + lawgo.clean_text(mok.get("목내용")))
                hang_text = "\n".join(p for p in parts if p.strip())
                if hang_text:
                    children.append(
                        {**base, "paragraph_no": i, "content": f"{heading}\n{hang_text}"}
                    )
            rows.append({**base, "paragraph_no": None, "content": full, "children": children})
    return rows


# ---------------------------------------------------------------- PG 적재

def upsert_law(cur, meta: dict) -> None:
    cur.execute(
        """
        INSERT INTO laws (law_id, law_name, hierarchy, ministry)
        VALUES (%(law_id)s, %(law_name)s, %(hierarchy)s, %(ministry)s)
        ON CONFLICT (law_id) DO UPDATE
          SET law_name = EXCLUDED.law_name, ministry = EXCLUDED.ministry
        """,
        {**meta, "hierarchy": _hierarchy(meta["law_name"])},
    )
    cur.execute(
        """
        INSERT INTO law_versions (law_id, mst, promulgation_date, enforcement_date, revision_type)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (law_id, mst) DO NOTHING
        """,
        (
            meta["law_id"],
            meta["mst"],
            _to_date(meta["promulgation_date"]),
            _to_date(meta["enforcement_date"]),
            meta["revision_type"],
        ),
    )


def _upsert_article_row(cur, law_id: str, mst: str, row: dict,
                        parent_pk: int | None = None) -> tuple[int, str]:
    """열린 행(valid_to IS NULL) 기준 upsert. 반환: (pk, 'new'|'changed'|'unchanged')."""
    chash = _content_hash(row["content"])
    is_current = row["valid_from"] <= date.today()
    cur.execute(
        """
        SELECT id, content_hash FROM law_articles
        WHERE law_id = %s AND article_no = %s
          AND paragraph_no IS NOT DISTINCT FROM %s AND item_no IS NULL
          AND valid_to IS NULL
        """,
        (law_id, row["article_no"], row["paragraph_no"]),
    )
    hit = cur.fetchone()
    if hit is None:
        cur.execute(
            """
            INSERT INTO law_articles
              (law_id, article_no, paragraph_no, title, content, content_hash,
               version_mst, valid_from, valid_to, is_current, parent_article_pk)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s)
            RETURNING id
            """,
            (law_id, row["article_no"], row["paragraph_no"], row["title"],
             row["content"], chash, mst, row["valid_from"], is_current, parent_pk),
        )
        return cur.fetchone()[0], "new"
    pk, old_hash = hit
    if old_hash == chash:
        return pk, "unchanged"
    # 초기 적재 시맨틱: 열린 행을 덮어쓴다. (버전 닫기+새 행은 Phase 3 델타 잡의 몫)
    cur.execute(
        """
        UPDATE law_articles
        SET title = %s, content = %s, content_hash = %s, version_mst = %s,
            valid_from = %s, is_current = %s, qdrant_point_id = NULL,
            parent_article_pk = %s
        WHERE id = %s
        """,
        (row["title"], row["content"], chash, mst,
         row["valid_from"], is_current, parent_pk, pk),
    )
    return pk, "changed"


def load_law(cur, meta: dict, rows: list[dict]) -> dict:
    upsert_law(cur, meta)
    stats = {"new": 0, "changed": 0, "unchanged": 0}
    for row in rows:
        pk, status = _upsert_article_row(cur, meta["law_id"], meta["mst"], row)
        stats[status] += 1
        for child in row["children"]:
            _, cstatus = _upsert_article_row(cur, meta["law_id"], meta["mst"], child, parent_pk=pk)
            stats[cstatus] += 1
    return stats


# ---------------------------------------------------------------- Qdrant 인덱싱

def index_qdrant(batch_size: int = 64) -> int:
    """is_current 이고 아직 포인트 없는 조문(분할 부모 제외)을 임베딩→upsert."""
    from qdrant_client import models

    from src.embed import dense

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.law_id, l.law_name, l.hierarchy, a.article_no,
                   a.paragraph_no, a.item_no, a.valid_from, a.content, a.version_mst
            FROM law_articles a JOIN laws l USING (law_id)
            WHERE a.is_current AND a.qdrant_point_id IS NULL
              AND NOT EXISTS (SELECT 1 FROM law_articles c WHERE c.parent_article_pk = a.id)
            ORDER BY a.id
            """
        )
        pending = cur.fetchall()
    if not pending:
        print("[qdrant] 인덱싱 대상 없음 (모두 최신)")
        return 0

    qc = qdrant_client()
    ensure_collection(qc)
    total = 0
    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        texts = [r[8] for r in batch]
        dvecs = dense.encode(texts)
        points, ids = [], []
        for r, dv in zip(batch, dvecs):
            pk, law_id, law_name, hierarchy, art_no, para_no, item_no, vfrom, text, mst = r
            pid = point_id(law_id, art_no, para_no, mst)
            sidx, sval = bm25.encode_doc(text)
            points.append(
                models.PointStruct(
                    id=pid,
                    vector={
                        DENSE_VECTOR: dv.tolist(),
                        BM25_VECTOR: models.SparseVector(indices=sidx, values=sval),
                    },
                    payload={
                        "article_pk": pk,
                        "law_id": law_id,
                        "law_name": law_name,
                        "hierarchy": hierarchy,
                        "article_no": art_no,
                        "paragraph_no": para_no,
                        "item_no": item_no,
                        "enforcement_date": vfrom.isoformat(),
                        "text": text,
                    },
                )
            )
            ids.append((pid, pk))
        qc.upsert(collection_name=COLLECTION, points=points)
        with connect() as conn, conn.cursor() as cur:
            cur.executemany(
                "UPDATE law_articles SET qdrant_point_id = %s WHERE id = %s", ids
            )
        total += len(points)
        print(f"[qdrant] {total}/{len(pending)} upsert")
    return total


# ---------------------------------------------------------------- 검증

def verify() -> None:
    with connect() as conn, conn.cursor() as cur:
        print("\n[PG 법령별 조문 수]")
        cur.execute(
            """
            SELECT l.law_name, count(*) FILTER (WHERE a.is_current) AS current,
                   count(*) AS total,
                   count(*) FILTER (WHERE a.qdrant_point_id IS NOT NULL) AS indexed
            FROM law_articles a JOIN laws l USING (law_id)
            GROUP BY l.law_name ORDER BY l.law_name
            """
        )
        for name, current, total, indexed in cur.fetchall():
            print(f"  {name}: 현행 {current} / 전체 {total} / 인덱싱 {indexed}")
        cur.execute("SELECT count(*) FROM law_articles WHERE parent_article_pk IS NOT NULL")
        print(f"  분할 청크(항 단위): {cur.fetchone()[0]}건")

    qc = qdrant_client()
    cnt = qc.count(COLLECTION).count
    print(f"[Qdrant] {COLLECTION} 포인트 수: {cnt}")


def smoke_search(query: str = "수입신고 시 세관장 확인이 필요한 물품") -> None:
    """dense arm / bm25 arm 각각 3건 — 융합·rerank 는 에이전트 소유(설계 §4.2)."""
    from qdrant_client import models

    from src.embed import dense

    qc = qdrant_client()
    dv = dense.encode([query])[0]
    hits = qc.query_points(COLLECTION, query=dv.tolist(), using=DENSE_VECTOR, limit=3)
    print(f"\n[smoke/dense] '{query}'")
    for p in hits.points:
        print(f"  {p.score:.4f} {p.payload['law_name']} 조문 {p.payload['article_no']}")
    sidx, sval = bm25.encode_query(query)
    hits = qc.query_points(
        COLLECTION,
        query=models.SparseVector(indices=sidx, values=sval),
        using=BM25_VECTOR,
        limit=3,
    )
    print("[smoke/bm25]")
    for p in hits.points:
        print(f"  {p.score:.4f} {p.payload['law_name']} 조문 {p.payload['article_no']}")


def main(argv: list[str]) -> int:
    use_cache = "--no-cache" not in argv
    for spec in TARGET_LAWS:
        body = lawgo.fetch_current_law(spec["law_id"], use_cache=use_cache)
        mst = resolve_current_mst(spec["law_id"], spec["name"])
        meta = build_meta(body, mst)
        assert meta["law_id"] == spec["law_id"], f"법령ID 불일치: {meta}"
        rows = parse_articles(body, meta)
        with connect() as conn, conn.cursor() as cur:
            stats = load_law(cur, meta, rows)
        split = sum(1 for r in rows if r["children"])
        print(
            f"[pg] {meta['law_name']} (MST {meta['mst']}, 시행 {meta['enforcement_date']}): "
            f"조문 {len(rows)} (분할 {split}) → {stats}"
        )
    if "--skip-embed" not in argv:
        index_qdrant()
    verify()
    if "--skip-embed" not in argv:
        smoke_search()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
