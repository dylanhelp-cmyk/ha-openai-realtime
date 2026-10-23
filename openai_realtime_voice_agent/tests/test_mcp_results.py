"""MCP errors and structured results must reach the model accurately."""
from types import SimpleNamespace
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pipecat.services.mcp_service import StreamableHttpParameters
from app.mcp_service import HomeAssistantMCPService, ReliableMCPClient, result_payload, discovery_instructions


class MCPResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_device_lookup_returns_error_instead_of_losing_callback(self):
        client = ReliableMCPClient(server_params=StreamableHttpParameters(url="http://localhost/api/mcp"))
        session = SimpleNamespace(call_tool=AsyncMock(side_effect=RuntimeError("lookup failed")))
        callback = AsyncMock()
        await client._call_tool(session, "intent__HassTurnOn", {"name": "test lamp"}, callback)
        self.assertTrue(json.loads(callback.call_args.args[0])["isError"])
        callback.assert_awaited_once()

    async def test_structured_only_device_result_is_not_reported_as_missing(self):
        payload = {"devices": [{"name": "Test lamp", "area": "Living Room"}]}
        result = SimpleNamespace(content=[], structuredContent=payload, isError=False)
        client = ReliableMCPClient(server_params=StreamableHttpParameters(url="http://localhost/api/mcp"))
        callback = AsyncMock()
        await client._call_tool(SimpleNamespace(call_tool=AsyncMock(return_value=result)),
                                "homeassistant__GetLiveContext", {}, callback)
        self.assertEqual(json.loads(callback.call_args.args[0]), {"isError": False, "result": payload})

    async def test_lookup_guidance_works_without_catalog_preload(self):
        hint = discovery_instructions("homeassistant__GetLiveContext")
        self.assertIn("homeassistant__GetLiveContext", hint)
        self.assertNotIn("DEVICE CATALOG SNAPSHOT", hint)
        self.assertEqual(discovery_instructions(None), "")

    async def test_home_assistant_tool_errors_are_preserved(self):
        result = SimpleNamespace(isError=True, content=[SimpleNamespace(text="No matching device")])
        self.assertEqual(result_payload(result), {"isError": True, "result": "No matching device"})

    async def test_catalog_is_read_once_and_reused_on_reconnect(self):
        service = HomeAssistantMCPService("http://localhost/api/mcp", "token")
        await service.initialize()
        session = SimpleNamespace(initialize=AsyncMock(), call_tool=AsyncMock(return_value=
            SimpleNamespace(content=[], structuredContent={"devices": [{"name": "Test lamp"}]}, isError=False)))
        transport = MagicMock()
        transport.return_value.__aenter__.return_value = (object(), object(), None)
        session_factory = MagicMock()
        session_factory.return_value.__aenter__.return_value = session
        with patch("app.mcp_service.streamablehttp_client", transport), patch("app.mcp_service.ClientSession", session_factory):
            first = await service.get_device_catalog("homeassistant__GetLiveContext")
            second = await service.get_device_catalog("homeassistant__GetLiveContext")
        self.assertEqual(first, second)
        session.call_tool.assert_awaited_once_with("homeassistant__GetLiveContext", arguments={})
        self.assertIn("call control tools DIRECTLY", discovery_instructions("homeassistant__GetLiveContext", first))

    async def test_known_device_control_does_not_prepend_discovery(self):
        client = ReliableMCPClient(server_params=StreamableHttpParameters(url="http://localhost/api/mcp"))
        session = SimpleNamespace(call_tool=AsyncMock(return_value=
            SimpleNamespace(content=[SimpleNamespace(text="Done")], isError=False)))
        callback = AsyncMock()
        await client._call_tool(session, "intent__HassTurnOn", {"name": "Test lamp"}, callback)
        session.call_tool.assert_awaited_once_with("intent__HassTurnOn", arguments={"name": "Test lamp"})
        self.assertFalse(json.loads(callback.call_args.args[0])["isError"])
