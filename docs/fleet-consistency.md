# Should every machine's dashboard show the same thing?

**Short answer: the fleet-wide half, yes, identically; the machine half, no.**
Split every dashboard into two halves with a hard rule about which panel lives
where, make the fleet-wide half come from one place (the hub's bundle) in one
order on every machine, and make its age visible so a stale copy is never
passed off as the truth.

## The question

With several machines (a hub and its peers), you log in to whichever one you
happen to need. Today each machine's layout is configured on its own, so the
same fact can look different depending on where you are:

- one machine lists two agents waiting on you, another none, because its own
  Drover daemon answered a minute ago and the other's did not;
- a panel exists on one host and is simply missing on another (the TODOS or
  money panel was never added to a peer's `layout.panels`);
- the order differs, so the eye has to search;
- a peer whose hub went quiet keeps showing the hub's last bundle, and nothing
  on screen says it is two hours old.

A case from the fleet this document was written for (the predecessor
dashboard, November 2026): the hub showed 14 panels, a newly added peer 10 (no
tenants, AWS, comms or business panel), a production server 13 (no repo
panel), each in its own order. None of those differences were decisions; they
were drift.

## Options

| | All identical | All per-host | **Two halves (recommended)** |
|---|---|---|---|
| What you see | the same dashboard everywhere | whatever each machine's config says | the same fleet-wide panels everywhere, then this machine's own |
| "Is *this* box OK?" | buried: the hub's SYSTEM is not this machine's | yes | yes |
| "What needs me?" | same everywhere | differs, and contradicts itself | same everywhere |
| Config | one layout, but host panels make no sense on other hosts | N layouts that drift | one fleet list (on the hub) + per-host extras |
| Credentials | every machine needs every key, or a bundle | each machine needs the keys of what it shows | only the hub holds account keys (already true) |

All-identical fails on the machine half: SYSTEM, SERVICES, DOCKER, REPOS and
NETWORK are *about the machine you are on*, and the reason to log in somewhere
is usually that machine. All-per-host fails on the fleet half: "what needs me"
is a property of you, not of the box you typed `ssh` to.

## The rule

A panel is **fleet-wide** when its data is about an account, a person or the
whole fleet, and it reads the same no matter which machine collects it:

> ATTENTION's fleet items · TODOS · DROVER (who waits on you) · AI LIMITS ·
> PULL REQUESTS · FLEET · KNOWLEDGE (brain, wikis) · EDGE · INFRA · AWS · MONEY ·
> COMMS · BACKUPS of account-level services

A panel is **per-host** when its data is about this machine:

> SYSTEM · SERVICES · AGENTS (local counts) · DOCKER · NETWORK · REPOS · FILES ·
> HEALTH of local routes · INSTANCES started from here

Identity still wins over both: a work laptop never shows the personal fleet's
fleet-wide panels (it is not a peer of the personal hub, and `[identities.*]
allow` lists keep personal sources off it), and the reverse.

## How it works (porchlight)

### 1. One list, decided on the hub

```toml
[hosts."atlas".fleet]          # the hub
peers = ["web-1", "db-1", "gpu-box"]
shared_panels = ["drover", "todos", "fleet", "ai", "knowledge", "prs", "edge", "infra", "aws", "money"]
```

The hub ships `shared_panels` inside its bundle (`layout.shared_panels`). A
peer renders **its own layout first** (`layout.panels`, the per-host half) and
then every shared panel it does not already show, **in the hub's order**. A
peer that lists a shared panel itself keeps its own position for it. Adding a
panel to the fleet half is therefore one edit on one machine, and every peer
has it within one exchange (4 minutes).

### 2. The bundle carries the fleet-wide data

The hub already pushes every `scope = "global"` record (SaaS, AI plans,
weather, todos, brain) to each peer and pulls each peer's snapshot, in one SSH
round trip per peer. Two additions:

- **Fleet-view local sources** (`fleet.bundle_local`, default `["drover"]`): a
  local source whose answer is a view of the whole fleet (Drover's daemon sees
  every host) travels in the bundle too. A peer with its own fresh record
  keeps using it (the cache prefers successful, then error-free, then newest);
  a peer without one (no daemon there) shows the hub's view instead of no
  panel at all.
- **Bundle v2**: `{"v": 2, "porchlight": "<version>", "from", "ts",
  "sources", "fleet", "layout": {"shared_panels": [...]}}`. A peer accepts v1
  bundles (no layout: nothing changes) and v2. The version lets the FLEET
  table and `doctor` show a hub/peer version skew.

### 3. Staleness is shown, never hidden

- The header says where the fleet half comes from when it is not this
  machine: `fleet via atlas · 3m`. Over `fleet.stale_after` (default: seven
  missed exchanges, 28 minutes at the default 240 s cycle) it turns amber and
  raises one warning: "fleet data from atlas is 47m old", with the fix
  `ssh atlas porchlight collect --only fleet`. No crit: a stale dashboard is a
  state, not an outage.
- Each fleet-wide panel keeps its own stale note (`12m`, `✖ 3h`) as today.
- After a source's `fallback` window (`max(3 × ttl, 45 min)`) a peer that holds
  the credentials collects it itself, so a dead hub degrades instead of
  freezing the dashboard (unchanged).
- `porchlight view` reports `fleet.role`, `fleet.hub` and `fleet.bundle_age`,
  so other programs can say "as of 3 minutes ago" too.

### 4. Why not a server, a database or a second hub

The bundle is a push over SSH the hub already has; the snapshot is the reply on
the same connection. That is the smallest primitive that keeps the invariant
("every machine shows the same fleet-wide facts, and says how old they are").
A standby hub, leases or a shared database would add a failure mode for a
benefit the per-source fallback already gives. Revisit only if the hub is down
often enough that the fallback's 45 minutes hurt.

## Rollout for the fleet this was written for

The machines run the predecessor dashboard (`motd/` in the dotfiles repo), with
the same hub-and-bundle design. The same rule applies there, ported in the
dotfiles branch of this change:

1. One `FLEET_PANELS` list in `motd/motdlib/hosts.py`, appended to every
   personal profile after its host panels, in one order.
2. The header's `fleet via mac-mini · 3m` fact and the stale warning.
3. A TODOS panel reading the same providers as porchlight's (contract:
   [contracts/todo-item-v1.md](contracts/todo-item-v1.md)), collected on the hub
   and shipped in the bundle.

Longer term the fleet should move to porchlight itself
([migrating-from-fleet-motd.md](migrating-from-fleet-motd.md)), so the two stop
being maintained twice. That swap changes every login on every machine, so it
is the owner's call, done one machine at a time with the old hook kept as a
fallback.
