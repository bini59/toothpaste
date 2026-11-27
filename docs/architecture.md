# 코드 지도

기능을 수정할 때 화면에서 HTTP API, 처리 모듈, 테스트 순서로 찾아볼 수 있다. 설치와 운영 흐름은 [README](../README.md)와 [사용 설명서](guide.md)에 있다.

## 진입점

- **CLI** — [bin/tp](../bin/tp)와 `python3 -m ticket_pilot.cli`는 [cli.py](../ticket_pilot/cli.py)의 `main()`으로 들어간다. 여기서 argparse 명령을 등록하고 인자를 넘기며, `tp precheck-*`의 결과를 종료 코드와 출력으로 변환한다.
- **대시보드 서버** — `tp serve`와 Docker의 `tp run`은 [server.py](../ticket_pilot/server.py)의 `serve()`를 호출한다. `tp run`은 [tick.py](../ticket_pilot/tick.py)의 `run_loop()`로 매분 tick 프로세스를 시작한다. 서버는 세션·Origin 검사, 요청 파싱, 공통 오류 응답과 요청 actor 정리를 처리하고 빌드된 React 앱을 `web/dist`에서 제공한다.
- **대시보드 HTTP API** — [routes.py](../ticket_pilot/http_api/routes.py)에서 메서드별 URL, 쿼리 매칭, 공개·관리자 경로를 확인할 수 있다. 경로 표가 [채팅·첨부](../ticket_pilot/http_api/chat.py), [프로젝트·설정·Notion](../ticket_pilot/http_api/settings.py), [로그인·계정 연결](../ticket_pilot/http_api/connections.py), [티켓·리뷰·배포](../ticket_pilot/http_api/tickets.py), [운영·모니터링](../ticket_pilot/http_api/operations.py)의 핸들러를 지정한다. [test_http_api.py](../tests/test_http_api.py)는 임시 서버에 요청해 기능별 GET·POST, 다운로드, 정적 파일과 actor 정리를 검증한다. 로그인·권한은 [test_lan.py](../tests/test_lan.py), [test_docker.py](../tests/test_docker.py), OAuth 콜백은 [test_notion.py](../tests/test_notion.py)에서도 검증한다.
- **대시보드 화면** — [index.html](../web/index.html)에서 [main.tsx](../web/src/main.tsx)로 진입해 로그인과 Notion 콜백을 처리한다. [App.tsx](../web/src/App.tsx)는 [탐색 훅](../web/src/app/useNavigation.ts)이 해석한 탭과 하위 경로로 화면을 렌더링하고 [공통 데이터 훅](../web/src/app/useDashboardData.ts)의 응답을 각 화면에 전달한다. [api.ts](../web/src/api.ts)와 [types.ts](../web/src/types.ts)가 API 호출과 응답 타입을 공유한다.
- **공용 저장소** — [core.py](../ticket_pilot/core.py)의 `db()`가 SQLite 연결과 스키마 초기화·마이그레이션을 담당한다.

## 자동화 tick과 사전 점검

- **실행** — launchd와 systemd의 `tp tick`, Docker의 tick 프로세스는 [tick.py](../ticket_pilot/tick.py)의 `run_tick()`으로 들어간다. 여기서 `data/tick.lock`과 Docker 유지보수 상태를 확인하고, `precheck-*` 프로세스 실행과 AI 러너 호출, `automations`와 `automation_runs` 메타데이터 저장을 담당한다.
- **스케줄과 상태** — [automation.py](../ticket_pilot/automation.py)의 `advance()`가 시계, 러너 상태, 사전 점검 종료 코드를 받아 자동화별 실행 주기와 타임아웃을 적용하고, 대시보드 스냅샷과 실행 이력을 계산한다.
- **사전 점검** — [precheck.py](../ticket_pilot/precheck.py)가 투입, 리뷰, 스테이징 차이, 워크트리 정리의 사전 점검을 실행한다. 투입과 리뷰 점검은 마일스톤, 승인, 대시보드 배포, 정기 배포를 순서대로 진행한다.
- **테스트** — [test_tick.py](../tests/test_tick.py), [test_agent_roles.py](../tests/test_agent_roles.py), [test_docker.py](../tests/test_docker.py)

