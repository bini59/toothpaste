# 코드 지도

기능을 수정할 때 화면에서 HTTP API, 처리 모듈, 테스트 순서로 찾아볼 수 있다. 설치와 운영 흐름은 [README](../README.md)와 [사용 설명서](guide.md)에 있다.

## 진입점

- **CLI** — [bin/tp](../bin/tp)가 [cli.py](../ticket_pilot/cli.py)의 `main()`을 실행한다. 여기서 명령을 등록하며, `tp tick`은 동기화·투입, 리뷰, 배포 관련 사전 점검과 자동화의 실행 주기를 관리한다.
- **대시보드 서버** — `tp serve`와 Docker의 `tp run`은 [server.py](../ticket_pilot/server.py)의 `serve()`를 호출한다. HTTP 핸들러의 `do_GET()`과 `do_POST()`가 API를 처리하고, 서버는 빌드된 React 앱을 `web/dist`에서 제공한다.
- **대시보드 화면** — [index.html](../web/index.html)에서 [main.tsx](../web/src/main.tsx)로 진입한다. [App.tsx](../web/src/App.tsx)가 주소의 해시로 화면을 선택하고, [api.ts](../web/src/api.ts)와 [types.ts](../web/src/types.ts)가 API 호출과 응답 타입을 공유한다.
- **공용 저장소** — [core.py](../ticket_pilot/core.py)의 `db()`가 SQLite 연결과 스키마 초기화·마이그레이션을 담당한다.

## 티켓 동기화와 투입

- **화면·API** — [Board.tsx](../web/src/views/Board.tsx)와 [Tickets.tsx](../web/src/views/Tickets.tsx)는 `GET /api/state`에서 티켓과 단계를 읽는다. `POST /api/refresh`는 동기화와 작업 상태 스캔을 실행하며, `POST /api/assign`은 투입할 저장소를 지정한다.
- **처리** — `tp sync`와 투입 사전 점검이 [pipeline.py](../ticket_pilot/pipeline.py)의 `sync()`를 호출한다. Asana와 GitHub 이슈는 `_sync_asana()`와 `sync_issues()`에서, Notion 티켓은 [notion_tickets.py](../ticket_pilot/notion_tickets.py)에서 동기화한다. `compute_stage()`가 단계를 계산하고, `dispatch()`가 대상 티켓과 저장소를 골라 [runner.py](../ticket_pilot/runner.py)로 워크트리와 에이전트를 시작한다.
- **테스트** — [test_issues.py](../tests/test_issues.py), [test_notion.py](../tests/test_notion.py), [test_deps.py](../tests/test_deps.py)

## PR 검사와 리뷰

- **화면·API** — [Reviews.tsx](../web/src/views/Reviews.tsx)는 `GET /api/reviews`를 읽는다. 보드의 승인과 검사 예외 요청은 `POST /api/approve`, `POST /api/check-override`를 사용한다.
- **처리** — [cli.py](../ticket_pilot/cli.py)의 `cmd_precheck_review()`가 PR을 스캔하고 [checks.py](../ticket_pilot/checks.py)에서 검사 상태를 확인한다. [pipeline.py](../ticket_pilot/pipeline.py)의 `review_queue()`와 `review_save()`가 AI 리뷰 대상과 결과를 관리한다. 리뷰 에이전트는 [automation_review.md](../prompts/automation_review.md)를 따르며, 수정 요청 후 재작업은 `pipeline.rework()`에서 처리한다.
- **테스트** — [test_checks.py](../tests/test_checks.py), [test_rework_cap.py](../tests/test_rework_cap.py)

## 마일스톤과 배포

