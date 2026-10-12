#!/usr/bin/env python3
"""Tiny client for the daemon's WiFi-stage remote approval relay."""

import argparse
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request


def sign(secret: str, method: str, path: str, device: str, ts: int, nonce: str, body: str) -> str:
    base = f"{method}\n{path}\n{device}\n{ts}\n{nonce}\n{body}"
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest()


def call(base_url: str, secret: str, device: str, method: str, path: str, payload=None):
    body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    ts = int(time.time())
    nonce = secrets.token_hex(8)
    sig = sign(secret, method, path, device, ts, nonce, body)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body.encode("utf-8") if method == "POST" else None,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Buddy-Device": device,
            "X-Buddy-Timestamp": str(ts),
            "X-Buddy-Nonce": nonce,
            "X-Buddy-Signature": sig,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.getcode(), json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw or "{}")
        except Exception:
            return e.code, {"ok": False, "error": raw}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:9876")
    ap.add_argument("--secret", required=True)
    ap.add_argument("--device", default="remote-phone")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pending")
    d = sub.add_parser("decision")
    d.add_argument("--request-id")
    d.add_argument("--decision", choices=("allow", "deny"), required=True)
    args = ap.parse_args()

    if args.cmd == "pending":
        code, obj = call(args.base_url, args.secret, args.device, "GET", "/remote/v1/pending")
        print(json.dumps({"status": code, "data": obj}, ensure_ascii=False, indent=2))
        return

    req_id = args.request_id
    if not req_id:
        _, obj = call(args.base_url, args.secret, args.device, "GET", "/remote/v1/pending")
        req = obj.get("request") if isinstance(obj, dict) else None
        req_id = (req or {}).get("request_id")
    if not req_id:
        raise SystemExit("no pending request_id found")

    code, obj = call(
        args.base_url, args.secret, args.device, "POST", "/remote/v1/decision",
        {"request_id": req_id, "decision": args.decision},
    )
    print(json.dumps({"status": code, "data": obj}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
