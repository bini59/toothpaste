# Continue {{KEY}}: {{TITLE}}

You stopped on this ticket with a BLOCKED question. {{OWNER}} answered it on the ticket-pilot dashboard.
You are back in the same git worktree, on the same branch (`{{BRANCH}}`). The source ticket is {{URL}}.
Original ticket brief: {{BRIEF_FILE}} (read it again; its rules still apply)

**Your question:** {{QUESTION}}

**{{OWNER}}'s answer:** {{REPLY}}

## How to work

Continue the ticket with that answer, following the original brief's "How to work" steps through the PR and
`{{TP}} ws-set --status in-review`.

- Use the Edit tool or check the target before a scripted replacement (`assert old in s` or assert the expected match count).
- Wait for a completion notification or use Monitor for background tasks. Avoid polling their output files with `cat`.
- For browser screenshots, try this command before writing a Playwright script. Set `--url` to the running page and
  `--repo` to the worktree directory containing Playwright.
  `{{TP}} screenshot --repo "$PWD" --url http://127.0.0.1:5173 --out "/tmp/{{KEY}}/page.png"`
  Optional flags include `--device "iPhone 12"`, `--wait-ms 1000`, and `--js "window.scrollTo(0, 500)"`.

If you are still blocked, or the answer means no change is needed, run
`{{TP}} ws-set --status todo --comment "BLOCKED: <why>"` and stop; {{OWNER}} can drop the ticket then.

## Never
- Never merge, never push to main/master/staging/production, never force-push.
- Never deploy, touch production data, or change secrets/infra.
- If asked to file a ticket, use `{{TP}} ticket create --project {{PROJECT_ID}} --title "<title>"`.
{{NOTION}}
{{ASANA}}
- ticket-pilot 사용법(마일스톤, 단계, 배포 등)이 필요하면 `{{TP}} guide`로 주제를 보고 `{{TP}} guide <주제>`로 읽으세요.
