# mcp-webhook-replay-server

![ci](https://github.com/Supp3r-Man/mcp-webhook-replay-server/actions/workflows/ci.yml/badge.svg)

A small **MCP server over stdio** for inspecting and replaying webhook events: verify their HMAC signatures, replay them to a mock sink with idempotency and bounded retries, and manage the dead letters. Everything is synthetic and every delivery goes to an in-process mock sink, never the network.

> **What is verified, and what is not.** 103 tests pass locally on Python 3.13 on Windows (stdlib only; the tests need only `pytest`). They cover the JSON-RPC and MCP conformance of the server, the signature and delivery logic, the audit log, and a **real subprocess driven over stdio by an in-repo mock client**. I broke the code on purpose 10 ways (signature comparison, stale boundary, idempotency ledger, double delivery, retry bound, the write gate, the verification gate, audit-before-write, notification handling, the dry-run default) and the tests failed every time; the script reports "pattern missed" instead of passing silently. **The server has only ever been exercised by that mock client.** It has not been connected to Claude Desktop, Claude Code or any other real MCP client, and it implements the protocol by hand (no MCP SDK), so a real client may expect something I got wrong. The GitHub Actions workflow (Linux, Python 3.11 and 3.13) also ran green after the first push. The events, companies and payloads are made up (no card, bank, health or personal data).

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `list_events` | read | Filter by `type` or signature `status` (`all`, `valid`, `bad_signature`, `stale`); page with `limit` (1-50) and `cursor`. |
| `get_event` | read | One event in full, with its current signature status and whether it was delivered. |
| `verify_signature` | read | HMAC-SHA256 check of `"<timestamp>.<body>"` in constant time, plus a timestamp window (default 300 s, `tolerance_seconds` to change). Reason: `ok`, `bad_signature`, `stale`, `future`. |
| `replay_event` | write | `dry_run` defaults to **true** and only reports what would happen. A real replay needs `--allow-writes` and an `idempotency_key`. Events with a bad or stale signature are refused unless `override_verification` is true. |
| `list_dead_letters` | read | Deliveries that failed after 3 attempts (backoff 1 s then 2 s, computed and reported, never slept). |
| `requeue_dead_letter` | write | Try a dead letter again (3 attempts per requeue, at most 3 requeues, then `abandoned`). `dry_run` defaults to true; a real requeue needs `--allow-writes`. |

**Safety rules the code enforces (and the tests pin):**
- Dry runs always work; real writes are off unless the server is started with `--allow-writes`.
- A replay never double-delivers: the same `idempotency_key` returns the stored first result, and a different key on an already-delivered event reports `already_delivered` without sending again.
- Every tool call is appended to a JSONL audit log. A write first records a `write_intent` line; **if that line cannot be written, the write does not happen.** Overrides are recorded (`override_used`).
- The signing secret is generated at start-up, held in memory, and never printed, logged or committed.
- Stdout carries protocol messages only; status text goes to stderr.

## Run it

```
python -m webhookreplay                       # read-only: dry runs only
python -m webhookreplay --allow-writes        # real replays to the mock sink
python -m webhookreplay --audit-file C:\temp\audit.jsonl --events 100 --tolerance 120
```

The synthetic data has known cases so there is something to find: every 7th event has a tampered payload (bad signature), every 5th is signed correctly but over an hour old (stale), events 11, 22 and 33 make the mock sink fail five times (dead letter, then success on requeue), events 13, 26 and 39 fail once (success on retry).

### Connecting a client (untested)

These snippets are my best reading of the client docs. I have **not** tried them against a real client.

Claude Desktop (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "webhook-replay": {
      "command": "python",
      "args": ["-m", "webhookreplay", "--audit-file", "C:/path/to/webhook_audit.jsonl"],
      "env": { "PYTHONPATH": "C:/path/to/mcp-webhook-replay-server" }
    }
  }
}
```

Claude Code (from inside the repo folder):
```
claude mcp add webhook-replay -- python -m webhookreplay
```

## Tests

```
python -m pytest
```

`tests/mock_client.py` is the client used for the end-to-end tests: it starts the server with `python -m webhookreplay` and exchanges newline-delimited JSON-RPC over its pipes.

## Known gaps

- Hand-written MCP: initialize, notifications, ping, tools/list, tools/call only. No resources, prompts, logging, cancellation, progress or pagination of `tools/list`. Never run against a real client.
- Argument validation uses a small JSON Schema subset I wrote (types, enums, ranges, lengths, required, no extra properties); the advertised schemas are checked structurally, not with a full JSON Schema validator.
- State (delivered set, idempotency ledger, dead letters) lives in memory and resets when the server restarts; only the audit log persists.
- Backoff is reported, not waited: a real sink would need a real scheduler.
- The audit log is append-only by convention, not tamper-evident, and has no rotation.
- The mock sink is deterministic by design; it says nothing about how a real endpoint fails.
- `--fixed-now` pins the clock for tests; it is not meant for normal use.

## License

MIT
