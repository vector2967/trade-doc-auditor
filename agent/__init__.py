"""에이전트 레이어 — 저장소(law_repository) 계약 위에서 융합·rerank·질의 전략을 소유한다.

설계 원칙(§4.2): repository.search() 는 arm 별 원시 결과만 반환하고,
정규화·RRF 융합·Reranker 는 이 레이어가 한다.
"""
