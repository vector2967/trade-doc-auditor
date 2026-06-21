law_test.py는 api통해서 불러온후
엠비딩 및 청킹과정까지 모두 포함하고있는 코드이기에
많은양의 데이터를 다운하고 모듈을 불러옵니다
직접 로컬에서 확인하고싶으시다면

docker설치후
docker run -p 6333:6333 -p 6334:6334 -v qdrant_storage:/qdrant/storage qdrant/qdrant

명령어 실행하신후에

law_for_vectordb.py 실행하시고 localhost:6333 가보시면 있을껍니다.
