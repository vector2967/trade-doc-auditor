"""Phase 2 — 법령 초기 적재 (설계 §4.1/§4.2, 로드맵 Phase 2).

현행 조문을 law_articles(PG, 시간버전 원장)에 넣고 Qdrant customs_law_current 에
dense(bge-m3)+bm25(sparse, idf) 로 인덱싱한다.

- 초기 적재 시맨틱: 모든 현행 조문 valid_from=조문시행일자, valid_to=NULL,
  is_current=(valid_from<=today). 버전 닫기(valid_to)·폐지 tombstone 은 Phase 3.
- 멱등: 같은 (law_id, article_no, paragraph_no) 의 열린(valid_to IS NULL) 행 기준
  upsert. content_hash 동일하면 아무것도 안 함(재임베딩 skip — 불변식).
- 청킹: 조문 1청크. SPLIT_THRESHOLD 초과 조문만 항 단위 분할 + parent_article_pk.
  분할 시 부모 행은 Qdrant 미적재(자식이 검색 단위).
- 캐시 3종(전부 커밋 대상 — API 쿼터·러너 CPU 시간 절약):
  · 본문: data/raw/law_current_<ID>.json (lawgo)  · MST: data/raw/law_mst.json
  · dense 벡터: data/dense_cache.npz — content sha256 키. 같은 텍스트+같은 모델이면
    벡터가 결정적이므로 --no-cache 와 무관하게 항상 사용(미스만 추론).

실행: law_repository/ 에서  python -m src.ingest.laws  [--no-cache] [--skip-embed]
"""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

import numpy as np

from src import lawgo
from src.db.postgres import connect
from src.db.qdrant import BM25_VECTOR, COLLECTION, DENSE_VECTOR, client as qdrant_client, ensure_collection
from src.embed import bm25

