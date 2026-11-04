#!/usr/bin/env python3
"""Emit OpenAPI schemas without starting workers or contacting dependencies."""

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["MCP_ENABLED"] = "false"
os.environ["HEALTH_PROBE_ON_STARTUP"] = "false"
from app.main import app  # noqa: E402

print(json.dumps(app.openapi()["components"]["schemas"]))