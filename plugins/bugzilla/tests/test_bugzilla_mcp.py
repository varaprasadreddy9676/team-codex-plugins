import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import bugzilla_mcp
from bugzilla_mcp import BugzillaClient, BugzillaConfig, BugzillaError, JsonRpcTransport


class FakeTransport(JsonRpcTransport):
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, method, params=None):
        self.calls.append((method, params or {}))
        response = self.responses.get(method)
        if isinstance(response, Exception):
            raise response
        return response


class BugzillaTransportTests(unittest.TestCase):
    def test_jsonrpc_login_token_is_added_to_subsequent_calls(self):
        transport = FakeTransport(
            {
                "User.login": {"id": 146, "token": "session-token"},
                "Bug.get": {"bugs": [{"id": 42, "summary": "Example"}]},
            }
        )
        client = BugzillaClient(
            BugzillaConfig("http://bugzilla.test", auth_mode="password", username="u", password="p", transport="jsonrpc"),
            transport=transport,
        )

        result = client.get_bug(42)

        self.assertEqual(result["bugs"][0]["id"], 42)
        self.assertEqual(transport.calls[0], ("User.login", {"login": "u", "password": "p", "remember": False}))
        self.assertEqual(transport.calls[1], ("Bug.get", {"ids": [42], "token": "session-token"}))

    def test_jsonrpc_errors_are_exposed_without_leaking_password(self):
        transport = FakeTransport({"User.login": BugzillaError("login failed")})
        client = BugzillaClient(
            BugzillaConfig("http://bugzilla.test", auth_mode="password", username="u", password="secret", transport="jsonrpc"),
            transport=transport,
        )

        with self.assertRaisesRegex(BugzillaError, "login failed") as caught:
            client.get_bug(42)
        self.assertNotIn("secret", str(caught.exception))

    def test_rest_probe_selects_rest_only_when_available(self):
        config = BugzillaConfig("http://bugzilla.test", auth_mode="token", api_token="api-token")
        client = BugzillaClient(config)
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/rest/version":
                return {"version": "5.2.0"}
            return {"bugs": []}

        client._http_json = fake_request
        self.assertEqual(client.detect_transport(), "rest")
        self.assertEqual(calls[0][1], "/rest/version")

    def test_jsonrpc_fallback_is_selected_when_rest_is_unavailable(self):
        config = BugzillaConfig("http://bugzilla.test", auth_mode="token", api_token="api-token")
        transport = FakeTransport({"Bugzilla.version": {"version": "5.0.2"}})
        client = BugzillaClient(config, transport=transport)
        client._http_json = lambda *args, **kwargs: (_ for _ in ()).throw(BugzillaError("404"))

        self.assertEqual(client.detect_transport(), "jsonrpc")
        self.assertEqual(client.server_info()["version"], "5.0.2")

    def test_mcp_lists_read_and_write_tools(self):
        result = bugzilla_mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {tool["name"] for tool in result["result"]["tools"]}
        self.assertIn("bugzilla_search", names)
        self.assertIn("bugzilla_to_openproject", names)

    def test_create_bug_without_confirmation_only_returns_preview(self):
        result = bugzilla_mcp.tool_create_bug(
            {"fields": {"product": "medics", "component": "EMR", "summary": "Test", "description": "Test"}}
        )
        self.assertTrue(result["preview"])
        self.assertIn("confirm=true", result["message"])

    def test_openproject_bridge_requires_confirmation(self):
        class FakeClient:
            def get_bug(self, bug_id):
                return {"bugs": [{"id": bug_id, "summary": "Bridge me", "description": "Details"}]}

        original = bugzilla_mcp._client
        bugzilla_mcp._client = FakeClient()
        try:
            result = bugzilla_mcp.tool_to_openproject(
                {"bug_id": 42, "openproject_project_id": 7, "openproject_type_id": 1}
            )
        finally:
            bugzilla_mcp._client = original
        self.assertTrue(result["preview"])
        self.assertEqual(result["subject"], "Bugzilla #42: Bridge me")


if __name__ == "__main__":
    unittest.main()
