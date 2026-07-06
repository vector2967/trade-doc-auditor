---
title: 관세법령 저장소 데모
emoji: ⚖️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 관세법령 저장소 웹 데모 (HF Space)

관세법·시행령·시행규칙 조문 검색(dense/BM25) + HS 품목 요건 + 시점 조회.
데이터는 Neon(Postgres) / Qdrant Cloud / Neo4j AuraDB 에 있고, 이 Space 는
임베딩 + 웹 UI 만 담당한다.

## 이 Space 만드는 법 (호스트 1회 작업)

1. huggingface.co → New Space → SDK: **Docker** (Blank)
2. 이 폴더의 `Dockerfile` 과 `README.md` 를 Space 저장소에 업로드
3. Settings → **Variables and secrets** 에 등록 (deploy/.env.cloud.example 의 키 그대로):
   - `POSTGRES_HOST` `POSTGRES_DB` `POSTGRES_USER` `POSTGRES_PASSWORD` `POSTGRES_SSLMODE=require`
   - `QDRANT_URL` `QDRANT_API_KEY`
   - `NEO4J_URI` `NEO4J_USER` `NEO4J_PASSWORD`
   - `DEMO_TOKEN` (팀원과 공유할 임의 문자열 — 없으면 전세계 공개)
4. 빌드 완료 후 팀원에게 링크 공유: `https://<space주소>/?token=<DEMO_TOKEN>`

- 무료 CPU Space 는 미사용 시 잠들었다가 첫 접속에서 깨어난다(~1분).
- GitHub 코드가 바뀌면: Settings → Factory rebuild (빌드 시 clone 이므로).
