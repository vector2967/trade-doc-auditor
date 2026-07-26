"""통관 법령 검색 시연 CLI — 에이전트 파이프라인 풀코스를 눈으로 확인하는 도구.

실행: 리포 루트에서  python demo.py
전제: 도커 3-스토어 기동(law_repository/docker-compose.yml).

파이프라인(개선계획 ①~⑥ 반영):
  질문 → 쿼리 재작성(LLM 캐시+레키콘) → dense(bge-m3) + bm25(Kiwi) 검색
       → RRF 융합 → bge-reranker-v2-m3 채점 → z(rerank)+2.0·z(RRF) 혼합 정렬

명령:
  <질문 문장>        검색 (풀 파이프라인 — GPU ~6초 / CPU ~1.5분)
  :조회 <법령> <조문>  조문 전문 + 시행기간 + 위임/인용 그래프
                    예) :조회 법 226 / :조회 영 5의2 / :조회 대외무역법 11
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


def _law_maps():
    """법령 별칭·표시명 (law_repository/src/cli.py 와 동일 파생)."""
    from src.ingest.laws import TARGET_LAWS

    alias = {
        "법": "001556", "관세법": "001556",
        "영": "002421", "시행령": "002421",
        "규칙": "006392", "시행규칙": "006392",
        **{t["name"]: t["law_id"] for t in TARGET_LAWS},
    }
    name = {t["law_id"]: t["name"] for t in TARGET_LAWS}
    name.update({"001556": "관세법", "002421": "관세법 시행령", "006392": "관세법 시행규칙"})
    return alias, name


def _parse_article_no(token: str) -> int:
    """'226' → 22600, '5의2' → 502 (article_no = 조번호*100 + 가지번호)."""
    if "의" in token:
        no, branch = token.split("의", 1)
        return int(no) * 100 + int(branch)
    return int(token) * 100


def _fmt_article_no(article_no: int) -> str:
    return f"제{article_no // 100}조" + (f"의{article_no % 100}" if article_no % 100 else "")


def do_lookup(args: list[str]) -> None:
    from src import repository as repo

    alias, name = _law_maps()
    if len(args) < 2 or (args[0] not in alias and args[0] not in name):
        print("사용법: :조회 <법|영|규칙|법령명> <조번호[의N]>   예) :조회 법 226")
        return
    law_id = alias.get(args[0], args[0])
    try:
        art_no = _parse_article_no(args[1])
    except ValueError:
        print(f"조번호 해석 불가: {args[1]}")
        return
    row = repo.resolve_as_of(law_id, art_no, None)
    if row is None:
        print("  현행 기준으로 해당 조문 없음")
        return
    until = row["valid_to"] or "현행"
    print(f"\n  {name.get(law_id, law_id)} {_fmt_article_no(art_no)} ({row['title']})")
    print(f"  [시행 {row['valid_from']} ~ {until}]")
    body = row["content"].splitlines()
    # 청크 헤더('[법령명 제N조(…)]')와 조문 제목 줄은 위 헤더와 중복 — 건너뜀
    while body and (body[0].strip().startswith("[") or body[0].strip().startswith(f"제{art_no // 100}조")):
        body = body[1:]
    for line in body[:15]:
        print(f"  {line}")
    if len(body) > 15:
        print(f"  … (총 {len(body)}줄, 이하 생략)")

    edges = repo.expand_article(row["article_pk"])
    print(f"\n  [그래프 연결 {len(edges)}건]")
    for e in edges[:10]:
        law = name.get(e["law_id"], e["law_id"])
        label = f"{law} {_fmt_article_no(e['article_no'])}" if e["article_no"] else (e["title"] or "")
        rel = {"DELEGATES": "위임→", "CITES": "인용→", "DETAILED_IN": "상세→"}.get(e["rel"], e["rel"])
        print(f"    {rel} {label} ({e['title'] or ''})")
    if len(edges) > 10:
        print(f"    … 외 {len(edges) - 10}건")


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
        elif cmd in ("조회", "art"):
            do_lookup(args)
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
            print("명령: :조회 <법령> <조문>  :fast on|off  :graph on|off  :limit N  :depth N  :q")


if __name__ == "__main__":
    sys.exit(main())
