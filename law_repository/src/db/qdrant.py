"""Qdrant 현행 인덱스 — customs_law_current (설계 §4.2).

named dense(bge-m3 1024, cosine) + sparse bm25(modifier=idf).
융합/rerank 는 저장소가 하지 않음 — arm 별 {article_pk, score, text} 만 반환.
"""
from __future__ import annotations

from qdrant_client import QdrantClient, models

from src.config import settings

COLLECTION = "customs_law_current"

DENSE_VECTOR = "dense"   # bge-m3 dense → "Vector 검색 근거"
BM25_VECTOR = "bm25"     # Qdrant native sparse(idf) → "BM25 검색 근거"

# 필터/표시용 payload 인덱스 (진실은 PG). 설계 §4.2 표.
_PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "article_pk": models.PayloadSchemaType.INTEGER,
    "law_id": models.PayloadSchemaType.KEYWORD,
    "law_name": models.PayloadSchemaType.KEYWORD,
    "hierarchy": models.PayloadSchemaType.KEYWORD,
    "article_no": models.PayloadSchemaType.INTEGER,
}


def client() -> QdrantClient:
    return QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,  # Qdrant Cloud 인증 (로컬은 비움)
    )


def ensure_collection(qc: QdrantClient | None = None) -> None:
    """컬렉션이 없으면 생성. 현행 인덱스이므로 drop-recreate 하지 않는다."""
    qc = qc or client()
    if not qc.collection_exists(COLLECTION):
        qc.create_collection(
            collection_name=COLLECTION,
            vectors_config={
                DENSE_VECTOR: models.VectorParams(
                    size=1024, distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={
                BM25_VECTOR: models.SparseVectorParams(
                    modifier=models.Modifier.IDF
                )
            },
        )
    for field, schema in _PAYLOAD_INDEXES.items():
        qc.create_payload_index(COLLECTION, field_name=field, field_schema=schema)
