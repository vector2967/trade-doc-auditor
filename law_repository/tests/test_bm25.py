"""BM25 sparse 인코더 단위 테스트 (DB 불필요)."""
from __future__ import annotations

from src.embed import bm25


def test_doc_and_query_share_token_space():
    """조사 붙은 표층형도 형태소 단위로 매칭돼야 한다."""
    d_idx, _ = bm25.encode_doc("수입물품의 통관")
    q_idx, _ = bm25.encode_query("물품")
    assert set(q_idx) & set(d_idx), "쿼리 '물품' 토큰이 문서와 겹치지 않음"


def test_josa_stripped():
    """조사만 다른 표층형('물품을'↔'물품')은 같은 토큰이어야 한다."""
    assert bm25.tokenize("물품을") == bm25.tokenize("물품")
    assert bm25.tokenize("세관장은") == bm25.tokenize("세관장")


def test_prefix_compound_distinguishes():
    """재수입/재수출 — 접두사 결합 토큰으로 유사 제도가 구분돼야 한다."""
    resu_in = bm25.tokenize("재수입")
    resu_out = bm25.tokenize("재수출")
    assert "재수입" in resu_in and "수입" in resu_in
    assert "재수출" in resu_out
    assert "재수입" not in resu_out


def test_adjacent_noun_compound():
    """붙여 쓴 복합명사는 결합 토큰도 방출 (부분 + 전체)."""
    toks = bm25.tokenize("잠정가격으로 가격신고")
    assert "잠정가격" in toks and "가격신고" in toks
    assert "잠정" in toks and "신고" in toks


def test_boilerplate_removed():
    """조사·어미·상투어(따라/경우/것/수)는 토큰이 되지 않는다."""
    toks = bm25.tokenize("대통령령으로 정하는 바에 따라 신고할 수 있는 경우")
    assert "신고" in toks
    for noise in ("경우", "바", "따르", "수", "있"):
        assert noise not in toks


def test_deterministic():
    assert bm25.encode_doc("관세법 제226조") == bm25.encode_doc("관세법 제226조")
    assert bm25.token_id("세관장") == bm25.token_id("세관장")


def test_tf_saturation():
    """같은 토큰 반복 시 값이 tf 에 따라 증가하되 포화(k1+1 상한)해야 한다."""
    _, v1 = bm25.encode_doc("관세")
    _, v5 = bm25.encode_doc("관세 " * 5)
    assert max(v5) > max(v1)
    assert max(v5) < bm25.K1 + 1


def test_empty_text():
    assert bm25.encode_doc("") == ([], [])
    assert bm25.encode_query("...") == ([], [])
