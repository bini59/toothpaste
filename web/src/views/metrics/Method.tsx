export function Method() {
  return (
    <div className="space-y-3 text-sm leading-relaxed text-text-muted">
      <p>티켓은 PR의 첫 프로덕션 확인 시각(<code>prs.prod_at</code>)에 한 번 셉니다.
        그 시각까지 BLOCKED로 제출한 <code>ws-set --status todo</code>(과거에는 수신함과 작업 상태),
        오너 답장·재개(<code>reply</code>, <code>unblock</code>), 리뷰·검사 override(<code>review_override</code>, <code>check_override</code>),
        수동 수정 요청(<code>review-save --verdict changes</code>)이 없으면 무개입으로 집계합니다.</p>
      <p>티켓당 토큰은 첫 프로덕션 확인까지 tp가 시작한 작업·수정·충돌 해결·리뷰 세션의 입력(캐시 포함)과 출력 토큰을 더한 값입니다.
        Claude 하위 에이전트는 남아 있는 transcript의 토큰과 도구 호출을 합산합니다. 읽을 수 있는 사용량이 있는 티켓으로 평균을 내고,
        실행 기록 범위를 함께 표시합니다. 여러 티켓을 처리한 리뷰는 PR별 시작 또는 리뷰 저장 시각으로 사용량을 배분합니다.</p>
      <p>첫 AI 리뷰 승인율은 그 주에 처음 AI 리뷰를 받은 PR 기준입니다.
        평균 수정 라운드는 그 주에 리뷰·검사 또는 수정 실행 기록이 있는 PR당 실제로 다시 보낸 라운드 수입니다.
        소요 시간은 머지 또는 프로덕션 확인이 일어난 주의 중앙값이며 PR 머지 대상별로 표시합니다.</p>
      <p>도구 호출과 반복 패턴은 읽을 수 있는 실행의 종료 주에 집계합니다.
        한 도구 호출에서 <code>tp screenshot --help</code> 또는 <code>tp guide</code>, dev 서버 직접 실행·재시작,
        <code>sleep</code>·<code>for … in $(seq …)</code>·<code>until</code>을 각각 최대 한 번 셉니다.
        오류는 기간 안의 같은 메시지를 하나로 묶고, 충돌 수정 실행도 집계합니다.</p>
      <p><code>ws-set</code> 누락은 직접 추적한 작업 실행과 과거 종료·시간 초과 이벤트에서 확인한 건수를 더합니다.
        확인할 수 있는 기록이 없는 기간은 비워 두며, 직접 추적한 실행 수를 전체 작업 실행 수와 함께 표시합니다.</p>
    </div>
  );
}
