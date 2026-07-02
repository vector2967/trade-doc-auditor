"""Phase 4 — HSK 요건 엣지: 세관장확인대상물품 (설계 §4.3(2), 로드맵 Phase 4).

소스: 관세청 세관장확인대상물품 API (data.go.kr, serviceKey=.env)
  GET apis.data.go.kr/1220000/retrieveCcctLworCd/getRetrieveCcctLworCd
      ?serviceKey&hsSgn=<10자리>&imexTpcd=<1|2>
  실측(2026-07-02): hsSgn 정확 10자리만(접두어 불가), imexTpcd 필수(1=수출, 2=수입),
  numOfRows/pageNo 무시(쿼리당 전체 반환), 응답 XML.
  → 전수조사 = leaf 11,327 × 2 = 22,654 호출. 진행 캐시(JSONL)로 중단 재개,
    쿼터 초과(resultCode 22) 시 안전 중단 후 다음 날 이어서.

그래프 (설계 §4.3 그대로):
  (:HSK {code})-[:REQUIRES {trade_type, valid_from, document, rtm_tpcd}]->(:Law {code, name})
  (:Law)-[:APPROVED_BY]->(:Agency {code, name})
  확인법령(약사법 등)의 :Law 는 조문 그래프와 공유될 노드 — code(dcerCfrmLworCd) 로 MERGE.
  REQUIRES 키 = (HSK, Law, trade_type). 기관이 HS 별로 달라도 APPROVED_BY 는
  법령 단위로 붙는다(설계 확정 — 규모 작아 속성 매칭 traverse 로 충분).
  상속(hs6/heading4 상위 요건)은 조회 시 속성 매칭으로 — 적재는 leaf 직접 요건만.

응답 필드: aplyStrtDt(적용시작→valid_from), dcerCfrmLworCd/Nm(확인법령),
reqApreIttCd/Nm(요건승인기관), reqCfrmIstmNm(요건확인서류명), bfhnAffcRtmTpcd(유형코드).

실행: law_repository/ 에서  python -m src.ingest.ccct [--sweep-only|--load-only] [--limit N]
"""
from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import xmltodict

from src.config import settings
from src.db.neo4j import driver as make_driver
from src.lawgo import as_list

URL = "https://apis.data.go.kr/1220000/retrieveCcctLworCd/getRetrieveCcctLworCd"
PROGRESS = Path("data/ccct_progress.jsonl")
TRADE_TYPES = {"1": "수출", "2": "수입"}
WORKERS = 8

_QUOTA_CODES = {"22", "23"}  # 일일 트래픽 초과 / 초당 요청 제한


class QuotaExceeded(RuntimeError):
    pass


# ---------------------------------------------------------------- sweep

def _query(hs: str, tpcd: str, retries: int = 4) -> list[dict]:
    import time

    for attempt in range(retries + 1):
        try:
            r = requests.get(
                URL,
                params={"serviceKey": settings.data_go_kr_service_key,
                        "hsSgn": hs, "imexTpcd": tpcd},
                timeout=30,
            )
            r.raise_for_status()
            break
        except requests.HTTPError as e:
            # 429 = 일일 쿼터 소진 (실측: XML 이 아니라 HTTP 429 로 옴) — 즉시 중단
            if e.response is not None and e.response.status_code == 429:
                raise QuotaExceeded("HTTP 429 — 일일 트래픽 소진, 내일 재실행") from e
            if attempt == retries:
                raise
            time.sleep(1.5 * (attempt + 1))  # 502 등 일시 장애 백오프
        except (requests.ConnectionError, requests.Timeout):
            if attempt == retries:
                raise
            time.sleep(1.5 * (attempt + 1))
    parsed = xmltodict.parse(r.text)
    # 쿼터 초과 등 게이트웨이 오류는 <response> 가 아니라 OpenAPI_ServiceResponse 로 옴
    gw = parsed.get("OpenAPI_ServiceResponse")
    if gw:
        hdr = gw.get("cmmMsgHeader") or {}
        reason = (hdr.get("returnReasonCode") or "").strip()
        msg = hdr.get("returnAuthMsg") or hdr.get("errMsg") or reason
        if reason in _QUOTA_CODES:
            raise QuotaExceeded(msg)
        raise RuntimeError(f"hsSgn={hs} tpcd={tpcd}: gateway {msg}")
    d = parsed.get("response") or {}
    code = ((d.get("header") or {}).get("resultCode") or "").strip()
    if code in _QUOTA_CODES:
        raise QuotaExceeded((d.get("header") or {}).get("resultMsg", code))
    if code != "00":
        raise RuntimeError(f"hsSgn={hs} tpcd={tpcd}: {d.get('header')}")
    items = as_list(((d.get("body") or {}).get("items") or {}).get("item"))
    return [dict(it) for it in items if it]


def leaf_codes() -> list[str]:
    drv = make_driver()
    try:
        with drv.session() as s:
            return [r["c"] for r in s.run("MATCH (n:HSK) RETURN n.code AS c ORDER BY c")]
    finally:
        drv.close()


