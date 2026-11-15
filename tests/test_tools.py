"""Smoke tests: the MCP server registers all tools with usable descriptions.

Runs without a Flipper attached (the device connection is opened lazily).
"""

import flipper_mcp.server as server


def _tools():
    # FastMCP exposes registered tools via the tool manager.
    return server.mcp._tool_manager.list_tools()


def test_registers_exactly_41_tools():
    assert len(_tools()) == 41


def test_every_tool_has_name_and_nonempty_description():
    for t in _tools():
        assert t.name, "a tool is missing a name"
        assert t.description and len(t.description.strip()) >= 20, (
            f"tool {t.name!r} has a too-short/empty description"
        )


def test_expected_core_tools_present():
    names = {t.name for t in _tools()}
    for expected in {
        "flipper_status", "flipper_connect", "flipper_device_info",
        "flipper_screenshot", "flipper_press", "flipper_cli",
        "fap_scaffold", "fap_build", "fap_deploy",
        "flipper_read_screen", "flipper_keys", "flipper_select", "flipper_goto",
    }:
        assert expected in names, f"missing tool {expected}"
