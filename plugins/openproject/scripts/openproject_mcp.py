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


SERVER_NAME = "openproject"
SERVER_VERSION = "0.1.0"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


for dotenv_path in (PLUGIN_ROOT / ".env", PLUGIN_ROOT / ".env.local"):
    load_dotenv(dotenv_path)


class ConfigError(Exception):
    pass


class OpenProjectApiError(Exception):
    def __init__(self, status: int, message: str, details: Any | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def make_json_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    text = json.dumps(payload, indent=2, sort_keys=True)
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
    }
    if isinstance(payload, dict):
        result["structuredContent"] = payload
    if is_error:
        result["isError"] = True
    return result


def formattable_raw(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("raw") or value.get("html")
    if isinstance(value, str):
        return value
    return None


def collection_elements(payload: dict[str, Any]) -> list[dict[str, Any]]:
    embedded = payload.get("_embedded") or {}
    elements = embedded.get("elements") or []
    return [item for item in elements if isinstance(item, dict)]


def link_info(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    link = (payload.get("_links") or {}).get(name)
    if not isinstance(link, dict):
        return None
    return {
        "href": link.get("href"),
        "title": link.get("title"),
    }


def duration_from_hours(hours: float | int | None) -> str | None:
    if hours is None:
        return None
    total_minutes = int(round(float(hours) * 60))
    whole_hours, minutes = divmod(total_minutes, 60)
    parts = ["PT"]
    if whole_hours:
        parts.append(f"{whole_hours}H")
    if minutes:
        parts.append(f"{minutes}M")
    if len(parts) == 1:
        parts.append("0H")
    return "".join(parts)


def summarize_project(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": project.get("id"),
        "identifier": project.get("identifier"),
        "name": project.get("name") or (link_info(project, "self") or {}).get("title"),
        "active": project.get("active"),
        "type": project.get("type"),
        "description": formattable_raw(project.get("description")),
        "updatedAt": project.get("updatedAt"),
        "apiHref": (link_info(project, "self") or {}).get("href"),
    }


def summarize_named_resource(item: dict[str, Any]) -> dict[str, Any]:
    self_link = link_info(item, "self") or {}
    return {
        "id": item.get("id"),
        "name": item.get("name") or self_link.get("title"),
        "apiHref": self_link.get("href"),
    }


def summarize_user(user: dict[str, Any]) -> dict[str, Any]:
    self_link = link_info(user, "self") or {}
    return {
        "id": user.get("id"),
        "name": user.get("name") or self_link.get("title"),
        "login": user.get("login"),
        "email": user.get("email"),
        "status": user.get("status"),
        "admin": user.get("admin"),
        "apiHref": self_link.get("href"),
    }


def summarize_activity(activity: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": activity.get("id"),
        "comment": formattable_raw(activity.get("comment")),
        "user": link_info(activity, "user"),
        "createdAt": activity.get("createdAt"),
        "updatedAt": activity.get("updatedAt"),
        "apiHref": (link_info(activity, "self") or {}).get("href"),
    }


def summarize_work_package(work_package: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": work_package.get("id"),
        "subject": work_package.get("subject"),
        "type": link_info(work_package, "type"),
        "status": link_info(work_package, "status"),
        "priority": link_info(work_package, "priority"),
        "project": link_info(work_package, "project"),
        "author": link_info(work_package, "author"),
        "assignee": link_info(work_package, "assignee"),
        "responsible": link_info(work_package, "responsible"),
        "parent": link_info(work_package, "parent"),
        "description": formattable_raw(work_package.get("description")),
        "startDate": work_package.get("startDate"),
        "dueDate": work_package.get("dueDate"),
        "date": work_package.get("date"),
        "estimatedTime": work_package.get("estimatedTime"),
        "percentageDone": work_package.get("percentageDone"),
        "lockVersion": work_package.get("lockVersion"),
        "createdAt": work_package.get("createdAt"),
        "updatedAt": work_package.get("updatedAt"),
        "apiHref": (link_info(work_package, "self") or {}).get("href"),
    }


class OpenProjectClient:
    def __init__(self) -> None:
        base_url = first_env("OPENPROJECT_BASE_URL")
        if not base_url:
            raise ConfigError(
                "OPENPROJECT_BASE_URL is not configured. Set it in the environment "
                "or in plugins/openproject/.env."
            )

        normalized = base_url.rstrip("/")
        if normalized.endswith("/api/v3"):
            self.api_root = normalized
        else:
            self.api_root = f"{normalized}/api/v3"

        self.bearer_token = first_env(
            "OPENPROJECT_API_TOKEN",
            "OPENPROJECT_ACCESS_TOKEN",
            "OPENPROJECT_TOKEN",
        )
        self.basic_token = first_env("OPENPROJECT_BASIC_TOKEN")

    def config_summary(self) -> dict[str, Any]:
        auth_mode = "none"
        if self.bearer_token:
            auth_mode = "bearer"
        elif self.basic_token:
            auth_mode = "basic"

        return {
            "configured": True,
            "apiRoot": self.api_root,
            "authMode": auth_mode,
        }

    def _build_url(self, path: str, params: dict[str, Any] | None = None) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            url = path
        elif path.startswith("/"):
            parsed = parse.urlparse(self.api_root)
            url = parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
        else:
            url = parse.urljoin(f"{self.api_root.rstrip('/')}/", path)

        if params:
            clean_params: dict[str, str] = {}
            for key, value in params.items():
                if value is None:
                    continue
                clean_params[key] = str(value)
            if clean_params:
                url = f"{url}?{parse.urlencode(clean_params)}"

        return url

    def _headers(self, include_json: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/hal+json, application/json;q=0.9",
            "User-Agent": f"codex-openproject-plugin/{SERVER_VERSION}",
        }
        if include_json:
            headers["Content-Type"] = "application/json"
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        elif self.basic_token:
            token = base64.b64encode(f"apikey:{self.basic_token}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        return headers

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = request.Request(
            self._build_url(path, params),
            data=data,
            headers=self._headers(include_json=body is not None),
            method=method.upper(),
        )

        try:
            with request.urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            payload: Any
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
            message = raw
            if isinstance(payload, dict):
                message = payload.get("message") or raw
            raise OpenProjectApiError(exc.code, message, payload) from exc
        except error.URLError as exc:
            raise ConfigError(f"Could not reach OpenProject at {self.api_root}: {exc.reason}") from exc

        if not raw:
            return {}

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OpenProjectApiError(502, "OpenProject returned invalid JSON.", raw) from exc

        if not isinstance(parsed, dict):
            raise OpenProjectApiError(502, "OpenProject returned a non-object JSON payload.", parsed)

        return parsed


def list_projects_impl(arguments: dict[str, Any]) -> dict[str, Any]:
    client = OpenProjectClient()
    page_size = int(arguments.get("page_size", 25))
    offset = int(arguments.get("offset", 1))
    query = (arguments.get("query") or "").strip().lower()

    payload = client.request_json(
        "GET",
        "projects",
        params={"pageSize": page_size, "offset": offset},
    )
    projects = [summarize_project(item) for item in collection_elements(payload)]
    if query:
        projects = [
            item
            for item in projects
            if query in (item.get("name") or "").lower()
            or query in (item.get("identifier") or "").lower()
            or query in (item.get("description") or "").lower()
        ]
    return {
        "count": len(projects),
        "total": payload.get("total"),
        "projects": projects,
    }


def resolve_project_ref(client: OpenProjectClient, value: str) -> str:
    candidate = str(value).strip()
    if not candidate:
        raise ConfigError("project_ref must not be empty.")
    if candidate.isdigit():
        return candidate
    if " " not in candidate:
        return candidate

    projects = list_projects_impl({"page_size": 100, "offset": 1, "query": candidate}).get("projects", [])
    exact = [
        item
        for item in projects
        if candidate.lower() in {
            str(item.get("name") or "").lower(),
            str(item.get("identifier") or "").lower(),
        }
    ]
    if len(exact) == 1:
        return exact[0].get("identifier") or str(exact[0].get("id"))
    if len(projects) == 1:
        return projects[0].get("identifier") or str(projects[0].get("id"))
    raise ConfigError(
        f"Could not uniquely resolve project_ref '{value}'. "
        "Use a project id or identifier."
    )


def resolve_named_resource(
    items: list[dict[str, Any]],
    value: str,
    *,
    kind: str,
) -> dict[str, Any]:
    candidate = str(value).strip()
    if not candidate:
        raise ConfigError(f"{kind} must not be empty.")
    if candidate.isdigit():
        for item in items:
            if str(item.get("id")) == candidate:
                return item
        raise ConfigError(f"Could not find {kind} id {candidate}.")

    exact_matches = []
    partial_matches = []
    lowered = candidate.lower()

    for item in items:
        haystacks = [
            str(item.get("name") or "").lower(),
            str(item.get("login") or "").lower(),
            str(item.get("email") or "").lower(),
            str(item.get("identifier") or "").lower(),
        ]
        if lowered in haystacks:
            exact_matches.append(item)
        elif any(lowered in hay for hay in haystacks if hay):
            partial_matches.append(item)

    if len(exact_matches) == 1:
        return exact_matches[0]
    if not exact_matches and len(partial_matches) == 1:
        return partial_matches[0]
    if exact_matches or partial_matches:
        matches = exact_matches or partial_matches
        candidates = [item.get("name") or item.get("login") or item.get("id") for item in matches[:10]]
        raise ConfigError(f"{kind} '{value}' is ambiguous. Candidates: {candidates}")
    raise ConfigError(f"Could not resolve {kind} '{value}'.")


def fetch_types(client: OpenProjectClient, project_ref: str) -> list[dict[str, Any]]:
    payload = client.request_json("GET", f"projects/{parse.quote(project_ref, safe='')}/types")
    return [summarize_named_resource(item) for item in collection_elements(payload)]


def fetch_statuses(client: OpenProjectClient) -> list[dict[str, Any]]:
    payload = client.request_json("GET", "statuses")
    return [summarize_named_resource(item) for item in collection_elements(payload)]


def fetch_priorities(client: OpenProjectClient) -> list[dict[str, Any]]:
    payload = client.request_json("GET", "priorities")
    return [summarize_named_resource(item) for item in collection_elements(payload)]


def fetch_users(client: OpenProjectClient, *, page_size: int = 100) -> list[dict[str, Any]]:
    payload = client.request_json("GET", "users", params={"pageSize": page_size, "offset": 1})
    return [summarize_user(item) for item in collection_elements(payload)]


def resolve_type_href(client: OpenProjectClient, project_ref: str, type_value: str) -> str:
    item = resolve_named_resource(fetch_types(client, project_ref), type_value, kind="type")
    return f"/api/v3/types/{item['id']}"


def resolve_status_href(client: OpenProjectClient, status_value: str) -> str:
    item = resolve_named_resource(fetch_statuses(client), status_value, kind="status")
    return f"/api/v3/statuses/{item['id']}"


def resolve_priority_href(client: OpenProjectClient, priority_value: str) -> str:
    item = resolve_named_resource(fetch_priorities(client), priority_value, kind="priority")
    return f"/api/v3/priorities/{item['id']}"


def resolve_user_href(client: OpenProjectClient, user_value: str) -> str:
    candidate = str(user_value).strip()
    if candidate.lower() == "me":
        me = client.request_json("GET", "users/me")
        return f"/api/v3/users/{me['id']}"
    item = resolve_named_resource(fetch_users(client), candidate, kind="user")
    return f"/api/v3/users/{item['id']}"


def maybe_add_formattable(payload: dict[str, Any], field: str, value: Any) -> None:
    if value is None:
        return
    payload[field] = {"format": "markdown", "raw": str(value)}


def maybe_add_scalar(payload: dict[str, Any], field: str, value: Any) -> None:
    if value is None:
        return
    payload[field] = value


def maybe_add_link(links: dict[str, Any], name: str, href: str | None) -> None:
    if href is None:
        return
    links[name] = {"href": href}


def build_work_package_payload(
    client: OpenProjectClient,
    *,
    project_ref: str,
    current_lock_version: int | None = None,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    links: dict[str, Any] = {}

    if current_lock_version is not None:
        payload["lockVersion"] = current_lock_version

    maybe_add_scalar(payload, "subject", arguments.get("subject"))
    maybe_add_formattable(payload, "description", arguments.get("description"))
    maybe_add_scalar(payload, "startDate", arguments.get("start_date"))
    maybe_add_scalar(payload, "dueDate", arguments.get("due_date"))
    maybe_add_scalar(payload, "date", arguments.get("date"))
    maybe_add_scalar(payload, "percentageDone", arguments.get("percentage_done"))

    estimated_hours = arguments.get("estimated_hours")
    if estimated_hours is not None:
        payload["estimatedTime"] = duration_from_hours(float(estimated_hours))

    maybe_add_link(links, "type", resolve_type_href(client, project_ref, arguments["type"]) if arguments.get("type") else None)
    maybe_add_link(links, "status", resolve_status_href(client, arguments["status"]) if arguments.get("status") else None)
    maybe_add_link(links, "priority", resolve_priority_href(client, arguments["priority"]) if arguments.get("priority") else None)
    maybe_add_link(links, "assignee", resolve_user_href(client, arguments["assignee"]) if arguments.get("assignee") else None)
    maybe_add_link(links, "responsible", resolve_user_href(client, arguments["responsible"]) if arguments.get("responsible") else None)
    maybe_add_link(
        links,
        "parent",
        f"/api/v3/work_packages/{arguments['parent_id']}" if arguments.get("parent_id") else None,
    )

    if links:
        payload["_links"] = links

    return payload


def tool_server_info(_: dict[str, Any]) -> dict[str, Any]:
    base_url = first_env("OPENPROJECT_BASE_URL")
    bearer = bool(first_env("OPENPROJECT_API_TOKEN", "OPENPROJECT_ACCESS_TOKEN", "OPENPROJECT_TOKEN"))
    basic = bool(first_env("OPENPROJECT_BASIC_TOKEN"))
    auth_mode = "none"
    if bearer:
        auth_mode = "bearer"
    elif basic:
        auth_mode = "basic"
    return {
        "configured": bool(base_url),
        "apiRoot": f"{base_url.rstrip('/')}/api/v3" if base_url else None,
        "authMode": auth_mode,
        "dotenvPath": str((PLUGIN_ROOT / ".env").resolve()),
    }


def tool_get_current_user(_: dict[str, Any]) -> dict[str, Any]:
    client = OpenProjectClient()
    return {"user": summarize_user(client.request_json("GET", "users/me"))}


def tool_list_projects(arguments: dict[str, Any]) -> dict[str, Any]:
    return list_projects_impl(arguments)


def tool_list_project_types(arguments: dict[str, Any]) -> dict[str, Any]:
    client = OpenProjectClient()
    project_ref = resolve_project_ref(client, str(arguments.get("project_ref") or ""))
    return {
        "project_ref": project_ref,
        "types": fetch_types(client, project_ref),
    }


def tool_list_statuses(_: dict[str, Any]) -> dict[str, Any]:
    client = OpenProjectClient()
    return {"statuses": fetch_statuses(client)}


def tool_list_priorities(_: dict[str, Any]) -> dict[str, Any]:
    client = OpenProjectClient()
    return {"priorities": fetch_priorities(client)}


def tool_list_users(arguments: dict[str, Any]) -> dict[str, Any]:
    client = OpenProjectClient()
    page_size = int(arguments.get("page_size", 50))
    query = (arguments.get("query") or "").strip().lower()
    users = fetch_users(client, page_size=page_size)
    if query:
        users = [
            item
            for item in users
            if query in (item.get("name") or "").lower()
            or query in (item.get("login") or "").lower()
            or query in (item.get("email") or "").lower()
        ]
    return {"count": len(users), "users": users}


def tool_search_work_packages(arguments: dict[str, Any]) -> dict[str, Any]:
    client = OpenProjectClient()
    page_size = int(arguments.get("page_size", 25))
    offset = int(arguments.get("offset", 1))
    query = (arguments.get("query") or "").strip()
    project_value = arguments.get("project_ref")
    path = "work_packages"
    if project_value:
        project_ref = resolve_project_ref(client, str(project_value))
        path = f"projects/{parse.quote(project_ref, safe='')}/work_packages"

    filters = []
    if query:
        filters.append({"subjectOrId": {"operator": "**", "values": [query]}})

    payload = client.request_json(
        "GET",
        path,
        params={
            "pageSize": page_size,
            "offset": offset,
            "filters": json.dumps(filters if filters else []),
            "sortBy": json.dumps([["updatedAt", "desc"]]),
        },
    )
    work_packages = [summarize_work_package(item) for item in collection_elements(payload)]
    return {
        "count": len(work_packages),
        "total": payload.get("total"),
        "work_packages": work_packages,
    }


def tool_get_work_package(arguments: dict[str, Any]) -> dict[str, Any]:
    client = OpenProjectClient()
    work_package_id = str(arguments.get("id") or "").strip()
    if not work_package_id:
        raise ConfigError("id is required.")

    work_package = client.request_json("GET", f"work_packages/{parse.quote(work_package_id, safe='')}")
    result: dict[str, Any] = {"work_package": summarize_work_package(work_package)}

    recent_activities_limit = int(arguments.get("recent_activities_limit", 0))
    if recent_activities_limit > 0:
        activities = client.request_json(
            "GET",
            f"work_packages/{parse.quote(work_package_id, safe='')}/activities",
        )
        result["activities"] = [
            summarize_activity(item)
            for item in collection_elements(activities)[:recent_activities_limit]
        ]

    return result


def tool_create_work_package(arguments: dict[str, Any]) -> dict[str, Any]:
    client = OpenProjectClient()
    project_ref = resolve_project_ref(client, str(arguments.get("project_ref") or ""))
    subject = str(arguments.get("subject") or "").strip()
    if not subject:
        raise ConfigError("subject is required.")

    payload = build_work_package_payload(client, project_ref=project_ref, arguments=arguments)
    payload["subject"] = subject

    work_package = client.request_json(
        "POST",
        f"projects/{parse.quote(project_ref, safe='')}/work_packages",
        body=payload,
    )
    return {"work_package": summarize_work_package(work_package)}


def tool_update_work_package(arguments: dict[str, Any]) -> dict[str, Any]:
    client = OpenProjectClient()
    work_package_id = str(arguments.get("id") or "").strip()
    if not work_package_id:
        raise ConfigError("id is required.")

    current = client.request_json("GET", f"work_packages/{parse.quote(work_package_id, safe='')}")
    project_href = (link_info(current, "project") or {}).get("href")
    if not project_href:
        raise ConfigError(f"Work package {work_package_id} is missing its project link.")

    project_ref = project_href.rstrip("/").split("/")[-1]
    payload = build_work_package_payload(
        client,
        project_ref=project_ref,
        current_lock_version=int(current.get("lockVersion")),
        arguments=arguments,
    )

    if len(payload) == 1 and "lockVersion" in payload:
        raise ConfigError("No update fields were provided.")

    work_package = client.request_json(
        "PATCH",
        f"work_packages/{parse.quote(work_package_id, safe='')}",
        body=payload,
    )
    return {"work_package": summarize_work_package(work_package)}


def tool_comment_on_work_package(arguments: dict[str, Any]) -> dict[str, Any]:
    client = OpenProjectClient()
    work_package_id = str(arguments.get("id") or "").strip()
    comment = str(arguments.get("comment") or "").strip()
    if not work_package_id:
        raise ConfigError("id is required.")
    if not comment:
        raise ConfigError("comment is required.")

    payload: dict[str, Any] = {"comment": {"raw": comment}}
    if arguments.get("internal"):
        payload["internal"] = True

    activity = client.request_json(
        "POST",
        f"work_packages/{parse.quote(work_package_id, safe='')}/activities",
        params={"notify": "true" if arguments.get("notify", True) else "false"},
        body=payload,
    )
    return {"activity": summarize_activity(activity)}


TOOLS: dict[str, dict[str, Any]] = {
    "server_info": {
        "description": "Return OpenProject plugin configuration status and resolved API root.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": tool_server_info,
    },
    "get_current_user": {
        "description": "Return the current OpenProject user associated with the configured token.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": tool_get_current_user,
    },
    "list_projects": {
        "description": "List OpenProject projects. Optional query filtering is applied client-side to the returned page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "page_size": {"type": "number"},
                "offset": {"type": "number"}
            },
            "additionalProperties": False,
        },
        "handler": tool_list_projects,
    },
    "list_project_types": {
        "description": "List work package types available in a project. project_ref can be a project id, identifier, or exact project name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_ref": {"type": "string"}
            },
            "required": ["project_ref"],
            "additionalProperties": False,
        },
        "handler": tool_list_project_types,
    },
    "list_statuses": {
        "description": "List OpenProject work package statuses.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": tool_list_statuses,
    },
    "list_priorities": {
        "description": "List OpenProject work package priorities.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": tool_list_priorities,
    },
    "list_users": {
        "description": "List OpenProject users. Optional query filtering is applied client-side to the returned page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "page_size": {"type": "number"}
            },
            "additionalProperties": False,
        },
        "handler": tool_list_users,
    },
    "search_work_packages": {
        "description": "Search work packages by subject or id, optionally scoped to one project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project_ref": {"type": "string"},
                "page_size": {"type": "number"},
                "offset": {"type": "number"}
            },
            "additionalProperties": False,
        },
        "handler": tool_search_work_packages,
    },
    "get_work_package": {
        "description": "Fetch one work package. Optionally include a small number of recent activities.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "recent_activities_limit": {"type": "number"}
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "handler": tool_get_work_package,
    },
    "create_work_package": {
        "description": "Create a work package in a project. type, status, priority, assignee, and responsible accept ids or human-readable names.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_ref": {"type": "string"},
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "type": {"type": "string"},
                "status": {"type": "string"},
                "priority": {"type": "string"},
                "assignee": {"type": "string"},
                "responsible": {"type": "string"},
                "parent_id": {"type": "string"},
                "start_date": {"type": "string"},
                "due_date": {"type": "string"},
                "date": {"type": "string"},
                "estimated_hours": {"type": "number"},
                "percentage_done": {"type": "number"}
            },
            "required": ["project_ref", "subject"],
            "additionalProperties": False,
        },
        "handler": tool_create_work_package,
    },
    "update_work_package": {
        "description": "Update a work package. Only provided fields are changed. type, status, priority, assignee, and responsible accept ids or human-readable names.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "type": {"type": "string"},
                "status": {"type": "string"},
                "priority": {"type": "string"},
                "assignee": {"type": "string"},
                "responsible": {"type": "string"},
                "parent_id": {"type": "string"},
                "start_date": {"type": "string"},
                "due_date": {"type": "string"},
                "date": {"type": "string"},
                "estimated_hours": {"type": "number"},
                "percentage_done": {"type": "number"}
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        "handler": tool_update_work_package,
    },
    "comment_on_work_package": {
        "description": "Add a comment to a work package.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "comment": {"type": "string"},
                "internal": {"type": "boolean"},
                "notify": {"type": "boolean"}
            },
            "required": ["id", "comment"],
            "additionalProperties": False,
        },
        "handler": tool_comment_on_work_package,
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
    if not body:
        return None
    return json.loads(body.decode("utf-8"))


