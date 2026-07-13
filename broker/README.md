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

> **Two front-ends, same store:**
> - **`webui.py`** — host-only, password-gated web app (see `docs/SANDBOX-PLAN.md` §15).
>   Launched by `./sandbox up`; URL + password shown by `./sandbox ls`. Set inject-by-host
>   headers, mint/revoke scoped handles, and watch the redacted egress log. This is the
>   normal path.
> - **`fill.py`** — scripting/CLI filler. Currently prints the redis commands to seed
>   directly via `docker compose exec gateway redis-cli -s /run/broker/redis.sock`.
>
> Both go through the gateway's control endpoint (`gateway/control_server.py`, listening on
> `/run/broker-control/broker.sock`) or redis. The endpoint is the only write path into the
> store and is reachable from the host but **not** from the workload (different mount ns).

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
