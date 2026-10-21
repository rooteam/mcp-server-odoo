"""Rooteam-specific MCP tools.

Kept out of tools.py so that syncing this fork with upstream
(ivnvxd/mcp-server-odoo) does not conflict: the only upstream file touched
is server.py, which calls register_rooteam_tools() after register_tools().

- execute_method: generic model-method execution, on by default. It runs
  through upstream's call_model_method handler (same guardrails, JSON-2
  support and result truncation) and is skipped when upstream's own
  call_model_method is enabled, so the two never appear together.
- create_artifact: attach a file to the rt_ai_chat conversation
  (ai.conversation) named by ODOO_CONVERSATION_ID.
"""

import asyncio
import base64
import binascii
import os
from typing import Any, Dict, Optional

from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from .binary_reads import uses_odoo_20_binaries
from .error_handling import ValidationError
from .error_sanitizer import ErrorSanitizer
from .logging_config import get_logger
from .schemas import CallModelMethodResult
from .tools import ArgumentList, KeywordArguments, OdooToolHandler

logger = get_logger(__name__)

# Set by rt_ai_chat when it starts this server for a conversation
CONVERSATION_ID_ENV = "ODOO_CONVERSATION_ID"
CONVERSATION_MODEL = "ai.conversation"


def _artifact_base64(content: str) -> str:
    """Return content as base64, encoding it only when it is not already.

    The model may send base64 for a binary file or plain text for any file
    (including text it produced for a "binary" type), so every file type is
    checked rather than trusting file_type.
    """
    clean = "".join(content.split())
    if clean and len(clean) % 4 == 0:
        try:
            base64.b64decode(clean, validate=True)
            return clean
        except (binascii.Error, ValueError):
            pass
    return base64.b64encode(content.encode("utf-8")).decode("ascii")


class RooteamToolHandler:
    """Registers the Rooteam tools on top of the upstream tool handler."""

    def __init__(self, app: MCPServer, tool_handler: OdooToolHandler):
        self.app = app
        self.tool_handler = tool_handler
        self.connection = tool_handler.connection
        self.config = tool_handler.config
        self._register_tools()

    def _register_tools(self):
        config = self.config

        if not (config.is_write_allowed and config.enable_method_calls):

            @self.app.tool(
                title="Execute Method",
                annotations=ToolAnnotations(
                    read_only_hint=False,
                    destructive_hint=True,
                    idempotent_hint=False,
                    open_world_hint=True,
                ),
            )
            async def execute_method(
                model: str,
                method: str,
                args: ArgumentList = None,
                kwargs: KeywordArguments = None,
                ctx: Optional[Context] = None,
            ) -> CallModelMethodResult:
                """Execute a public method on an Odoo model.

                For workflow actions the CRUD tools do not cover: confirming a
                sale order (sale.order.action_confirm), posting an invoice
                (account.move.action_post), validating a picking
                (stock.picking.button_validate), and so on. Odoo still applies
                the connected user's access rights and record rules.

                Refused: methods on ir.actions.* and ir.cron, run and
                method_direct_trigger on any model, the web_* family, and ORM
                data-access primitives (create, read, write, search_read,
                copy, fields_get, ...): use the dedicated tools for those.
                List results are truncated to 100 items.

                Args:
                    model: The Odoo model name (e.g., 'sale.order')
                    method: The method name (e.g., 'action_confirm')
                    args: Positional arguments. For recordset methods the
                        first one is the list of ids: [[42]] runs on id 42.
                        On Odoo 19 and later (JSON-2) it can hold only that
                        id list; pass every other argument in kwargs.
                    kwargs: Keyword arguments (e.g., {"context": {...}})

                Returns:
                    The method's return value in 'result'
                """
                result = await self.tool_handler._handle_call_model_method_tool(
                    model, method, args, kwargs, ctx
                )
                return CallModelMethodResult(**result)

        @self.app.tool(
            title="Create Artifact",
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=True,
            ),
        )
        async def create_artifact(
            name: str,
            content: str,
            file_type: str = "text/plain",
        ) -> Dict[str, Any]:
            """Create a file artifact in Odoo linked to the current AI conversation.

            Only works when the server was started by the Odoo AI chat
            (rt_ai_chat), which sets ODOO_CONVERSATION_ID.

            Args:
                name: The name of the file (e.g., "report.pdf", "data.xlsx")
                content: The content of the file. For binary files (pdf, xlsx,
                    etc.), this should be base64 encoded. For text files, it can
                    be plain text.
                file_type: The MIME type of the file (e.g., "application/pdf",
                    "text/csv")
            """
            return await self._handle_create_artifact_tool(name, content, file_type)

    async def _handle_create_artifact_tool(
        self, name: str, content: str, file_type: str
    ) -> Dict[str, Any]:
        """Handle create artifact tool request."""
        conversation_id = os.environ.get(CONVERSATION_ID_ENV, "").strip()
        if not conversation_id:
            raise ValidationError(
                f"{CONVERSATION_ID_ENV} is not set. This tool can only be used "
                "within an Odoo AI conversation."
            )
        if not conversation_id.isdigit():
            raise ValidationError(
                f"{CONVERSATION_ID_ENV} must be a record id, got {conversation_id!r}"
            )
        if not name or not name.strip():
            raise ValidationError("Provide a file name")
        if not isinstance(content, str):
            content = str(content)

        if not self.connection.is_authenticated:
            raise ValidationError("Not authenticated with Odoo")

        # Odoo 20 removed ir.attachment.datas; raw takes the same base64 string
        content_field = "raw" if uses_odoo_20_binaries(self.connection) else "datas"
        values = {
            "name": name,
            content_field: _artifact_base64(content),
            "mimetype": file_type,
            "res_model": CONVERSATION_MODEL,
            "res_id": int(conversation_id),
            "type": "binary",
        }
        try:
            attachment_id = await asyncio.to_thread(self.connection.create, "ir.attachment", values)
        except Exception as e:
            logger.error(f"Error in create_artifact tool: {e}")
            sanitized_msg = ErrorSanitizer.sanitize_message(str(e))
            raise ValidationError(f"Failed to create artifact: {sanitized_msg}") from e

        return {
            "success": True,
            "attachment_id": attachment_id,
            "message": f"Artifact created successfully with ID: {attachment_id}",
        }


def register_rooteam_tools(app: MCPServer, tool_handler: OdooToolHandler) -> RooteamToolHandler:
    """Register the Rooteam tools with the MCPServer app."""
    handler = RooteamToolHandler(app, tool_handler)
    logger.info("Registered Rooteam MCP tools")
    return handler
