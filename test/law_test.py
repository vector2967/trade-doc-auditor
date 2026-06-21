from __future__ import annotations

import os
import re
import sys
import json
import time
import html
from pathlib import Path

import requests

OC = os.environ.get("LAW_API_OC", "alfinekey")
BASE = "http://www.law.go.kr/DRF"

RAW_DIR = Path("data/raw")
OUT_PATH = Path("data/law_chunks.jsonl")

TARGET_LAWS = [
    {"name": "관세법", "mst": "280363"},
    {"name": "관세법 시행령", "mst": "285897"},
    {"name": "관세법 시행규칙", "mst": "284979"},
]

session = requests.Session()
session.headers.update({"User-Agent": "trade-doc-auditor/1.0"})


def _get(endpoint: str, **params) -> dict:
    params.setdefault("OC", OC)
    params.setdefault("type", "JSON")
    r = session.get(f"{BASE}/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return _clean_keys(r.json())


def _clean_keys(obj):
    if isinstance(obj, dict):
        return {re.sub(r"\s+", "", k): _clean_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_keys(v) for v in obj]
    return obj


def _as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _squash(s: str | None) -> str:
    return re.sub(r"\s+", "", s or "")


def _clean_text(s) -> str:
    if not s:
        return ""
    if isinstance(s, list):
        s = "\n".join(_clean_text(x) for x in s)
    s = html.unescape(str(s)).replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return s.strip()


def jo_label(no: str, branch: str) -> str:
    n = int(no) if str(no).isdigit() else no
    if branch and str(branch) not in ("00", "0", ""):
        b = int(branch) if str(branch).isdigit() else branch
        return f"제{n}조의{b}"
    return f"제{n}조"


def search_law(name: str) -> dict | None:
    data = _get("lawSearch.do", target="law", query=name, display=100)
    for it in _as_list(data.get("LawSearch", {}).get("law")):
        if _squash(it.get("법령명한글")) == _squash(name):
            return it
    return None


def fetch_law_body(mst: str) -> dict:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache = RAW_DIR / f"law_{mst}.json"
    local = Path(f"law_{mst}.json")
    for p in (cache, local):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    body = _get("lawService.do", target="law", MST=mst)
    cache.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return body


def _assemble_text(unit: dict) -> str:
    parts = []
    head = _clean_text(unit.get("조문내용"))
    if head:
        parts.append(head)
    for hang in _as_list(unit.get("항")):
        h = _clean_text(hang.get("항내용"))
        if h:
            parts.append(h)
        for ho in _as_list(hang.get("호")):
            t = _clean_text(ho.get("호내용"))
            if t:
                parts.append("  " + t)
            for mok in _as_list(ho.get("목")):
                m = _clean_text(mok.get("목내용"))
                if m:
                    parts.append("    " + m)
    return "\n".join(parts)


def _content(x) -> str:
    if isinstance(x, dict):
        return str(x.get("content", ""))
    return str(x) if x else ""


def build_meta(body: dict, mst: str) -> dict:
    info = body.get("법령", {}).get("기본정보", {})
    return {
        "법령명": (info.get("법령명_한글") or "").strip(),
        "법령구분": _content(info.get("법종구분")).strip(),
        "법령ID": (info.get("법령ID") or "").strip(),
        "MST": mst,
        "시행일자": _squash(info.get("시행일자")),
        "공포일자": _squash(info.get("공포일자")),
        "공포번호": _squash(info.get("공포번호")),
        "소관부처": _content(info.get("소관부처")).strip(),
    }


def parse_articles(body: dict, meta: dict) -> list[dict]:
    law = body.get("법령", body)
    units = _as_list(law.get("조문", {}).get("조문단위"))

    detail_url = (
        f"{BASE}/lawService.do?OC={OC}&target=law&MST={meta['MST']}"
        f"&type=HTML&efYd={meta['시행일자']}"
    )

    chunks = []
    for u in _as_list(units):
        if u.get("조문여부") and u.get("조문여부") != "조문":
            continue

        jo_no = _squash(u.get("조문번호"))
        jo_branch = _squash(u.get("조문가지번호")) or "00"
        title = _clean_text(u.get("조문제목"))
        jo_ef = _squash(u.get("조문시행일자"))
        text = _assemble_text(u)
        if not text:
            continue

        label = jo_label(jo_no, jo_branch)
        heading = f"[{meta['법령명']} {label}({title})]" if title else f"[{meta['법령명']} {label}]"

        chunks.append({
            "id": f"{meta['법령ID']}-{jo_no}-{jo_branch}",
            "text": f"{heading}\n{text}",
            "metadata": {
                **meta,
                "조문번호": jo_no,
                "조문가지번호": jo_branch,
                "조문라벨": label,
                "조문제목": title,
                "조문시행일자": jo_ef,
                "source_url": detail_url,
            },
        })
    return chunks


def inspect_structure():
    spec = TARGET_LAWS[0]
    body = fetch_law_body(spec["mst"])

    def walk(o, depth=0, max_depth=3):
        pad = "  " * depth
        if isinstance(o, dict):
            for k, v in o.items():
                kind = type(v).__name__
                n = f"[{len(v)}]" if isinstance(v, (list, dict)) else ""
                print(f"{pad}{k} <{kind}>{n}")
                if depth < max_depth:
                    walk(v, depth + 1, max_depth)
        elif isinstance(o, list) and o:
            walk(o[0], depth, max_depth)

    print(f"=== '{spec['name']}' 본문 JSON 구조 ===")
    walk(body)


def embed_and_upsert(collection: str = "law",
                     qdrant_url: str = "http://localhost:6333"):
    import numpy as np
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams, PointStruct

    records = [json.loads(l) for l in OUT_PATH.open(encoding="utf-8")]
    if not records:
        print("[!] 청크가 없음. 먼저 수집을 실행하세요.")
        return

    client = QdrantClient(url=qdrant_url)
    try:
        client.get_collections()
    except Exception as e:
        print(f"[!] Qdrant 연결 실패 ({qdrant_url}). 서버를 먼저 띄우세요:")
        print("    docker run -p 6333:6333 -p 6334:6334 -v qdrant_storage:/qdrant/storage qdrant/qdrant")
        print(f"    원인: {e}")
        return

    cache = OUT_PATH.with_suffix(".dense.npy")
    if cache.exists():
        dense = np.load(cache)
        print(f"[cache] 임베딩 로드 {cache} {dense.shape}")
    else:
        from FlagEmbedding import BGEM3FlagModel
        model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True)
        dense = model.encode([r["text"] for r in records],
                             batch_size=12, max_length=8192)["dense_vecs"]
        dense = np.asarray(dense, dtype="float32")
        np.save(cache, dense)
        print(f"[cache] 임베딩 저장 {cache} {dense.shape}")

    if client.collection_exists(collection):
        client.delete_collection(collection)
    client.create_collection(
        collection_name=collection,
        vectors_config=VectorParams(size=int(dense.shape[1]), distance=Distance.COSINE),
    )

    points = [
        PointStruct(
            id=i,
            vector=dense[i].tolist(),
            payload={**records[i]["metadata"], "text": records[i]["text"]},
        )
        for i in range(len(records))
    ]
    client.upsert(collection_name=collection, points=points)
    print(f"[ok] {len(points)}개 업서트 → '{collection}'  count={client.count(collection).count}")


