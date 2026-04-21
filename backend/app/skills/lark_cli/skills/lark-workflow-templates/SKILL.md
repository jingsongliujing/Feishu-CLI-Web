---
name: lark-workflow-templates
version: 1.0.0
description: Fixed workflow templates for common Feishu CLI Web scenarios.
metadata:
  cliHelp: "Use scenario templates before planning ad-hoc commands when the request matches a stable workflow."
  requires:
    lark-cli: ">=0.0.0"
---

# Lark Workflow Templates

Use this skill when the user asks for a common Feishu workflow that should be handled as a repeatable process instead of a one-off command.

## Stable Workflows

- Send a group notice: find the target chat, preview the message, then send it only after write confirmation.
- Schedule a meeting: resolve attendees, inspect availability, create the calendar event, then optionally notify users or a group.
- Create a document: create the document first, then update or insert the requested content.
- Import a spreadsheet into Base: upload/import the local file, then return the created Base or Drive link.
- Summarize meeting minutes: search minutes, fetch the relevant result, then summarize conclusions and action items.

## Planning Rules

1. Prefer a previewable multi-step plan with `expected` set to `read` or `write`.
2. Mark every send, create, update, delete, upload, import, export, move, or schedule step as `write`.
3. If the workflow contains any `write` step, require confirmation before execution.
4. Keep generated commands concrete and compatible with the referenced domain skill docs.
5. When the user provides incomplete parameters, use the chat response to ask for the smallest missing set instead of guessing destructive targets.
