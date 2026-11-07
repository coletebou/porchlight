# Contract: `porchlight-bundle v2` (hub → peer)

The hub's `fleet` source writes this document to every peer's
`<cache>/hub/bundle.json` over SSH (stdin of one remote command; the same
connection prints the peer's `snapshot.json` back). The design and the reasons
are in [../fleet-consistency.md](../fleet-consistency.md).

```json
{
  "v": 2,
  "porchlight": "0.3.0",
  "from": "atlas",
  "ts": 1794057333.6,
  "interval": 240,
  "sources": {"weather": {"v": 1, "name": "weather", "ts": 1794057100.0, "ok": true, "data": {}}},
  "fleet": {"hosts": {"atlas": {"ok": true, "seen": 1794057333.6, "snap": {}}}},
  "layout": {"shared_panels": ["drover", "todos", "fleet", "ai", "knowledge", "prs"]}
}
```

| Field | Since | Meaning |
|---|---|---|
| `v` | v1 | `2`. A peer accepts `1` and `2`; any other value is ignored as if no bundle had arrived (and `doctor` says so). |
| `porchlight` | v2 | the hub's version, so `doctor` and the FLEET panel can show a hub/peer version skew |
| `from`, `ts` | v1 | the hub's machine name; when the bundle was built (epoch seconds) |
| `interval` | v2 | the hub's exchange cycle in seconds (the `fleet` source's TTL, 240 by default). A peer's staleness threshold is `fleet.stale_after`, or `7 × interval` when that is 0 |
| `sources` | v1 | every record whose scope on the hub is `global`, plus (v2) the records of the local sources named in the hub's `fleet.bundle_local` (default `["drover"]`): local sources whose data is a view of the whole fleet. The peer's cache picks, per source, between its own record and this copy: successful first, then without a soft error, then newest (unchanged) |
| `fleet.hosts` | v1 | the fleet table: per machine `{ok, seen, snap, error?, ms?, fails?}` |
| `layout.shared_panels` | v2 | the hub's `fleet.shared_panels`, in order: the fleet-wide half every machine shows the same way |

## How a peer uses `layout.shared_panels`

A peer's panel list is its own `layout.panels` (the per-host half; `"auto"`
expands as today) followed by every shared panel it does not already list, in
the hub's order. A panel the peer lists itself keeps the peer's position. A
peer that sets `fleet.shared_panels` itself (non-empty) uses its own list
instead of the hub's (an escape hatch for a machine that must differ; `doctor`
notes it). The `[identities.*]` allow/deny rules still apply: a panel whose
sources a machine may not show renders nothing there.

`--only`/`porchlight <panel>` and compact mode are unchanged: they show
exactly what was asked for.

## Staleness

A peer computes `age = now - ts`. Over the threshold it:

- shows `fleet via <from> · <age>` in amber in the header (dim when fresh);
- raises one `warn` alert, source `fleet`: `fleet data from <from> is <age> old`,
  fix `ssh <from> porchlight collect --only fleet`;
- keeps showing the data (each panel keeps its own stale note). After a global
  source's `fallback` window the peer collects it itself where it holds the
  credentials (unchanged).

No bundle at all on a machine without peers is `solo`, not stale.

## Compatibility

- Hub v2 → peer v1 (old porchlight): the peer ignores `interval`, `layout`
  and `porchlight`; the extra `sources` entries are read like any hub copy.
  Nothing breaks.
- Hub v1 → peer v2: no layout (the peer shows only its own `layout.panels`),
  threshold `7 × 240 s`.
- Additive fields keep `v: 2`. Consumers ignore fields they do not know.
