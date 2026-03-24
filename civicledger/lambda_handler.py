"""AWS Lambda handler for CivicLedger MCP server.

Serves the MCP server over HTTP via Lambda Function URL with response streaming.
Also handles EventBridge scheduled invocations for data refresh.

Architecture:
  - Lambda Function URL (streaming) -> this handler -> MCP SSE transport
  - EventBridge rule -> this handler -> CLI refresh commands
  - GET /health -> health check
  - GET /sse -> SSE endpoint for MCP session initialization
  - POST /message -> MCP message endpoint (JSON-RPC over HTTP)

Lambda Function URL provides the HTTP layer; this handler translates between
Lambda's event format and the MCP server's ASGI interface.
"""

import asyncio
import json
import traceback
from datetime import date, timedelta

from loguru import logger


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def _health_response():
    """Return a health check response."""
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "status": "ok",
            "version": "0.1.0",
            "runtime": "aws-lambda",
            "sources": ["SEC EDGAR", "FRED", "Senate eFD", "House Clerk"],
        }),
    }


# ---------------------------------------------------------------------------
# EventBridge scheduled refresh
# ---------------------------------------------------------------------------

async def _run_refresh(command: str):
    """Run a CLI refresh command inside Lambda.

    Args:
        command: One of "fundamentals", "all", etc.
    """
    from civicledger.cli import (
        _refresh_all,
        _refresh_fundamentals,
    )

    today = date.today()
    from_date = (today - timedelta(days=7)).isoformat()
    to_date = today.isoformat()
    year = today.year

    if command == "fundamentals":
        await _refresh_fundamentals()
    elif command == "all":
        await _refresh_all(from_date, to_date, year)
    else:
        logger.warning(f"Unknown refresh command: {command}")


def _handle_eventbridge(event):
    """Handle EventBridge scheduled events for data refresh."""
    detail = event.get("detail", {})
    command = detail.get("command", "all")

    logger.info(f"EventBridge refresh triggered: command={command}")

    try:
        asyncio.run(_run_refresh(command))
        return {
            "statusCode": 200,
            "body": json.dumps({"status": "ok", "command": command}),
        }
    except Exception as e:
        logger.error(f"Refresh failed: {e}\n{traceback.format_exc()}")
        return {
            "statusCode": 500,
            "body": json.dumps({"status": "error", "error": str(e)}),
        }


# ---------------------------------------------------------------------------
# MCP over HTTP (Lambda Function URL)
# ---------------------------------------------------------------------------

def _mcp_sse_response():
    """Return SSE session initialization.

    MCP SSE transport expects:
    1. Client connects to GET /sse
    2. Server sends an SSE event with the message endpoint URL
    3. Client POSTs JSON-RPC messages to that endpoint

    For Lambda Function URL, we simplify: return the message endpoint
    so the client knows where to POST.
    """
    # Lambda Function URL base is in the request context,
    # but for SSE we return a well-known path.
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
        "body": "event: endpoint\ndata: /message\n\n",
    }


async def _handle_mcp_message(body: str):
    """Process a JSON-RPC MCP message and return the response.

    This runs the MCP server's tool dispatch inline (not as a long-running
    server) -- each Lambda invocation handles one request/response cycle.
    """
    from civicledger.mcp_server import mcp

    try:
        request = json.loads(body)
    except json.JSONDecodeError as e:
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": f"Parse error: {e}"},
                "id": None,
            }),
        }

    method = request.get("method", "")
    params = request.get("params", {})
    request_id = request.get("id")

    logger.info(f"MCP request: method={method}, id={request_id}")

    try:
        if method == "initialize":
            # MCP initialization -- return server capabilities
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                },
                "serverInfo": {
                    "name": "CivicLedger",
                    "version": "0.1.0",
                },
                "instructions": (
                    "US financial intelligence from public domain sources. "
                    "SEC EDGAR filings, FRED economic calendar, and congressional stock trades."
                ),
            }

        elif method == "tools/list":
            # List available tools by inspecting the FastMCP server
            tools = []
            for tool in mcp._tool_manager.list_tools():
                tool_schema = {
                    "name": tool.name,
                    "description": tool.description or "",
                    "inputSchema": tool.parameters if hasattr(tool, "parameters") else {"type": "object", "properties": {}},
                }
                tools.append(tool_schema)

            result = {"tools": tools}

        elif method == "tools/call":
            # Execute a tool
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})

            logger.info(f"Calling tool: {tool_name} with args: {json.dumps(tool_args)[:200]}")

            # Use the FastMCP server's tool execution
            tool_result = await mcp._tool_manager.call_tool(tool_name, tool_args)

            # Tool results come back as string or list of content items
            if isinstance(tool_result, str):
                content = [{"type": "text", "text": tool_result}]
            elif isinstance(tool_result, list):
                content = []
                for item in tool_result:
                    if hasattr(item, "text"):
                        content.append({"type": "text", "text": item.text})
                    elif hasattr(item, "model_dump"):
                        content.append(item.model_dump())
                    else:
                        content.append({"type": "text", "text": str(item)})
            else:
                content = [{"type": "text", "text": str(tool_result)}]

            result = {"content": content, "isError": False}

        elif method == "notifications/initialized":
            # Client notification -- no response needed for notifications
            return {
                "statusCode": 204,
                "headers": {"Content-Type": "application/json"},
                "body": "",
            }

        elif method == "ping":
            result = {}

        else:
            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": request_id,
                }),
            }

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "jsonrpc": "2.0",
                "result": result,
                "id": request_id,
            }, default=str),
        }

    except Exception as e:
        logger.error(f"MCP error: {e}\n{traceback.format_exc()}")
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": f"Internal error: {e}"},
                "id": request_id,
            }),
        }


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------

def handler(event, context):
    """AWS Lambda handler.

    Dispatches to:
    1. EventBridge scheduled events -> data refresh
    2. Lambda Function URL requests -> MCP protocol / health checks
    """
    # EventBridge scheduled events have a "source" field
    if event.get("source") in ("aws.events", "aws.scheduler"):
        return _handle_eventbridge(event)

    # Lambda Function URL events
    request_context = event.get("requestContext", {})
    http = request_context.get("http", {})
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")

    logger.info(f"HTTP {method} {path}")

    # Health check
    if path == "/health" or path == "/":
        return _health_response()

    # MCP SSE endpoint
    if path == "/sse" and method == "GET":
        return _mcp_sse_response()

    # MCP message endpoint
    if path == "/message" and method == "POST":
        body = event.get("body", "")
        # Lambda Function URL may base64 encode the body
        if event.get("isBase64Encoded", False):
            import base64
            body = base64.b64decode(body).decode("utf-8")

        return asyncio.run(_handle_mcp_message(body))

    # Unknown path
    return {
        "statusCode": 404,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": "Not found", "path": path}),
    }
