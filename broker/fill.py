#!/usr/bin/env python3
"""
fill.py — host-side credential filler (P5 skeleton).

Mints a scoped phantom handle for a real credential and stores it in the gateway's
redis. The real credential stays on the host; the container only gets the handle.
See broker/README.md and docs/SANDBOX-PLAN.md §4.

UNVERIFIED scaffold. Transport to the gateway redis is a TODO (see README). For now
this prints the redis commands so you can pipe them via:
    docker compose exec -T gateway redis-cli -s /run/broker/redis.sock

Examples
--------
  # Phantom handle for a kube apiserver, read-only, 1h TTL:
  ./fill.py handle --host kube-apiserver.example.internal \
      --method GET --method LIST --ttl 3600 \
      --header "Authorization=Bearer $(get-short-lived-token)"

  # Inject-by-host static token for the GitHub API:
  ./fill.py host --host api.github.com --header "Authorization=token $GH_TOKEN"
"""
import argparse
import json
import secrets
import sys


def parse_headers(pairs):
    out = {}
    for p in pairs:
        if "=" not in p:
            sys.exit(f"bad --header {p!r}, expected NAME=VALUE")
        k, v = p.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main():
    ap = argparse.ArgumentParser(description="mint sandbox credential records")
    sub = ap.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("handle", help="mint a scoped phantom handle")
    h.add_argument("--host", required=True)
    h.add_argument("--path-prefix", default="/")
    h.add_argument("--method", action="append", default=[], help="repeatable; omit for any")
    h.add_argument("--ttl", type=int, default=3600)
    h.add_argument("--header", action="append", default=[], required=True)

    ho = sub.add_parser("host", help="register inject-by-host headers")
    ho.add_argument("--host", required=True)
    ho.add_argument("--header", action="append", default=[], required=True)

    args = ap.parse_args()

    if args.cmd == "handle":
        handle = secrets.token_urlsafe(32)
        spec = {
            "host": args.host,
            "path_prefix": args.path_prefix,
            "methods": args.method or None,
            "headers": parse_headers(args.header),
        }
        key = f"broker:handle:{handle}"
        print(f"# handle (give this to the sandbox as X-Sandbox-Handle):\n{handle}\n", file=sys.stderr)
        print(f"SET {key} {json.dumps(json.dumps(spec))}")
        print(f"EXPIRE {key} {args.ttl}")

    elif args.cmd == "host":
        hdrs = parse_headers(args.header)
        key = f"broker:host:{args.host.lower()}"
        parts = " ".join(f"{json.dumps(k)} {json.dumps(v)}" for k, v in hdrs.items())
        print(f"HSET {key} {parts}")


if __name__ == "__main__":
    main()
