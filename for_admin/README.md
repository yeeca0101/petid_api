# Admin Dashboard (`for_admin`)

초기등록 이미지(Exemplar/Seed) 관리를 위한 관리자용 웹 GUI입니다.

## 포함 기능
- Exemplar 조회/검색: `GET /v1/exemplars`
- Exemplar 등록: `POST /v1/exemplars`
- 빠른 등록(이름+이미지): `POST /v1/exemplars/upload`
- 폴더 일괄 등록(폴더명=pet_name): `POST /v1/exemplars/upload-folder`
- Exemplar 수정: `PATCH /v1/exemplars/{instance_id}`
- Exemplar 삭제(해제): `DELETE /v1/exemplars/{instance_id}`
- 이미지/인스턴스 탐색 보조:
  - `GET /v1/images`
  - `GET /v1/images/{image_id}/meta`
- Daily bucket 확정 + ZIP 다운로드:
  - `POST /v1/buckets/finalize`
  - `GET /v1/buckets/{daycare_id}/{day}/zip`

## 접속
서버 실행 후 브라우저에서:
- `http://<host>:<port>/admin`

`app/main.py`에서 `for_admin` 디렉터리를 `/admin`으로 static mount 합니다.

## 참고
- 기본 API Base는 `${window.location.origin}/v1` 로 자동 설정됩니다.
- CORS를 피하려면 API와 같은 origin에서 `/admin`을 열어 사용하세요.

## 폴더 업로드 규칙
- 예시:
  - `업로드루트/초코/초코_0.jpg`
  - `업로드루트/엘/0.png`
- 서버는 상대경로에서 pet 폴더명을 읽어 `pet_name`/`pet_id`로 사용합니다.
- quick 모드와 동일하게 `pet_name` 고유성 가정을 사용합니다.

## Daily Bucket ZIP
- 좌측 `Daily Ops` 패널에서 `버킷 확정` 후 `ZIP 다운로드`를 사용할 수 있습니다.
- ZIP은 기본적으로 `{root_folder_name}/{pet_name}/{daily_images}` 구조로 내려갑니다.
- 버킷 확정 전 `ZIP 다운로드`를 누르면 먼저 버킷 확정을 하라는 안내가 표시됩니다.


## Download Guidance

- Exemplar ZIP: pet 이름별 폴더로 정리된 query/reference 이미지셋입니다. 단일 개체가 선명하게 나온 이미지를 권장합니다.
- Daily ZIP: 날짜 폴더 아래 원본 이미지와 paired `_anno.json`을 함께 제공합니다.
- Daily annotation에는 이미지 내 각 개체의 `name`, `pet_id`, `bbox`, `assignment_status`가 포함됩니다.
