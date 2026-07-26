---
title: 관세법령 저장소 데모
emoji: ⚖️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 통관 법령 검색 데모 (HF Space)

에이전트 파이프라인 검색(재작성+dense/bm25 융합, recall@5 0.73) + 법령
목록/조문/위임체인 열람(demo_web.py, Toss 스타일 UI). 데이터는
Neon(Postgres) / Qdrant Cloud / Neo4j AuraDB 에 있고, 이 Space 는
임베딩 + 웹 UI 만 담당한다. CPU Space 라 '빠른 모드(rerank 생략)'가
기본이다 — rerank 혼합 풀 파이프라인은 GPU 로컬 데모에서.

## 이 Space 만드는 법 (호스트 1회 작업)

1. huggingface.co → New Space → SDK: **Docker** (Blank)
2. 이 폴더의 `Dockerfile` 과 `README.md` 를 Space 저장소에 업로드
   (기존 Space 가 있으면 두 파일 교체 후 Settings → Factory rebuild)
3. Settings → **Variables and secrets** 에 등록 (deploy/.env.cloud.example 의 키 그대로):
   - `POSTGRES_HOST` `POSTGRES_DB` `POSTGRES_USER` `POSTGRES_PASSWORD` `POSTGRES_SSLMODE=require`
   - `QDRANT_URL` `QDRANT_API_KEY`
   - `NEO4J_URI` `NEO4J_USER` `NEO4J_PASSWORD`
   - `DEMO_TOKEN` (팀원과 공유할 임의 문자열 — 없으면 전세계 공개)
   - (선택, 실시간 LLM 재작성) `ANTHROPIC_API_KEY` `QUERY_REWRITE_OPENAI_URL`
     `QUERY_REWRITE_MODEL` — 없으면 캐시+레키콘만으로 동작
4. 빌드 완료 후 팀원에게 링크 공유: `https://<space주소>/?token=<DEMO_TOKEN>`

- 무료 CPU Space 는 미사용 시 잠들었다가 첫 접속에서 깨어난다(~1분).
- GitHub 코드가 바뀌면: Settings → Factory rebuild (빌드 시 clone 이므로).
