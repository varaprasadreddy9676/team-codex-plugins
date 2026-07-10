---
name: bugzilla
description: Use the Bugzilla MCP tools to inspect bugs and create approved Bugzilla or OpenProject work items.
---

# Bugzilla

Use this skill for Bugzilla search, inspection, comments, updates, and the
Bugzilla-to-OpenProject workflow.

## Workflow

1. Check `bugzilla_server_info` before the first operation.
2. Use `bugzilla_list_products` or `bugzilla_search` to discover records.
3. Use `bugzilla_get` before proposing a write.
4. Present a preview before every mutation.
5. Call a write tool only after the user explicitly approves it with
   `confirm=true`.
6. For `bugzilla_to_openproject`, resolve the OpenProject project and type IDs
   with the OpenProject plugin before creating the work package.

Never place credentials in tool arguments or chat messages. Use the plugin-local
`.env` file or process environment instead.
