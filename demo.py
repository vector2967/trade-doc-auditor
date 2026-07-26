"""통관 법령 검색 시연 CLI — 에이전트 파이프라인 풀코스를 눈으로 확인하는 도구.

실행: 리포 루트에서  python demo.py
전제: 도커 3-스토어 기동(law_repository/docker-compose.yml).

파이프라인(개선계획 ①~⑥ 반영):
  질문 → 쿼리 재작성(LLM 캐시+레키콘) → dense(bge-m3) + bm25(Kiwi) 검색
       → RRF 융합 → bge-reranker-v2-m3 채점 → z(rerank)+2.0·z(RRF) 혼합 정렬

명령:
  <질문 문장>        검색 (기본: 풀 파이프라인 — CPU rerank 라 문항당 ~1분)
  :fast on|off      빠른 모드 = rerank 생략, 융합 순위만 (~수 초, 기본 off)
  :graph on|off     위임(DELEGATES) 이웃 후보 추가 (기본 off)
  :limit N          표시 결과 수 (기본 5)
  :depth N          융합/rerank 후보 폭 (기본 30 — 줄이면 빨라지고 살짝 부정확)
  :q                종료
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "law_repository"))


def _preload() -> None:
    """모델 2종 선로딩 — 첫 질문에서 수십 초 멈춘 것처럼 보이는 것 방지."""
    t0 = time.perf_counter()
    print("[준비] bge-m3 임베더 로딩 중...", flush=True)
    from src.embed import dense

    dense.encode(["워밍업"])
    print(f"[준비] bge-m3 완료 ({time.perf_counter() - t0:.0f}초)", flush=True)
    t1 = time.perf_counter()
    print("[준비] bge-reranker-v2-m3 로딩 중...", flush=True)
    from agent import retrieval

    retrieval._get_reranker()
    print(f"[준비] reranker 완료 ({time.perf_counter() - t1:.0f}초)\n", flush=True)


class State:
    fast = False
    graph = False
    limit = 5
    depth = 30


def _headline(text: str) -> str:
    head = text.splitlines()[0].strip().strip("[]")
    return head


def _snippet(text: str, width: int = 76) -> str:
    head = _headline(text)
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line or line in head:  # 헤더(조문 제목) 중복 줄은 건너뜀
            continue
        return line if len(line) <= width else line[: width - 1] + "…"
    return ""


def do_search(question: str, st: State) -> None:
    from agent import query_rewrite, retrieval

    variants = query_rewrite.rewrite(question)
    if variants:
        print("  [재작성 변형]")
        for v in variants:
            print(f"    + {v}")
    else:
        print("  [재작성 변형 없음 — 원 쿼리만 검색]")

    mode = "빠른(융합만)" if st.fast else "풀(혼합 rerank)"
    print(f"  [검색 중… 모드={mode}, depth={st.depth}"
          + (", graph=on" if st.graph else "") + "]", flush=True)
    t0 = time.perf_counter()
    hits = retrieval.search(
        question, limit=st.limit, depth=st.depth,
        use_rerank=not st.fast, use_rewrite=True, use_graph=st.graph,
    )
    dt = time.perf_counter() - t0

    print(f"\n  ── 결과 상위 {len(hits)}건 ({dt:.1f}초) " + "─" * 40)
    if not hits:
        print("  (결과 없음)")
    for i, h in enumerate(hits, 1):
        print(f"  {i}. [{h.score:+.3f}] {_headline(h.text)}")
        body = _snippet(h.text)
        if body:
            print(f"       {body}")


def main() -> int:
    # cp949 콘솔에서 유니코드 출력 크래시 방어 (law_repository/src/cli.py 와 동일)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    print(__doc__.split("명령:")[1].rstrip())
    _preload()
    st = State()
    print("질문을 입력하세요 (:q 종료)")
    while True:
        try:
            line = input("\n질문> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        line = line.encode("utf-8", "replace").decode("utf-8").strip()
        if not line:
            continue
        if not line.startswith(":"):
            do_search(line, st)
            continue

        cmd, *args = line[1:].split()
        if cmd in ("q", "quit", "exit"):
            return 0
        elif cmd == "fast" and args:
            st.fast = args[0] == "on"
            print(f"빠른 모드: {'on (rerank 생략)' if st.fast else 'off (풀 파이프라인)'}")
        elif cmd == "graph" and args:
            st.graph = args[0] == "on"
            print(f"그래프 확장: {'on' if st.graph else 'off'}")
        elif cmd == "limit" and args and args[0].isdigit():
            st.limit = int(args[0])
            print(f"표시 {st.limit}건")
        elif cmd == "depth" and args and args[0].isdigit():
            st.depth = int(args[0])
            print(f"후보 폭 {st.depth}")
        else:
            print("명령: :fast on|off  :graph on|off  :limit N  :depth N  :q")


if __name__ == "__main__":
    sys.exit(main())
