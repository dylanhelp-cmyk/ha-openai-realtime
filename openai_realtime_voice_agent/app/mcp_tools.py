"""Expose only the existing Home Assistant MCP tools to the Responses backend."""

import asyncio
import json
import logging
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger(__name__)


class HomeTools:
    def __init__(self, settings, token):
        self.settings = settings
        self.url = settings.ha_mcp_url or "http://supervisor/core/api/mcp"
        self.token = token
        self.tools = []
        self.names = set()
        self.catalog = ""

    async def _run(self, operation):
        async with asyncio.timeout(self.settings.tool_timeout_s):
            async with streamablehttp_client(
                self.url, headers={"Authorization": f"Bearer {self.token}"}
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await operation(session)

    async def initialize(self):
        if not self.token:
            raise ValueError("Home Assistant API token unavailable")
        allowed = {
            x.strip() for x in self.settings.mcp_tool_allowlist.split(",") if x.strip()
        }

        async def load(session):
            cursor = None
            tools = []
            while True:
                page = await session.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.nextCursor
                if not cursor:
                    break
            return tools

        tools = await self._run(load)
        for tool in tools:
            if allowed and tool.name not in allowed:
                continue
            self.names.add(tool.name)
            self.tools.append(
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description or tool.name,
                    "parameters": tool.inputSchema,
                    "strict": False,
                }
            )
        log.info("Loaded %d Assist MCP tools", len(self.tools))
        discovery = next((n for n in self.names if n.endswith("GetLiveContext")), None)
        if self.settings.mcp_preload_context and discovery:
            result = await self.call(discovery, {})
            if not result["isError"]:
                self.catalog = json.dumps(result["result"], ensure_ascii=False)[:48000]
                log.info(
                    "Cached Assist device context; known commands need no preliminary discovery"
                )

    async def call(self, name, arguments):
        if name not in self.names:
            return {
                "isError": True,
                "result": "Tool unavailable or excluded by allowlist",
            }
        if not isinstance(arguments, dict):
            return {"isError": True, "result": "Arguments must be an object"}
        try:
            result = await self._run(
                lambda session: session.call_tool(name, arguments=arguments)
            )
            structured = getattr(result, "structuredContent", None)
            text = "\n".join(p.text for p in result.content if getattr(p, "text", None))
            payload = {
                "isError": bool(result.isError),
                "result": structured if structured is not None else text,
            }
            log.info(
                "HA tool %s: %s", name, "error" if payload["isError"] else "completed"
            )
            return payload
        except Exception as exc:
            log.warning(
                "HA tool %s failed (%s); no automatic retry", name, type(exc).__name__
            )
            return {
                "isError": True,
                "result": "Home Assistant request failed. Outcome may be unknown; check state before retrying.",
            }
