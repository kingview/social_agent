from __future__ import annotations

import asyncio
import atexit
import json
import threading
from dataclasses import dataclass
from typing import Any, Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class PluginHostError(RuntimeError):
    pass


@dataclass(frozen=True)
class PluginEndpoint:
    plugin_id: str
    version: str
    parameters: StdioServerParameters
    expected_tools: tuple[str, ...]

    @property
    def key(self) -> tuple[
        str,
        str,
        str,
        tuple[str, ...],
        str | None,
        tuple[tuple[str, str], ...],
    ]:
        return (
            self.plugin_id,
            self.version,
            self.parameters.command,
            tuple(self.parameters.args),
            self.parameters.cwd,
            tuple(sorted((self.parameters.env or {}).items())),
        )


@dataclass
class _Request:
    operation: Literal["call", "list"]
    result: asyncio.Future[Any]
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None


class _PluginWorker:
    """Owns one MCP process and handles every request in the task that opened it."""

    def __init__(self, endpoint: PluginEndpoint) -> None:
        self.endpoint = endpoint
        self.queue: asyncio.Queue[_Request | None] = asyncio.Queue()
        self.ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._run())

    async def request(
        self,
        operation: Literal["call", "list"],
        *,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        await self.ready
        result: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self.queue.put(
            _Request(
                operation=operation,
                result=result,
                tool_name=tool_name,
                arguments=arguments,
            )
        )
        return await result

    async def close(self) -> None:
        if not self.task.done():
            await self.queue.put(None)
        try:
            await self.task
        except Exception:
            pass

    async def _run(self) -> None:
        active_request: _Request | None = None
        try:
            async with stdio_client(self.endpoint.parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    actual_names = tuple(sorted(tool.name for tool in listed.tools))
                    expected_names = tuple(sorted(self.endpoint.expected_tools))
                    if actual_names != expected_names:
                        raise PluginHostError(
                            f"plugin {self.endpoint.plugin_id} MCP tools do not match its "
                            f"allow-list; expected {expected_names}, got {actual_names}"
                        )
                    if not self.ready.done():
                        self.ready.set_result(None)
                    while True:
                        active_request = await self.queue.get()
                        if active_request is None:
                            return
                        try:
                            if active_request.operation == "list":
                                response = await session.list_tools()
                                value = [
                                    tool.model_dump(mode="json") for tool in response.tools
                                ]
                            else:
                                response = await session.call_tool(
                                    active_request.tool_name or "",
                                    active_request.arguments or {},
                                )
                                value = _decode_tool_result(
                                    response,
                                    active_request.tool_name or "plugin Tool",
                                )
                        except Exception as exc:
                            if not active_request.result.done():
                                active_request.result.set_exception(exc)
                        else:
                            if not active_request.result.done():
                                active_request.result.set_result(value)
                        finally:
                            active_request = None
        except BaseException as exc:
            error = PluginHostError(_exception_message(exc))
            if not self.ready.done():
                self.ready.set_exception(error)
            if active_request is not None and not active_request.result.done():
                active_request.result.set_exception(error)
            while not self.queue.empty():
                request = self.queue.get_nowait()
                if request is not None and not request.result.done():
                    request.result.set_exception(error)


class PluginHost:
    """A process-wide background event loop that keeps plugin MCP servers warm."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._workers: dict[tuple[Any, ...], _PluginWorker] = {}
        self._thread = threading.Thread(
            target=self._run_loop,
            name="social-agent-plugin-host",
            daemon=True,
        )
        self._thread.start()

    async def call(
        self,
        endpoint: PluginEndpoint,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        return await self._submit(
            self._request(endpoint, "call", tool_name=tool_name, arguments=arguments)
        )

    async def list_tools(self, endpoint: PluginEndpoint) -> list[dict[str, Any]]:
        return await self._submit(self._request(endpoint, "list"))

    def close(self) -> None:
        if not self._loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(self._close_workers(), self._loop)
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def invalidate(self, plugin_id: str) -> None:
        if not self._loop.is_running():
            return
        future = asyncio.run_coroutine_threadsafe(
            self._invalidate(plugin_id), self._loop
        )
        try:
            future.result(timeout=5)
        except Exception:
            pass

    async def _submit(self, coroutine: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return await asyncio.wrap_future(future)

    async def _request(
        self,
        endpoint: PluginEndpoint,
        operation: Literal["call", "list"],
        *,
        tool_name: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        worker = self._workers.get(endpoint.key)
        if worker is None or worker.task.done():
            await self._retire_plugin_versions(endpoint)
            worker = _PluginWorker(endpoint)
            self._workers[endpoint.key] = worker
        return await worker.request(
            operation,
            tool_name=tool_name,
            arguments=arguments,
        )

    async def _retire_plugin_versions(self, endpoint: PluginEndpoint) -> None:
        stale = [
            key
            for key in self._workers
            if key[0] == endpoint.plugin_id and key != endpoint.key
        ]
        for key in stale:
            worker = self._workers.pop(key)
            await worker.close()

    async def _close_workers(self) -> None:
        workers = list(self._workers.values())
        self._workers.clear()
        await asyncio.gather(*(worker.close() for worker in workers), return_exceptions=True)

    async def _invalidate(self, plugin_id: str) -> None:
        keys = [key for key in self._workers if key[0] == plugin_id]
        workers = [self._workers.pop(key) for key in keys]
        await asyncio.gather(*(worker.close() for worker in workers), return_exceptions=True)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        self._loop.close()


def _decode_tool_result(result: Any, tool_name: str) -> Any:
    if result.isError:
        message = "\n".join(
            str(getattr(item, "text", "")) for item in result.content
        ).strip()
        raise PluginHostError(message or f"plugin Tool failed: {tool_name}")
    if result.structuredContent is not None:
        return result.structuredContent
    for item in result.content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            try:
                return json.loads(text)
            except ValueError:
                continue
    return {"content": [item.model_dump(mode="json") for item in result.content]}


def _exception_message(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", None)
    if nested:
        messages = [_exception_message(item) for item in nested]
        return "; ".join(message for message in messages if message)
    return str(exc).strip() or type(exc).__name__


_default_host: PluginHost | None = None
_default_host_lock = threading.Lock()


def default_plugin_host() -> PluginHost:
    global _default_host
    with _default_host_lock:
        if _default_host is None:
            _default_host = PluginHost()
            atexit.register(_default_host.close)
        return _default_host


def invalidate_default_plugin(plugin_id: str) -> None:
    with _default_host_lock:
        host = _default_host
    if host is not None:
        host.invalidate(plugin_id)
