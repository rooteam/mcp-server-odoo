# Rooteam fork

This is Rooteam's fork of [ivnvxd/mcp-server-odoo](https://github.com/ivnvxd/mcp-server-odoo).
We do not contribute it upstream; we pull upstream into it.

## What the fork adds

| Tool | What it does |
|---|---|
| `execute_method` | Calls a public model method (`action_confirm`, `action_post`, `button_validate`, ...). On by default. Uses upstream's `call_model_method` guardrails: no `ir.cron`/`ir.actions.*`, no `run`/`method_direct_trigger`, no `web_*`, no ORM primitives such as `create`/`read`/`copy` (use the dedicated tools). |
| `create_artifact` | Attaches a file to the `ai.conversation` in `ODOO_CONVERSATION_ID`. Only works when `rt_ai_chat` starts the server. |

Everything else is upstream. The fork code is in `mcp_server_odoo/rooteam_tools.py`
and is hooked in by one line in `server.py`.

## Install

Pin a tag so every machine runs the same build:

```json
"command": "uvx",
"args": [
  "--from", "git+https://github.com/rooteam/mcp-server-odoo@v0.9.0-rooteam.1",
  "mcp-server-odoo"
]
```

Plain `uvx mcp-server-odoo` installs upstream from PyPI, without the Rooteam tools.

## Syncing with upstream

```bash
git remote add upstream https://github.com/ivnvxd/mcp-server-odoo.git
git fetch upstream --tags
git merge upstream/main
```

Conflicts should only come up in `server.py`, the version lines (`pyproject.toml`,
`mcp_server_odoo/__init__.py`, `uv.lock`) and `CHANGELOG.md`. Set the version to
`<upstream version>+rooteam.<n>`, run `uv run pytest -m "not yolo and not mcp"`,
then tag `v<upstream version>-rooteam.<n>`. Do not publish a GitHub Release: the
Release event starts the PyPI and Docker workflows.
