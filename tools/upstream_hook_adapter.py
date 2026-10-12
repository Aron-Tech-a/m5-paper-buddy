#!/usr/bin/env python3
"""Map non-Claude hook/event payloads to m5-paper-buddy daemon /hook schema.

Usage examples:
  cat event.json | python3 tools/upstream_hook_adapter.py --source dsh
  cat event.json | python3 tools/upstream_hook_adapter.py --source workbuddy
  cat event.json | python3 tools/upstream_hook_adapter.py --mapping tools/mappings/dsh_minimal_mapping.json
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, Optional


DEFAULT_EVENT_MAP = {
    "sessionstart": "SessionStart",
    "session_start": "SessionStart",
    "session.end": "Stop",
    "sessionend": "Stop",
    "stop": "Stop",
    "userpromptsubmit": "UserPromptSubmit",
    "user_prompt_submit": "UserPromptSubmit",
    "pretooluse": "PreToolUse",
    "tools/pre-execute": "PreToolUse",
    "tool/pre": "PreToolUse",
    "posttooluse": "PostToolUse",
    "posttoolusefailure": "PostToolUse",
    "tools/post-execute": "PostToolUse",
    "tool/post": "PostToolUse",
}


DEFAULT_PATHS = {
    "hook_event_name": ["hook_event_name", "event", "type", "name"],
    "session_id": ["session_id", "session.id", "session.sid", "sid", "conversation_id"],
    "cwd": ["cwd", "workspace", "project.cwd", "meta.cwd"],
    "tool_name": ["tool_name", "tool.name", "tool", "action.tool"],
    "tool_input": ["tool_input", "tool.input", "input", "action.input", "payload.tool_input"],
    "prompt": ["prompt", "user_input", "message", "input.prompt"],
    "transcript_path": ["transcript_path", "session.transcript_path", "meta.transcript_path"],
    "permission_mode": ["permission_mode", "meta.permission_mode"],
}


def _norm_event(name: str) -> str:
    return (name or "").strip().lower()


def _get_path(obj: Dict[str, Any], path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _first(obj: Dict[str, Any], paths: Iterable[str]) -> Any:
    for p in paths:
        v = _get_path(obj, p)
        if v is not None:
            return v
    return None


def _load_mapping(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_payload(event: Dict[str, Any], mapping: Dict[str, Any], source: str) -> Dict[str, Any]:
    event_map = dict(DEFAULT_EVENT_MAP)
    event_map.update({k.lower(): v for k, v in (mapping.get("event_map") or {}).items()})
    paths = dict(DEFAULT_PATHS)
    for k, v in (mapping.get("field_paths") or {}).items():
        if isinstance(v, list) and v:
            paths[k] = v

    raw_event = _first(event, paths["hook_event_name"])
    raw_event = str(raw_event or "")
    hook_event = event_map.get(_norm_event(raw_event), raw_event)
    if source == "claude":
        hook_event = raw_event or hook_event

    payload: Dict[str, Any] = {
        "hook_event_name": hook_event,
        "session_id": str(_first(event, paths["session_id"]) or "unknown"),
        "cwd": str(_first(event, paths["cwd"]) or "."),
    }

    # Optional common fields.
    for k in ("prompt", "transcript_path", "permission_mode"):
        v = _first(event, paths[k])
        if v is not None:
            payload[k] = v

    if hook_event in ("PreToolUse", "PostToolUse"):
        tname = _first(event, paths["tool_name"])
        payload["tool_name"] = str(tname or "UnknownTool")
        tinput = _first(event, paths["tool_input"])
        payload["tool_input"] = tinput if isinstance(tinput, dict) else {"raw": tinput} if tinput is not None else {}

    # Let users pass through additional override fields if present.
    extra = mapping.get("static_fields") or {}
    if isinstance(extra, dict):
        payload.update(extra)
    return payload


def _post_json(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace") or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"http {e.code}: {body}") from e


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("generic", "claude", "dsh", "workbuddy"), default="generic")
    ap.add_argument("--daemon-url", default="http://127.0.0.1:9876/hook")
    ap.add_argument("--timeout", type=float, default=35.0)
    ap.add_argument("--mapping", help="JSON mapping file; see tools/mappings/*.json")
    ap.add_argument("--dry-run", action="store_true", help="print mapped payload only")
    args = ap.parse_args()

    if not args.mapping:
        here = os.path.dirname(os.path.abspath(__file__))
        if args.source == "dsh":
            args.mapping = os.path.join(here, "mappings", "dsh_minimal_mapping.json")
        elif args.source == "workbuddy":
            args.mapping = os.path.join(here, "mappings", "workbuddy_minimal_mapping.json")

    try:
        incoming = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"invalid input json: {e}"}))
        return 2

    mapping = _load_mapping(args.mapping)
    payload = _build_payload(incoming, mapping, args.source)

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    try:
        out = _post_json(args.daemon_url, payload, args.timeout)
    except Exception as e:
        # Same fallback semantics as existing hooks: return {} on failure.
        print("{}")
        print(f"[adapter] post failed: {e}", file=sys.stderr)
        return 0

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
