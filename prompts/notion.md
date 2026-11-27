- When the owner or the ticket asks to edit a Notion page, use this project's connected database through `{{TP}} notion`.
  Pass `--project {{PROJECT_ID}}` on every edit. Return the ticket key and Notion link from the result.
  `{{TP}} notion update --project {{PROJECT_ID}} <NT-key> --assignee me --due 2027-01-15`
  `{{TP}} notion status --project {{PROJECT_ID}} <NT-key>` lists the available values. Add the selected value to set it.
  `{{TP}} notion tag --project {{PROJECT_ID}} <NT-key> 긴급` adds that tag. Append `--remove` to remove it.
  `{{TP}} notion comment --project {{PROJECT_ID}} <NT-key> "수정 PR: <URL>"`
  The host reflects configured work stages automatically. tp manages the connection credentials.
