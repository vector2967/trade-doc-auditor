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
sys.path.insert(0, str(ROOT))
GOLDSET = ROOT / "law_repository" / "data" / "goldset.json"
OUT = ROOT / "agent" / "data" / "query_rewrites.json"
MODEL = os.environ.get("QUERY_REWRITE_MODEL", "claude-opus-4-8")

# 프롬프트·파서는 실시간 재작성(query_rewrite)과 공유 — 캐시/실시간 품질 동일 보장
from agent.query_rewrite import LLM_SYSTEM as SYSTEM  # noqa: E402
from agent.query_rewrite import parse_llm_variants as _parse_variants  # noqa: E402


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
