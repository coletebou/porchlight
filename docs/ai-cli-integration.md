# porchlight and ai-cli: one TUI or two?

[ai-cli](https://github.com/coletebou/ai-cli) (`ai`) is a Rust terminal client
that asks several models at once, with a one-shot mode, a chat (`ai -i`) and a
side-by-side ratatui TUI (`ai -t`). porchlight now has an interactive mode
(`porchlight -i`). Should they be one program?

## Recommendation: no merge; share the data and hand off in both directions

| Option | Verdict |
|---|---|
| **Merge into one binary** (porchlight inside ai-cli, or the reverse) | No. porchlight is standard-library Python that must render a login in ~20 ms with zero dependencies and no network; ai-cli is an async Rust client with a provider stack, TLS and a release pipeline. One binary would tax every login with a model client nobody asked for, or force a Python dashboard into Rust for no user-visible gain. They also ship on different cadences. |
| **Embed one TUI in the other** (a dashboard pane inside `ai -t`, a chat pane inside `porchlight -i`) | Not now. Two full-screen apps negotiating one terminal (raw mode, alternate screen, resize, focus) is a lot of code for a view you can already get with one key. Revisit if the hand-off below proves too slow in daily use. |
| **Shared data + hand-off** | **Yes.** Small, reversible, and useful on day one. |

## What the shared part is

1. **The data contract**: `porchlight view` prints the dashboard as JSON
   ([contracts/porchlight-view-v1.md](contracts/porchlight-view-v1.md)): alerts,
   panels as plain text, and the items and actions behind them, from the cache,
   with no credentials. Anything can read it.
2. **porchlight → ai**: in `porchlight -i`, the `a` key on any item (an alert,
   a waiting agent, a todo) runs `[interactive] ai_command` (default `["ai"]`)
   with a question built from the item, its panel and its details: "explain
   this and tell me what to do first". The TUI steps aside for the answer and
   comes back.
3. **ai → porchlight**: `ai --motd "what needs me first?"` (and `/motd` in
   `ai -i`) runs `porchlight view --compact-json`, turns the alerts, waiting
   agents and top todos into a short context block, and asks the models with
   it. Without porchlight on `PATH` it says so and continues without context.
   Because `ai` already reads piped stdin, `porchlight view --compact-json | ai
   "summarize"` works too, with no new code.

The pieces stay independently useful: porchlight never needs ai-cli, and ai-cli
never needs porchlight.

## The voice angle

The same contract serves the Lux voice gateway's "what needs me?" route: a
text-in/text-out adapter can call `porchlight view --compact-json` on the hub
and read the attention list and the top todos aloud. Nothing in the document
is ANSI or layout-dependent, which is what a speech path needs.
