"""Tests for the Rooteam tools (execute_method, create_artifact)."""

import base64
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest
from mcp.server.mcpserver import MCPServer

from mcp_server_odoo.access_control import AccessController
from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnection
from mcp_server_odoo.rooteam_tools import _artifact_base64, register_rooteam_tools
from mcp_server_odoo.tools import OdooToolHandler

from .test_schema_rules import schema_violations


def _config(*, yolo_mode: str = "true", enable: bool = False) -> OdooConfig:
    return OdooConfig(
        url="http://localhost:8069",
        api_key="test_api_key",
        username="admin",
        yolo_mode=yolo_mode,
        enable_method_calls=enable,
    )


@pytest.fixture
def mock_app():
    app = MagicMock(spec=MCPServer)
    app._tools = {}

    def tool_decorator(**kwargs):
        def decorator(func):
            app._tools[func.__name__] = func
            return func

        return decorator

    app.tool = tool_decorator
    return app


@pytest.fixture
def mock_connection():
    connection = MagicMock(spec=OdooConnection)
    connection.is_authenticated = True
    connection.performance_manager = MagicMock()
    connection.get_major_version.return_value = 17
    return connection


@pytest.fixture
def mock_access_controller():
    return MagicMock(spec=AccessController)


def _register(app, connection, access_controller, config):
    handler = OdooToolHandler(app, connection, access_controller, config)
    register_rooteam_tools(app, handler)
    return handler


class TestRegistration:
    def test_both_tools_registered_by_default(
        self, mock_app, mock_connection, mock_access_controller
    ):
        _register(mock_app, mock_connection, mock_access_controller, _config())
        assert "execute_method" in mock_app._tools
        assert "create_artifact" in mock_app._tools
        assert "call_model_method" not in mock_app._tools

    def test_execute_method_yields_to_upstream_call_model_method(
        self, mock_app, mock_connection, mock_access_controller
    ):
        _register(mock_app, mock_connection, mock_access_controller, _config(enable=True))
        assert "call_model_method" in mock_app._tools
        assert "execute_method" not in mock_app._tools

    @pytest.mark.parametrize("yolo_mode", ["off", "read"])
    def test_execute_method_registered_outside_full_yolo(
        self, mock_app, mock_connection, mock_access_controller, yolo_mode
    ):
        _register(mock_app, mock_connection, mock_access_controller, _config(yolo_mode=yolo_mode))
        assert "execute_method" in mock_app._tools

    def test_old_tools_are_gone(self, mock_app, mock_connection, mock_access_controller):
        _register(mock_app, mock_connection, mock_access_controller, _config())
        assert "list_model_methods" not in mock_app._tools
        assert "plot_revenge" not in mock_app._tools


class TestExecuteMethod:
    @pytest.mark.asyncio
    async def test_calls_the_method(self, mock_app, mock_connection, mock_access_controller):
        _register(mock_app, mock_connection, mock_access_controller, _config())
        mock_connection.execute_kw.return_value = True

        result = await mock_app._tools["execute_method"](
            model="sale.order", method="action_confirm", args=[[7]]
        )

        assert result.success is True
        assert result.result is True
        mock_access_controller.validate_model_access.assert_called_once_with("sale.order", "write")
        mock_connection.execute_kw.assert_called_once_with(
            "sale.order", "action_confirm", [[7]], {}
        )

    @pytest.mark.asyncio
    async def test_passes_kwargs(self, mock_app, mock_connection, mock_access_controller):
        _register(mock_app, mock_connection, mock_access_controller, _config())
        mock_connection.execute_kw.return_value = None

        await mock_app._tools["execute_method"](
            model="stock.picking",
            method="button_validate",
            args=[[3]],
            kwargs={"context": {"skip_backorder": True}},
        )

        mock_connection.execute_kw.assert_called_once_with(
            "stock.picking", "button_validate", [[3]], {"context": {"skip_backorder": True}}
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model,method",
        [
            ("res.partner", "search_read"),
            ("res.partner", "create"),
            ("ir.cron", "method_direct_trigger"),
            ("ir.actions.server", "run"),
            ("res.partner", "web_search_read"),
            ("res.partner", "_compute_display_name"),
        ],
    )
    async def test_guardrails_apply(
        self, mock_app, mock_connection, mock_access_controller, model, method
    ):
        _register(mock_app, mock_connection, mock_access_controller, _config())

        with pytest.raises(ValidationError):
            await mock_app._tools["execute_method"](model=model, method=method)

        mock_connection.execute_kw.assert_not_called()


