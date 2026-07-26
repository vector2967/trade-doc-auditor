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


_HIER_RANK = {"법률": 0, "시행령": 1, "시행규칙": 2}
# 위계 배지 (bg, fg) — Toss 팔레트: fill primary / weak / surface
_HIER_COLOR = {
    "법률": ("#3182f6", "#ffffff"),
    "시행령": ("#e8f3ff", "#1b64da"),
    "시행규칙": ("#f2f4f6", "#4e5968"),
}


def _law_info() -> dict[str, tuple[str, str]]:
    """law_id → (법령명, 위계). laws 테이블에서 1회 로드."""
    from src.db.postgres import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT law_id, law_name, hierarchy FROM laws")
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def _title_of(content: str) -> str:
    """조문 본문 헤더('[법령명 제N조(제목)]')에서 제목만 추출."""
    import re

    m = re.search(r"제\d+조(?:의\d+)?\(([^)]*)\)", content.splitlines()[0])
    return m.group(1) if m else ""


def _clean_body(content: str, art_no: int) -> str:
    lines = content.splitlines()
    # 청크 헤더('[법령명 제N조(…)]')와 조문 제목 줄은 표제와 중복 — 건너뜀
    while lines and (lines[0].strip().startswith("[")
                     or lines[0].strip().startswith(f"제{art_no // 100}조")):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _node_html(law_id: str, art_no: int, title: str, body: str | None,
               info: dict, open_body: bool = False) -> str:
    import html as H

    law_name, hier = info.get(law_id, (law_id, "법률"))
    bg, fg = _HIER_COLOR.get(hier, ("#f2f4f6", "#4e5968"))
    head = (f'<span class="badge" style="background:{bg};color:{fg}">{H.escape(hier)}</span>'
            f'<b>{H.escape(law_name)} {_fmt_article_no(art_no)}</b>'
            f'<span class="title">{H.escape(title or "")}</span>')
    if body:
        return (f'<div class="card">'
                f'<details{" open" if open_body else ""}><summary>{head}</summary>'
                f'<pre>{H.escape(body)}</pre></details></div>')
    return f'<div class="card">{head}</div>'


# Toss(TDS) 토큰: canvas #ffffff, foreground #191f28, body #4e5968, muted #8b95a1,
# surface #f2f4f6, border #e5e8eb, primary #3182f6, weak #e8f3ff/#1b64da.
# 그림자 토큰 없음 → 플랫 색 레이어링. 간격 4/6/8/16/24/32, 라운드 8~16.
TOSS_CSS = """
  body { font-family: 'Toss Product Sans', Pretendard, -apple-system, 'Malgun Gothic',
          sans-serif; max-width: 960px; margin: 32px auto; padding: 0 24px;
          background: #ffffff; color: #4e5968; font-size: 16px; line-height: 24px; }
  h1 { color: #191f28; font-size: 30px; font-weight: 600; line-height: 45px;
        margin: 0 0 4px; }
  .meta { color: #8b95a1; font-size: 14px; line-height: 21px; margin-bottom: 24px; }
  h2 { color: #191f28; font-size: 22px; font-weight: 600; line-height: 33px;
        margin: 32px 0 8px; }
  .count { color: #8b95a1; font-weight: 400; font-size: 14px; margin-left: 6px; }
  .badge { display: inline-block; border-radius: 6px; padding: 2px 8px;
            font-size: 13px; font-weight: 600; line-height: 20px; margin-right: 8px; }
  .title { color: #8b95a1; margin-left: 8px; font-weight: 400; }
  b { color: #191f28; font-weight: 600; }
  .card { border: 1px solid #e5e8eb; border-radius: 16px; background: #ffffff;
           padding: 12px 20px; margin: 8px 0; }
  section.root > .card { background: #f2f4f6; border-color: #f2f4f6; }
  pre { white-space: pre-wrap; font-family: inherit; font-size: 16px; line-height: 24px;
         color: #4e5968; margin: 12px 0 4px; padding-top: 12px;
         border-top: 1px solid #e5e8eb; }
  summary { cursor: pointer; }  summary::marker { color: #3182f6; }
  summary:hover b { color: #3182f6; }
  ul { list-style: none; padding-left: 28px; position: relative; margin: 0; }
  ul li { position: relative; }
  ul li::before { content: ""; position: absolute; left: -16px; top: 26px;
                   width: 12px; border-top: 2px solid #e5e8eb; }
  ul li::after { content: ""; position: absolute; left: -16px; top: 0; bottom: 0;
                  border-left: 2px solid #e5e8eb; }
  ul li:last-child::after { height: 26px; bottom: auto; }
  .chip { display: inline-block; background: #f2f4f6; color: #4e5968;
           border-radius: 999px; padding: 4px 16px; margin: 4px 6px 4px 0;
           font-size: 14px; line-height: 21px; }
  a { color: inherit; text-decoration: none; }
"""


