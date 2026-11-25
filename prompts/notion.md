- When the owner or the ticket asks for a Notion change, use this project's connected database through `{{TP}} notion`.
  Pass `--project {{PROJECT_ID}}` on every command. Return the ticket key and Notion link from the result.
  `{{TP}} notion create --project {{PROJECT_ID}} --title "로그인 오류" --body "재현 순서" --tag 버그`
  For a subtask, pass the parent's NT-key if known, or its title as supplied by the owner:
  `{{TP}} notion create --project {{PROJECT_ID}} --parent "로그인 오류" --title "재현 테스트"`.
  If the command lists multiple parent candidates, ask the owner which key to use.
  `{{TP}} notion update --project {{PROJECT_ID}} <NT-key> --assignee me --due 2027-01-15`
  `{{TP}} notion status --project {{PROJECT_ID}} <NT-key>` lists the available values. Add the selected value to set it.
  `{{TP}} notion tag --project {{PROJECT_ID}} <NT-key> 긴급` adds that tag. Append `--remove` to remove it.
  `{{TP}} notion comment --project {{PROJECT_ID}} <NT-key> "수정 PR: <URL>"`
  The host reflects configured work stages automatically. tp manages the connection credentials.