TARGET_LAWS = [
    # 관세 3법 (코어)
    {"law_id": "001556", "name": "관세법"},
    {"law_id": "002421", "name": "관세법 시행령"},
    {"law_id": "006392", "name": "관세법 시행규칙"},
    # 특례법 2계열 — 기록지 평가에서 미적재로 확인(개선계획 ②a, 법령ID 2026-07-24 API 실측)
    {"law_id": "010097", "name": "자유무역협정의 이행을 위한 관세법의 특례에 관한 법률"},
    {"law_id": "010175", "name": "자유무역협정의 이행을 위한 관세법의 특례에 관한 법률 시행령"},
    {"law_id": "010172", "name": "자유무역협정의 이행을 위한 관세법의 특례에 관한 법률 시행규칙"},
    {"law_id": "000592", "name": "수출용 원재료에 대한 관세 등 환급에 관한 특례법"},
    {"law_id": "004043", "name": "수출용 원재료에 대한 관세 등 환급에 관한 특례법 시행령"},
    {"law_id": "007591", "name": "수출용 원재료에 대한 관세 등 환급에 관한 특례법 시행규칙"},
    # 무역·통관 개별법 — 골드셋(data/goldset.json) 정답에 등장하는 법령 (개선계획 ②c)
    {"law_id": "001467", "name": "대외무역법"},
    {"law_id": "003313", "name": "대외무역법 시행령"},
    {"law_id": "001513", "name": "식물방역법"},
    {"law_id": "012247", "name": "수입식품안전관리 특별법"},
    {"law_id": "001783", "name": "약사법"},
    {"law_id": "002015", "name": "화장품법"},
    {"law_id": "001459", "name": "전기용품 및 생활용품 안전관리법"},
    # 특수물자 개별법령 49종 — 세관장확인(관세법226조)·수출입 제한금지(대외무역법11조)·
    # 전략물자(대외무역법19조) 축의 통합공고 게재 법률 (법령ID 2026-07-25 lawSearch 실측).
    # 시행령·시행규칙은 미포함(필요 시 후속). 고시·공고(admrul)는 수집 경로 미구축.
    # -- 의약품·바이오·화장품
    {"law_id": "013572", "name": "첨단재생의료 및 첨단바이오의약품 안전 및 지원에 관한 법률"},
    {"law_id": "009514", "name": "의료기기법"},
    {"law_id": "009609", "name": "인체조직안전 및 관리 등에 관한 법률"},
    # -- 마약류
    {"law_id": "002025", "name": "마약류 관리에 관한 법률"},
    # -- 식품
    {"law_id": "001805", "name": "식품위생법"},
    {"law_id": "001507", "name": "축산물 위생관리법"},
    {"law_id": "009353", "name": "건강기능식품에 관한 법률"},
    {"law_id": "000419", "name": "친환경농어업 육성 및 유기식품 등의 관리ㆍ지원에 관한 법률"},
    {"law_id": "000474", "name": "양곡관리법"},
    {"law_id": "000165", "name": "먹는물관리법"},
    {"law_id": "012863", "name": "위생용품 관리법"},
    # -- 농림·수산 검역
    {"law_id": "001504", "name": "가축전염병 예방법"},
    {"law_id": "000422", "name": "종자산업법"},
    {"law_id": "001509", "name": "축산법"},
    {"law_id": "001499", "name": "사료관리법"},
    {"law_id": "001514", "name": "농약관리법"},
    {"law_id": "000416", "name": "비료관리법"},
    {"law_id": "010602", "name": "수산생물질병 관리법"},
    {"law_id": "001978", "name": "농수산물 품질관리법"},
    # -- 화학·환경·생활안전
    {"law_id": "000162", "name": "화학물질관리법"},
    {"law_id": "011857", "name": "화학물질의 등록 및 평가 등에 관한 법률"},
    {"law_id": "013098", "name": "생활화학제품 및 살생물제의 안전관리에 관한 법률"},
    {"law_id": "010383", "name": "잔류성오염물질 관리법"},
    {"law_id": "000324", "name": "오존층 보호 등을 위한 특정물질의 관리에 관한 법률"},
    {"law_id": "000154", "name": "폐기물의 국가 간 이동 및 그 처리에 관한 법률"},
    {"law_id": "001771", "name": "폐기물관리법"},
    {"law_id": "000155", "name": "자원의 절약과 재활용촉진에 관한 법률"},
    {"law_id": "000317", "name": "화학무기ㆍ생물무기의 금지와 특정화학물질ㆍ생물작용제 등의 제조ㆍ수출입 규제 등에 관한 법률"},
    # -- 방사선·원자력
    {"law_id": "011435", "name": "원자력안전법"},
    {"law_id": "011433", "name": "생활주변방사선 안전관리법"},
    # -- 전기·통신·계량·기계
    {"law_id": "001732", "name": "전파법"},
    {"law_id": "001457", "name": "계량에 관한 법률"},
    {"law_id": "001766", "name": "산업안전보건법"},
    {"law_id": "000239", "name": "건설기계관리법"},
    {"law_id": "001747", "name": "자동차관리법"},
    {"law_id": "000167", "name": "소음ㆍ진동관리법"},
    {"law_id": "001867", "name": "에너지이용 합리화법"},
    {"law_id": "001850", "name": "고압가스 안전관리법"},
    # -- 소비자·어린이 안전
    {"law_id": "012070", "name": "어린이제품 안전 특별법"},
    # -- 생물다양성·유전자원
    {"law_id": "009683", "name": "야생생물 보호 및 관리에 관한 법률"},
    {"law_id": "011540", "name": "생물다양성 보전 및 이용에 관한 법률"},
    {"law_id": "012788", "name": "유전자원의 접근ㆍ이용 및 이익 공유에 관한 법률"},
    {"law_id": "010499", "name": "농업생명자원의 보존ㆍ관리 및 이용에 관한 법률"},
    {"law_id": "011632", "name": "해양수산생명자원의 확보ㆍ관리 및 이용 등에 관한 법률"},
    # -- 목재·석유·에너지
    {"law_id": "011620", "name": "목재의 지속가능한 이용에 관한 법률"},
    {"law_id": "001860", "name": "석유 및 석유대체연료 사업법"},
    # -- 전략물자·방산
    {"law_id": "010107", "name": "방위사업법"},
    {"law_id": "001642", "name": "총포ㆍ도검ㆍ화약류 등의 안전관리에 관한 법률"},
    # -- 문화재·통관제한
    {"law_id": "001607", "name": "문화유산의 보존 및 활용에 관한 법률"},
]

SPLIT_THRESHOLD = 6000  # bge-m3 8192 토큰 대비 여유. 관세 3법 최장 5,325자였으나 개별법 추가로 분할 발생 가능(항 단위 분할 경로 사용)