def collect() -> list[dict]:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    all_chunks: list[dict] = []

    for spec in TARGET_LAWS:
        name, mst = spec["name"], spec["mst"]
        print(f"[fetch] {name} (MST={mst}) ...", flush=True)
        body = fetch_law_body(mst)
        chunks = parse_articles(body, build_meta(body, mst))
        print(f"        조문 {len(chunks)}개 추출")
        all_chunks.extend(chunks)
        time.sleep(0.5)

    with OUT_PATH.open("w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"\n총 {len(all_chunks)}개 청크 ({len(TARGET_LAWS)}개 법령) → {OUT_PATH}")
    return all_chunks


def dump_vectors(collection: str = "law",
                 qdrant_url: str = "http://localhost:6333"):
    import numpy as np
    from qdrant_client import QdrantClient
    client = QdrantClient(url=qdrant_url)
    pts, _ = client.scroll(collection, limit=100000,
                           with_payload=False, with_vectors=True)
    pts.sort(key=lambda p: int(p.id))
    arr = np.asarray([p.vector for p in pts], dtype="float32")
    out = OUT_PATH.with_suffix(".dense.npy")
    out.parent.mkdir(parents=True, exist_ok=True)
    np.save(out, arr)
    print(f"[ok] {out} {arr.shape} (Qdrant에서 추출, 재임베딩 없음)")


if __name__ == "__main__":
    if "--inspect" in sys.argv:
        inspect_structure()
    elif "--upsert" in sys.argv:
        embed_and_upsert()
    elif "--dump-vectors" in sys.argv:
        dump_vectors()
    else:
        collect()