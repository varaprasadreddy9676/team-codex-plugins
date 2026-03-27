# Team Codex Plugins

This repository distributes Codex plugins for internal use.

## Included plugins

- `openproject`: OpenProject MCP-backed plugin for projects and work packages

## Install in Codex

1. Clone this repository.
2. Keep the marketplace file at `.agents/plugins/marketplace.json`.
3. Restart Codex.
4. Enable the `OpenProject` plugin from the plugin UI if it is not already enabled.

Codex reads repo marketplaces from `.agents/plugins/marketplace.json` and loads
plugins from the relative `source.path` entries in that file.

## OpenProject setup

Each user must create their own local environment file:

`plugins/openproject/.env`

with:

```bash
OPENPROJECT_BASE_URL=https://your-openproject-host
OPENPROJECT_API_TOKEN=your-openproject-api-token
```

Do not commit `.env` files or real tokens.

To get an API token in OpenProject:

1. Sign in.
2. Open `Account settings`.
3. Go to `Access tokens`.
4. Create a new `API token`.

You can also start from [plugins/openproject/.env.example](/Users/sai/Documents/GitHub/team-codex-plugins/plugins/openproject/.env.example).

