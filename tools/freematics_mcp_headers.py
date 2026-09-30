#!/usr/bin/env python3
"""Codex HTTP headers helper for the locally stored Freematics MCP token."""

import json
from pathlib import Path

token_path = Path.home() / ".config/freematics/mcp-token"
token = token_path.read_text().strip()
if len(token) < 32:
    raise SystemExit("Freematics MCP token is missing or too short")
print(json.dumps({"Authorization": f"Bearer {token}"}))
