import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp import StdioServerParameters
from social_ops_agent.plugin_host import PluginEndpoint, PluginHost


def test_same_version_different_conversation_environments_do_not_retire_each_other():
    def endpoint(model, version="1.0.0"):
        return PluginEndpoint(plugin_id="test.plugin", version=version, expected_tools=("test",),
            parameters=StdioServerParameters(command="python", args=["-m", "fixture"], env={"MODEL": model}))
    first, second = endpoint("first"), endpoint("second")
    first_worker, second_worker = SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock())
    host = SimpleNamespace(_workers={first.key: first_worker, second.key: second_worker})
    asyncio.run(PluginHost._retire_plugin_versions(host, second))
    assert len(host._workers) == 2
    first_worker.close.assert_not_called()
    second_worker.close.assert_not_called()
    asyncio.run(PluginHost._retire_plugin_versions(host, endpoint("new", "2.0.0")))
    assert host._workers == {}
    first_worker.close.assert_awaited_once()
    second_worker.close.assert_awaited_once()
