# Knowledge

The `knowledge` source gathers what you and your agents wrote down: a shared
memory service (a "brain": an MCP server with a public health endpoint and a
list tool), open reflections from a local [Drover](drover.md) daemon, wikis
(a local Markdown checkout: a git wiki, an Obsidian vault) and plain links.
The KNOWLEDGE panel and `porchlight -i` read it. Off by default (opt-in): it
runs once `[sources.knowledge]` exists.

```text
 KNOWLEDGE ● ─────────────────────────────────── 3 new since last visit
 brain     ● 1,840 entries · new: voice gateway: AEC decision +2
 reflect   9 open · 6 actions · 3 questions
 wikis     docs 42 pages · Runbook 2h
 links     Dashboard · Status
```

| Row | Shows |
| --- | --- |
| brain | entry count, and the newest entry written since your last visit (`+N` for more); with none new, the newest entry on file; dimmed and the error when the health check failed |
| reflect | open reflections across every repo the local Drover daemon knows, by kind |
| wikis | each configured wiki's page count, and the single most recently modified page across all of them |
| links | the labels you configured |

Every part is independent and optional: one that is not configured in
`[sources.knowledge]` is simply absent, from both the panel and the record.

## Settings (`[sources.knowledge]`)

```toml
[sources.knowledge]
brain_name = "brain"
brain_health_url = ""            # a public GET, e.g. "https://brain.example.com/api/mcp/healthz"
brain_mcp_url = ""                # a stateless MCP endpoint, e.g. "https://brain.example.com/api/mcp"
brain_key_env = ""                # the env var that holds the bearer key, e.g. "BRAIN_KEY"
brain_list_tool = "brain_list"    # the MCP tool, called {"limit": brain_recent}
brain_recent = 5                  # at most this many (capped at 50)
brain_web_url = ""                # a link for the panel and porchlight -i

reflections = true                # open reflections from the local Drover daemon, when one is running

[[sources.knowledge.wikis]]
name = "docs"
url = "https://example.com/docs/wiki"
path = "~/vaults/docs"            # a local Markdown checkout; omit to keep only the link

[[sources.knowledge.links]]
label = "Status"
url = "https://example.com/status"
```

### The brain

A "brain" here is any MCP server that answers two things over plain HTTP, no
session and no credential for the health check:

1. **Health**: `GET brain_health_url` returns JSON with at least `ok`
   (boolean) and, where the server has them, `entries` (a count) and `stale`
   (boolean). Anything else in the body is ignored.
2. **Recent entries**: a stateless JSON-RPC 2.0 `tools/call` `POST` to
   `brain_mcp_url` —

   ```json
   {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "brain_list", "arguments": {"limit": 5}}}
   ```

   with `Authorization: Bearer <the value of $brain_key_env>`. No MCP
   `initialize` handshake is needed first: one POST, one JSON reply. The
   source reads the tool's `structuredContent.items` when present, else
   parses the first text content block as JSON, and otherwise reports an
   error; a tool-level refusal (`result.isError`) and a JSON-RPC-level error
   are both read as a short error message, never raised. Each item becomes
   one `recent` entry: its title (masked, see below), kind, when it was
   created, and a URL when the server gives one — nothing else is read or
   kept, so a brain entry's body never reaches the cache.

The credential is read only from the environment variable named by
`brain_key_env`; it never appears in the config file, the cache or a log
line. `brain_recent` is clamped to 1–50 whatever the config says.

### Reflections

Reads the same local Drover daemon the [`drover`](drover.md) source does
(`sources.drover`'s own `state_dir`, so the two stay in sync), via
`reflections.list {all: true}`: every repo's open reflections, the main
token only (`reflections.list` is not on the read-only widget token's
scope). The record keeps counts by kind and up to five of the most recent
titles — not their bodies. Capped at 200 reflections for the "open" count: a
dashboard number, not a promise of every one of them.

### Wikis

Each entry needs a `name` and `url`; `path`, if given, is walked for
`.md` files (`.git` skipped, at most 5,000 files examined) to find the 3
most recently modified pages. Without `path`, only the link is kept — no
page count, no recent list. A `path` that does not exist is an error for
that wiki alone; the others, and the rest of the source, are unaffected.

### Links

Plain `{label, url}` pairs: no fetch, no failure mode, just one keypress in
`porchlight -i` to open them.

## Privacy

- **Titles only.** No entry, reflection or page body is ever read into the
  cache: the record is a pointer list (ids, titles, paths, URLs), not a copy
  of what you wrote. A title is one line, control characters removed, at
  most 160 characters.
- **Masked by shape.** Every title and error message goes through the same
  token-shape and URI-credential masking every other source uses
  (`secrets.mask_shapes`): a brain entry that happens to quote something
  token-shaped shows `<redacted>` in the same place a real key would.
- **The key stays in the environment.** `brain_key_env` names a variable;
  the source reads its value only to put it in one outgoing `Authorization`
  header, never into the config, the cache or a result's error text.

## Items (`porchlight -i` and `porchlight view`)

One per recent brain entry (open its URL when it has one; copy its id), per
reflection (copy its id), per wiki page (open the wiki's URL; copy the
page's path) and per link (open its URL).

## Alerts

| Severity | When |
| --- | --- |
| warn | the brain's health check says not ok, or its index is empty or stale |

## A generic MCP brain, end to end

Any MCP server works as a brain as long as it exposes a plain HTTP health
GET and a stateless `tools/call` list endpoint protected by a bearer token.
For example, pointed at a self-hosted server with a tool named `kb_recent`
instead of `brain_list`:

```toml
[sources.knowledge]
brain_name = "my-kb"
brain_health_url = "https://kb.example.com/mcp/healthz"
brain_mcp_url = "https://kb.example.com/mcp"
brain_key_env = "MY_KB_KEY"
brain_list_tool = "kb_recent"
brain_recent = 10
```

`MY_KB_KEY` comes from the shell environment or a secrets file
([configuration.md](../configuration.md)) — never from the config file
itself.
