"""Verify real MCP discovery/calls against the local KaloData mock."""

import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from kalodata.mcp_server import QUERY_COMMANDS, create_server


def test_discovery_and_rank(env, capsys):
    env.responses["/tiktok/product/rank"] = {
        "success": True, "data": [{"product_id": "123", "revenue": "12.5"}]
    }

    async def check():
        async with Client(create_server()) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(tools) == set(QUERY_COMMANDS)
            assert not any(name.startswith("config_") for name in tools)
            assert tools["product_detail"].input_schema["required"] == ["ids"]
            assert tools["product_rank"].input_schema["additionalProperties"] is False
            assert "json" not in tools["product_rank"].input_schema["properties"]
            limit = tools["product_rank"].input_schema["properties"]["limit"]
            assert (limit["minimum"], limit["maximum"]) == (5, 100)
            result = await client.call_tool("product_rank", {
                "region": "GB", "range": "last7Day", "category": ["42"], "limit": 5,
            })
            assert not result.is_error
            assert result.structured_content == {"data": [{"product_id": "123", "revenue": 12.5}]}
            assert json.loads(result.content[0].text) == result.structured_content

    anyio.run(check)
    assert capsys.readouterr().out == ""
    path, headers, body = env.requests[0]
    assert path == "/tiktok/product/rank"
    assert headers["secret-key"] == "test-key"
    assert body["region"] == "GB" and body["date_range"] == "last7Day"
    assert body["category_ids"] == ["42"]
    assert body["page_size"] == 5


@pytest.mark.parametrize("arguments", [
    {"limit": "five"}, {"unknown": True}, {"region": "INVALID"},
    {"affiliate": True, "no_affiliate": True},
    {"limit": 1}, {"limit": 101}, {"page": 0}, {"page": 6},
])
def test_invalid_arguments_do_not_query(env, arguments):
    async def check():
        async with Client(create_server()) as client:
            result = await client.call_tool("product_rank", arguments)
            assert result.is_error

    anyio.run(check)
    assert env.requests == []


def test_picsearch_tool(env, image_file, capsys):
    env.responses["/tiktok/product/picsearch/one-step"] = {
        "success": True, "data": [{"product_id": "123", "revenue": "12.5"}],
    }

    async def check():
        async with Client(create_server()) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert tools["product_picsearch"].input_schema["required"] == ["image_path"]
            result = await client.call_tool("product_picsearch", {"image_path": str(image_file)})
            assert not result.is_error
            assert result.structured_content == {"data": [{"product_id": "123", "revenue": 12.5}]}

    anyio.run(check)
    assert capsys.readouterr().out == ""
    path, headers, body = env.requests[0]
    assert path == "/tiktok/product/picsearch/one-step"
    assert headers["x-api-key"] == "test-key"
    assert "secret-key" not in headers
    assert "x-user-id" not in headers
    assert body["pic"]["content"] == image_file.read_bytes()


def test_batch_partial_failure_and_total_failure(env):
    env.responses["/tiktok/product/detail"] = lambda body: (
        {"success": True, "data": {"product_id": "1"}}
        if body["product_id"] == "1" else {"success": False, "code": "400", "message": "missing"}
    )

    async def check():
        async with Client(create_server()) as client:
            result = await client.call_tool("product_detail", {"ids": ["1", "2"]})
            assert not result.is_error
            rows = result.structured_content["data"]
            assert rows[0] == {"id": "1", "data": {"product_id": "1"}}
            assert "error" in rows[1]
            failed = await client.call_tool("product_detail", {"ids": ["2"]})
            assert failed.is_error

    anyio.run(check)


def test_missing_key_is_actionable(env, monkeypatch):
    monkeypatch.delenv("KALODATA_API_KEY")

    async def check():
        async with Client(create_server()) as client:
            result = await client.call_tool("credit_balance", {})
            assert result.is_error
            assert result.structured_content["kind"] == "auth"
            assert result.structured_content["help"]

    anyio.run(check)
    assert env.requests == []


def test_rejected_key_is_auth_error(env):
    env.responses["/credit/balance"] = {
        "success": False, "code": "501", "message": "The key is not allowed",
    }

    async def check():
        async with Client(create_server()) as client:
            result = await client.call_tool("credit_balance", {})
            assert result.is_error
            assert result.structured_content["kind"] == "auth"
            assert result.structured_content["help"]

    anyio.run(check)


def test_concurrent_calls_keep_results_separate(env):
    env.responses["/tiktok/product/rank"] = {
        "success": True, "data": [{"product_id": "1"}]
    }
    env.responses["/credit/balance"] = {"success": True, "data": {"totalRemain": 42}}

    async def check():
        async with Client(create_server()) as client:
            results = {}

            async def call(name):
                results[name] = (await client.call_tool(name, {})).structured_content

            async with anyio.create_task_group() as group:
                group.start_soon(call, "product_rank")
                group.start_soon(call, "credit_balance")
            assert results == {
                "product_rank": {"data": [{"product_id": "1"}]},
                "credit_balance": {"data": {"totalRemain": 42}},
            }

    anyio.run(check)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_real_stdio_process(env, mode):
    env.responses["/credit/balance"] = {"success": True, "data": {"totalRemain": 7}}
    scripts = Path(__file__).resolve().parents[1] / "skills" / "kalodata" / "scripts"
    process_env = dict(os.environ, PYTHONPATH=str(scripts))

    async def check():
        async with Client(StdioServerParameters(
            command=sys.executable, args=["-m", "kalodata.mcp_server"], env=process_env,
        ), mode=mode) as client:
            assert len((await client.list_tools()).tools) == len(QUERY_COMMANDS)
            result = await client.call_tool("credit_balance", {})
            assert not result.is_error
            assert result.structured_content == {"data": {"totalRemain": 7}}

    anyio.run(check)
