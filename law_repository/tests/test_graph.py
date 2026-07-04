"""조문 위임/인용 그래프 — 참조 추출 규칙·엣지 불변식·Phase 5 연동 검증.

전제(live): Neo4j 기동 + `python -m src.ingest.graph` 적재. 미기동/미구축 시 skip.
"""
from __future__ import annotations

import pytest

from src import repository as repo
from src.db.postgres import connect
from src.ingest import graph


# ------------------------------------------------- 단위(DB 불필요)

def test_resolve_qualifier():
    assert graph._resolve_qualifier(None, "002421") == "002421"      # bare → 동일 법령
    assert graph._resolve_qualifier("법", "002421") == "001556"        # 법 → 관세법
    assert graph._resolve_qualifier("영", "006392") == "002421"        # 영 → 시행령
    assert graph._resolve_qualifier("규칙", "001556") == "006392"      # 규칙 → 시행규칙
    assert graph._resolve_qualifier("관세법시행령", "001556") == "002421"
    assert graph._resolve_qualifier("이 법", "002421") == "002421"     # 이 법 → 출발 법령
    assert graph._resolve_qualifier("외국환거래법", "002421") is None   # 외부 → 스킵


def test_extract_refs_resolves_and_filters():
    content = (
        "[관세법 시행령 제233조(구비조건의 확인)]\n"
        "제233조(구비조건의 확인) 법 제226조에 따른 허가ㆍ승인의 증명은 "
        "제234조를 준용하며 「외국환거래법」 제5조는 적용하지 아니한다."
    )
    refs = graph.extract_refs(content, "002421", 23300)
    assert ("001556", 22600) in refs            # 법 제226조 → 관세법
    assert ("002421", 23400) in refs            # bare 제234조 → 동일 시행령
    assert ("002421", 23300) not in refs        # self(제233조) 제외
    assert all(art != 500 for _, art in refs)   # 외국환거래법 제5조 → 외부 스킵


def test_extract_refs_heading_not_selfcited():
    """헤딩의 '관세법 제226조' 라벨이 참조로 새지 않아야(오탐 방지)."""
    content = "[관세법 제226조(허가ㆍ승인 등의 증명 및 확인)]\n제226조(허가) 제245조를 준용한다."
    refs = graph.extract_refs(content, "001556", 22600)
    assert ("001556", 22600) not in refs        # 자기 라벨 제외
    assert ("001556", 24500) in refs            # 제245조만 남음


# ------------------------------------------------- live fixture

@pytest.fixture(scope="module")
def gsession():
    drv = graph.make_driver()
    try:
        drv.verify_connectivity()
    except Exception as e:  # noqa: BLE001
        drv.close()
        pytest.skip(f"Neo4j 접속 불가: {e}")
    with drv.session() as s:
        if s.run("MATCH (a:Article) RETURN count(a) AS c").single()["c"] == 0:
            pytest.skip("조문 그래프 미구축 (python -m src.ingest.graph 필요)")
        yield s
    drv.close()


def test_article_count_matches_current_pg(gsession):
    n = gsession.run("MATCH (a:Article) RETURN count(a) AS c").single()["c"]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM law_articles WHERE is_current AND paragraph_no IS NULL"
        )
        pg = cur.fetchone()[0]
    assert n == pg


def test_no_self_loops(gsession):
    bad = gsession.run("MATCH (a:Article)-[r]->(a) RETURN count(r) AS c").single()["c"]
    assert bad == 0


def test_delegates_direction_hi_to_lo(gsession):
    """DELEGATES 는 항상 상위법령→하위법령(법률→시행령→시행규칙)."""
    bad = gsession.run(
        """
        MATCH (hi:Article)-[:DELEGATES]->(lo:Article)
        WITH CASE hi.law_id WHEN '001556' THEN 1 WHEN '002421' THEN 2 ELSE 3 END AS rh,
             CASE lo.law_id WHEN '001556' THEN 1 WHEN '002421' THEN 2 ELSE 3 END AS rl
        WHERE rh >= rl RETURN count(*) AS bad
        """
    ).single()["bad"]
    assert bad == 0


def test_cites_within_same_law(gsession):
    bad = gsession.run(
        "MATCH (s:Article)-[:CITES]->(t:Article) WHERE s.law_id <> t.law_id "
        "RETURN count(*) AS bad"
    ).single()["bad"]
    assert bad == 0


def test_known_citation_226_to_245(gsession):
    """관세법 제226조 본문의 '제245조제2항을 준용' → CITES(226→245)."""
    c = gsession.run(
        "MATCH (:Article {law_id:'001556', article_no:22600})"
        "-[:CITES]->(:Article {article_no:24500}) RETURN count(*) AS c"
    ).single()["c"]
    assert c >= 1


def test_expand_article_integration(gsession):
    """Phase 5 repository.expand_article 가 이 그래프 위에서 실제 확장을 반환."""
    rec = gsession.run(
        "MATCH (a:Article)-[]->() RETURN a.article_pk AS pk LIMIT 1"
    ).single()
    out = repo.expand_article(rec["pk"])
    assert out
    for e in out:
        assert e["rel"] in ("CITES", "DELEGATES")
        assert isinstance(e["article_pk"], int)