MST_CACHE = Path("data/raw/law_mst.json")
EFLAW_INDEX_CACHE = Path("data/raw/law_eflaw_index.json")
DENSE_CACHE = Path("data/dense_cache.npz")

_POINT_NS = uuid.uuid5(uuid.NAMESPACE_URL, "trade-doc-auditor/law-article")


def _hierarchy(name: str) -> str:
    if name.endswith("시행령"):
        return "시행령"
    if name.endswith("시행규칙"):
        return "시행규칙"
    return "법률"


def _to_date(yyyymmdd: str) -> date:
    return datetime.strptime(yyyymmdd, "%Y%m%d").date()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def point_id(law_id: str, article_no: int, paragraph_no: int | None, mst: str) -> str:
    return str(uuid.uuid5(_POINT_NS, f"{law_id}:{article_no}:{paragraph_no or 0}:{mst}"))


# ---------------------------------------------------------------- 파싱/청킹

def resolve_current_mst(law_id: str, name: str, use_cache: bool = True) -> str:
    """현행법령 목록(lawSearch target=law)에서 법령일련번호(MST) 확인.

    본문 응답의 '법령키'는 법령ID+공포일자+공포번호 연결값이라 MST 가 아님 —
    Phase 3 변경이력(lsHstInf)의 법령일련번호와 조인하려면 진짜 MST 가 필요하다.
    결과는 data/raw/law_mst.json 에 캐시 — 쿼터 소진/API 장애 시에도 재적재 가능.
    """
    cached: dict[str, str] = (
        json.loads(MST_CACHE.read_text(encoding="utf-8")) if MST_CACHE.exists() else {}
    )
    if use_cache and law_id in cached:
        return cached[law_id]
    data = lawgo.get("lawSearch.do", target="law", query=name, display=100)
    for it in lawgo.as_list(data.get("LawSearch", {}).get("law")):
        if lawgo.squash(it.get("법령ID")) == law_id:
            mst = lawgo.squash(it.get("법령일련번호"))
            cached[law_id] = mst
            MST_CACHE.parent.mkdir(parents=True, exist_ok=True)
            MST_CACHE.write_text(
                json.dumps(cached, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return mst
    raise LookupError(f"현행법령 목록에서 법령ID {law_id}({name}) 미발견")


def build_meta(body: dict, mst: str) -> dict:
    info = body.get("법령", {}).get("기본정보", {})
    return {
        "law_id": (info.get("법령ID") or "").strip(),
        "law_name": (info.get("법령명_한글") or "").strip(),
        "mst": mst,
        "enforcement_date": lawgo.squash(info.get("시행일자")),
        "promulgation_date": lawgo.squash(info.get("공포일자")),
        "revision_type": lawgo.content_of(info.get("제개정구분")).strip() or "일부개정",
        "ministry": lawgo.content_of(info.get("소관부처")).strip(),
    }


def _assemble_text(unit: dict) -> str:
    """항/호/목 계층을 들여쓰기 평문으로 (프로토타입 검증 로직 이식)."""
    parts = []
    head = lawgo.clean_text(unit.get("조문내용"))
    if head:
        parts.append(head)
    for hang in lawgo.as_list(unit.get("항")):
        h = lawgo.clean_text(hang.get("항내용"))
        if h:
            parts.append(h)
        for ho in lawgo.as_list(hang.get("호")):
            t = lawgo.clean_text(ho.get("호내용"))
            if t:
                parts.append("  " + t)
            for mok in lawgo.as_list(ho.get("목")):
                m = lawgo.clean_text(mok.get("목내용"))
                if m:
                    parts.append("    " + m)
    return "\n".join(parts)


def parse_articles(body: dict, meta: dict) -> list[dict]:
    """조문단위 → 청크 행 목록. 임계 초과 조문은 항 단위 분할(부모+자식)."""
    units = lawgo.as_list(body.get("법령", {}).get("조문", {}).get("조문단위"))
    rows: list[dict] = []
    for u in units:
        if u.get("조문여부") and u.get("조문여부") != "조문":
            continue
        jo_no = lawgo.squash(u.get("조문번호"))
        jo_branch = lawgo.squash(u.get("조문가지번호")) or "0"
        text = _assemble_text(u)
        if not text:
            continue
        title = lawgo.clean_text(u.get("조문제목"))
        label = lawgo.jo_label(jo_no, jo_branch)
        heading = f"[{meta['law_name']} {label}({title})]" if title else f"[{meta['law_name']} {label}]"
        valid_from = lawgo.squash(u.get("조문시행일자")) or meta["enforcement_date"]
        base = {
            "article_no": lawgo.jo_code(jo_no, jo_branch),
            "title": title or None,
            "valid_from": _to_date(valid_from),
            "label": label,
        }
        full = f"{heading}\n{text}"
        if len(full) <= SPLIT_THRESHOLD:
            rows.append({**base, "paragraph_no": None, "content": full, "children": []})
        else:
            children = []
            for i, hang in enumerate(lawgo.as_list(u.get("항")), start=1):
                parts = [lawgo.clean_text(hang.get("항내용"))]
                for ho in lawgo.as_list(hang.get("호")):
                    parts.append("  " + lawgo.clean_text(ho.get("호내용")))
                    for mok in lawgo.as_list(ho.get("목")):
                        parts.append("    " + lawgo.clean_text(mok.get("목내용")))
                hang_text = "\n".join(p for p in parts if p.strip())
                if hang_text:
                    children.append(
                        {**base, "paragraph_no": i, "content": f"{heading}\n{hang_text}"}
                    )
            rows.append({**base, "paragraph_no": None, "content": full, "children": children})
    return rows


# ---------------------------------------------------------------- PG 적재

def upsert_law(cur, meta: dict) -> None:
    cur.execute(
        """
        INSERT INTO laws (law_id, law_name, hierarchy, ministry)
        VALUES (%(law_id)s, %(law_name)s, %(hierarchy)s, %(ministry)s)
        ON CONFLICT (law_id) DO UPDATE
          SET law_name = EXCLUDED.law_name, ministry = EXCLUDED.ministry
        """,
        {**meta, "hierarchy": _hierarchy(meta["law_name"])},
    )
    cur.execute(
        """
        INSERT INTO law_versions (law_id, mst, promulgation_date, enforcement_date, revision_type)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (law_id, mst) DO NOTHING
        """,
        (
            meta["law_id"],
            meta["mst"],
            _to_date(meta["promulgation_date"]),
            _to_date(meta["enforcement_date"]),
            meta["revision_type"],
        ),
    )


def _upsert_article_row(cur, law_id: str, mst: str, row: dict,
                        parent_pk: int | None = None) -> tuple[int, str]:
    """열린 행(valid_to IS NULL) 기준 upsert. 반환: (pk, 'new'|'changed'|'unchanged')."""
    chash = _content_hash(row["content"])
    is_current = row["valid_from"] <= date.today()
    cur.execute(
        """
        SELECT id, content_hash FROM law_articles
        WHERE law_id = %s AND article_no = %s
          AND paragraph_no IS NOT DISTINCT FROM %s AND item_no IS NULL
          AND valid_to IS NULL
        """,
        (law_id, row["article_no"], row["paragraph_no"]),
    )
    hit = cur.fetchone()
    if hit is None:
        cur.execute(
            """
            INSERT INTO law_articles
              (law_id, article_no, paragraph_no, title, content, content_hash,
               version_mst, valid_from, valid_to, is_current, parent_article_pk)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s)
            RETURNING id
            """,
            (law_id, row["article_no"], row["paragraph_no"], row["title"],
             row["content"], chash, mst, row["valid_from"], is_current, parent_pk),
        )
        return cur.fetchone()[0], "new"
    pk, old_hash = hit
    if old_hash == chash:
        return pk, "unchanged"
    # 초기 적재 시맨틱: 열린 행을 덮어쓴다. (버전 닫기+새 행은 Phase 3 델타 잡의 몫)
    cur.execute(
        """
        UPDATE law_articles
        SET title = %s, content = %s, content_hash = %s, version_mst = %s,
            valid_from = %s, is_current = %s, qdrant_point_id = NULL,
            parent_article_pk = %s
        WHERE id = %s
        """,
        (row["title"], row["content"], chash, mst,
         row["valid_from"], is_current, parent_pk, pk),
    )
    return pk, "changed"


def load_law(cur, meta: dict, rows: list[dict]) -> dict:
    upsert_law(cur, meta)
    stats = {"new": 0, "changed": 0, "unchanged": 0}
    for row in rows:
        pk, status = _upsert_article_row(cur, meta["law_id"], meta["mst"], row)
        stats[status] += 1
        for child in row["children"]:
            _, cstatus = _upsert_article_row(cur, meta["law_id"], meta["mst"], child, parent_pk=pk)
            stats[cstatus] += 1
    return stats


# ------------------------------------- 시행 전 법령: 현재 시행 중 버전 병행 적재
#
# lawService target=law("현행")는 공포된 최신 버전을 반환하는데, 그 시행일이 미래면
# 전 조문 is_current=false 로 남아 검색 인덱스에서 통째로 빠진다 (실측: 약사법
# 시행 2026-11-12, 식물방역법 2026-12-17). 이때 eflaw 목록의 '현행' 항목(현재
# 시행 중 버전)을 [그 시행일, 미래 시행일) 닫힌 구간 + is_current=true 로 병행
# 적재한다. 미래 버전 행은 열린 구간 그대로 pre-load — 시행일 도래 시
# sync.delta.promote() 가 교대시킨다.

def resolve_inforce_version(law_id: str, name: str, use_cache: bool = True) -> dict | None:
    """eflaw 목록에서 '현행'(현재 시행 중) 버전의 {mst, ef_yd}. 캐시: law_eflaw_index.json."""
    cached: dict[str, dict] = (
        json.loads(EFLAW_INDEX_CACHE.read_text(encoding="utf-8"))
        if EFLAW_INDEX_CACHE.exists() else {}
    )
    if use_cache and law_id in cached:
        return cached[law_id]
    data = lawgo.get("lawSearch.do", target="eflaw", query=name, display=100)
    for it in lawgo.as_list(data.get("LawSearch", {}).get("law")):
        if lawgo.squash(it.get("법령ID")) != law_id:
            continue
        if it.get("현행연혁코드") != "현행":
            continue
        entry = {
            "mst": lawgo.squash(it.get("법령일련번호")),
            "ef_yd": lawgo.squash(it.get("시행일자")),
        }
        cached[law_id] = entry
        EFLAW_INDEX_CACHE.parent.mkdir(parents=True, exist_ok=True)
        EFLAW_INDEX_CACHE.write_text(
            json.dumps(cached, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return entry
    return None


def _insert_inforce_row(cur, law_id: str, mst: str, row: dict, default_valid_to: date,
                        parent_pk: int | None = None) -> tuple[int | None, str]:
    """현재 시행 중 버전 행을 미래(열린) 행과 겹치지 않는 닫힌 구간으로 삽입. 멱등."""
    # 같은 조문의 미래 열린 행 → 그 valid_from 이 이 행의 valid_to (없으면 법령 미래 시행일)
    cur.execute(
        """
        SELECT min(valid_from) FROM law_articles
        WHERE law_id = %s AND article_no = %s
          AND paragraph_no IS NOT DISTINCT FROM %s AND item_no IS NULL
          AND valid_from > %s
        """,
        (law_id, row["article_no"], row["paragraph_no"], row["valid_from"]),
    )
    hit = cur.fetchone()
    valid_to = (hit[0] if hit and hit[0] else default_valid_to)
    if valid_to <= row["valid_from"]:  # 방어: 구간이 비면 스킵
        return None, "unchanged"
    # 멱등 + 이력 존중: 후보 구간과 겹치는 행이 하나라도 있으면 삽입하지 않는다.
    # (클라우드 DB엔 델타 잡/타 세션이 만든 다른 구간의 이력 행이 있을 수 있음 — EXCLUDE 위반 방지.)
    # 다만 그 행이 오늘을 커버하는데 비현행으로 남아 있으면 현행으로만 올려 검색 공백을 닫는다.
    cur.execute(
        """
        SELECT id FROM law_articles
        WHERE law_id = %s AND article_no = %s
          AND paragraph_no IS NOT DISTINCT FROM %s AND item_no IS NULL
          AND daterange(valid_from, valid_to, '[)') && daterange(%s, %s, '[)')
        """,
        (law_id, row["article_no"], row["paragraph_no"], row["valid_from"], valid_to),
    )
    if overlaps := cur.fetchall():
        cur.execute(
            """
            UPDATE law_articles SET is_current = true
            WHERE id = ANY(%s) AND NOT is_current
              AND valid_from <= CURRENT_DATE
              AND (valid_to IS NULL OR valid_to > CURRENT_DATE)
            """,
            ([o[0] for o in overlaps],),
        )
        return overlaps[0][0], "unchanged"
    is_current = row["valid_from"] <= date.today() < valid_to
    cur.execute(
        """
        INSERT INTO law_articles
          (law_id, article_no, paragraph_no, title, content, content_hash,
           version_mst, valid_from, valid_to, is_current, parent_article_pk)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (law_id, row["article_no"], row["paragraph_no"], row["title"],
         row["content"], _content_hash(row["content"]), mst,
         row["valid_from"], valid_to, is_current, parent_pk),
    )
    return cur.fetchone()[0], "new"


def load_inforce_version(law_id: str, name: str, future_enforcement: str,
                         use_cache: bool = True) -> None:
    ver = resolve_inforce_version(law_id, name, use_cache=use_cache)
    if ver is None:
        print(f"[inforce] {name}: eflaw 목록에서 현행 버전 미발견 — 스킵")
        return
    body = lawgo.fetch_eflaw_law(ver["mst"], ver["ef_yd"], use_cache=use_cache)
    meta = build_meta(body, ver["mst"])
    assert meta["law_id"] == law_id, f"법령ID 불일치: {meta}"
    rows = parse_articles(body, meta)
    default_valid_to = _to_date(future_enforcement)
    stats = {"new": 0, "unchanged": 0}
    with connect() as conn, conn.cursor() as cur:
        upsert_law(cur, meta)
        for row in rows:
            pk, status = _insert_inforce_row(cur, law_id, meta["mst"], row, default_valid_to)
            stats[status] += 1
            for child in row["children"]:
                _, cstatus = _insert_inforce_row(
                    cur, law_id, meta["mst"], child, default_valid_to, parent_pk=pk)
                stats[cstatus] += 1
    print(
        f"[inforce] {meta['law_name']} (MST {meta['mst']}, 시행 {meta['enforcement_date']}"
        f" → {future_enforcement} 전까지): 조문 {len(rows)} → {stats}"
    )


# ---------------------------------------------------------------- Qdrant 인덱싱

def index_qdrant(batch_size: int = 64) -> int:
    """is_current 이고 아직 포인트 없는 조문(분할 부모 제외)을 임베딩→upsert."""
    from qdrant_client import models

    from src.embed import dense

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, a.law_id, l.law_name, l.hierarchy, a.article_no,
                   a.paragraph_no, a.item_no, a.valid_from, a.content, a.version_mst
            FROM law_articles a JOIN laws l USING (law_id)
            WHERE a.is_current AND a.qdrant_point_id IS NULL
              AND NOT EXISTS (SELECT 1 FROM law_articles c WHERE c.parent_article_pk = a.id)
            ORDER BY a.id
            """
        )
        pending = cur.fetchall()
    if not pending:
        print("[qdrant] 인덱싱 대상 없음 (모두 최신)")
        return 0

    qc = qdrant_client()
    ensure_collection(qc)

    # dense 벡터 캐시 — content sha256 키. 미스만 추론(전 히트면 모델 로드 자체가 없음).
    cache_vecs: dict[str, np.ndarray] = {}
    if DENSE_CACHE.exists():
        with np.load(DENSE_CACHE) as z:
            cache_vecs = {k: z[k] for k in z.files}
    cache_hits, cache_dirty = 0, False

    total = 0
    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        texts = [r[8] for r in batch]
        hashes = [_content_hash(t) for t in texts]
        miss = [j for j, h in enumerate(hashes) if h not in cache_vecs]
        if miss:
            for j, v in zip(miss, dense.encode([texts[j] for j in miss])):
                cache_vecs[hashes[j]] = v
            cache_dirty = True
        cache_hits += len(batch) - len(miss)
        dvecs = [cache_vecs[h] for h in hashes]
        points, ids = [], []
        for r, dv in zip(batch, dvecs):
            pk, law_id, law_name, hierarchy, art_no, para_no, item_no, vfrom, text, mst = r
            pid = point_id(law_id, art_no, para_no, mst)
            sidx, sval = bm25.encode_doc(text)
            points.append(
                models.PointStruct(
                    id=pid,
                    vector={
                        DENSE_VECTOR: dv.tolist(),
                        BM25_VECTOR: models.SparseVector(indices=sidx, values=sval),
                    },
                    payload={
                        "article_pk": pk,
                        "law_id": law_id,
                        "law_name": law_name,
                        "hierarchy": hierarchy,
                        "article_no": art_no,
                        "paragraph_no": para_no,
                        "item_no": item_no,
                        "enforcement_date": vfrom.isoformat(),
                        "text": text,
                    },
                )
            )
            ids.append((pid, pk))
        qc.upsert(collection_name=COLLECTION, points=points)
        with connect() as conn, conn.cursor() as cur:
            cur.executemany(
                "UPDATE law_articles SET qdrant_point_id = %s WHERE id = %s", ids
            )
        total += len(points)
        print(f"[qdrant] {total}/{len(pending)} upsert")
    if cache_dirty:
        np.savez_compressed(DENSE_CACHE, **cache_vecs)
    print(f"[cache] dense 히트 {cache_hits}/{total} · 저장분 {len(cache_vecs)}건 ({DENSE_CACHE})")
    return total


# ---------------------------------------------------------------- 검증

def verify() -> None:
    with connect() as conn, conn.cursor() as cur:
        print("\n[PG 법령별 조문 수]")
        cur.execute(
            """
            SELECT l.law_name, count(*) FILTER (WHERE a.is_current) AS current,
                   count(*) AS total,
                   count(*) FILTER (WHERE a.qdrant_point_id IS NOT NULL) AS indexed
            FROM law_articles a JOIN laws l USING (law_id)
            GROUP BY l.law_name ORDER BY l.law_name
            """
        )
        for name, current, total, indexed in cur.fetchall():
            print(f"  {name}: 현행 {current} / 전체 {total} / 인덱싱 {indexed}")
        cur.execute("SELECT count(*) FROM law_articles WHERE parent_article_pk IS NOT NULL")
        print(f"  분할 청크(항 단위): {cur.fetchone()[0]}건")

    qc = qdrant_client()
    cnt = qc.count(COLLECTION).count
    print(f"[Qdrant] {COLLECTION} 포인트 수: {cnt}")


def smoke_search(query: str = "수입신고 시 세관장 확인이 필요한 물품") -> None:
    """dense arm / bm25 arm 각각 3건 — 융합·rerank 는 에이전트 소유(설계 §4.2)."""
    from qdrant_client import models

    from src.embed import dense

    qc = qdrant_client()
    dv = dense.encode([query])[0]
    hits = qc.query_points(COLLECTION, query=dv.tolist(), using=DENSE_VECTOR, limit=3)
    print(f"\n[smoke/dense] '{query}'")
    for p in hits.points:
        print(f"  {p.score:.4f} {p.payload['law_name']} 조문 {p.payload['article_no']}")
    sidx, sval = bm25.encode_query(query)
    hits = qc.query_points(
        COLLECTION,
        query=models.SparseVector(indices=sidx, values=sval),
        using=BM25_VECTOR,
        limit=3,
    )
    print("[smoke/bm25]")
    for p in hits.points:
        print(f"  {p.score:.4f} {p.payload['law_name']} 조문 {p.payload['article_no']}")


def main(argv: list[str]) -> int:
    use_cache = "--no-cache" not in argv
    for spec in TARGET_LAWS:
        body = lawgo.fetch_current_law(spec["law_id"], use_cache=use_cache)
        mst = resolve_current_mst(spec["law_id"], spec["name"], use_cache=use_cache)
        meta = build_meta(body, mst)
        assert meta["law_id"] == spec["law_id"], f"법령ID 불일치: {meta}"
        rows = parse_articles(body, meta)
        with connect() as conn, conn.cursor() as cur:
            stats = load_law(cur, meta, rows)
        split = sum(1 for r in rows if r["children"])
        print(
            f"[pg] {meta['law_name']} (MST {meta['mst']}, 시행 {meta['enforcement_date']}): "
            f"조문 {len(rows)} (분할 {split}) → {stats}"
        )
        # '현행' 응답이 시행 전 버전이면 현재 시행 중 버전을 병행 적재 (검색 공백 방지)
        if _to_date(meta["enforcement_date"]) > date.today():
            load_inforce_version(
                spec["law_id"], spec["name"], meta["enforcement_date"], use_cache=use_cache)
    if "--skip-embed" not in argv:
        index_qdrant()
    verify()
    if "--skip-embed" not in argv:
        smoke_search()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