def write_message(message: dict[str, Any]) -> None:
    body = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8"))
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def success_response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str, data: Any | None = None) -> dict[str, Any]:
    error_payload: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error_payload["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error_payload}


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        return success_response(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return success_response(request_id, {})

    if method == "tools/list":
        return success_response(
            request_id,
            {
                "tools": [
                    {
                        "name": name,
                        "description": spec["description"],
                        "inputSchema": spec["inputSchema"],
                    }
                    for name, spec in TOOLS.items()
                ]
            },
        )

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        spec = TOOLS.get(tool_name)
        if spec is None:
            return success_response(
                request_id,
                make_json_result({"error": f"Unknown tool '{tool_name}'."}, is_error=True),
            )

        try:
            payload = spec["handler"](arguments)
            return success_response(request_id, make_json_result(payload))
        except (ConfigError, OpenProjectApiError, ValueError) as exc:
            details = getattr(exc, "details", None)
            status = getattr(exc, "status", None)
            error_payload = {"error": str(exc)}
            if status is not None:
                error_payload["status"] = status
            if details is not None:
                error_payload["details"] = details
            return success_response(request_id, make_json_result(error_payload, is_error=True))
        except Exception as exc:  # pragma: no cover - defensive fallback
            return success_response(
                request_id,
                make_json_result(
                    {
                        "error": f"Unhandled server error: {exc}",
                        "traceback": traceback.format_exc(),
                    },
                    is_error=True,
                ),
            )

    return error_response(request_id, -32601, f"Method not found: {method}")


def main() -> int:
    while True:
        message = read_message()
        if message is None:
            return 0
        response = handle_request(message)
        if response is not None:
            write_message(response)


if __name__ == "__main__":
    sys.exit(main())
