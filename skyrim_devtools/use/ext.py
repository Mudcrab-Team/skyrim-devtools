"""Methods registered by other plugins as MCP tools, and argument checking driven by tool schemas.

An extension plugin publishes methods with a JSON Schema for their params. Each becomes a tool whose input schema
is that schema, so the agent sees the plugin's own documentation. The same mechanism checks the arguments of every
tool: FastMCP drops keys that a tool does not declare, which turns a misspelt argument into a silent default.
Here the unknown keys are reported in a `warnings` entry on the reply (or refused in strict mode).
"""

import inspect
import json
import keyword
import re
from typing import Any

from .manager import GameError

IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
EXTRA_TIMEOUT_S = 5.0


class ArgumentError(GameError):
    """Strict mode: a call carried keys the tool does not accept."""


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def accepted_keys(schema):
    """The top-level keys a schema declares, or None when it does not restrict them."""
    if not isinstance(schema, dict) or "properties" not in schema or not isinstance(schema["properties"], dict):
        return None
    extra = schema.get("additionalProperties", False)
    if extra is True or isinstance(extra, dict):
        return None
    return list(schema["properties"])


def check_args(schema, arguments, strict=False, label="the call"):
    """Split `arguments` into the accepted ones and warnings about the unknown top-level keys."""
    arguments = dict(arguments or {})
    accepted = accepted_keys(schema)
    if accepted is None:
        return arguments, []
    unknown = [key for key in arguments if key not in accepted]
    if not unknown:
        return arguments, []
    names = ", ".join(repr(key) for key in unknown)
    listing = ", ".join(accepted) if accepted else "none"
    message = f"{label}: unknown argument(s) {names} ignored; accepted keys: {listing}"
    if strict:
        raise ArgumentError(f"{label}: unknown argument(s) {names}; accepted keys: {listing}")
    clean = {key: value for key, value in arguments.items() if key in accepted}
    return clean, [{"code": "unknownArguments", "message": message, "unknown": unknown, "accepted": accepted}]


def attach_warnings(result, warnings):
    """Add warnings to a tool result without changing its shape for callers (same convention as alerts)."""
    if not warnings:
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            parsed["warnings"] = [*parsed.get("warnings", []), *warnings] if isinstance(parsed.get("warnings"), list) else warnings
            return dumps(parsed)
        return result + "\nwarnings: " + dumps(warnings)
    extra = "warnings: " + dumps(warnings)
    if isinstance(result, (list, tuple)):
        return [*result, extra]
    return [result, extra]


def snake(text):
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()


def tool_name(method, taken):
    """`HelloExt.playerName` becomes hello_ext_player_name; a clash with a curated tool gets an ext_ prefix."""
    name = snake(method)
    return f"ext_{name}" if name in taken else name


def install_arg_checking(mcp, strict, on_context=None):
    """Check every tool call against the tool's own input schema. `strict` is a callable giving the current mode."""
    manager = mcp._tool_manager
    original = manager.call_tool

    async def call_tool(name, arguments, context=None, convert_result=False):
        tool = manager.get_tool(name)
        if tool is None:
            return await original(name, arguments, context=context, convert_result=convert_result)
        if on_context is not None:
            on_context(context)
        clean, warnings = check_args(tool.parameters, arguments, strict(), label=f"tool {name}")
        result = await original(name, clean, context=context, convert_result=False)
        result = attach_warnings(result, warnings)
        return tool.fn_metadata.convert_result(result) if convert_result else result

    manager.call_tool = call_tool


def argument_signature(schema):
    """A call signature that mirrors the schema's top-level properties, or None when they cannot be Python names."""
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return None
    required = set(schema.get("required") or [])
    names = list(properties)
    if any(not IDENT.fullmatch(key) or keyword.iskeyword(key) for key in names):
        return None
    parameters = [inspect.Parameter(key, inspect.Parameter.KEYWORD_ONLY, annotation=Any,
                                    default=inspect.Parameter.empty if key in required else None) for key in names]
    return inspect.Signature(parameters, return_annotation=str)


class ExtTools:
    """Keeps the MCP tool list in step with the methods other plugins have registered."""

    def __init__(self, mcp, manager):
        self.mcp = mcp
        self.manager = manager
        self.tools = {}  # method name -> (tool name, fingerprint)
        self.session = None

    def remember(self, context):
        try:
            self.session = context.session
        except (AttributeError, ValueError):
            pass

    def wanted(self):
        if not self.manager.alive:
            return {}
        return {name: m for name, m in self.manager.methods.items() if m.get("owner")}

    def sync(self):
        """Add, replace and remove tools so they match manager.methods; True when the list changed."""
        wanted = self.wanted()
        changed = False
        for method in [m for m in self.tools if m not in wanted]:
            self.mcp.remove_tool(self.tools.pop(method)[0])
            changed = True
        taken = {tool.name for tool in self.mcp._tool_manager.list_tools()} - {t[0] for t in self.tools.values()}
        for name, info in wanted.items():
            fingerprint = dumps([info.get("description"), info.get("schema"), info.get("timeoutMs")])
            current = self.tools.get(name)
            if current and current[1] == fingerprint:
                continue
            if current:
                self.mcp.remove_tool(current[0])
            tool = tool_name(name, taken)
            taken.add(tool)
            self._add(tool, name, info)
            self.tools[name] = (tool, fingerprint)
            changed = True
        if changed:
            self._notify()
        return changed

    def _add(self, tool, method, info):
        schema = info.get("schema") if isinstance(info.get("schema"), dict) else None
        signature = argument_signature(schema) if schema else None
        timeout = (info.get("timeoutMs") or 0) / 1000.0
        manager = self.manager

        async def call(**kwargs):
            params = kwargs.get("arguments") if signature is None and schema else kwargs
            params = {key: value for key, value in (params or {}).items() if value is not None}
            return dumps(await manager.call(method, timeout=timeout + EXTRA_TIMEOUT_S if timeout else None, **params))

        if signature is not None:
            call.__signature__ = signature
        elif schema:
            # property names that are not Python identifiers travel in one object
            call.__signature__ = inspect.Signature(
                [inspect.Parameter("arguments", inspect.Parameter.KEYWORD_ONLY, annotation=dict)], return_annotation=str)
        else:
            call.__signature__ = inspect.Signature(
                [inspect.Parameter("arguments", inspect.Parameter.KEYWORD_ONLY, annotation=dict | None, default=None)],
                return_annotation=str)
        call.__name__ = tool
        description = (info.get("description") or method).strip()
        description += f"\n\nExtension method {method} registered by plugin {info.get('owner')}."
        if signature is None and schema:
            description += f" Pass the parameters as one object in `arguments`; schema: {dumps(schema)}"
        self.mcp.tool(name=tool, description=description)(call)
        if signature is not None:
            registered = self.mcp._tool_manager.get_tool(tool)
            # the plugin's own schema, with its descriptions and types, replaces the permissive one derived from the signature
            registered.parameters = {"type": "object", **{k: v for k, v in schema.items() if k != "$schema"}}

    def _notify(self):
        session = self.session
        if session is None:
            return
        import asyncio

        try:
            task = asyncio.get_running_loop().create_task(session.send_tool_list_changed())
        except RuntimeError:
            return
        task.add_done_callback(lambda done: done.exception())
