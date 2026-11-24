# Continue {{KEY}}: {{TITLE}}

You stopped on this ticket with a BLOCKED question. {{OWNER}} answered it on the ticket-pilot dashboard.
You are back in the same git worktree, on the same branch (`{{BRANCH}}`). Asana: {{URL}}
Original ticket brief: {{BRIEF_FILE}} (read it again; its rules still apply)

**Your question:** {{QUESTION}}

**{{OWNER}}'s answer:** {{REPLY}}

Continue the ticket with that answer, following the original brief's "How to work" steps through the PR and
`{{TP}} ws-set --status in-review`. If you are still blocked, or the answer means no change is needed, run
`{{TP}} ws-set --status todo --comment "BLOCKED: <why>"` and stop; {{OWNER}} can drop the ticket then.

## Never
- Never merge, never push to main/master/staging/production, never force-push.
- Never deploy, touch production data, or change secrets/infra.
{{NOTION}}
{{ASANA}}
- ticket-pilot 사용법(마일스톤, 단계, 배포 등)이 필요하면 `{{TP}} guide`로 주제를 보고 `{{TP}} guide <주제>`로 읽으세요.
