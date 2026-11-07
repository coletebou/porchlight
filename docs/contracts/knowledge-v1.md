# Contract: `knowledge v1`

The `knowledge` source (`porchlight/sources/knowledge.py`, scope `global`,
opt-in) gathers what you and your agents wrote down: a shared memory service
(a "brain": an MCP server with a health endpoint and a list/search tool),
open reflections (from a Drover daemon), wikis and plain links. The KNOWLEDGE
panel (`porchlight/panels/knowledge.py`) and `porchlight -i` read it. Additive
changes keep `v1`.

## Settings (`[sources.knowledge]`)

```toml
[sources.knowledge]
# A brain: health is a public GET; recent entries come from an MCP tools/call
# (JSON-RPC 2.0 over HTTP POST) with a bearer key read from the environment.
brain_name = "brain"
brain_health_url = ""            # e.g. "https://brain.example.com/api/mcp/healthz"
brain_mcp_url = ""               # e.g. "https://brain.example.com/api/mcp"
brain_key_env = ""               # the env var that holds the key, e.g. "BRAIN_KEY"
brain_list_tool = "brain_list"   # called with {"limit": brain_recent}
brain_recent = 5
brain_web_url = ""               # a link for the panel and porchlight -i
reflections = true               # open reflections from the local Drover daemon, when there is one
wikis = []                       # [{name, url, path?}]: path = a local Markdown checkout (git wiki, vault)
links = []                       # [{label, url}]: dashboards worth one keypress in porchlight -i
```

Every part is optional and failure-isolated: a part that is not configured is
absent from the record; a part that fails reports `ok: false` and a short
`error` and keeps its previous data (`ctx.prev`).

## Record (`porchlight json knowledge` → `data`)

```json
{
  "v": 1,
  "brain": {
    "name": "brain", "ok": true, "error": "", "ms": 180,
    "entries": 1840, "stale": false,
    "recent": [{"id": "6c1f…", "title": "voice gateway: AEC decision", "kind": "decision",
                "created": 1794050000.0, "url": ""}],
    "web_url": ""
  },
  "reflections": {"ok": true, "error": "", "open": 9, "by_kind": {"action": 6, "question": 3},
                  "recent": [{"id": "r-12", "title": "…", "kind": "action", "host": "atlas", "created": 1794040000.0}]},
  "wikis": [{"name": "docs", "url": "https://github.com/acme/docs/wiki", "ok": true, "error": "",
             "pages": 42, "recent": [{"title": "Runbook", "path": "Runbook.md", "mtime": 1794000000.0}]}],
  "links": [{"label": "Badland", "url": "https://example.com"}],
  "alerts": []
}
```

- Titles are one line, control characters removed, token shapes masked
  (`secrets.mask_shapes`), at most 160 characters. No entry bodies are stored:
  the record is a pointer list, not a copy of the brain.
- Ids (`brain.recent[].id`, `reflections.recent[].id`) are not run through
  `mask_shapes`: a real brain slug is a long, readable, hyphenated string, the
  very shape its 40-plus-character rule redacts, which would break "copy id".
  They are control-character-stripped, length-bounded, and masked for known
  token prefixes and URI credentials only (`secrets.mask` with no environment).
- `brain.recent`: newest first, at most `brain_recent`.
- `wikis[].recent`: the 3 most recently modified pages under `path` (Markdown
  files, `.git` skipped, at most 5000 files walked); `pages` counts them.
  Without `path`, only the link is kept.
- `alerts`: `warn` when the brain's health says not ok, or its index is empty
  or stale. Nothing for a part that is not configured.

## Panel

```
KNOWLEDGE ● ─────────────────────────────────── 3 new since last visit
brain     ● 1,840 entries · new: voice gateway: AEC decision · +2
reflect   9 open · 6 actions · 3 questions
wikis     docs 42 pages · Runbook 2h
links     Badland · Grafana
```

Items (for `porchlight -i`): one per recent brain entry, reflection, wiki page
and link, each with `open_url` when it has a URL and `copy` for its id.
