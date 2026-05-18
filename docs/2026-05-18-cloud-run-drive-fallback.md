# 2026-05-18 Cloud Run + Google Drive Fallback 기록

이 문서는 `stylisk/kidsnote-backup` 저장소를 **로컬 개발 + GitHub Actions 클라우드 실행** 구조로 정리하면서 반영한 변경사항을 기록한다.

## 운영 원칙

- 로컬 경로: `/Users/jeong/Dropbox/Repositories/kidsnote-backup`
- GitHub 저장소: `https://github.com/stylisk/kidsnote-backup`
- 로컬에서는 코드 수정, 검증, 커밋, 푸시만 수행한다.
- 실제 Kidsnote 백업 실행은 GitHub Actions의 `Kidsnote → Notion mirror` 워크플로에서 수행한다.
- 민감정보는 코드나 README에 저장하지 않고 GitHub Actions Secrets로만 전달한다.

## 현재 GitHub Actions 구조

워크플로 파일: `.github/workflows/kidsnote-to-notion.yml`

- 전역 env:
  - `OLLAMA_MODEL=gemma4:e4b`
  - `OLLAMA_CACHE_KEY=ollama-gemma4-e4b-v2`
- Python:
  - `actions/setup-python@v5`
  - Python `3.12`
  - `tools/kidsnote_fetch/requirements.txt` 설치
- Preflight guard:
  - 실행 커밋 SHA 출력
  - Ollama 모델명 출력
  - Ollama cache key 출력
  - requirements 내용 출력
  - `OLLAMA_MODEL`이 `gemma4:e4b`가 아니면 실패
  - `requirements.txt`에 `pillow` 또는 `piexif`가 들어가면 실패. 원본 보존 정책상 EXIF 제거/압축 의존성을 다시 넣지 않는다.
- Secrets 전달:
  - 필수: `KIDSNOTE_SESSION_COOKIE`, `NOTION_TOKEN`, `NOTION_DATABASE_ID`
  - 선택: `GOOGLE_SERVICE_ACCOUNT_JSON`, `GOOGLE_DRIVE_FOLDER_ID`

## Google Drive fallback 설계

구현 파일: `tools/kidsnote_fetch/notion_mirror.py`

목표:

- Notion 무료 플랜의 파일당 5MB 제한 때문에 큰 동영상, PDF, 엑셀 첨부가 누락되는 문제를 줄인다.
- Notion `file_uploads`가 실패해도 Google Drive에 같은 bytes/파일명으로 파일을 올리고 Notion 페이지에는 외부 링크를 남긴다.
- Drive fallback이 필요한데 설정이 없거나 업로드가 실패하면 조용히 skip하지 않고 해당 run을 실패시켜 다음 cron에서 재시도한다.

동작 조건:

- `GOOGLE_DRIVE_FOLDER_ID`와 `GOOGLE_SERVICE_ACCOUNT_JSON`이 모두 존재할 때 켜진다.
- 둘 중 하나라도 없으면 5MB 이하 파일은 Notion 업로드만 시도한다.
- fallback이 필요한 파일이 나타나면 실패 처리한다.

권한 구조:

- GitHub Actions가 사용자의 Google 계정 비밀번호를 사용하지 않는다.
- Google Cloud 서비스 계정 JSON을 GitHub Secret으로 전달한다.
- 서비스 계정은 사용자가 명시적으로 공유한 Drive 폴더에만 접근한다.
- Drive API scope는 `https://www.googleapis.com/auth/drive.file`을 사용한다.
- 업로드 성공 후 Notion에서 열람 가능하도록 해당 파일에 `anyone reader` 권한을 붙인다.
- 공개되는 것은 fallback으로 업로드된 개별 파일 링크이며, 사용자의 Drive 전체가 공개되는 것이 아니다.

업로드 흐름:

1. 사진:
   - Kidsnote `original` URL만 사용한다.
   - `original_file_name`, `file_name`, `filename`, `name`, URL basename 순으로 원본 파일명을 결정한다.
   - EXIF/GPS 제거, 리사이즈, JPEG 압축을 하지 않는다.
   - 5MB 초과이거나 Notion upload handle/create/upload가 실패하면 같은 bytes/파일명으로 Drive fallback을 시도한다.