- **화면·API** — [Milestones.tsx](../web/src/views/Milestones.tsx)의 작업 완료는 `POST /api/milestone-done`을 사용한다. [Release.tsx](../web/src/views/Release.tsx)는 `GET /api/state`의 배포 상태를 표시하고 `POST /api/deploy`, `POST /api/deploy-cancel`, `POST /api/release-hold`로 요청을 보낸다.
- **처리** — [milestone.py](../ticket_pilot/milestone.py)와 [release.py](../ticket_pilot/release.py)의 `process_approvals()`가 마일스톤과 스테이징 병합을 처리한다. [deploy.py](../ticket_pilot/deploy.py)의 `deploy_tick()`은 대시보드 배포를, `release.release_tick()`은 정기 배포를 진행한다. `release.verify_deploy()`와 `release.finalize()`가 프로덕션 반영을 확인하고 티켓 완료를 처리한다. 별도로 병합된 PR의 프로덕션 상태는 [track.py](../ticket_pilot/track.py)가 추적한다.
- **공개 릴리스** — [test.yml](../.github/workflows/test.yml)의 전체 테스트는 같은 저장소의 `staging → main` PR에서 실행한다. `deployment-tests-passed`는 배포 PR의 전체 테스트 작업이 성공해야 통과한다. 다른 `main` 대상 PR에서는 이 작업을 건너뛰며 GitHub는 이를 필수 검사 충족으로 처리한다. 배포 화면과 열차는 `release._open_pr()`에서 같은 저장소의 배포 PR을 찾아 사용한다. [release_checks.py](../ticket_pilot/release_checks.py)에서 최신 테스트, 머지 후보의 부모와 `main` 보호 규칙을 확인한 뒤 검사한 `staging` SHA를 지정해 GitHub PR 머지 API를 호출하고, 반환된 머지의 대상과 파일 트리를 확인한다. [release.yml](../.github/workflows/release.yml)은 머지 이벤트를 받고 같은 모듈에서 머지 전 테스트 결과와 실제 `main` 커밋을 확인한 뒤 발행한다. [test_verify_release.py](../tests/test_verify_release.py)는 보호 규칙, PR과 브랜치 경합, 머지 결과와 발행 조건을 검증한다.
- **테스트** — [test_milestone.py](../tests/test_milestone.py), [test_release_candidates.py](../tests/test_release_candidates.py), [test_busy_release.py](../tests/test_busy_release.py), [test_track.py](../tests/test_track.py)

## 프로젝트와 저장소 설정

- **화면·API** — [Projects.tsx](../web/src/views/Projects.tsx)가 프로젝트별 탭을 구성한다. [Onboard.tsx](../web/src/views/Onboard.tsx)는 프로젝트·저장소 추가, [Settings.tsx](../web/src/views/Settings.tsx)는 프로젝트·저장소·전역 설정 편집, [NotionSource.tsx](../web/src/views/NotionSource.tsx)는 Notion 소스 설정을 담당한다. 관련 API는 `GET /api/projects`, `GET /api/config`, `POST /api/config`, `/api/onboard/*`, `/api/notion/*`이다.
- **처리** — [config.py](../ticket_pilot/config.py)가 `tp config`와 대시보드의 설정 검증, 저장, 변경 이력을 공유한다. [core.py](../ticket_pilot/core.py)가 DB에서 설정을 읽고 기본값과 합치며, [onboard.py](../ticket_pilot/onboard.py)가 저장소 탐색과 복제를 처리한다.
- **테스트** — [test_config.py](../tests/test_config.py), [test_onboard.py](../tests/test_onboard.py), [test_notion.py](../tests/test_notion.py), [settings.check.mjs](../web/src/views/settings.check.mjs)

## 프로젝트 채팅과 압축

- **화면·API** — [ProjectChat.tsx](../web/src/views/ProjectChat.tsx)가 `GET /api/chat/conversations`, `GET /api/chat/messages`로 대화와 메시지를 읽고 `POST /api/chat/new`, `POST /api/chat/send`로 대화를 시작하거나 이어간다. 모니터링의 [ChatCompactions.tsx](../web/src/views/ChatCompactions.tsx)는 `GET /api/chat/compactions`를 읽는다.
- **처리** — [chat.py](../ticket_pilot/chat.py)가 대화, 첨부, 에이전트 세션과 응답을 관리한다. `tp tick`에서 호출되는 `compact_idle()`은 압축 대상을 고르고, `_finalize_compaction()`은 요약을 다음 세션에 전달할 수 있게 저장한다. [chat_usage.py](../ticket_pilot/chat_usage.py)가 사용량을 집계하고, `chat.compaction_report()`가 압축 이력을 제공한다.
- **테스트** — [test_chat_attach.py](../tests/test_chat_attach.py)에서 첨부와 압축 후 세션 인계를, [test_chat_unread.py](../tests/test_chat_unread.py)에서 읽음 상태를, [test_chat_usage.py](../tests/test_chat_usage.py)에서 사용량을 확인한다.

