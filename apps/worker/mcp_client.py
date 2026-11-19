import json

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from apps.common.config import settings

async def call_mcp_tool(tool_name: str, arguments: dict) -> dict:
    try:
        timeout = httpx2.Timeout(30, read=300)
        async with httpx2.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            trust_env=settings.mcp_trust_env,
        ) as http_client:
            async with streamable_http_client(
                settings.mcp_server_url,
                http_client=http_client,
            ) as transport:
                # 只使用传输层返回的读写流。
                read, write, *_ = transport
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)

                    if getattr(result, "isError", False) or getattr(result, "is_error", False):
                        message = result.content[0].text if result.content else "MCP 工具调用失败"
                        raise RuntimeError(message)
                    if getattr(result, "structuredContent", None):
                        return result.structuredContent
                    if getattr(result, "structured_content", None):
                        return result.structured_content
                    if result.content:
                        text = result.content[0].text
                        try:
                            return json.loads(text)
                        except (TypeError, json.JSONDecodeError):
                            return {"raw": text}
                    return {}
    except Exception as error:
        raise RuntimeError(
            f"无法连接或调用 MCP 服务（{settings.mcp_server_url}）：{error}"
        ) from error
