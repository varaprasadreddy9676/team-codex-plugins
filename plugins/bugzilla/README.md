# Bugzilla Codex Plugin

This plugin connects Codex and other MCP clients to Bugzilla. It selects the
native REST API when available and falls back to Bugzilla JSON-RPC for older
installations such as Bugzilla 5.0.2, where `/rest` may not be enabled.

## Capabilities

- detect the available Bugzilla transport and server version
- list accessible products
- search bugs using native Bugzilla criteria
- inspect a bug
- preview or create bugs
- preview or add public/private comments
- preview or update bug fields
- preview or create an OpenProject work package from a Bugzilla bug

All write operations require `confirm=true`. Calls without confirmation return
a preview and never modify either system.

## Configuration

Create a local file and keep it uncommitted:

```bash
cp plugins/bugzilla/.env.example plugins/bugzilla/.env
```

Set `BUGZILLA_BASE_URL` and either `BUGZILLA_API_TOKEN` or the username/password
pair. The plugin reads `.env` and `.env.local` from this directory, then uses
process environment variables without overwriting them.

`BUGZILLA_TRANSPORT=auto` probes `/rest/version` first. If that endpoint is not
available, the plugin uses `/jsonrpc.cgi`. Password authentication uses
`User.login` and retains only the returned session token in memory.

For the OpenProject bridge, also set `OPENPROJECT_BASE_URL` and
`OPENPROJECT_API_TOKEN`. The bridge creates a work package only after a preview
has been reviewed and the tool is called with `confirm=true`.

Do not commit `.env`, API keys, passwords, or session tokens.

## Local verification

```bash
python3 -m unittest discover -s plugins/bugzilla/tests -v
```

The MCP server uses stdio and is declared in `plugins/bugzilla/.mcp.json`, so it
can be used by Codex and other MCP-compatible clients that support stdio.
