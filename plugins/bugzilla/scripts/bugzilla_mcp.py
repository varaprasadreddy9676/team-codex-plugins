#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any
from urllib import error, parse, request


SERVER_NAME = "bugzilla"
SERVER_VERSION = "0.1.0"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


for dotenv_path in (PLUGIN_ROOT / ".env", PLUGIN_ROOT / ".env.local"):
    load_dotenv(dotenv_path)


class BugzillaError(Exception):
    pass


class ConfigError(BugzillaError):
    pass


class BugzillaConfig:
    def __init__(
        self,
        base_url: str,
        *,
        auth_mode: str = "auto",
        api_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        transport: str = "auto",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_mode = auth_mode
        self.api_token = api_token
        self.username = username
        self.password = password
        self.transport = transport
        if not self.base_url.startswith(("http://", "https://")):
            raise ConfigError("BUGZILLA_BASE_URL must start with http:// or https://")
        if auth_mode not in {"auto", "token", "password", "anonymous"}:
            raise ConfigError("BUGZILLA_AUTH_MODE must be auto, token, password, or anonymous")
        if transport not in {"auto", "rest", "jsonrpc"}:
            raise ConfigError("BUGZILLA_TRANSPORT must be auto, rest, or jsonrpc")
        if auth_mode == "token" and not api_token:
            raise ConfigError("BUGZILLA_API_TOKEN is required in token auth mode")
        if auth_mode == "password" and not username:
            raise ConfigError("BUGZILLA_USERNAME is required in password auth mode")
        if auth_mode == "password" and password is None:
            raise ConfigError("BUGZILLA_PASSWORD is required in password auth mode")

    @classmethod
    def from_env(cls) -> "BugzillaConfig":
        base_url = os.getenv("BUGZILLA_BASE_URL")
        if not base_url:
            raise ConfigError("Set BUGZILLA_BASE_URL in plugins/bugzilla/.env or the process environment")
        token = os.getenv("BUGZILLA_API_TOKEN")
        username = os.getenv("BUGZILLA_USERNAME") or os.getenv("BUGZILLA_LOGIN")
        password = os.getenv("BUGZILLA_PASSWORD")
        auth_mode = os.getenv("BUGZILLA_AUTH_MODE", "auto").lower()
        if auth_mode == "auto":
            auth_mode = "token" if token else ("password" if username and password is not None else "anonymous")
        return cls(
            base_url,
            auth_mode=auth_mode,
            api_token=token,
            username=username,
            password=password,
            transport=os.getenv("BUGZILLA_TRANSPORT", "auto").lower(),
        )


class JsonRpcTransport:
    def __init__(self, config: BugzillaConfig | None = None) -> None:
        self.config = config

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self.config is None:
            raise BugzillaError("JSON-RPC transport is not configured")
        payload = {"method": method, "params": [params or {}], "id": 1}
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        response = _urlopen(self.config.base_url + "/jsonrpc.cgi", body=body, headers=headers, method="POST")
        try:
            envelope = json.loads(response.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BugzillaError("Bugzilla returned invalid JSON-RPC response") from exc
        if envelope.get("error"):
            error_payload = envelope["error"]
            message = error_payload.get("message") if isinstance(error_payload, dict) else str(error_payload)
            raise BugzillaError(f"Bugzilla {method} failed: {message}")
        return envelope.get("result") or {}


def _urlopen(url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None, method: str | None = None) -> bytes:
    try:
        with request.urlopen(request.Request(url, data=body, headers=headers or {}, method=method), timeout=30) as response:
            return response.read()
    except error.HTTPError as exc:
        detail = exc.read(512).decode("utf-8", errors="replace")
        raise BugzillaError(f"Bugzilla HTTP {exc.code}: {detail[:300]}") from exc
    except error.URLError as exc:
        raise BugzillaError(f"Could not reach Bugzilla: {exc.reason}") from exc


class BugzillaClient:
    def __init__(self, config: BugzillaConfig, *, transport: JsonRpcTransport | None = None) -> None:
        self.config = config
        self.transport = transport or JsonRpcTransport(config)
        self._selected_transport: str | None = None
        self._session_token: str | None = None

    def _http_json(self, method: str, path: str, *, payload: Any | None = None) -> Any:
        query = {}
        if self.config.api_token:
            query["api_key"] = self.config.api_token
        url = self.config.base_url + path
        if query:
            separator = "&" if "?" in url else "?"
            url += separator + parse.urlencode(query)
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        raw = _urlopen(url, body=body, headers=headers, method=method)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BugzillaError("Bugzilla returned invalid JSON") from exc

    def detect_transport(self) -> str:
        if self._selected_transport:
            return self._selected_transport
        if self.config.transport in {"rest", "jsonrpc"}:
            self._selected_transport = self.config.transport
            return self._selected_transport
        if self.config.auth_mode == "password":
            self._selected_transport = "jsonrpc"
            return self._selected_transport
        try:
            self._http_json("GET", "/rest/version")
            self._selected_transport = "rest"
        except BugzillaError:
            self._selected_transport = "jsonrpc"
        return self._selected_transport

    def _rpc_params(self, params: dict[str, Any]) -> dict[str, Any]:
        result = dict(params)
        if self._session_token:
            result["token"] = self._session_token
        elif self.config.api_token:
            result["api_key"] = self.config.api_token
        return result

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self.config.auth_mode == "password" and not self._session_token:
            login = self.transport.request(
                "User.login",
                {"login": self.config.username, "password": self.config.password, "remember": False},
            )
            self._session_token = login.get("token") if isinstance(login, dict) else None
            if not self._session_token:
                raise BugzillaError("Bugzilla login succeeded without a session token")
        return self.transport.request(method, self._rpc_params(params or {}))

    def server_info(self) -> dict[str, Any]:
        selected = self.detect_transport()
        if selected == "rest":
            payload = self._http_json("GET", "/rest/version")
            return {"base_url": self.config.base_url, "transport": selected, **payload}
        payload = self._rpc("Bugzilla.version")
        return {"base_url": self.config.base_url, "transport": selected, **payload}

    def search_bugs(self, criteria: dict[str, Any]) -> dict[str, Any]:
        if self.detect_transport() == "rest":
            query: list[tuple[str, str]] = []
            for key, value in criteria.items():
                if key in {"limit", "offset"}:
                    query.append((key, str(value)))
                elif isinstance(value, list):
                    query.append((key, ",".join(str(item) for item in value)))
                else:
                    query.append((key, str(value)))
            path = "/rest/bug" + (("?" + parse.urlencode(query)) if query else "")
            return self._http_json("GET", path, payload=None)
        return self._rpc("Bug.search", criteria)

    def get_bug(self, bug_id: int) -> dict[str, Any]:
        if self.detect_transport() == "rest":
            return self._http_json("GET", f"/rest/bug/{int(bug_id)}")
        return self._rpc("Bug.get", {"ids": [int(bug_id)]})

    def create_bug(self, fields: dict[str, Any]) -> dict[str, Any]:
        if self.detect_transport() == "rest":
            return self._http_json("POST", "/rest/bug", payload=fields)
        return self._rpc("Bug.create", fields)

    def update_bug(self, bug_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        if self.detect_transport() == "rest":
            return self._http_json("PUT", f"/rest/bug/{int(bug_id)}", payload=fields)
        return self._rpc("Bug.update", {"ids": [int(bug_id)], **fields})

    def list_products(self) -> dict[str, Any]:
        if self.detect_transport() == "rest":
            return self._http_json("GET", "/rest/product")
        accessible = self._rpc("Product.get_accessible_products")
        ids = accessible.get("ids", []) if isinstance(accessible, dict) else []
        return self._rpc("Product.get", {"ids": ids})


def make_json_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}]}
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    if is_error:
        result["isError"] = True
    return result


