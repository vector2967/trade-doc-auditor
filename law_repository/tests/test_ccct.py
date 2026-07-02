"""Phase 4 HSK 요건 그래프 불변식 검증.

전제: python -m src.ingest.ccct 적재 완료(부분이라도). 미적재/미기동 시 skip.
"""
from __future__ import annotations

import pytest

from src.db.neo4j import driver as make_driver


@pytest.fixture(scope="module")
def session():
    drv = make_driver()
    try:
        drv.verify_connectivity()
    except Exception as e:
        drv.close()
        pytest.skip(f"Neo4j 접속 불가: {e}")
    with drv.session() as s:
        if s.run("MATCH ()-[r:REQUIRES]->() RETURN count(r) AS c").single()["c"] == 0:
            pytest.skip("REQUIRES 미적재 (python -m src.ingest.ccct 필요)")
        yield s
    drv.close()


def test_requires_only_from_leaf(session):
    """불변식: 요건 엣지는 leaf(:HSK, 10자리)에서만 출발."""
    r = session.run(
        """
        MATCH (h)-[:REQUIRES]->()
        WHERE NOT h:HSK OR h.level <> 10
        RETURN count(*) AS bad
        """
    ).single()
    assert r["bad"] == 0


def test_requires_edge_shape(session):
    """엣지 속성: trade_type ∈ {수출, 수입}, valid_from 은 YYYYMMDD."""
    r = session.run(
        """
        MATCH ()-[r:REQUIRES]->()
        WHERE NOT r.trade_type IN ['수출', '수입']
           OR NOT r.valid_from =~ '\\d{8}'
        RETURN count(*) AS bad
        """
    ).single()
    assert r["bad"] == 0


def test_law_always_has_agency(session):
    """확인법령은 최소 1개 요건승인기관(APPROVED_BY)을 가져야 traverse 가 완성."""
    r = session.run(
        """
        MATCH (law:Law)<-[:REQUIRES]-()
        WHERE NOT (law)-[:APPROVED_BY]->(:Agency)
        RETURN count(DISTINCT law) AS bad
        """
    ).single()
    assert r["bad"] == 0


def test_requirement_traverse(session):
    """설계 §4.3 요건 조회: HSK → 확인법령 → 기관 + 서류가 한 번에 나와야 한다."""
    rec = session.run(
        """
        MATCH (h:HSK)-[r:REQUIRES]->(law:Law)-[:APPROVED_BY]->(ag:Agency)
        RETURN h.code AS hs, r.trade_type AS tt, law.name AS law,
               r.document AS doc, ag.name AS agency
        LIMIT 1
        """
    ).single()
    assert rec and rec["law"] and rec["agency"]
    assert len(rec["hs"]) == 10
