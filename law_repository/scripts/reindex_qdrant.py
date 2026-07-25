"""Qdrant 전량 재인덱싱 — BM25 토크나이저 교체 등 인코더 변경 시 사용.

법령 원문 재수집 없이 PG 에 있는 조문으로 Qdrant 만 다시 만든다:
qdrant_point_id 를 리셋해 index_qdrant() 의 '미인덱싱' 조건에 다시 걸리게 한다.
point id 는 (law_id, article_no, paragraph_no, mst) 결정적 해시라 upsert 가
기존 포인트를 덮어쓴다(고아 포인트 없음). dense 는 content-hash 캐시 전량
히트이므로 임베딩 추론이 없고, sparse(BM25)만 재계산된다.

실행: law_repository/ 에서  python scripts/reindex_qdrant.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db.postgres import connect  # noqa: E402
from src.ingest.laws import index_qdrant, verify  # noqa: E402


def main() -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE law_articles SET qdrant_point_id = NULL"
            " WHERE qdrant_point_id IS NOT NULL"
        )
        print(f"[reindex] qdrant_point_id 리셋: {cur.rowcount}건")
    total = index_qdrant()
    print(f"[reindex] 재인덱싱 완료: {total}건")
    verify()
    return 0


if __name__ == "__main__":
    sys.exit(main())
