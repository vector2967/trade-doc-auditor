"""통관 법령 검색 웹 데모 — 브라우저에서 질문 입력 → 검색 결과 → 조문/위임 체인.

실행: 리포 루트에서  python demo_web.py   (모델 로딩 후 브라우저 자동 오픈)
전제: 도커 3-스토어 기동. GPU 있으면 풀 검색 ~6초, 없으면 빠른 모드 권장.

라우트:
  /                    검색 홈 (예시 질문 포함)
  /search?q=…&fast=1   검색 결과 (fast=1 이면 rerank 생략)
  /article/{law_id}/{조문}   조문 전문 + 위임 체인 (demo.py 와 동일 페이지)
"""
from __future__ import annotations

import html as H
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "law_repository"))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402

import demo  # noqa: E402  (Toss CSS·조문 페이지 빌더 공유)

app = FastAPI()

EXAMPLES = [
    "해외에서 수리하고 다시 들여오는 기계 세금 어떻게 되나요",
    "세관에 신고한 가격이 잘못됐는데 나중에 고칠 수 있나요",
    "수입신고 전에 물품을 미리 반출할 수 있나요",
    "밀수하면 어떤 처벌 받나요",
]

# 검색 UI 전용 추가 스타일 (TDS: 버튼 fill primary 48px/14px, 입력 box 스타일)
SEARCH_CSS = demo.TOSS_CSS + """
  form.search { display: flex; gap: 8px; margin: 16px 0 8px; }
  input[type=text] { flex: 1; height: 48px; border: 1px solid #e5e8eb;
    border-radius: 14px; padding: 0 16px; font: inherit; color: #191f28;
    background: #f2f4f6; outline: none; }
  input[type=text]:focus { border-color: #3182f6; background: #ffffff; }
  button { height: 48px; padding: 0 24px; border: 0; border-radius: 14px;
    background: #3182f6; color: #ffffff; font: inherit; font-weight: 600;
    cursor: pointer; }
  button:hover { background: #2272eb; }
  label.fast { color: #4e5968; font-size: 14px; display: inline-flex;
    align-items: center; gap: 6px; margin-bottom: 16px; }
  a.result { display: block; }
  a.result .card:hover { border-color: #3182f6; }
  .rank { color: #8b95a1; font-size: 14px; margin-right: 8px; }
  .snippet { color: #4e5968; font-size: 14px; line-height: 21px; margin: 6px 0 0 0; }
  .elapsed { color: #8b95a1; font-size: 14px; }
  .home-title { margin-top: 64px; }
  .spin { display: none; color: #8b95a1; font-size: 14px; margin: 16px 0; }
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{H.escape(title)}</title><style>{SEARCH_CSS}</style></head>
<body>{body}</body></html>""")


def _search_form(q: str = "", fast: bool = False) -> str:
    chips = "".join(
        f'<a href="/search?q={H.escape(ex)}"><span class="chip">{H.escape(ex)}</span></a>'
        for ex in EXAMPLES)
    return f"""
<form id="sf" class="search" action="/search"
      onsubmit="document.querySelector('.spin').style.display='block'">
  <input type="text" name="q" value="{H.escape(q)}" placeholder="통관·관세 질문을 일상어로 입력하세요" autofocus>
  <button type="submit">검색</button>
</form>
<label class="fast"><input type="checkbox" name="fast" value="1" form="sf"
  {"checked" if fast else ""}> 빠른 모드 (rerank 생략 — GPU 없을 때)</label>
<div class="spin">검색 중… (재작성 → dense+bm25 → RRF 융합 → rerank 혼합)</div>
<div>{chips}</div>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return _page("통관 법령 검색 데모", f"""
<h1 class="home-title">통관 법령 검색</h1>
<div class="meta">법령 65종 · 조문 5,897청크 · 재작성 + dense/bm25 융합 + rerank 혼합 (recall@5 0.730)</div>
{_search_form()}""")


@app.get("/search", response_class=HTMLResponse)
def search(q: str = "", fast: bool = False):
    if not q.strip():
        return RedirectResponse("/")
    from agent import query_rewrite, retrieval
    from src.db.postgres import connect

    q = q.strip()
    variants = query_rewrite.rewrite(q)
    t0 = time.perf_counter()
    hits = retrieval.search(q, limit=8, use_rerank=not fast, use_rewrite=True)
    dt = time.perf_counter() - t0

    # pk → (law_id, article_no, 제목) — 조문 페이지 링크용
    pk_rows: dict[int, tuple] = {}
    if hits:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, law_id, article_no, coalesce(parent_article_pk, id)"
                " FROM law_articles WHERE id = ANY(%s)",
                ([h.article_pk for h in hits],))
            pk_rows = {r[0]: r for r in cur.fetchall()}
    info = demo._law_info()

    cards, seen_art = "", set()
    rank = 0
    for h in hits:
        r = pk_rows.get(h.article_pk)
        if not r:
            continue
        _, law_id, art_no, _ = r
        if (law_id, art_no) in seen_art:  # 항 청크 중복은 조문 단위로 접기
            continue
        seen_art.add((law_id, art_no))
        rank += 1
        law_name, hier = info.get(law_id, (law_id, "법률"))
        bg, fg = demo._HIER_COLOR.get(hier, ("#f2f4f6", "#4e5968"))
        art_token = f"{art_no // 100}" + (f"의{art_no % 100}" if art_no % 100 else "")
        snippet = demo._clean_body(h.text, art_no)
        snippet = snippet[:150] + ("…" if len(snippet) > 150 else "")
        cards += f"""
<a class="result" href="/article/{law_id}/{art_token}"><div class="card">
  <span class="rank">{rank}</span>
  <span class="badge" style="background:{bg};color:{fg}">{H.escape(hier)}</span>
  <b>{H.escape(law_name)} {demo._fmt_article_no(art_no)}</b>
  <span class="title">{H.escape(demo._title_of(h.text))}</span>
  <p class="snippet">{H.escape(snippet)}</p>
</div></a>"""

    variants_html = "".join(f'<span class="chip">+ {H.escape(v)}</span>' for v in variants)
    return _page(f"{q} — 검색", f"""
<h1><a href="/">통관 법령 검색</a></h1>
{_search_form(q, fast)}
<h2>결과<span class="count">{rank}건 · {dt:.1f}초 · {"빠른 모드(융합만)" if fast else "풀 파이프라인"}</span></h2>
{cards or '<p class="meta">결과 없음</p>'}
{f'<h2>재작성 변형<span class="count">검색에 함께 사용됨</span></h2>{variants_html}' if variants else ''}""")


@app.get("/article/{law_id}/{art_token}", response_class=HTMLResponse)
def article(law_id: str, art_token: str):
    try:
        art_no = demo._parse_article_no(art_token)
    except ValueError:
        return _page("오류", "<h1>조번호 해석 불가</h1>")
    built = demo.build_lookup_html(law_id, art_no)
    if built is None:
        return _page("없음", "<h1>현행 기준으로 해당 조문 없음</h1>"
                             '<p><a href="javascript:history.back()">← 돌아가기</a></p>')
    page, _ = built
    back = '<div style="max-width:960px;margin:0 auto;padding:0 24px">' \
           '<a href="javascript:history.back()" style="color:#3182f6">← 검색으로</a></div>'
    return HTMLResponse(page.replace("<body>", "<body>" + back, 1))


def main() -> int:
    import threading
    import webbrowser

    import uvicorn

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    demo._preload()
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:8899/")).start()
    print("웹 데모: http://127.0.0.1:8899/  (종료: Ctrl+C)")
    uvicorn.run(app, host="127.0.0.1", port=8899, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
