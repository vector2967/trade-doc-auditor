"""LLM 쿼리 재작성 캐시 생성 — 골드셋 질문을 법률 용어 멀티쿼리로 확장해 커밋용 캐시 저장.

전제: `pip install anthropic` + 환경변수
  ANTHROPIC_API_KEY   (필수 — 커밋 금지, 실행 시에만 주입)
  ANTHROPIC_BASE_URL  (선택 — Anthropic 호환 엔드포인트 사용 시)
  QUERY_REWRITE_MODEL (선택 — 기본 claude-opus-4-8)
키가 없는 환경(팀 노트북·Actions)은 캐시 파일만 읽으므로 실행할 필요 없다.

실행: 리포 루트에서  python agent/scripts/generate_rewrites.py
출력: agent/data/query_rewrites.json  (질문 원문 → 변형 쿼리 목록, 커밋 대상)

주의: 프롬프트에 골드 정답을 넣지 말 것 — 질문 텍스트만으로 재작성해야 평가가 오염되지 않는다.
호환 엔드포인트가 structured outputs 를 지원하지 않을 수 있어 프롬프트 JSON 방식을 쓴다.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parents[2]
GOLDSET = ROOT / "law_repository" / "data" / "goldset.json"
OUT = ROOT / "agent" / "data" / "query_rewrites.json"
MODEL = os.environ.get("QUERY_REWRITE_MODEL", "claude-opus-4-8")

SYSTEM = """\
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


def _parse_variants(text: str) -> list[str]:
    """응답에서 JSON 배열 추출 (호환 모델이 코드펜스/부연을 붙여도 방어)."""
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        raise ValueError(f"JSON 배열 미발견: {text[:200]}")
    variants = json.loads(m.group())
    return [v.strip() for v in variants if isinstance(v, str) and v.strip()][:3]


def main() -> int:
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL 은 env 에서
    questions = [
        q["question"] for q in json.loads(GOLDSET.read_text(encoding="utf-8"))["questions"]
    ]
    cache: dict[str, list[str]] = (
        json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    )
    for i, question in enumerate(questions, 1):
        if question in cache:
            continue
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM,
            messages=[{"role": "user", "content": question}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            cache[question] = _parse_variants(text)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"[{i}/{len(questions)}] 파싱 실패, 스킵: {e}")
            continue
        print(f"[{i}/{len(questions)}] {question} -> {cache[question]}")
        # 문항마다 저장 — 중단돼도 재실행 시 캐시된 문항은 건너뛴다
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(
            json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {OUT} ({len(cache)}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
