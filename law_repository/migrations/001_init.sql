-- 설계 §4.1 Postgres DDL — 시간버전 원장
-- 실행: python scripts/migrate.py  (또는 psql -f)

-- pgvector 포함 이미지 + 유효구간 겹침 제약(EXCLUDE)용 확장
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- 법령 마스터: 개정돼도 불변인 단위
CREATE TABLE laws (
  law_id       text PRIMARY KEY,        -- 법령ID (개정 불변)
  law_name     text NOT NULL,
  hierarchy    text NOT NULL,           -- 법률 / 시행령 / 시행규칙
  ministry     text
);

-- 개정 단위: 개정마다 새 행
CREATE TABLE law_versions (
  id                bigserial PRIMARY KEY,
  law_id            text NOT NULL REFERENCES laws(law_id),
  mst               text NOT NULL,      -- 법령일련번호 (개정마다 새로 발급)
  promulgation_date date NOT NULL,      -- 공포일자
  enforcement_date  date NOT NULL,      -- 시행일자
  revision_type     text NOT NULL,      -- 제정/전부개정/일부개정/폐지
  UNIQUE (law_id, mst)
);

-- 조문(temporal): 같은 조문의 각 버전이 유효구간을 가짐
CREATE TABLE law_articles (
  id            bigserial PRIMARY KEY,
  law_id        text NOT NULL REFERENCES laws(law_id),
  article_no    int  NOT NULL,          -- 조
  paragraph_no  int,                    -- 항
  item_no       int,                    -- 호
  title         text,
  content       text NOT NULL,          -- 조문 원문
  content_hash  text NOT NULL,          -- 재임베딩 판단용
  version_mst   text NOT NULL,          -- 어느 개정에서 온 조문인지
  valid_from    date NOT NULL,          -- 효력 시작 = 해당 개정 시행일
  valid_to      date,                   -- 효력 종료 = 다음 개정 시행일 (최신 버전이면 NULL)
  is_current    boolean NOT NULL DEFAULT false,  -- "지금" 효력 있는 버전인가 (매일 잡이 유지)
  qdrant_point_id uuid,                 -- 현행일 때만 Qdrant 포인트, 아니면 NULL

  -- 같은 조문의 유효구간이 겹치지 않도록 강제 (반열림 [from, to))
  EXCLUDE USING gist (
    law_id      WITH =,
    article_no  WITH =,
    coalesce(paragraph_no, -1) WITH =,
    coalesce(item_no, -1)      WITH =,
    daterange(valid_from, valid_to, '[)') WITH &&
  )
);

-- 현행 조문 조회 가속 (repository 의 temporal predicate)
CREATE INDEX idx_law_articles_current
  ON law_articles (law_id, article_no)
  WHERE is_current;

-- 동기화 watermark
CREATE TABLE sync_state (
  id                 int PRIMARY KEY DEFAULT 1,
  last_synced_at     timestamptz NOT NULL,
  last_change_marker text            -- API 페이지네이션/변경 커서
);

-- 감사 findings 는 버전이 박힌 조문을 가리켜야 사후 추적이 됨
-- (audit_findings 는 별도 슬라이스에서 생성. 여기선 참조 컬럼만 정의해두는 스텁)
CREATE TABLE audit_findings (
  id              uuid PRIMARY KEY,
  law_article_id  bigint REFERENCES law_articles(id),  -- 실제 근거 = 특정 버전에 고정
  severity        text,
  law_ref         text,          -- 표시용 텍스트 표기
  confidence      float
);
