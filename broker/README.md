# Credential broker — host-side filler

The gateway injects real credentials on egress so they never enter the container
(see `docs/SANDBOX-PLAN.md` §4). The *real* credentials are obtained **here, on the
host**, where the browser, MFA, and long-lived secrets live — then pushed into the
gateway's redis store. The container only ever holds an opaque handle (or nothing).

## How it connects

The gateway runs redis on a unix socket inside its own mount namespace (unreachable
from the workload). To let the host write to it, compose bind-mounts a control dir:

```
${BROKER_CONTROL_DIR:-./.broker-control}  (host)  ->  /run/broker-control  (gateway)
```

`fill.py` runs on the host and writes credential records that the gateway reads.

> **TODO (P5):** the cleanest transport is a tiny broker control endpoint inside the
> gateway listening on `/run/broker-control/broker.sock`, which validates and writes
> into redis. The skeleton below assumes that endpoint exists. Until it's built, you
> can seed redis directly via `docker compose exec gateway redis-cli -s /run/broker/redis.sock`.

## What it writes (redis schema)

```
broker:handle:<handle>  -> JSON {"host","path_prefix","methods":[...],"headers":{...}}   (with TTL)
broker:host:<host>      -> HASH header-name -> header-value
```

`broker_addon.py` reads these. Handles are scope-checked (host + path prefix + method
+ TTL) before the real header is swapped in. Revoke by deleting the key.

## Flow

1. You authenticate the real provider here on the host (OAuth device flow, paste a
   token, etc.). Refresh tokens stay here — never near the container.
2. `fill.py` mints a high-entropy handle, stores `real header + scope` in redis with a
   TTL, and prints the handle.
3. Drop the handle into the sandbox (env var / kubeconfig field / `.netrc`). The
   workload sends it as `X-Sandbox-Handle`; the gateway swaps in the real credential
   for in-scope requests only.
4. For OAuth: a refresher loop here re-mints the upstream access token and updates the
   redis record; the handle is stable.