def build_lookup_html(law_id: str, art_no: int) -> tuple[str, dict] | None:
    """조문 페이지 HTML — CLI(:조회)와 웹데모(/article)가 공유. 없으면 None."""
    from src import repository as repo

    row = repo.resolve_as_of(law_id, art_no, None)
    if row is None:
        return None

    info = _law_info()
    law_name = info.get(law_id, (law_id, ""))[0]
    until = row["valid_to"] or "현행"

    # 위임 체인: 1홉(내용 포함) → 2홉은 각 1홉 노드보다 하위 위계만 (법→영→규칙 방향)
    hop1 = repo.delegation_neighbors([row["article_pk"]])
    seen = {row["article_pk"]} | {n["article_pk"] for n in hop1}
    hop2_by_src: dict[int, list[dict]] = {}
    if hop1:
        rank_of = {n["article_pk"]: _HIER_RANK.get(info.get(n["law_id"], ("", ""))[1], 0)
                   for n in hop1}
        for n2 in repo.delegation_neighbors([n["article_pk"] for n in hop1]):
            src = n2["src_article_pk"]
            if (n2["article_pk"] not in seen
                    and _HIER_RANK.get(info.get(n2["law_id"], ("", ""))[1], 0) > rank_of.get(src, 0)
                    and len(hop2_by_src.get(src, [])) < 4):
                hop2_by_src.setdefault(src, []).append(n2)
                seen.add(n2["article_pk"])
    cites = [e for e in repo.expand_article(row["article_pk"])
             if e["rel"] == "CITES" and e["article_no"]]

    chain_html = ""
    for n in hop1:
        chain_html += "<li>" + _node_html(
            n["law_id"], n["article_no"], _title_of(n["content"]),
            _clean_body(n["content"], n["article_no"]), info)
        kids = hop2_by_src.get(n["article_pk"], [])
        if kids:
            chain_html += "<ul>" + "".join(
                "<li>" + _node_html(
                    k["law_id"], k["article_no"], _title_of(k["content"]),
                    _clean_body(k["content"], k["article_no"]), info) + "</li>"
                for k in kids) + "</ul>"
        chain_html += "</li>"
    cites_html = "".join(
        f'<span class="chip">{info.get(e["law_id"], (e["law_id"],))[0]} '
        f'{_fmt_article_no(e["article_no"])} ({e["title"] or ""})</span>'
        for e in cites)

    import html as H
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>{H.escape(law_name)} {_fmt_article_no(art_no)}</title>
<style>{TOSS_CSS}</style></head><body>
<h1>{H.escape(law_name)} {_fmt_article_no(art_no)}</h1>
<div class="meta">{H.escape(row["title"] or "")} · 시행 {row["valid_from"]} ~ {until}{"" if row["is_current"] else " · 이력 버전"}</div>
<section class="root">{_node_html(law_id, art_no, row["title"], _clean_body(row["content"], art_no), info, open_body=True)}</section>
<h2>위임 체인<span class="count">{len(hop1)}건{" · 카드를 누르면 조문 본문" if hop1 else ""}</span></h2>
{f'<ul style="padding-left:4px">{chain_html}</ul>' if hop1 else '<p class="meta">위임 관계 없음</p>'}
<h2>인용<span class="count">{len(cites)}건</span></h2>
{cites_html or '<p class="meta">인용 관계 없음</p>'}
</body></html>"""
    return page, {"law_name": law_name, "title": row["title"],
                  "hop1": len(hop1), "cites": len(cites)}


def do_lookup(args: list[str]) -> None:
    import webbrowser

    alias, _ = _law_maps()
    if len(args) < 2 or (args[0] not in alias and not args[0].isdigit()):
        print("사용법: :조회 <법|영|규칙|법령명> <조번호[의N]>   예) :조회 법 226")
        return
    law_id = alias.get(args[0], args[0])
    try:
        art_no = _parse_article_no(args[1])
    except ValueError:
        print(f"조번호 해석 불가: {args[1]}")
        return
    built = build_lookup_html(law_id, art_no)
    if built is None:
        print("  현행 기준으로 해당 조문 없음")
        return
    page, meta = built

    out_dir = ROOT / "demo_out"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"조회_{meta['law_name']}_{args[1]}.html".replace(" ", "_")
    out.write_text(page, encoding="utf-8")
    webbrowser.open(out.as_uri())
    print(f"  {meta['law_name']} {_fmt_article_no(art_no)} ({meta['title']}) — "
          f"위임 {meta['hop1']}건, 인용 {meta['cites']}건")
    print(f"  브라우저로 열었습니다: {out}")


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
