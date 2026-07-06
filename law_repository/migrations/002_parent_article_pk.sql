-- 설계 §10 청킹 확정 반영: 조문 1청크 기본, 토큰 임계 초과 조문만 항 단위 분할.
-- 분할 청크(항 행)는 원 조문 행을 parent_article_pk 로 가리킨다.
ALTER TABLE law_articles
  ADD COLUMN IF NOT EXISTS parent_article_pk bigint REFERENCES law_articles(id);