## 지표

- **화면·API** — [Metrics.tsx](../web/src/views/Metrics.tsx)가 `GET /api/metrics`로 기간·기준점별 리포트를 읽고 `POST /api/metrics/mark`로 기준점을 기록한다.
- **처리** — [metrics_usage.py](../ticket_pilot/metrics_usage.py)가 작업·리뷰 실행을 기록하고 `tp tick`에서 에이전트 로그의 사용량을 수집한다. [metrics.py](../ticket_pilot/metrics.py)의 `report()`가 DB의 티켓, PR, 작업 기록을 기간별로 집계한다.
- **테스트** — [test_metrics.py](../tests/test_metrics.py)

## PR에서 코드 지도 갱신

코드 경로가 바뀌면 같은 PR에서 이 지도 파일을 추가하거나 내용을 수정한다. 경로, 호출 관계 또는 담당 범위가 바뀐 부분을 지도에 반영한다. 지도 삭제나 다른 경로로의 이동은 갱신으로 인정하지 않는다. 코드 변경이 없는 PR은 지도 변경 여부와 관계없이 통과한다.

[architecture-map.yml](../.github/workflows/architecture-map.yml)은 PR 이벤트의 대상 커밋과 PR HEAD에서 공통 조상을 찾아, 그 조상부터 PR HEAD까지 비교한다. 마일스톤 하위 PR도 해당 PR의 대상 커밋을 사용한다. 추가, 수정, 삭제를 모두 검사하고 이동은 이전 경로의 삭제와 새 경로의 추가로 검사한다. 실패 시 갱신할 문서와 변경된 코드 경로를 출력한다.

[검사 스크립트](../scripts/check_architecture_map.py)는 다음 경로를 코드로 판정한다. 디렉터리 규칙은 하위 디렉터리에도 적용된다.

| 경로 | 확장자 또는 파일 |
| --- | --- |
| `ticket_pilot/` | `.py`, `.js`, `.mjs`, `.cjs` |
| `web/` | `.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`, `.css`, `.html`, `.svg`, 그리고 `web/package.json`, `web/package-lock.json`, `web/tsconfig.json`, `web/themes/` 아래의 `.json` |
| `tests/` | `.py`, `.js`, `.mjs`, `.cjs`, `.ts`, `.sh` |
| 저장소 루트, `bin/`, `scripts/` | `bin/tp`, `.py`, `.sh`, `.bash`, `.js`, `.mjs`, `.cjs`, `.ts`, 그리고 Git에서 실행 권한이 있는 확장자 없는 파일 |
| `docker/`와 루트의 Docker 빌드 입력 | `docker/Dockerfile`, 루트의 `.dockerignore`, `docker/` 아래의 `.py`, `.sh`, `.yml`, `.yaml` |
| `launchd/` | `.sh`, `.plist` |
| `systemd/` | `.sh`, `.service`, `.timer` |
| `.github/workflows/` | `.yml`, `.yaml` |

로컬에서는 PR의 실제 대상 브랜치 또는 대상 커밋을 지정한다. 두 커밋과 공통 이력이 로컬 Git에 있어야 한다. 두 번째 인자를 생략하면 `HEAD`를 사용하며, 커밋에 들어간 변경을 검사한다.

```bash
python3 scripts/check_architecture_map.py origin/<대상-브랜치> HEAD
python3 -m unittest discover tests -p 'test_architecture_map.py'
```

첫 번째 명령의 `<대상-브랜치>`는 실제 이름으로 바꿔 실행한다. [test_architecture_map.py](../tests/test_architecture_map.py)는 임시 Git 저장소에서 PR별 검사 결과를 검증한다.
