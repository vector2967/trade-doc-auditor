"""쿼리 재작성 (정확도 개선계획 ④) — 일상어 질문 → 법률 용어 멀티쿼리.

우선순위:
1. LLM 재작성 캐시 (agent/data/query_rewrites.json) — scripts/generate_rewrites.py 로
   생성·커밋. API 키 없이도(로컬·Actions) 캐시만으로 동작한다.
2. 실시간 LLM 재작성 — 캐시 미스 + ANTHROPIC_API_KEY 존재 시에만. 결과는
   런타임 캐시(query_rewrites_runtime.json, gitignore)에 저장해 같은 질문 재호출 방지.
   키 없음·타임아웃·파싱 실패는 조용히 3번으로 폴백 — 시연이 LLM 장애에 안 죽는다.
3. 규칙 기반 레키콘 — 일상어→법률용어 매핑 + 요건성 질문의 관세법 226조 앵커 라우팅.
   골드셋 오염 방지: 문항별 하드코딩 금지, 도메인 일반 규칙만 둘 것.

반환된 변형 쿼리들은 retrieval.search(rewrite=True) 에서 arm 별로 검색된 뒤
RRF 로 원 쿼리 결과와 융합된다(원 쿼리 가중 우선).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_CACHE_PATH = Path(__file__).resolve().parent / "data" / "query_rewrites.json"
_RUNTIME_PATH = Path(__file__).resolve().parent / "data" / "query_rewrites_runtime.json"
_cache: dict[str, list[str]] | None = None
_runtime: dict[str, list[str]] | None = None

# 실시간 재작성용 프롬프트 — generate_rewrites.py(캐시 생성)와 공유
LLM_SYSTEM = """\
너는 한국 관세·무역 법령 검색 시스템의 쿼리 재작성기다. 일상어 질문을 받아,
법령 조문(관세법·시행령·시행규칙, FTA관세특례법, 관세환급특례법, 대외무역법,
식물방역법, 수입식품안전관리 특별법, 약사법, 화장품법, 전기용품법 등)에 실제로
등장하는 법률 용어로 표현한 검색 쿼리 변형을 만든다.

규칙:
- 변형은 최대 3개. 각각 질문의 핵심 의도를 법률 용어로 바꾼 짧은 검색어구.
- 유사 제도를 혼동하지 말 것 (재수입면세≠재수출면세, 잠정가격신고≠가격신고,
  경정≠환급, 수정신고≠경정청구 등) — 질문이 어느 쪽인지 분명할 때만 그 용어 사용.
