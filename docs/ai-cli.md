# porchlight and ai-cli

[ai-cli](https://github.com/coletebou/ai-cli) (`ai`) asks several models
at once: a one-shot question, a chat REPL (`ai -i`), or a side-by-side TUI
(`ai -t`, built with ratatui). porchlight is the dashboard you see at login,
and `porchlight -i` is its interactive mode. Both are terminal programs by the
same author, both MIT, and both have a "panels on screen, vi keys" TUI. So
should they be one program?

## Decision: two programs, one handoff, one small JSON contract

| Option | Verdict |
|---|---|
| Merge into one TUI | **No.** porchlight is stdlib-only Python on purpose: a login render takes milliseconds, with no install beyond the files. ai-cli is async Rust with streaming HTTP clients for five providers. Either merge means rewriting one of them, and it buys nothing. The two show different things: your machines and your work, against model answers side by side. |
| A shared UI component | **Not now.** No code could be shared across Python and Rust without a dependency that both would have to carry. |
| **porchlight launches ai-cli, and reads one JSON command** | **Yes.** The proven pattern (`D` already does it for Drover's TUI): porchlight leaves the screen, runs the other program in the foreground, then comes back and reloads. Data flows through one versioned command, so neither program imports the other. |
| Nothing | Leaves the two unaware of each other. You'd lose a cheap win: your recent model chats next to your todos and sessions. |

## What was built

- **`A` in `porchlight -i`** runs `ai -t` full-screen and returns
  ([interactive.md](interactive.md)). The key is one entry in
  `[interactive] programs`, so `A = ["ai", "-i", "-c"]` (resume the newest
  chat) or `A = []` (off) are one line of config.
- **`ai sessions [--json] [--limit N]`** in ai-cli lists saved chats, newest
  first. The JSON is metadata only, never a message and never a key:

  ```json
  {"version": 1, "dir": "~/.local/share/ai-cli/chats", "skipped": 0,
   "sessions": [{"name": "rust-lifetimes", "updated": "2026-11-07T14:02:11Z",
                 "turns": 6, "providers": ["claude", "gpt"]}]}
  ```

- **The `ai_chats` source and AI CHATS panel** in porchlight read that
  command when `ai` is installed (it's off otherwise), every 10 minutes. The
  panel shows the newest chats: name, models, turns, age. In `-i` each chat
  is an item, and `A` takes you into ai-cli. A different `version` is an
  error the panel names, never a misread.

```toml
[sources.ai_chats]
limit = 8
command = ["ai", "sessions", "--json"]   # porchlight appends --limit N
```

## Rules for the contract

- ai-cli owns the shape. Adding a field keeps `version: 1`. Renaming or
  removing one bumps `version`, and porchlight then shows "unknown output
  version" until it learns the new shape.
- No message text, no config values and no keys in the output. ai-cli's
  config file can hold literal `api_key` values. porchlight never reads that
  file, and it never runs `ai` with anything but `sessions --json` and the
  TUI keys you configure.
- porchlight masks token shapes in chat names before storing them, as it
  does for every source.

## Later, if wanted

An `ai usage --json` (spend per provider this month) would feed the MONEY
panel the same way. It's out of scope until ai-cli tracks usage.