## 티켓 동기화와 투입

- **화면·API** — [Board.tsx](../web/src/views/Board.tsx)와 [Tickets.tsx](../web/src/views/Tickets.tsx)는 `GET /api/state`에서 티켓과 단계를 읽는다. `POST /api/refresh`는 동기화와 작업 상태 스캔을 실행하며, `POST /api/assign`은 투입할 저장소를 지정한다.
- **처리** — `tp sync`와 투입 사전 점검이 [pipeline.py](../ticket_pilot/pipeline.py)의 `sync()`를 호출한다. Asana와 GitHub 이슈는 `_sync_asana()`와 `sync_issues()`에서, Notion 티켓은 [notion_tickets.py](../ticket_pilot/notion_tickets.py)에서 동기화한다. `compute_stage()`가 단계를 계산하고, `dispatch()`가 대상 티켓과 저장소를 골라 [runner.py](../ticket_pilot/runner.py)로 워크트리와 에이전트를 시작한다.
- **테스트** — [test_issues.py](../tests/test_issues.py), [test_notion.py](../tests/test_notion.py), [test_deps.py](../tests/test_deps.py)

## PR 검사와 리뷰

- **화면·API** — [Reviews.tsx](../web/src/views/Reviews.tsx)는 `GET /api/reviews`를 읽는다. 보드의 승인과 검사 예외 요청은 `POST /api/approve`, `POST /api/check-override`를 사용한다.
- **추적** — `tp scan`, `tp ws-set`, 리뷰 사전 점검과 대시보드 새로고침은 [pipeline.py](../ticket_pilot/pipeline.py)를 거쳐 [pr_tracking.py](../ticket_pilot/pr_tracking.py)에서 워크트리, 에이전트 종료, GitHub PR 상태를 읽고 기록한다. 추적 모듈은 [milestone.py](../ticket_pilot/milestone.py)의 규칙에 따라 열린 PR의 base를 재지정하고, 작업 중 머지와 base 브랜치 직접 push를 감시한다.
- **검사·리뷰** — [precheck.py](../ticket_pilot/precheck.py)의 `review()`는 스캔 후 [checks.py](../ticket_pilot/checks.py)에서 PR HEAD의 검사 상태를 확인한다. [review.py](../ticket_pilot/review.py)의 `review_queue()`와 `review_save()`가 HEAD별 리뷰 대상과 결과를 관리한다. 리뷰 에이전트는 [automation_review.md](../prompts/automation_review.md)를 따르며, `dispatch()`는 재작업과 충돌·답변 후 재시도를 리뷰 모듈에 위임한다. 티켓 단계 갱신은 `pipeline.restage()`가 수행한다.
- **테스트** — [test_pr_tracking.py](../tests/test_pr_tracking.py), [test_checks.py](../tests/test_checks.py), [test_rework_cap.py](../tests/test_rework_cap.py), [test_milestone.py](../tests/test_milestone.py)

## 마일스톤과 배포

