"""법제처 국가법령정보 DRF API 클라이언트 (공통).

확정된 target (2026-07-02, guideList 실측 + 실키 호출 검증 — 작업현황 §10 blocker 해소):
- 현행법령 본문   : lawService.do?target=law&ID=<법령ID>          (MST 없이 현행 조회)
- 시행일 법령 목록 : lawSearch.do?target=eflaw&query=<법령명>
- 법령 변경이력    : lawSearch.do?target=lsHstInf&regDt=<YYYYMMDD>  (Phase 3)
- 조문별 변경이력  : lawService.do?target=lsJoHstInf&ID=<법령ID>&JO=<6자리>  (Phase 3)

JO 6자리 규약 = 조문번호 4자리 + 조문가지번호 2자리 (제226조 → '022600',
제5조의2 → '000502'). law_articles.article_no(int)도 같은 인코딩을 쓴다:
article_no = 조문번호*100 + 가지번호.

API 응답 JSON 키에 공백이 섞여 오므로(法제처 특성) 모든 키를 정규화한다.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

import requests

from src.config import settings

BASE = "https://www.law.go.kr/DRF"
RAW_DIR = Path("data/raw")

_session = requests.Session()
_session.headers.update({"User-Agent": "trade-doc-auditor/law-repository"})


def _clean_keys(obj):
    """API 가 키에 공백을 섞어 반환하므로 재귀적으로 제거."""
    if isinstance(obj, dict):
        return {re.sub(r"\s+", "", k): _clean_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_keys(v) for v in obj]
    return obj


def squash(s: str | None) -> str:
    """값(날짜·번호)의 공백 제거."""
    return re.sub(r"\s+", "", s or "")


def as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def clean_text(s) -> str:
    if not s:
        return ""
    if isinstance(s, list):
        s = "\n".join(clean_text(x) for x in s)
    s = html.unescape(str(s)).replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return s.strip()


def content_of(x) -> str:
    """{'content': ...} 형태 필드(법종구분·소관부처 등) 언래핑."""
    if isinstance(x, dict):
        return str(x.get("content", ""))
    return str(x) if x else ""


def get(endpoint: str, **params) -> dict:
    params.setdefault("OC", settings.law_api_oc)
    params.setdefault("type", "JSON")
    r = _session.get(f"{BASE}/{endpoint}", params=params, timeout=60)
    r.raise_for_status()
    return _clean_keys(r.json())


def fetch_current_law(law_id: str, use_cache: bool = True) -> dict:
    """현행 법령 본문. target=law + ID(법령ID) — MST 하드코딩 없이 항상 현행."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache = RAW_DIR / f"law_current_{law_id}.json"
    if use_cache and cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    body = get("lawService.do", target="law", ID=law_id)
    cache.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return body


def jo_code(no: str | int, branch: str | int = 0) -> int:
    """조문번호+가지번호 → article_no 정수 인코딩 (JO 규약과 동일)."""
    return int(no) * 100 + int(branch or 0)


def jo_label(no: str, branch: str) -> str:
    n = int(no) if str(no).isdigit() else no
    if branch and str(branch) not in ("00", "0", ""):
        b = int(branch) if str(branch).isdigit() else branch
        return f"제{n}조의{b}"
    return f"제{n}조"
