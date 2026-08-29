# Scheduling `sandbox reap`

`sandbox reap` enforces the wall-clock TTL set by `--ttl` / a lane policy: it brings
down every sandbox whose `started-at + ttl-minutes` has passed. It is a poll from
outside, so it needs a scheduler. Every 5 minutes is the intended cadence.

## The one rule: run it as the user that owns the sandboxes

Sandbox state is per-user (`$SANDBOX_STATE_DIR`, default
`$XDG_STATE_HOME/agent-sandbox` — see plan §10.1). `reap` walks that directory, so a
job running as **root** looks at root's state dir, finds nothing, and reaps nothing —
silently. There is no error to notice; expired sandboxes simply keep running.

So: a **user** timer / a cron line with the owning user in it, never a plain root job.

## systemd user timer (recommended; the only option on a stock NixOS)

```bash
mkdir -p ~/.config/systemd/user
cp sandbox-reap.service sandbox-reap.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now sandbox-reap.timer

systemctl --user list-timers sandbox-reap.timer   # confirm it is scheduled
journalctl --user -u sandbox-reap.service -n 20   # see what it reaped
```

The unit calls `%h/.local/bin/sandbox` by absolute path: a systemd user unit does not
inherit your shell's `PATH`, so `~/.local/bin` is not on it even when your interactive
shell has it. Adjust `ExecStart` if you installed to another prefix.

**A service account needs lingering.** User timers only run while the user has a
systemd user instance, which normally means being logged in:

```bash
sudo loginctl enable-linger omniroot
```

This is the same setting that gives the account a persistent `/run/user/<uid>`, so it
also removes the non-tmpfs mount fallback described in plan §9. Worth doing for any
account that drives sandboxes unattended.

## NixOS — declarative

```nix
systemd.user.services.sandbox-reap = {
  description = "Reap agent sandboxes past their TTL";
  serviceConfig = {
    Type = "oneshot";
    ExecStart = "%h/.local/bin/sandbox reap";
    Environment = "DOCKER_HOST=unix://%t/docker.sock";  # rootless docker
  };
};

systemd.user.timers.sandbox-reap = {
  description = "Reap agent sandboxes every 5 minutes";
  wantedBy = [ "timers.target" ];
  timerConfig = {
    OnCalendar = "*:0/5";
    Persistent = true;
    AccuracySec = "30s";
  };
};

# Lingering, so the timer runs without an interactive login.
users.users.omniroot.linger = true;   # NixOS 23.11+
```

On NixOS older than 23.11 there is no `users.users.<name>.linger`; create the marker
file instead, which is all `loginctl enable-linger` does:

```nix
systemd.tmpfiles.rules = [ "f /var/lib/systemd/linger/omniroot 0644 root root -" ];
```

Note these go in `configuration.nix` as **system** config that defines *user* units —
`systemd.user.*` is not the same as `systemd.services.*`. Under home-manager the
equivalents are `systemd.user.services` / `systemd.user.timers` in the user's config.

## Actual cron, if you want it

There is no `crontab` on a stock NixOS because cron is not installed. You can have it:

```nix
services.cron = {
  enable = true;
  systemCronJobs = [
    "*/5 * * * * omniroot /home/omniroot/.local/bin/sandbox reap"
  ];
};
```

Note the user field (`omniroot`) — `systemCronJobs` entries are system crontab lines,
so without it the job runs as root and reaps nothing (see the rule above).

On Arch and other distros with cron installed, the per-user crontab is equivalent and
simpler, since it already carries the right user:

```cron
*/5 * * * * $HOME/.local/bin/sandbox reap
```

**Cron caveat.** Cron jobs get a minimal environment: no `XDG_RUNTIME_DIR`, and no
`DOCKER_HOST`. The CLI handles the first (it falls back to mounting under the state
dir, plan §9), but under **rootless docker** you must set `DOCKER_HOST` yourself or
the client will not find the daemon:

```cron
*/5 * * * * omniroot DOCKER_HOST=unix:///run/user/1002/docker.sock /home/omniroot/.local/bin/sandbox reap
```

The systemd user timer avoids all of this, which is why it is the recommendation.

## Checking it works

`reap` is idempotent and safe to run by hand — `--dry-run` shows what it would tear
down without touching anything:

```bash
sandbox reap --dry-run     # what would go
sandbox ls                 # lane, age, TTL, EXPIRED marker per sandbox
```

A sandbox with no recorded TTL is never reaped unless you pass `--all`, which falls
back to a 24h default — so an interactive session is not swept away by the timer that
exists to catch runaway batch runs.