- **화면·API** — [Milestones.tsx](../web/src/views/Milestones.tsx)의 작업 완료는 `POST /api/milestone-done`을 사용한다. [Release.tsx](../web/src/views/Release.tsx)는 `GET /api/state`의 배포 상태를 표시하고 `POST /api/deploy`, `POST /api/deploy-cancel`, `POST /api/release-hold`로 요청을 보낸다.
- **처리** — [milestone.py](../ticket_pilot/milestone.py)와 [release.py](../ticket_pilot/release.py)의 `process_approvals()`가 마일스톤과 스테이징 병합을 처리한다. [deploy.py](../ticket_pilot/deploy.py)의 `deploy_tick()`은 대시보드 배포를, `release.release_tick()`은 정기 배포를 진행한다. `release.verify_deploy()`와 `release.finalize()`가 프로덕션 반영을 확인하고 티켓 완료를 처리한다. 별도로 병합된 PR의 프로덕션 상태는 [track.py](../ticket_pilot/track.py)가 추적한다.
- **공개 릴리스** — [test.yml](../.github/workflows/test.yml)의 전체 테스트는 같은 저장소의 `staging → main` PR에서 실행한다. `deployment-tests-passed`는 배포 PR의 전체 테스트 작업이 성공해야 통과한다. 다른 `main` 대상 PR에서는 이 작업을 건너뛰며 GitHub는 이를 필수 검사 충족으로 처리한다. 배포 화면과 열차는 `release._open_pr()`에서 같은 저장소의 배포 PR을 찾는다. [release_checks.py](../ticket_pilot/release_checks.py)는 최신 테스트, 머지 후보의 부모와 `staging` 파일 트리, `main` 보호 규칙을 확인한다. 호스트는 GitHub PR 머지 API에 검사한 `staging` SHA를 지정하고, GitHub는 그 SHA가 최신 `main`을 포함하도록 필수 검사로 강제한다. 호스트는 반환된 머지의 대상과 파일 트리를 확인한다. [release.yml](../.github/workflows/release.yml)은 머지 이벤트를 받고 같은 모듈에서 머지 전 테스트 결과와 실제 `main` 커밋을 확인한 뒤 발행한다. [test_verify_release.py](../tests/test_verify_release.py)는 보호 규칙, PR과 브랜치 경합, 머지 결과와 발행 조건을 검증한다.
- **테스트** — [test_milestone.py](../tests/test_milestone.py), [test_release_candidates.py](../tests/test_release_candidates.py), [test_busy_release.py](../tests/test_busy_release.py), [test_track.py](../tests/test_track.py)

## 프로젝트와 저장소 설정

- **화면·API** — [Projects.tsx](../web/src/views/Projects.tsx)가 프로젝트별 탭을 구성한다. [Onboard.tsx](../web/src/views/Onboard.tsx)는 프로젝트·저장소 추가, [Settings.tsx](../web/src/views/Settings.tsx)는 프로젝트·저장소·전역 설정 편집, [NotionSource.tsx](../web/src/views/NotionSource.tsx)는 Notion 소스 설정을 담당한다. 관련 API는 `GET /api/projects`, `GET /api/config`, `POST /api/config`, `/api/onboard/*`, `/api/notion/*`이다.
- **처리** — [config.py](../ticket_pilot/config.py)가 `tp config`와 대시보드의 설정 검증, 저장, 변경 이력을 공유한다. [core.py](../ticket_pilot/core.py)가 DB에서 설정을 읽고 기본값과 합치며, [onboard.py](../ticket_pilot/onboard.py)가 저장소 탐색과 복제를 처리한다.
- **테스트** — [test_config.py](../tests/test_config.py), [test_onboard.py](../tests/test_onboard.py), [test_notion.py](../tests/test_notion.py), [settings.check.mjs](../web/src/views/settings.check.mjs)

## 프로젝트 채팅과 압축

