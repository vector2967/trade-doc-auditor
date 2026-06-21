from qdrant_client import QdrantClient
from FlagEmbedding import BGEM3FlagModel

client = QdrantClient(url="http://localhost:6333")
model = BGEM3FlagModel("BAAI/bge-m3", use_fp16=True)

q = "수입물품의 과세가격은 어떻게 결정하나?"
qv = model.encode([q], max_length=8192)["dense_vecs"][0]

hits = client.query_points(
    collection_name="customs_law",
    query=qv.tolist(),
    limit=5,
    with_payload=True,
).points

for h in hits:
    m = h.payload
    print(f"{h.score:.3f}  {m['법령명']} {m['조문라벨']}({m['조문제목']})")