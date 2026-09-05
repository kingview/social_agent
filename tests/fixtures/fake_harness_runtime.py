from __future__ import annotations

import json
import sys
import uuid
import os
import time


def emit(value: dict) -> None:
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    request = json.loads(raw_line)
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        emit({"jsonrpc": "2.0", "id": request_id, "result": {"ready": True}})
    elif method == "shutdown":
        emit({"jsonrpc": "2.0", "id": request_id, "result": None})
        break
    elif method == "session/prompt":
        params = request["params"]
        session_id = params["sessionId"]
        message_id = uuid.uuid4().hex
        emit({"jsonrpc": "2.0", "id": request_id, "result": {"messageId": message_id}})
        time.sleep(float(os.environ.get('FAKE_HARNESS_DELAY', '0')))
        emit(
            {
                "jsonrpc": "2.0",
                "method": "session.event",
                "params": {
                    "sessionId": session_id,
                    "event": {
                        "type": "agent/inbox/spliced",
                        "data": {"inserted": [{"id": message_id}]},
                    },
                },
            }
        )
        emit(
            {
                "jsonrpc": "2.0",
                "method": "session.event",
                "params": {
                    "sessionId": session_id,
                    "event": {"type": "tool/call", "data": {"name": "mcp__social__browse_posts"}},
                },
            }
        )
        emit(
            {
                "jsonrpc": "2.0",
                "method": "session.event",
                "params": {
                    "sessionId": session_id,
                    "event": {
                        "type": "assistant/message",
                        "data": {
                            "message": {
                                "content": [{"type": "text", "text": "fake runtime complete"}]
                            }
                        },
                    },
                },
            }
        )
        emit(
            {
                "jsonrpc": "2.0",
                "method": "session.event",
                "params": {
                    "sessionId": session_id,
                    "event": {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
                },
            }
        )
        emit(
            {
                "jsonrpc": "2.0",
                "method": "session.status",
                "params": {"sessionId": session_id, "status": "idle"},
            }
        )
    else:
        emit(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            }
        )