- 수입 가능 여부·요건·필요서류 질문이면 "관세법 제226조 세관장확인" 앵커 변형을 포함.
- 답을 추측해 특정 조문 번호를 넣지 말 것 (제226조 앵커만 예외).
- 출력은 반드시 JSON 배열 하나만: ["변형1", "변형2", ...]  다른 텍스트 금지."""


def parse_llm_variants(text: str) -> list[str]:
    """응답에서 JSON 배열 추출 (호환 모델이 코드펜스/부연을 붙여도 방어)."""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise ValueError(f"JSON 배열 미발견: {text[:200]}")
    variants = json.loads(m.group())
    return [v.strip() for v in variants if isinstance(v, str) and v.strip()][:3]

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

# 개인 해외구매(직구) 질문 → 소액·자가사용 면세 앵커. 원 문장을 붙이면
# "직구/싸게" 표층이 가격결정 조문으로 어그로를 끌어 독립 쿼리로 둔다.
_SHOPPING_TRIGGER = re.compile(r"직구|구매대행|배송대행|해외.{0,4}(쇼핑|주문)")
_SHOPPING_ANCHOR = "소액물품 면세 자가사용물품 관세 면제 한도"


def _load_cache() -> dict[str, list[str]]:
    global _cache
    if _cache is None:
        _cache = (
            json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if _CACHE_PATH.exists() else {}
        )
    return _cache


def _load_runtime() -> dict[str, list[str]]:
    global _runtime
    if _runtime is None:
        _runtime = (
            json.loads(_RUNTIME_PATH.read_text(encoding="utf-8"))
            if _RUNTIME_PATH.exists() else {}
        )
    return _runtime


def _llm_variants_live(query: str) -> list[str] | None:
    """캐시 미스 시 실시간 LLM 재작성. 키 없음/실패 → None (레키콘만 사용).

    실패한 질문도 런타임 캐시에 [] 로 남겨 같은 세션·다음 세션에서 재호출하지
    않는다(시연 중 같은 질문을 다시 칠 때 지연 반복 방지).
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    runtime = _load_runtime()
    if query in runtime:
        return runtime[query] or None
    variants: list[str] = []
    try:
        import anthropic

        # 재시도 0 — 재작성은 best-effort: 실패하면 레키콘 폴백이 기다리고 있고,
        # 시연 중 응답 지연(타임아웃×재시도)이 실패 자체보다 나쁘다.
        client = anthropic.Anthropic(
            timeout=float(os.environ.get("QUERY_REWRITE_TIMEOUT", "10")),
            max_retries=0)
        response = client.messages.create(
            model=os.environ.get("QUERY_REWRITE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=512,
            system=LLM_SYSTEM,
            messages=[{"role": "user", "content": query}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        variants = parse_llm_variants(text)
    except Exception:  # noqa: BLE001 — 어떤 장애도 검색 자체를 막으면 안 됨
        pass
    runtime[query] = variants
    try:
        _RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
        _RUNTIME_PATH.write_text(
            json.dumps(runtime, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return variants or None


def _lexicon_variants(query: str) -> list[tuple[str, bool]]:
    """(변형, 앵커 여부). 앵커 = 원 문장 없이 법률 용어만으로 만든 독립 쿼리."""
    out: list[tuple[str, bool]] = []
    for pattern, expansion in _LEXICON:
        if re.search(pattern, query):
            out.append((f"{query} {expansion}", False))
        if len(out) >= MAX_VARIANTS - 1:
            break
    if _REQUIREMENT_TRIGGER.search(query):
        out.append((_REQUIREMENT_ANCHOR, True))
    if _SHOPPING_TRIGGER.search(query):
        out.append((_SHOPPING_ANCHOR, True))
    return out[:MAX_VARIANTS]


def rewrite_tagged(query: str) -> list[tuple[str, bool]]:
    """(변형, 앵커 여부) 목록 — retrieval 이 앵커에 원 쿼리급 가중을 주는 데 사용.

    골드셋 실측(2026-07-25): LLM 재작성과 레키콘이 서로 다른 문항을 살림
    (LLM=잠정가격 #4, 레키콘=수출입신고 #45) → 병합이 단독보다 커버리지 넓음.
    앵커 가중 실측(2026-07-26): 일상어("직구")가 법률 용어와 안 겹치는 질문은
    변형 0.6 가중으론 융합 중위권에 그침 — 앵커만 1.0 으로 승격.
    """
    llm = _load_cache().get(query)
    if llm is None:
        llm = _llm_variants_live(query) or []
    out: list[tuple[str, bool]] = [(v, False) for v in llm[:MAX_VARIANTS]]
    seen = {v for v, _ in out}
    # 앵커 먼저 — MAX_TOTAL 상한에 걸릴 때 덧붙임 변형이 앵커를 밀어내지 않게
    # (실측: #8 에서 범용 덧붙임이 앵커를 밀어내 규칙45 가 top5 이탈)
    for v, anchor in sorted(_lexicon_variants(query), key=lambda t: not t[1]):
        if v not in seen:
            out.append((v, anchor))
    return out[:MAX_TOTAL]


def rewrite(query: str) -> list[str]:
    """변형 쿼리 목록(원 쿼리 제외) — 표시/호환용."""
    return [v for v, _ in rewrite_tagged(query)]
