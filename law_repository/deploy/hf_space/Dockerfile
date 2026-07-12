# HF Spaces (Docker Space) 용 — 관세법령 저장소 웹 데모
# Space 저장소에 이 Dockerfile 과 README.md(메타데이터) 두 파일만 올리면 된다.
# 코드는 빌드 시 GitHub 에서 clone — 코드 업데이트 반영은 Space 설정의 Factory rebuild.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN git clone --depth 1 https://github.com/vector2967/trade-doc-auditor.git repo
WORKDIR /app/repo/law_repository

# torch 는 CPU 휠로 (기본 인덱스는 CUDA 포함이라 이미지가 수 GB 커짐)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

# bge-m3 + bge-reranker-v2-m3(A1 rerank) 를 이미지에 미리 다운로드 — 콜드스타트에서 모델 받는 시간 제거
ENV HF_HOME=/app/hf_cache
RUN python -c "from FlagEmbedding import BGEM3FlagModel; BGEM3FlagModel('BAAI/bge-m3', use_fp16=False)" \
 && python -c "from FlagEmbedding import FlagReranker; FlagReranker('BAAI/bge-reranker-v2-m3', use_fp16=False)" \
 && chmod -R a+rwX /app/hf_cache

# DB 접속정보(POSTGRES_*, QDRANT_*, NEO4J_*)와 DEMO_TOKEN 은
# Space Settings > Variables and secrets 에 등록한다 (.env 파일 불필요 — env 가 우선).
ENV PORT=7860
EXPOSE 7860
CMD ["python", "-m", "src.webapp"]
