# 관세법령 저장소 (law_repository)

무역서류 AI 감사 프로젝트의 법령 저장소 레이어.
관세법·시행령·시행규칙 전 조문(1,008개)을 자동 수집해서 3개 저장소에 나눠 담는다:

| 저장소 | 역할 |
|---|---|
| Postgres (`:5432`) | 시간버전 원장 — 모든 조문의 모든 버전 + 유효기간. 진실의 원천 |
| Qdrant (`:6333`) | 현행 조문 검색 인덱스 — 의미검색(bge-m3 dense) + 키워드(BM25) |
| Neo4j (`:7474`/`:7687`) | 그래프 — 조문 위임/인용, HS품목→요건법령→서류→기관 |

배경·설계 근거는 `docs/` (로컬 문서) 및 팀 공유 보고서 참고.

---

## A. 그냥 써보고 싶은 팀원 (설치 없음)

호스트(서버 담당자)가 데모 서버를 띄운 상태라면 **브라우저에서
`http://<호스트IP>:8010`** 만 열면 된다. 끝.

- **조문 검색**: 일상어로 질문 (예: "물건이 상해서 돌려보내면 세금 돌려받을 수 있나?")
  → dense(의미)/bm25(키워드) 결과 나란히, 클릭하면 조문 전문
- **품목 요건**: HS코드 입력 (예: `0203291000` 삼겹살) → 세관장확인 대상 여부 + 필요서류·기관
- **조문 직접 조회**: 시행령 92조를 시점 `2026-04-01`로 조회하면 개정 전 본문이 나온다 (시간원장 데모)

## B. 전체 환경을 내 PC에 구축하려는 팀원

전제: Docker Desktop, Python 3.11+

```bash
git clone https://github.com/vector2967/trade-doc-auditor.git
cd trade-doc-auditor/law_repository

cp .env.example .env        # 비밀번호 등 값 채우기 (기본값으로도 로컬 구동 가능)
pip install -r requirements.txt

docker compose up -d        # Postgres / Qdrant / Neo4j 기동
python scripts/migrate.py   # 스키마 적용

python -m src.ingest.hsk            # ① HSK 마스터 (레포 내 xlsx, ~1분)
python -m src.ingest.laws           # ② 조문 수집+임베딩 (~15분, CPU 임베딩)
python -m src.ingest.ccct --load-only   # ③ 세관장확인 요건 (레포 내 스윕 결과 적재, ~1분)
python -m src.ingest.graph          # ④ 조문 위임/인용 그래프 (~1분)

pytest tests/               # 검증 63건 (모두 통과해야 정상)
```

- ③에서 `--load-only` 를 빼면 관세청 API 전수 재수집을 시도하는데, 일일 쿼터(1만건) 때문에
  3일 걸린다. 레포에 커밋된 `data/ccct_progress.jsonl` 이 그 결과물이니 그대로 적재하면 된다.
- 이후 최신화는 `python -m src.sync.delta` (매일 1회 권장, 재실행 안전/멱등).

## C. 서버를 띄우는 사람 (호스트)

```bash
python -m src.webapp        # http://0.0.0.0:8010 — 팀원에게 내 IP 공유
```

- 기동 시 임베딩 모델을 예열하므로 준비까지 수십 초.
- Windows 방화벽이 물으면 "허용". 수동 개방:
  `netsh advfirewall firewall add rule name="lawrepo-demo" dir=in action=allow protocol=TCP localport=8010`
- **인증이 없다. 사내망/같은 공유기 안에서만 쓰고 외부 인터넷에 노출하지 말 것.**

## 터미널 도구 (환경 구축한 사람용)

```bash
python -m src.cli           # 대화형 검색 (:hsk, :art, :asof 명령 — 파일 docstring 참고)
python -m src.repository    # 스모크 데모
```

## 코드 구조

```
src/
  lawgo.py          법제처 API 클라이언트 (target 규약 문서화)
  ingest/           적재기 — laws(조문) / hsk(품목) / ccct(요건) / graph(조문그래프)
  sync/delta.py     델타 동기화 + 승격 잡 (매일 1회)
  embed/            bge-m3 dense + BM25 sparse 인코더 (인덱스/쿼리 동일 인코더)
  repository.py     조회 계약 — search / resolve_as_of / hsk_requirements / expand_article
  cli.py            대화형 CLI
  webapp.py         팀 데모 웹서버 (FastAPI)
migrations/         Postgres DDL (유효구간 EXCLUDE 제약 포함)
tests/              63건 — 스키마/적재/델타/그래프/repository/cross-store 정합성
```

주의: 검색 결과 융합·rerank 는 이 레이어가 **일부러 안 한다** (에이전트 레이어 소유).
`search()` 는 arm 별 `{article_pk, score, text}` 만 반환한다.
