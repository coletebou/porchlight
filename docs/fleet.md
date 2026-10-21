# Fleet mode

Several machines, one picture, and nothing new to run: no agent, no open
port, no server. One machine is the **hub**; the rest are **peers**. Every few
minutes the hub opens one SSH connection per peer, over your existing
`~/.ssh/config` aliases, and in that single round trip:

1. **pushes** its bundle to the peer: the hub's records for every *global*
   source (SaaS status, AI plan limits, weather, quotes, `[[custom]]` sources
   with `scope = "global"`) plus the fleet table;
2. **pulls** the peer's `snapshot.json`: vitals, agent counts, its top alerts,
   an optional badge, and the records of its *shared* sources.

```
            ┌───────────── hub (fleet.peers = [...]) ──────────────┐
            │ collects global sources: cloud APIs, AI limits, …     │
            └───┬───────────────┬────────────────┬─────────────────┘
   ssh web-1    │  ssh db-1      │  ssh gpu-box    │   (one call each, every 4 min)
   ▲ bundle     ▼ snapshot       ▼                 ▼
 ┌──────┐   ┌──────┐   ┌─────────┐
 │web-1 │   │ db-1 │   │ gpu-box │   peers render the hub's copy of every global panel,
 └──────┘   └──────┘   └─────────┘   so they never hold a SaaS credential or call an API
```

Peers therefore render the account-wide panels (AI LIMITS, EDGE, INFRA,
MONEY, …) and the FLEET table without a single API call or credential of
their own, and the hub's dashboard shows every peer's health and critical
alerts.

## Setup

1. Install porchlight on every machine (`porchlight install timer` on each, so
   peers keep their snapshot fresh).
2. Make sure the hub can `ssh <alias> true` to each peer non-interactively
   (keys or an agent; `BatchMode=yes` is used, so password prompts fail fast).
3. On the hub:

   ```toml
   [fleet]
   peers = ["web-1", "db-1", "gpu-box"]
   order = ["hub", "web-1", "db-1", "gpu-box"]   # optional: table order
   labels = { "ip-10-0-1-17" = "web-1" }          # optional: short names
   ssh_options = ["ProxyJump=bastion"]           # optional: extra ssh -o options
   ```

One config file can serve the whole fleet:

```toml
[hosts."hub"]
fleet.peers = ["web-1", "db-1", "gpu-box"]
```

## What a peer sees

- Global panels from the hub's bundle (`porchlight doctor` shows "hub bundle
  from hub, 3m old", and those sources show scope `hub`).
- If the hub's copy of a global source goes stale (older than `max(3 × ttl, 45
  min)`), a peer that holds the credentials collects it itself, so a dead hub
  degrades gracefully instead of blanking the dashboard.
- When both copies exist, the successful one whose data carries no soft error
  wins, then the newest. A peer's "token expired" never shadows the hub's live
  numbers.

## The fleet table

```
 host         load  mem disk  agents    alerts   seen
● hub          0.84  52%  61%  3cl 2co   ok       now
● web-1        0.42  41%  63%  12/12 ctr ok       now
● db-1         1.21  78%  88%  -         1▲       now
● nas         offline
```

- `agents` shows AI coding agent counts by default, or a source's `badge`
  (set `fleet.badge = "docker"` on a peer to show `running/expected`
  containers).
- Alerts count the peer's own alerts (live probes and local sources), never
  the fleet-wide ones every machine shares.
- A peer is `offline` only when Tailscale (if present) also says it has not
  been seen for 10 minutes; otherwise "no data" with the ssh error.
- Three consecutive SSH failures raise a warning with `ssh <peer> true` as the fix.

## Shared sources

A host-local source marked `share` (MongoDB, or `[[custom]] share = true`)
travels inside the snapshot, bounded to 16 KB per host. The hub renders it as
if it were local, which keeps the credential on the one machine that owns it.

## Remote side

The command the hub runs on each peer is plain POSIX shell:

```sh
umask 077; d="${XDG_CACHE_HOME:-$HOME/.cache}/porchlight"; mkdir -p "$d/hub" &&
cat > "$d/hub/.bundle.tmp" && mv -f "$d/hub/.bundle.tmp" "$d/hub/bundle.json";
cat "$d/snapshot.json" 2>/dev/null || true
```

If a peer uses a non-default cache directory, set `fleet.remote_cache` on the
hub. Files are written mode 0600 in a 0700 directory.
