# OpenProject Codex Plugin

This plugin connects Codex to an OpenProject instance through a plugin-local MCP
server.

## What it can do

- inspect the current OpenProject user and connection state
- list projects and available work package types
- list priorities, statuses, and users
- search work packages globally or inside one project
- fetch a work package with recent activity
- create work packages
- update work packages
- add comments to work packages

## Configuration

The MCP server reads configuration from process environment variables and from
an optional plugin-local `.env` file next to this README.

Each user should create a local `.env` file in this plugin directory:

```bash
cp .env.example .env
```

Supported variables:

- `OPENPROJECT_BASE_URL`
- `OPENPROJECT_API_TOKEN`
- `OPENPROJECT_ACCESS_TOKEN`

`OPENPROJECT_BASE_URL` should be the root URL of your instance, for example:

```bash
OPENPROJECT_BASE_URL=https://pm.example.com
```

Do not append `/api/v3`; the server normalizes that automatically.

For authentication, the recommended path is an OpenProject API token passed as a
Bearer token via `OPENPROJECT_API_TOKEN`. OpenProject's API docs also document
Bearer tokens and OAuth2 access tokens for API v3.

Do not commit `.env` or real tokens.

## Notes

- The implementation focuses on OpenProject API v3 work package workflows.
- Type, priority, status, assignee, and responsible values may be passed as IDs
  or human-readable names in the MCP tools.
- The server reads a fresh `lockVersion` before updates so optimistic locking is
  handled automatically.
