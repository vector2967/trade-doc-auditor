"""BM25 sparse 인코더 (설계 §4.2 — 'BM25 검색 근거').

Qdrant 네이티브 sparse + `modifier: idf`(1.15.2+) 조합:
- IDF 는 Qdrant 가 서버사이드에서 계산.
- 클라이언트는 문서측 TF 성분만 보낸다:  tf*(k1+1) / (tf + k1*(1-b + b*len/avg_len))
- 쿼리측은 토큰당 1.0 (fastembed Bm25 와 동일한 관례).

한국어 처리: Kiwi 형태소 분석(개선계획 ⑤ — 종전 한글 2-gram 대체).
내용어(명사류·숫자·어근·명사파생접미사·외국어·용언 어간)만 남기고 조사·어미·
의존명사(것/수/등)를 버려 "신고/가격/물품" 류 범용 표층형의 부분 겹침 어그로를
줄인다. 전 조문에 등장하는 상투 용언(따르-/위하-/의하-)은 불용어로 제거.
인덱스·쿼리가 반드시 같은 인코더를 써야 하므로 양쪽 모두 이 모듈만 사용할 것.
토크나이저를 바꾸면 전체 재인덱싱(qdrant_point_id 리셋 후 index_qdrant) 필수.

토큰 → 인덱스 는 md5 앞 4바이트(uint32) — 결정적이라 재적재에 안전.
"""
from __future__ import annotations

import hashlib
from collections import Counter

K1 = 1.2
B = 0.75
AVG_LEN = 141.0  # 형태소 토큰 기준 코퍼스 실측 평균 (65종 5,897청크, 2026-07-25)

# 남길 품사(kiwipiepy 태그, -I/-R 변이는 기본형으로 접음):
#   NNG/NNP 명사, NR/SN 수사·숫자, XR 어근, XSN 명사파생접미사(세/장/료…),
#   SL/SH 외국어·한자, VV/VA 용언 어간(활용형은 Kiwi 가 기본형으로 복원)
_KEEP_TAGS = {"NNG", "NNP", "NR", "SN", "XR", "XSN", "SL", "SH", "VV", "VA"}

# 법률 상투어 — 거의 모든 조문에 나와 신호가 없는데 문서 길이만 부풀리는 토큰
_STOPWORDS = {
    "들", "등", "것", "경우", "다음", "해당", "관하", "대하", "따르", "의하", "위하",
    "규정", "정하", "각", "호", "항", "조", "바",
    "하", "되", "있", "없", "않", "아니",  # 범용 용언 어간
}

_kiwi = None


def _get_kiwi():
    global _kiwi
    if _kiwi is None:
        from kiwipiepy import Kiwi

        _kiwi = Kiwi()
    return _kiwi


def _root(tag: str) -> str:
    return tag.split("-")[0]


def tokenize(text: str) -> list[str]:
    """내용어 형태소 + 접두/접미 복합 토큰.

    Kiwi 는 '재수입'→재(XPN)+수입, '세관장'→세관+장, '가격신고'→가격+신고 로
    분해한다. 부분 형태소만 남기면 재수입↔재수출·가격신고↔수입신고 같은 유사
    제도 구분이 사라지므로, 원문에서 인접(start 연속)한 XPN+명사 / 명사+명사 /
    명사+XSN 쌍은 결합형도 함께 방출한다 ('재수입'→[재수입, 수입],
    '가격신고'→[가격, 가격신고, 신고]). 결합은 원문에서 붙여 쓴 경우에만
    일어나며, 양측이 같은 규칙이라 인덱스·쿼리 정합이 유지된다.
    """
    toks = _get_kiwi().tokenize(text)
    out: list[str] = []
    _NOUNISH = ("NNG", "NNP", "XR", "XSN")
    for i, tok in enumerate(toks):
        tag = _root(tok.tag)
        form = tok.form.lower()
        if tag == "XPN":  # 접두사 단독은 신호가 약함 — 다음 명사와의 결합형만
            nxt = toks[i + 1] if i + 1 < len(toks) else None
            if (
                nxt is not None
                and _root(nxt.tag) in _NOUNISH
                and nxt.start == tok.start + tok.len
            ):
                out.append(form + nxt.form.lower())
            continue
        if tag not in _KEEP_TAGS or form in _STOPWORDS:
            continue
        out.append(form)
        if tag in _NOUNISH and i > 0:
            prv = toks[i - 1]
            if (
                _root(prv.tag) in _NOUNISH
                and prv.form.lower() not in _STOPWORDS
                and tok.start == prv.start + prv.len
            ):
                out.append(prv.form.lower() + form)
    return out


def token_id(token: str) -> int:
    return int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:4], "little")


def encode_doc(text: str) -> tuple[list[int], list[float]]:
    """문서측 sparse 벡터 (indices, values)."""
    tokens = tokenize(text)
    if not tokens:
        return [], []
    length = len(tokens)
    indices, values = [], []
    for tok, tf in Counter(tokens).items():
        indices.append(token_id(tok))
        values.append(tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / AVG_LEN)))
    return indices, values


def encode_query(text: str) -> tuple[list[int], list[float]]:
    """쿼리측 sparse 벡터 — 토큰당 1.0."""
    ids = {token_id(tok) for tok in tokenize(text)}
    return list(ids), [1.0] * len(ids)