- **화면·API** — [ProjectChat.tsx](../web/src/views/ProjectChat.tsx)가 해시에서 선택한 대화에 맞춰 [ConversationList.tsx](../web/src/views/chat/ConversationList.tsx)와 [ChatThread.tsx](../web/src/views/chat/ChatThread.tsx)를 배치한다. [Composer.tsx](../web/src/views/chat/Composer.tsx)는 입력·첨부·미리보기를, [Messages.tsx](../web/src/views/chat/Messages.tsx)는 메시지 렌더링과 스크롤을 담당한다. 채팅·도움말·배포 화면은 [markdown.tsx](../web/src/components/markdown.tsx)의 렌더링 규칙을 공유한다. 모니터링 → 지표의 채팅 압축 탭은 [ChatCompactions.tsx](../web/src/views/ChatCompactions.tsx)에서 `GET /api/chat/compactions`를 읽는다.
- **채팅 상태** — [useChat.ts](../web/src/views/chat/useChat.ts)가 대화 목록과 메시지를 [pollChat.ts](../web/src/views/chat/pollChat.ts)로 갱신하고, 화면에 표시된 대화의 읽음 처리와 전송 상태를 관리한다. [api.ts](../web/src/views/chat/api.ts)와 [types.ts](../web/src/views/chat/types.ts)에 `/api/chat/conversations`, `/api/chat/messages`, `/api/chat/read`, 새 대화와 전송 등 프로젝트 채팅의 요청과 응답을 모아 둔다.
- **처리** — [chat.py](../ticket_pilot/chat.py)가 대화, 첨부, 에이전트 세션과 응답을 관리한다. `tp tick`에서 호출되는 `compact_idle()`은 압축 대상을 고르고, `_finalize_compaction()`은 요약을 다음 세션에 전달할 수 있게 저장한다. [chat_usage.py](../ticket_pilot/chat_usage.py)가 사용량을 집계하고, `chat.compaction_report()`가 압축 이력을 제공한다.
- **테스트** — [test_chat_attach.py](../tests/test_chat_attach.py)에서 첨부와 압축 후 세션 인계를, [test_chat_unread.py](../tests/test_chat_unread.py)에서 읽음 상태를, [test_chat_usage.py](../tests/test_chat_usage.py)에서 사용량을, [pollChat.check.mjs](../web/src/views/chat/pollChat.check.mjs)에서 폴링 주기와 요청·타이머 정리를 확인한다.

## 지표

