"""Local stdio MCP adapter over the CLI's query registry.

The CLI never imports the SDK, so the skill scripts still run on the standard library.
"""

from __future__ import annotations

import json
import sys
from typing import Literal

from . import __version__, config, render
from .commands import COMMANDS
from .errors import KaloError, UsageError

# Configuration is controlled by the operator, never by model-supplied arguments.
QUERY_COMMANDS = {c.path.replace(" ", "_"): c for c in COMMANDS if not c.path.startswith("config ")}
# These flags affect terminal presentation only; MCP returns the CLI's raw JSON data.
PRESENTATION_FLAGS = {"json", "fields", "full", "comments"}


def create_server():
    import anyio
    from mcp import types
    from mcp.server import Server
    from pydantic import ConfigDict, Field, ValidationError, create_model

    models = {}
    tools = []
    for name, cmd in QUERY_COMMANDS.items():
        fields = {}
        if cmd.positional:
            if cmd.pos_max == 1:
                fields[cmd.positional] = (str, Field(min_length=1, description=cmd.positional))
            else:
                fields["ids"] = (
                    list[str],
                    Field(min_length=cmd.pos_min, max_length=100,
                          description=f"One to 100 {cmd.positional} values; keep IDs as strings"),
                )
        for flag in cmd.flags:
            if flag.dest in PRESENTATION_FLAGS:
                continue
            annotation = {"str": str, "int": int, "flag": bool, "multi": list[str]}[flag.kind]
            if flag.choices:
                annotation = Literal[flag.choices]
            if flag.default is None:
                annotation = annotation | None
            fields[flag.dest] = (annotation, Field(default=flag.default, description=flag.help,
                                                   ge=flag.min, le=flag.max))
        model = create_model(
            f"{name}Arguments", __config__=ConfigDict(extra="forbid", strict=True), **fields
        )
        models[name] = model
        description = cmd.summary
        if cmd.path.endswith((" images", " url")):
            description += " Signed media URLs expire in approximately five minutes."
        tools.append(types.Tool(
            name=name, description=description, input_schema=model.model_json_schema(),
            annotations=types.ToolAnnotations(
                read_only_hint=True, destructive_hint=False,
                idempotent_hint=False, open_world_hint=True,
            ),
        ))

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=tools)

    def execute(name, arguments):
        cmd = QUERY_COMMANDS[name]
        opts = {flag.dest: list(flag.default) if isinstance(flag.default, list) else flag.default
                for flag in cmd.flags}
        arguments = dict(arguments)
        args = []
        if cmd.positional:
            args = ([arguments.pop(cmd.positional)] if cmd.pos_max == 1
                    else arguments.pop("ids"))
            if any(not entity_id.strip() for entity_id in args):
                raise UsageError("IDs must not be empty")
        opts.update(arguments)
        opts["json"] = True
        with render.collect_results() as results:
            status = cmd.handler(config.load(), opts, args)
        if len(results) != 1:
            raise RuntimeError(f"{name} did not produce one structured result")
        return {"data": results[0]}, status != 0

    async def call_tool(ctx, params):
        try:
            if params.name not in models:
                raise UsageError(f"unknown tool '{params.name}'")
            arguments = models[params.name].model_validate(params.arguments or {}).model_dump()
            # urllib requests and retries block; run them outside the protocol event loop.
            payload, is_error = await anyio.to_thread.run_sync(execute, params.name, arguments)
        except ValidationError as exc:
            payload = {"error": "Invalid tool arguments", "kind": "parameter",
                       "details": exc.errors(include_input=False, include_url=False)}
            is_error = True
        except (KaloError, UsageError) as exc:
            payload = {"error": str(exc), "kind": getattr(exc, "kind", "parameter"),
                       "help": exc.help_lines}
            is_error = True
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
            structured_content=payload, is_error=is_error,
        )

    return Server(
        "kalodata", version=__version__,
        instructions="KaloData TikTok Shop analytics. IDs are strings. Credentials come from "
                     "the local environment/configuration. Results contain raw upstream JSON data. "
                     "Use credit_balance only when the user asks about balance or a call fails "
                     "for insufficient credits.",
        on_list_tools=list_tools, on_call_tool=call_tool,
        get_tool_input_schema=lambda name: models[name].model_json_schema() if name in models else None,
    )


async def serve():
    from mcp.server.stdio import stdio_server

    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def entry() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="KaloData local stdio MCP server")
    parser.add_argument("--version", action="version", version=f"kalodata-mcp {__version__}")
    parser.parse_args()
    try:
        import anyio
        import mcp  # noqa: F401
    except ImportError:
        print("MCP dependencies missing. Install with: "
              'uv pip install "git+https://github.com/kalowave-hz/kalodata-skill"',
              file=sys.stderr)
        raise SystemExit(1)
    try:
        anyio.run(serve)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    entry()
