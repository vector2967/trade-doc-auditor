"""법령 조회 전용 웹 — 목록에서 골라 조문을 열람 (검색·모델 없음, 즉시 기동).

실행: 리포 루트에서  python demo_laws.py   (Postgres·Neo4j 만 필요, 1초 내 오픈)

라우트:
  /                        법령 65종 목록 (위계 배지 + 이름 필터)
  /law/{law_id}            조문 목록 (조번호+제목 필터)
  /article/{law_id}/{조문}  조문 전문 + 시행기간 + 위임 체인 + 인용
"""
from __future__ import annotations

import html as H
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "law_repository"))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

import demo  # noqa: E402  (Toss CSS·조문 페이지 빌더 공유)

app = FastAPI()

CSS = demo.TOSS_CSS + """
  input.filter { width: 100%; height: 48px; border: 1px solid #e5e8eb;
    border-radius: 14px; padding: 0 16px; font: inherit; color: #191f28;
    background: #f2f4f6; outline: none; margin: 16px 0; }
  input.filter:focus { border-color: #3182f6; background: #ffffff; }
  a.row { display: block; }
  a.row .card { display: flex; align-items: center; gap: 4px; padding: 10px 20px; }
  a.row .card:hover { border-color: #3182f6; }
  .grow { flex: 1; }
  .num { color: #8b95a1; font-size: 14px; min-width: 90px; }
"""

_FILTER_JS = """<script>
function flt(box) {
  const q = box.value.trim();
  document.querySelectorAll('a.row').forEach(a => {
    a.style.display = !q || a.textContent.includes(q) ? '' : 'none';
  });
}
</script>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{H.escape(title)}</title><style>{CSS}</style></head>
<body>{body}{_FILTER_JS}</body></html>""")


def _badge(hier: str) -> str:
    bg, fg = demo._HIER_COLOR.get(hier, ("#f2f4f6", "#4e5968"))
    return f'<span class="badge" style="background:{bg};color:{fg}">{H.escape(hier)}</span>'


@app.get("/", response_class=HTMLResponse)
def _root():
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/laws")


# 목록은 /laws 고정 경로 — demo_web 에 합쳐질 때도 링크가 그대로 동작한다
@app.get("/laws", response_class=HTMLResponse)
def laws():
    from src.db.postgres import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT l.law_id, l.law_name, l.hierarchy,
                   count(*) FILTER (WHERE a.is_current AND a.parent_article_pk IS NULL)
            FROM laws l LEFT JOIN law_articles a USING (law_id)
            GROUP BY l.law_id, l.law_name, l.hierarchy
            """
        )
        rows = cur.fetchall()
    # 법률 → 시행령 → 시행규칙, 각 그룹 안은 가나다순 (DB 콜레이션 무관하게 파이썬 정렬)
    rows.sort(key=lambda r: (demo._HIER_RANK.get(r[2], 9), r[1]))
    items = "".join(
        f'<a class="row" href="/law/{lid}"><div class="card">{_badge(hier)}'
        f'<b class="grow">{H.escape(name)}</b>'
        f'<span class="count">조문 {cnt}</span></div></a>'
        for lid, name, hier, cnt in rows)
    return _page("법령 조회", f"""
<h1>법령 조회</h1>
<div class="meta">현행 조문 기준 · 목록에서 법령을 선택하세요 ·
 <a href="/" style="color:#3182f6">질문으로 검색하기 →</a></div>
<input class="filter" placeholder="법령명으로 찾기 (예: 관세, 식품, 화학)" oninput="flt(this)" autofocus>
{items}""")


@app.get("/law/{law_id}", response_class=HTMLResponse)
def law(law_id: str):
    from src.db.postgres import connect

    info = demo._law_info()
    if law_id not in info:
        return _page("없음", '<h1>등록되지 않은 법령</h1><p><a href="/laws" style="color:#3182f6">← 목록으로</a></p>')
    law_name, hier = info[law_id]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT article_no, title FROM law_articles"
            " WHERE law_id = %s AND is_current AND parent_article_pk IS NULL"
            " ORDER BY article_no",
            (law_id,))
        rows = cur.fetchall()
    items = ""
    for art_no, title in rows:
        token = f"{art_no // 100}" + (f"의{art_no % 100}" if art_no % 100 else "")
        items += (f'<a class="row" href="/article/{law_id}/{token}"><div class="card">'
                  f'<span class="num">{demo._fmt_article_no(art_no)}</span>'
                  f'<b class="grow">{H.escape(title or "(제목 없음)")}</b></div></a>')
    return _page(law_name, f"""
<div class="meta" style="margin-bottom:8px"><a href="/laws" style="color:#3182f6">← 법령 목록</a></div>
<h1>{H.escape(law_name)}</h1>
<div class="meta">{_badge(hier)} 현행 조문 {len(rows)}건</div>
<input class="filter" placeholder="조번호·제목 필터 (예: 226, 환급, 면세)" oninput="flt(this)" autofocus>
{items}""")


@app.get("/article/{law_id}/{art_token}", response_class=HTMLResponse)
def article(law_id: str, art_token: str):
    try:
        art_no = demo._parse_article_no(art_token)
    except ValueError:
        return _page("오류", "<h1>조번호 해석 불가</h1>")
    built = demo.build_lookup_html(law_id, art_no)
    if built is None:
        return _page("없음", "<h1>현행 기준으로 해당 조문 없음</h1>"
                             f'<p><a href="/law/{law_id}" style="color:#3182f6">← 조문 목록</a></p>')
    page, _ = built
    back = ('<div style="max-width:960px;margin:0 auto;padding:0 24px">'
            f'<a href="/law/{law_id}" style="color:#3182f6">← 조문 목록</a></div>')
    return HTMLResponse(page.replace("<body>", "<body>" + back, 1))


def main() -> int:
    import threading
    import webbrowser

    import uvicorn

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    threading.Timer(0.8, lambda: webbrowser.open("http://127.0.0.1:8898/")).start()
    print("법령 조회: http://127.0.0.1:8898/  (종료: Ctrl+C)")
    uvicorn.run(app, host="127.0.0.1", port=8898, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