- **화면·API** — 모니터링 목록의 지표 링크(`#metrics`)를 열면 [App.tsx](../web/src/App.tsx)가 [Metrics.tsx](../web/src/views/Metrics.tsx)를 불러온다. 채팅 압축 검색 항목(`#metrics/chat-compactions`)과 기존 `#chat-compactions` 주소는 지표의 채팅 압축 탭을 연다. 개요·주간 추이·시점 비교는 `GET /api/metrics`로 리포트를 읽고 `POST /api/metrics/mark`로 시점을 기록한다.
- **처리** — [metrics_usage.py](../ticket_pilot/metrics_usage.py)가 작업·리뷰 실행을 기록하고 `tp tick`에서 에이전트 로그의 사용량을 수집한다. [metrics.py](../ticket_pilot/metrics.py)의 `report()`가 DB의 티켓, PR, 작업 기록을 기간별로 집계한다. [compactionData.ts](../web/src/views/metrics/compactionData.ts)는 압축 기록을 UTC 기준 일·주·월별로 묶는다. [MetricChart.tsx](../web/src/views/metrics/MetricChart.tsx)는 차트 크기, 텍스트 값, 주간 차트의 시점선과 이름을 관리한다. [uPlot의 로케일 수정 커밋](https://github.com/leeoniya/uPlot/commit/8eafc9531b26596a2389cff331ad9cbfc5223f44)을 호출 시점에 불러오고, 로드나 초기화에 실패하면 차트 위치에 안내를 표시하면서 텍스트 값과 화면의 집계·기록은 제공한다. [Metrics.tsx](../web/src/views/Metrics.tsx)가 차트와 선택 상자의 시점을 비교 탭에 전달한다.
- **테스트** — [test_metrics.py](../tests/test_metrics.py), [compactionData.check.mjs](../web/src/views/metrics/compactionData.check.mjs)

## 대시보드 탐색과 모니터링

- [navigation.ts](../web/src/app/navigation.ts)가 탭·검색·모니터링 목록과 프로젝트 하위 탭 주소를 정의한다. 기존 채팅 압축, 저장소 이름 기반 프로젝트, 설정 이력 링크도 여기서 해석한다. [useNavigation.ts](../web/src/app/useNavigation.ts)는 해시 변경과 스크롤 위치, `POST /api/view`, Cmd/Ctrl+K를 처리한다. [Sidebar.tsx](../web/src/ui/Sidebar.tsx)는 데스크톱 메뉴와 모바일 메뉴에 모니터링 링크를 표시한다.
- [useDashboardData.ts](../web/src/app/useDashboardData.ts)는 `GET /api/state`를 60초마다(작업실은 15초) 읽고 `POST /api/refresh` 뒤 다시 조회한다. `GET /api/projects`와 읽지 않은 브라우저 알림은 15초마다 확인하고, [UsageCard.tsx](../web/src/components/UsageCard.tsx)의 사용량은 60초마다 또는 창에 포커스가 돌아올 때 갱신한다. [usePolling.ts](../web/src/app/usePolling.ts)는 조회에 30초 제한을 두고 갱신 타이머와 진행 중인 요청을 정리한다.
- `#monitoring`의 [Monitoring.tsx](../web/src/views/Monitoring.tsx)에서 각 상세 화면의 기존 해시 주소로 이동한다. [useMonitoringData.ts](../web/src/views/useMonitoringData.ts)는 자동화 로그와 리뷰 화면에서 사용하는 `GET /api/events`와 `GET /api/reviews` 응답을 개별적으로 불러온다.
- `npm run check`는 [monitoring.check.mjs](../web/src/monitoring.check.mjs)의 탐색 검사와 [usage-card.check.mjs](../web/src/components/usage-card.check.mjs)의 한도 초기화 시각 검사를 실행한다.

## 호스트 서비스 설치와 업데이트

- **호출 경로** — `tp setup`의 [setup.py](../ticket_pilot/setup.py), `tp update`의 [update.py](../ticket_pilot/update.py), `POST /api/lan/share`의 [connections.py](../ticket_pilot/http_api/connections.py), `tp mode host`의 [mode.py](../ticket_pilot/mode.py), `tp migrate`의 [migrate.py](../ticket_pilot/migrate.py)가 [launchd/install.sh](../launchd/install.sh)를 실행한다. Linux에서는 이 스크립트가 [systemd/install.sh](../systemd/install.sh)로 넘긴다.
- **처리** — 각 설치 스크립트가 Python과 서비스 경로를 선택하고 사용자 서비스를 등록·재시작한다. 두 스크립트가 읽는 [service-install.sh](../scripts/service-install.sh)는 [core.py](../ticket_pilot/core.py)의 `settings()`로 `dashboard_port`와 `lan_share`를 읽고, 각 플랫폼의 템플릿을 렌더링하고, 루프백 주소의 `/api/state` 응답을 기다린다.
- **운영** — 공개 저장소의 [release.yml](../.github/workflows/release.yml)이 GitHub Release와 웹 자산, Docker 이미지를 발행한다. 호스트는 `tp update`로 릴리스를 적용한다. 별도의 레거시 설치 저장소에서 쓰는 `deploy.yml`은 호스트의 `tp update --to <sha>`를 호출한다. Docker에서는 [compose.yaml](../docker/compose.yaml)의 `tp run`이 `tp-data` 볼륨의 DB를 쓰고, 호스트의 [docker_update_manager.py](../ticket_pilot/docker_update_manager.py)가 배포 이미지를 교체한다.
- **테스트** — [test_install.py](../tests/test_install.py), [test_setup.py](../tests/test_setup.py), [test_update.py](../tests/test_update.py), [test_mode.py](../tests/test_mode.py), [test_lan.py](../tests/test_lan.py)

## PR에서 코드 지도 갱신

코드 경로가 바뀌면 같은 PR에서 이 지도 파일을 추가하거나 내용을 수정한다. 경로, 호출 관계 또는 담당 범위가 바뀐 부분을 지도에 반영한다. 지도 삭제나 다른 경로로의 이동은 갱신으로 인정하지 않는다. 코드 변경이 없는 PR은 지도 변경 여부와 관계없이 통과한다.

[architecture-map.yml](../.github/workflows/architecture-map.yml)은 PR을 열거나 다시 열 때, 커밋을 올릴 때, 또는 대상을 바꿀 때 이벤트의 대상 커밋과 PR HEAD의 공통 조상부터 PR HEAD까지 비교한다. 마일스톤 하위 PR도 해당 PR의 대상 커밋을 사용한다. 제목이나 본문 편집은 진행 중인 검사와 기존 검사 결과에 영향을 주지 않는다. 추가, 수정, 삭제를 모두 검사하고 이동은 이전 경로의 삭제와 새 경로의 추가로 검사한다. 실패 시 갱신할 문서와 변경된 코드 경로를 출력한다.

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