class TestCreateArtifact:
    @pytest.mark.asyncio
    async def test_needs_conversation_id(
        self, mock_app, mock_connection, mock_access_controller, monkeypatch
    ):
        monkeypatch.delenv("ODOO_CONVERSATION_ID", raising=False)
        _register(mock_app, mock_connection, mock_access_controller, _config())

        with pytest.raises(ValidationError, match="ODOO_CONVERSATION_ID"):
            await mock_app._tools["create_artifact"](name="a.txt", content="hi")

        mock_connection.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_rejects_non_numeric_conversation_id(
        self, mock_app, mock_connection, mock_access_controller, monkeypatch
    ):
        monkeypatch.setenv("ODOO_CONVERSATION_ID", "abc")
        _register(mock_app, mock_connection, mock_access_controller, _config())

        with pytest.raises(ValidationError, match="record id"):
            await mock_app._tools["create_artifact"](name="a.txt", content="hi")

    @pytest.mark.asyncio
    async def test_attaches_to_conversation(
        self, mock_app, mock_connection, mock_access_controller, monkeypatch
    ):
        monkeypatch.setenv("ODOO_CONVERSATION_ID", "12")
        _register(mock_app, mock_connection, mock_access_controller, _config())
        mock_connection.create.return_value = 99

        result = await mock_app._tools["create_artifact"](
            name="notes.csv", content="a,b\n1,2\n", file_type="text/csv"
        )

        assert result["attachment_id"] == 99
        model, values = mock_connection.create.call_args.args
        assert model == "ir.attachment"
        assert values["res_model"] == "ai.conversation"
        assert values["res_id"] == 12
        assert values["mimetype"] == "text/csv"
        assert base64.b64decode(values["datas"]) == b"a,b\n1,2\n"

    @pytest.mark.asyncio
    async def test_writes_raw_on_odoo_20(
        self, mock_app, mock_connection, mock_access_controller, monkeypatch
    ):
        monkeypatch.setenv("ODOO_CONVERSATION_ID", "12")
        mock_connection.get_major_version.return_value = 20
        _register(mock_app, mock_connection, mock_access_controller, _config())
        mock_connection.create.return_value = 5

        await mock_app._tools["create_artifact"](name="a.txt", content="hi")

        values = mock_connection.create.call_args.args[1]
        assert "raw" in values and "datas" not in values

    @pytest.mark.asyncio
    async def test_odoo_error_is_a_tool_error(
        self, mock_app, mock_connection, mock_access_controller, monkeypatch
    ):
        monkeypatch.setenv("ODOO_CONVERSATION_ID", "12")
        _register(mock_app, mock_connection, mock_access_controller, _config())
        mock_connection.create.side_effect = Exception("boom")

        with pytest.raises(ValidationError, match="Failed to create artifact"):
            await mock_app._tools["create_artifact"](name="a.txt", content="hi")


class TestArtifactBase64:
    def test_keeps_valid_base64(self):
        encoded = base64.b64encode(b"%PDF-1.7 binary").decode()
        assert _artifact_base64(encoded) == encoded

    def test_strips_whitespace_from_base64(self):
        encoded = base64.b64encode(b"x" * 90).decode()
        wrapped = encoded[:60] + "\n" + encoded[60:]
        assert _artifact_base64(wrapped) == encoded

    def test_encodes_plain_text(self):
        assert base64.b64decode(_artifact_base64("hello world")) == b"hello world"

    def test_encodes_empty_content(self):
        assert _artifact_base64("") == ""


async def test_rooteam_tool_schemas_pass_the_client_rules():
    app = MCPServer("rooteam-schema-rules")
    handler = OdooToolHandler(
        app, MagicMock(spec=OdooConnection), MagicMock(spec=AccessController), _config()
    )
    register_rooteam_tools(app, handler)
    schemas: Dict[str, Any] = {
        tool.name: tool.model_dump(by_alias=True)["inputSchema"] for tool in await app.list_tools()
    }

    assert {"execute_method", "create_artifact"} <= set(schemas)
    for name in ("execute_method", "create_artifact"):
        assert not schema_violations(schemas[name]), name
