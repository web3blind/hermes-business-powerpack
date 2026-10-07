#!/usr/bin/env python3
"""Controlled child fixture, NOT an LLM or a live Hermes execution.

Uses the installed host's real StreamJsonEmitter and an observable deterministic
local tool action. Records argv/stdin for entrypoint-to-transport assertions.
"""
import json
import os
from pathlib import Path
import sys

home = Path(os.environ["HERMES_HOME"])
query = sys.stdin.read()
(home / "fixture-call.json").write_text(json.dumps({"argv": sys.argv[1:], "query": query,
                                                    "env_keys": sorted(os.environ)}))
# Actual local side effect stands in for a model-selected tool, no network/model.
(home / "fixture-tool.txt").write_text("tool ran")
from hermes_cli.stream_json import StreamJsonEmitter
emitter = StreamJsonEmitter(model="test-fixture", session_id="fixture-session")
emitter.on_tool_progress("fixture_tool", "tool.started", args={"secret": "DO_NOT_SEND"})  # pragma: allowlist secret — non-secret test sentinel
emitter.on_text_delta("DO_NOT_SEND intermediate")
emitter.emit_result({"final_response": "Готово: инструмент выполнен."})
