# Merge conflict {{KEY}}: {{TITLE}}

Your PR {{PR_URL}} (#{{PR_NUMBER}}, head `{{HEAD_SHA}}`) can no longer be merged: GitHub reports a conflict with its
base branch `{{BASE}}` (other PRs were merged into `{{BASE}}` after you branched).
You are back in the same git worktree, on the same branch (`{{BRANCH}}`). Asana: {{URL}}

## How to work
1. Read the repo's AGENTS.md / CLAUDE.md again.
2. `git fetch origin {{BASE}}` and `git merge origin/{{BASE}}` into your branch (a merge commit, not a rebase).
3. Resolve every conflict so that **both** sides keep their intent: your ticket's change and whatever `{{BASE}}` changed
   in the same place (read those commits: `git log --oneline HEAD...origin/{{BASE}} -- <file>`). Never drop the other
   side's change just to make the conflict go away. Do not add unrelated changes.
4. Run the repo's typecheck / lint / tests. Fix what the merge broke.
5. Commit the merge (message starts with `[{{KEY}}] merge {{BASE}}: resolve conflicts`) and push to the same branch.
   Do not open a new PR, do not force-push, do not rebase.
6. Comment on the PR with which files conflicted and how you resolved each one:
   `gh pr comment {{PR_URL}} --body-file <file>`
7. Mark the ticket ready for review again (this triggers an AI re-review of the merged result), then stop:
   `{{TP}} ws-set --status in-review --comment "PR #{{PR_NUMBER}} conflicts with {{BASE}} resolved: <one line>"`

## If you get stuck
If the two changes really contradict each other (a product decision is needed), run `git merge --abort`, then:
`{{TP}} ws-set --status todo --comment "BLOCKED: conflict with {{BASE}}: <files + question for {{OWNER}}>"`
and stop.

## Never
- Never merge the PR, never push to main/master/staging/production, never force-push, never rebase.
- Never deploy, touch production data, or change secrets/infra.
{{NOTION}}
{{ASANA}}
- ticket-pilot 사용법(마일스톤, 단계, 배포 등)이 필요하면 `{{TP}} guide`로 주제를 보고 `{{TP}} guide <주제>`로 읽으세요.