def sweep(limit: int | None = None) -> None:
    """전수조사. 결과(빈 것 포함)를 PROGRESS 에 append — 재실행 시 완료분 skip."""
    done = {(rec["hs"], rec["tpcd"]) for rec in _iter_progress()}

    todo = [
        (hs, tpcd)
        for hs in leaf_codes()
        for tpcd in TRADE_TYPES
        if (hs, tpcd) not in done
    ]
    if limit:
        todo = todo[:limit]
    print(f"[sweep] 완료 {len(done)} / 남음 {len(todo)} (workers={WORKERS})")
    if not todo:
        return

    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    stop = threading.Event()
    n_done = n_hit = 0

    def work(pair: tuple[str, str]):
        if stop.is_set():
            return None
        return pair, _query(*pair)

    with PROGRESS.open("a", encoding="utf-8") as out, ThreadPoolExecutor(WORKERS) as ex:
        futures = [ex.submit(work, p) for p in todo]
        n_fail = 0
        try:
            for fut in as_completed(futures):
                try:
                    res = fut.result()  # QuotaExceeded 는 여기서 전파
                except QuotaExceeded:
                    raise
                except Exception as e:
                    # 개별 실패는 격리 — progress 미기록이라 다음 실행이 재시도
                    n_fail += 1
                    if n_fail <= 5:
                        print(f"[sweep] 실패(다음 실행 때 재시도): {e}")
                    continue
                if res is None:
                    continue
                (hs, tpcd), items = res
                with lock:
                    out.write(json.dumps({"hs": hs, "tpcd": tpcd, "items": items},
                                         ensure_ascii=False) + "\n")
                    out.flush()
                    n_done += 1
                    n_hit += bool(items)
                    if n_done % 500 == 0:
                        print(f"[sweep] {n_done}/{len(todo)} (요건 있는 코드 {n_hit})")
        except QuotaExceeded as e:
            stop.set()
            print(f"[sweep] 쿼터 초과로 중단 — 진행분은 저장됨, 내일 재실행: {e}")
        finally:
            stop.set()
    print(f"[sweep] 이번 실행 {n_done}건 조회, 요건 있음 {n_hit}건, 실패 {n_fail}건")


# ---------------------------------------------------------------- load

_LOAD_CYPHER = """
UNWIND $rows AS row
MATCH (h:HSK {code: row.hs})
MERGE (law:Law {code: row.law_code})
  ON CREATE SET law.name = row.law_name
MERGE (h)-[r:REQUIRES {trade_type: row.trade_type}]->(law)
  SET r.valid_from = row.valid_from, r.document = row.document, r.rtm_tpcd = row.rtm_tpcd
MERGE (ag:Agency {code: row.agency_code})
  ON CREATE SET ag.name = row.agency_name
MERGE (law)-[:APPROVED_BY]->(ag)
"""


def _iter_progress():
    """PROGRESS 라인 이터레이터 — 강제 종료로 깨진 꼬리 라인은 무시(다음 sweep 이 재조회)."""
    if not PROGRESS.exists():
        return
    with PROGRESS.open(encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load() -> None:
    """PROGRESS → Neo4j MERGE (멱등)."""
    rows = []
    for rec in _iter_progress():
        for it in rec["items"]:
                rows.append(
                    {
                        "hs": rec["hs"],
                        "trade_type": TRADE_TYPES[rec["tpcd"]],
                        "law_code": (it.get("dcerCfrmLworCd") or "").strip(),
                        "law_name": (it.get("dcerCfrmLworNm") or "").strip(),
                        "agency_code": (it.get("reqApreIttCd") or "").strip(),
                        "agency_name": (it.get("reqApreIttNm") or "").strip(),
                        "document": (it.get("reqCfrmIstmNm") or "").strip(),
                        "valid_from": (it.get("aplyStrtDt") or "").strip(),
                        "rtm_tpcd": (it.get("bfhnAffcRtmTpcd") or "").strip(),
                    }
                )
    print(f"[load] 요건 레코드 {len(rows)}건")
    drv = make_driver()
    try:
        with drv.session() as s:
            s.run("CREATE CONSTRAINT law_code IF NOT EXISTS FOR (n:Law) REQUIRE n.code IS UNIQUE")
            s.run("CREATE CONSTRAINT agency_code IF NOT EXISTS FOR (n:Agency) REQUIRE n.code IS UNIQUE")
            for i in range(0, len(rows), 1000):
                s.run(_LOAD_CYPHER, rows=rows[i : i + 1000])
        verify(drv)
    finally:
        drv.close()


def verify(drv=None) -> None:
    own = drv is None
    drv = drv or make_driver()
    try:
        with drv.session() as s:
            req = s.run("MATCH (:HSK)-[r:REQUIRES]->() RETURN count(r) AS c").single()["c"]
            laws = s.run("MATCH (n:Law) RETURN count(*) AS c").single()["c"]
            ags = s.run("MATCH (n:Agency) RETURN count(*) AS c").single()["c"]
            hsk = s.run("MATCH (h:HSK) WHERE (h)-[:REQUIRES]->() RETURN count(*) AS c").single()["c"]
            print(f"[verify] REQUIRES {req} | 확인법령 {laws} | 기관 {ags} | 요건 걸린 HSK {hsk}")
            sample = s.run(
                """
                MATCH (h:HSK)-[r:REQUIRES]->(law:Law)-[:APPROVED_BY]->(ag:Agency)
                RETURN h.code AS hs, h.name_ko AS name, r.trade_type AS tt,
                       law.name AS law, r.document AS doc, collect(ag.name)[..2] AS agencies
                LIMIT 3
                """
            )
            for r in sample:
                print(f"  {r['hs']}({r['name']}) --{r['tt']}--> {r['law']} / {r['doc']} / {r['agencies']}")
    finally:
        if own:
            drv.close()


def main(argv: list[str]) -> int:
    limit = None
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    if "--load-only" not in argv:
        sweep(limit=limit)
    if "--sweep-only" not in argv:
        load()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
