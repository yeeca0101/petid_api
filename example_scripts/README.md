# 예제 스크립트 안내 (Qdrant PoC)

기본값은 API 서버가 `http://localhost:8001`에서 실행 중이라고 가정합니다.  
다른 주소를 사용할 경우 `API_BASE` 환경변수를 지정하세요.

예:
```bash
API_BASE=http://<host>:8001 bash 00_health.sh
```

## 사전 준비

- 실행 위치: `example_scripts` 디렉터리
- 필수 도구: `bash`, `curl`, `python3`
- 일부 스크립트는 `jq`가 있으면 출력 가독성이 좋아집니다.

## 스크립트 목록

### 기본 점검/유틸

- `00_health.sh`  
  서버 헬스체크 (`GET /v1/health`)

- `00_compile_api.sh`  
  API 파이썬 코드 컴파일 체크 (`python3 -m compileall app`)

- `06_clear_qdrant.sh`  
  Qdrant 데이터 정리 유틸 (points 삭제 또는 collection 삭제)

### 임베딩/인입/검색/라벨

- `01_embed.sh`  
  단일 이미지 임베딩 (`POST /v1/embed`)

- `02_embed_batch.sh`  
  배치 임베딩 (`POST /v1/embed/batch`)

- `03_ingest.sh`  
  이미지 인입 (`POST /v1/ingest`) 후 응답을  
  `example_scripts/last_ingest.json`에 저장

- `04_search.sh`  
  유사 검색 (`POST /v1/search`)

- `05_labels.sh`  
  라벨 반영 (`POST /v1/labels`)

### 반자동 분류 E2E

- `07_classification_smoke.sh`  
  `classify/auto -> classify/similar -> buckets/finalize -> get buckets` 스모크 테스트

- `08_e2e_registered_unlabeled.sh`  
  등록 이미지 라벨링 + unlabeled 인입 + 자동분류 + finalize E2E

- `09_verify_after_e2e.sh`  
  E2E 이후 pets/images/buckets/meta 검증 리포트

- `10_reset_and_reseed_e2e.sh`  
  Qdrant + 로컬 저장소 초기화 후 `08 -> 09` 연속 실행

- `gradio_demo/run_gradio_demo.sh`  
  웹 UI로 반자동 분류 흐름 검증

## 빠른 시작

```bash
bash 00_health.sh
bash 01_embed.sh
bash 03_ingest.sh
bash 04_search.sh
bash 05_labels.sh
bash 07_classification_smoke.sh
```

## 중요한 주의사항 (현재 코드 기준)

- `01_embed.sh`, `02_embed_batch.sh`, `03_ingest.sh`의 기본 샘플 경로는  
  `/workspace/PoC/dogface_fastapi_poc/test_images/...` 입니다.  
  해당 파일이 없으면 `IMG`, `IMG1`, `IMG2`를 직접 지정해야 합니다.

- `03_ingest.sh`가 저장한 `last_ingest.json`을  
  `04_search.sh`, `05_labels.sh`, `07_classification_smoke.sh`가 재사용할 수 있습니다.

- `04_search.sh`에서 직접 지정하려면 JSON 배열 문자열 사용:
  - `QUERY_INSTANCE_IDS_JSON='["ins_123","ins_456"]'`

- `05_labels.sh`는 유효한 `INSTANCE_ID`가 필요합니다.

- `08_e2e_registered_unlabeled.sh` 입력 데이터 구조:
  - `data/images_for_test/{DAYCARE_ID}/registered/{pet_id}/*`
  - `data/images_for_test/{DAYCARE_ID}/{DAY}/unlabeled/**`
  - 가독성 이름 지원: `registered/{pet_id}__{pet_name}/*`
    - 예: `registered/pet_001__뽀미/*`

- `10_reset_and_reseed_e2e.sh` 동작:
  - 기본: Qdrant points 삭제만 수행 (`HARD_COLLECTION_RESET=0`)
  - 컬렉션 삭제까지 수행: `HARD_COLLECTION_RESET=1`
  - 로컬 디렉터리 초기화:
    - `data/images`, `data/thumbs`, `data/meta`, `data/pets`, `data/buckets`, `data/trials`
  - 비대화식 실행:
    - `FORCE=1 bash 10_reset_and_reseed_e2e.sh`

## 자주 쓰는 실행 예시

```bash
# 단일 인입
IMG=/path/to/image.jpg DAYCARE_ID=dc_001 bash 03_ingest.sh

# 특정 인스턴스 라벨 반영
INSTANCE_ID=ins_xxx PET_ID=pet_pomi bash 05_labels.sh

# E2E (daycare/day 지정)
DAYCARE_ID=dc_001 DAY=2026-02-13 bash 08_e2e_registered_unlabeled.sh

# 초기화 + 재시드 + 검증
FORCE=1 DAYCARE_ID=dc_001 DAY=2026-02-13 bash 10_reset_and_reseed_e2e.sh
```