def bug_summary(payload: dict[str, Any]) -> dict[str, Any]:
    bugs = payload.get("bugs") if isinstance(payload, dict) else None
    return {"bugs": bugs if isinstance(bugs, list) else [], "count": len(bugs) if isinstance(bugs, list) else 0}


class OpenProjectClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("OPENPROJECT_BASE_URL", "").rstrip("/")
        self.bearer_token = os.getenv("OPENPROJECT_API_TOKEN") or os.getenv("OPENPROJECT_ACCESS_TOKEN")
        self.basic_token = os.getenv("OPENPROJECT_BASIC_TOKEN")
        if not self.base_url or not (self.bearer_token or self.basic_token):
            raise ConfigError("Set OPENPROJECT_BASE_URL and OPENPROJECT_API_TOKEN for the OpenProject bridge")

    def _request(self, method: str, path: str, payload: Any | None = None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/hal+json",
        }
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        else:
            auth = base64.b64encode(f"apikey:{self.basic_token}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {auth}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        raw = _urlopen(self.base_url + path, body=body, headers=headers, method=method)
        try:
            result = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BugzillaError("OpenProject returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise BugzillaError("OpenProject returned an unexpected response")
        return result

    def create_work_package(self, *, project_id: int, type_id: int, subject: str, description: str) -> dict[str, Any]:
        payload = {
            "subject": subject,
            "description": {"format": "markdown", "raw": description},
            "_links": {
                "project": {"href": f"/api/v3/projects/{int(project_id)}"},
                "type": {"href": f"/api/v3/types/{int(type_id)}"},
            },
        }
        return self._request("POST", "/api/v3/work_packages", payload)


_client: BugzillaClient | None = None


def get_client() -> BugzillaClient:
    global _client
    if _client is None:
        _client = BugzillaClient(BugzillaConfig.from_env())
    return _client


def _require_confirm(arguments: dict[str, Any]) -> None:
    if arguments.get("confirm") is not True:
        raise ValueError("This operation changes data. Re-run with confirm=true after reviewing the preview.")


def tool_server_info(_: dict[str, Any]) -> dict[str, Any]:
    return get_client().server_info()


def tool_list_products(_: dict[str, Any]) -> dict[str, Any]:
    return get_client().list_products()


def tool_search_bugs(arguments: dict[str, Any]) -> dict[str, Any]:
    criteria = dict(arguments.get("criteria") or {})
    for key in ("ids", "product", "component", "status", "resolution", "assigned_to", "creator", "summary"):
        if key in arguments:
            criteria[key] = arguments[key]
    if "limit" in arguments:
        limit = int(arguments["limit"])
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        criteria["limit"] = limit
    if "offset" in arguments:
        criteria["offset"] = max(0, int(arguments["offset"]))
    return bug_summary(get_client().search_bugs(criteria))


def tool_get_bug(arguments: dict[str, Any]) -> dict[str, Any]:
    return get_client().get_bug(int(arguments["bug_id"]))


def tool_create_bug(arguments: dict[str, Any]) -> dict[str, Any]:
    fields = dict(arguments.get("fields") or {})
    if arguments.get("confirm") is not True:
        return {"preview": True, "would_create": fields, "message": "Re-run with confirm=true to create this Bugzilla bug."}
    if not fields.get("product") or not fields.get("component") or not fields.get("summary") or not fields.get("description"):
        raise ValueError("fields must include product, component, summary, and description")
    return get_client().create_bug(fields)


def tool_add_comment(arguments: dict[str, Any]) -> dict[str, Any]:
    bug_id = int(arguments["bug_id"])
    comment = str(arguments["comment"]).strip()
    if not comment:
        raise ValueError("comment must not be empty")
    if arguments.get("confirm") is not True:
        return {"preview": True, "bug_id": bug_id, "comment": comment, "message": "Re-run with confirm=true to add the comment."}
    return get_client().update_bug(bug_id, {"comments": [{"body": comment, "is_private": bool(arguments.get("is_private", False))}]})


def tool_update_bug(arguments: dict[str, Any]) -> dict[str, Any]:
    fields = dict(arguments.get("fields") or {})
    if not fields:
        raise ValueError("fields must not be empty")
    if arguments.get("confirm") is not True:
        return {"preview": True, "bug_id": int(arguments["bug_id"]), "fields": fields, "message": "Re-run with confirm=true to update the bug."}
    return get_client().update_bug(int(arguments["bug_id"]), fields)


def tool_to_openproject(arguments: dict[str, Any]) -> dict[str, Any]:
    bug_id = int(arguments["bug_id"])
    project_id = int(arguments["openproject_project_id"])
    type_id = int(arguments["openproject_type_id"])
    bug_payload = get_client().get_bug(bug_id)
    bugs = bug_payload.get("bugs") or []
    if not bugs:
        raise BugzillaError(f"Bugzilla bug {bug_id} was not found")
    bug = bugs[0]
    subject = str(arguments.get("subject") or f"Bugzilla #{bug_id}: {bug.get('summary', '')}").strip()
    description = str(arguments.get("description") or bug.get("description") or "")
    preview = {
        "bug_id": bug_id,
        "openproject_project_id": project_id,
        "openproject_type_id": type_id,
        "subject": subject,
        "description": description,
    }
    if arguments.get("confirm") is not True:
        return {"preview": True, **preview, "message": "Re-run with confirm=true to create the OpenProject work package."}
    created = OpenProjectClient().create_work_package(
        project_id=project_id,
        type_id=type_id,
        subject=subject,
        description=description,
    )
    return {"bugzilla": {"id": bug_id}, "openproject": created}


TOOLS: dict[str, dict[str, Any]] = {
    "bugzilla_server_info": {
        "description": "Detect Bugzilla REST or JSON-RPC transport and return server information.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_server_info,
    },
    "bugzilla_list_products": {
        "description": "List products accessible to the configured Bugzilla user.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "handler": tool_list_products,
    },
    "bugzilla_search": {
        "description": "Search Bugzilla bugs using native Bugzilla search criteria.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "criteria": {"type": "object"}, "ids": {"type": "array"}, "product": {"type": "string"},
                "component": {"type": "string"}, "status": {"type": "array"}, "resolution": {"type": "array"},
                "assigned_to": {"type": "string"}, "creator": {"type": "string"}, "summary": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "offset": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "handler": tool_search_bugs,
    },
    "bugzilla_get": {
        "description": "Get complete details for one Bugzilla bug.",
        "inputSchema": {"type": "object", "properties": {"bug_id": {"type": "integer"}}, "required": ["bug_id"], "additionalProperties": False},
        "handler": tool_get_bug,
    },
    "bugzilla_create": {
        "description": "Preview or create a Bugzilla bug. Creation requires confirm=true.",
        "inputSchema": {"type": "object", "properties": {"fields": {"type": "object"}, "confirm": {"type": "boolean"}}, "required": ["fields"], "additionalProperties": False},
        "handler": tool_create_bug,
    },
    "bugzilla_add_comment": {
        "description": "Preview or add a Bugzilla comment. Writing requires confirm=true.",
        "inputSchema": {"type": "object", "properties": {"bug_id": {"type": "integer"}, "comment": {"type": "string"}, "is_private": {"type": "boolean"}, "confirm": {"type": "boolean"}}, "required": ["bug_id", "comment"], "additionalProperties": False},
        "handler": tool_add_comment,
    },
    "bugzilla_update": {
        "description": "Preview or update Bugzilla fields. Writing requires confirm=true.",
        "inputSchema": {"type": "object", "properties": {"bug_id": {"type": "integer"}, "fields": {"type": "object"}, "confirm": {"type": "boolean"}}, "required": ["bug_id", "fields"], "additionalProperties": False},
        "handler": tool_update_bug,
    },
    "bugzilla_to_openproject": {
        "description": "Preview or create an OpenProject work package from a Bugzilla bug. Creation requires confirm=true.",
        "inputSchema": {
            "type": "object",
            "properties": {"bug_id": {"type": "integer"}, "openproject_project_id": {"type": "integer"}, "openproject_type_id": {"type": "integer"}, "subject": {"type": "string"}, "description": {"type": "string"}, "confirm": {"type": "boolean"}},
            "required": ["bug_id", "openproject_project_id", "openproject_type_id"],
            "additionalProperties": False,
        },
        "handler": tool_to_openproject,
    },
}


def read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in {b"\n", b"\r\n"}:
            break
        header = line.decode("utf-8").strip()
        if ":" in header:
            key, value = header.split(":", 1)
            headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8")) if body else None


def write_message(message: dict[str, Any]) -> None:
    body = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        return response(request_id, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return response(request_id, {})
    if method == "tools/list":
        return response(request_id, {"tools": [{"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]} for name, spec in TOOLS.items()]})
    if method == "tools/call":
        name = params.get("name")
        spec = TOOLS.get(name)
        if not spec:
            return response(request_id, make_json_result({"error": f"Unknown tool '{name}'."}, is_error=True))
        try:
            return response(request_id, make_json_result(spec["handler"](params.get("arguments") or {})))
        except (BugzillaError, ValueError, KeyError) as exc:
            return response(request_id, make_json_result({"error": str(exc)}, is_error=True))
        except Exception as exc:  # Keep protocol output clean while retaining a diagnostic on stderr.
            print(f"Unexpected {name} failure: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return response(request_id, make_json_result({"error": "Unexpected server error"}, is_error=True))
    return response(request_id, {"error": {"code": -32601, "message": f"Unknown method: {method}"}})


def main() -> None:
    while True:
        message = read_message()
        if message is None:
            return
        result = handle_request(message)
        if result is not None:
            write_message(result)


if __name__ == "__main__":
    main()