2. 동영상/PDF/엑셀 등 일반 첨부:
   - 5MB 이하는 Notion `file_uploads`에 직접 업로드
   - 5MB 초과이거나 Notion 업로드 실패 시 같은 bytes/파일명으로 Drive fallback 시도
3. Notion 블록 생성:
   - Notion 업로드 성공 시 `file_upload` 블록 사용
   - Drive fallback 성공 시 `external` 링크 블록 사용
   - Drive fallback 동영상은 Notion video block 대신 file block으로 링크를 남긴다

## Python 의존성

파일: `tools/kidsnote_fetch/requirements.txt`

현재 requirements:

```text
requests>=2.31
browser-cookie3>=0.20
kiwipiepy>=0.17
google-api-python-client>=2.120
google-auth>=2.28
```

`Pillow`와 `piexif`는 requirements에 넣지 않는다. workflow의 preflight guard가 이 상태를 확인하며, workflow에서도 별도 설치하지 않는다.

## README 반영 내용

파일: `README.md`

반영한 내용:

- 첫 화면에 `현재 버전 요약 (2026-05-18)` 추가
- 로컬 개발 + GitHub Actions 클라우드 실행 구조 명시
- `gemma4:e4b` 모델 고정 사실 명시
- 알림장 제목은 `[YYYY-MM-DD] 알림장: 작성자 Gemma4 한줄요약` 형식으로 생성한다고 명시
- 알림장 본문 안의 LLM callout은 생성하지 않고, 키즈노트 입력 날씨 callout만 최상단에 둔다고 명시
- 원본 파일 bytes/파일명/EXIF/GPS metadata 보존 정책 명시
- 필수 Secrets와 선택 Secrets 구분
- Google Drive fallback의 권한 구조, 설정 순서, 작동 방식 추가
- Drive fallback 사용 시 개별 fallback 파일만 링크 공개된다는 점 명시
- 문제 해결 표에 Google Drive fallback 관련 에러 추가
- 기술 스택을 현재 의존성 기준으로 업데이트

## 사용자가 GitHub에서 실행하기 전 확인할 것

필수 Secrets:

- `KIDSNOTE_SESSION_COOKIE`
- `NOTION_TOKEN`
- `NOTION_DATABASE_ID`

Drive fallback 선택 Secrets:

- `GOOGLE_DRIVE_FOLDER_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON`

Google Drive 쪽 확인:

- fallback용 Drive 폴더가 존재한다.
- 폴더 ID가 `GOOGLE_DRIVE_FOLDER_ID`와 일치한다.
- 서비스 계정 JSON의 `client_email`이 Drive 폴더에 편집자로 공유되어 있다.
- JSON 키 파일 자체는 repo에 커밋하지 않았다.

첫 실행 권장값:

- `limit=3`
- `monthly_sample=false`
- `force_refresh=false`

`limit=3` 테스트가 정상 완료된 뒤 전체 백업을 실행할 때 `limit`을 비운다.

## 검증 기록

로컬에서 수행한 검증:

```bash
python3 -m py_compile tools/kidsnote_fetch/notion_mirror.py tools/kidsnote_fetch/fetch.py
python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check
ruby -e 'require "yaml"; data=YAML.load_file(".github/workflows/kidsnote-to-notion.yml"); abort "bad model" unless data["env"]["OLLAMA_MODEL"] == "gemma4:e4b"; puts "workflow yaml ok"'
grep -Eiq '^[[:space:]]*(pillow|piexif)([[:space:]<>=!~]|$)' tools/kidsnote_fetch/requirements.txt; rc=$?; if [ "$rc" -eq 0 ]; then echo 'guard would fail'; exit 1; else echo 'requirements guard ok'; fi
```

이전 커밋:

- `fc7718d Add Drive fallback and Gemma4 workflow guard`

이 문서와 README 업데이트는 위 변경 이후, 실행 전 사용자 안내와 기록을 보강하기 위해 추가했다.
