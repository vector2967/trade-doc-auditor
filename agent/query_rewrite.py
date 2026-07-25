"""쿼리 재작성 (정확도 개선계획 ④) — 일상어 질문 → 법률 용어 멀티쿼리.

우선순위:
1. LLM 재작성 캐시 (agent/data/query_rewrites.json) — scripts/generate_rewrites.py 로
   생성·커밋. API 키 없이도(로컬·Actions) 캐시만으로 동작한다.
2. 규칙 기반 레키콘 — 일상어→법률용어 매핑 + 요건성 질문의 관세법 226조 앵커 라우팅.
   골드셋 오염 방지: 문항별 하드코딩 금지, 도메인 일반 규칙만 둘 것.

반환된 변형 쿼리들은 retrieval.search(rewrite=True) 에서 arm 별로 검색된 뒤
RRF 로 원 쿼리 결과와 융합된다(원 쿼리 가중 우선).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_CACHE_PATH = Path(__file__).resolve().parent / "data" / "query_rewrites.json"
_cache: dict[str, list[str]] | None = None

MAX_VARIANTS = 3   # 백엔드(캐시/레키콘) 각각의 상한
MAX_TOTAL = 4      # 병합 후 총 변형 상한 (변형당 arm 2회 검색 비용)

# 일상어 → 법률 용어 확장. (트리거 정규식, 추가 검색어) — 치환이 아니라 변형 쿼리 생성.
# 통관 도메인 일반 지식 기반 (골드셋 정답 참조 금지).
_LEXICON: list[tuple[str, str]] = [
    (r"돌려받|환불|되돌려", "관세 환급"),
    (r"세금.{0,6}(면제|안 ?내|깎)|면제", "관세 면세 감면"),
    (r"고장|불량|하자|파손|상해|상했|망가|계약.{0,4}(다르|위반)", "위약물품 환급"),
    (r"수리|고치", "재수출 재수입 면세"),
    (r"여행|휴대품|출장|입국", "여행자 휴대품 면세"),
    (r"이사|이주", "이사물품 감면"),
    (r"샘플|견본", "견본품 면세"),
    (r"연구|실험|학술", "학술연구용품 감면"),
    (r"종교|자선|기부", "종교용품 자선용품 면세"),
    (r"가격|값|금액", "과세가격 결정"),
    (r"세율|관세율|얼마", "세율 적용"),
    (r"늦|기한|지연", "신고기한 가산세"),
    (r"잘못 ?(신고|냈)|정정|고쳐", "수정신고 경정청구"),
    (r"FTA|자유무역|협정", "자유무역협정 협정관세 원산지"),
    (r"원산지", "원산지증명서"),
    (r"보세", "보세구역 보세운송"),
    (r"몰수|압수|유치", "몰수 유치 통관보류"),
    (r"밀수|처벌|벌금", "벌칙 몰수 추징"),
]

# 요건성 질문(수입 가능 여부·필요 서류·검사) → 세관장확인 앵커.
_REQUIREMENT_TRIGGER = re.compile(
    r"수입.{0,10}(가능|되나|해도|할 수|허가|승인|요건|절차|서류|검사|검역|확인|신고해야)"
    r"|필요한 ?서류|허가.{0,4}받|검역|승인.{0,4}받"
)
_REQUIREMENT_ANCHOR = "관세법 제226조 세관장확인대상물품 허가 승인 요건 구비 확인"


def _load_cache() -> dict[str, list[str]]:
    global _cache
    if _cache is None:
        _cache = (
            json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if _CACHE_PATH.exists() else {}
        )
    return _cache


def _lexicon_variants(query: str) -> list[str]:
    out: list[str] = []
    for pattern, expansion in _LEXICON:
        if re.search(pattern, query):
            out.append(f"{query} {expansion}")
        if len(out) >= MAX_VARIANTS - 1:
            break
    if _REQUIREMENT_TRIGGER.search(query):
        out.append(_REQUIREMENT_ANCHOR)
    return out[:MAX_VARIANTS]


def rewrite(query: str) -> list[str]:
    """변형 쿼리 목록(원 쿼리 제외). LLM 캐시 + 규칙 레키콘 병합.

    골드셋 실측(2026-07-25): LLM 재작성과 레키콘이 서로 다른 문항을 살림
    (LLM=잠정가격 #4, 레키콘=수출입신고 #45) → 병합이 단독보다 커버리지 넓음.
    """
    out = list(_load_cache().get(query, [])[:MAX_VARIANTS])
    for v in _lexicon_variants(query):
        if v not in out:
            out.append(v)
    return out[:MAX_TOTAL]
