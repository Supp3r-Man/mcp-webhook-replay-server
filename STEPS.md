# STEPS

**Part A** is what I did, in order. **Part B** is how you run it and connect it to a client.

---

## Part A: steps taken

1. **Looked at the existing MCP code** in `customer-ops-agent` to reuse its approach. It uses the official `mcp` SDK, so there was no hand-written protocol layer to copy. Decision: implement the protocol directly (newline-delimited JSON-RPC 2.0 over stdio, stdlib only) so the repo has no dependencies and the conformance behaviour is visible and testable.
2. **Designed the tool surface** around one idea: reads are free, writes are gated. Six tools (`list_events`, `get_event`, `verify_signature`, `replay_event`, `list_dead_letters`, `requeue_dead_letter`); the two write tools default to `dry_run`.
3. **Wrote the signing module:** HMAC-SHA256 over `"<timestamp>.<body>"`, constant-time compare, inclusive tolerance window, signature checked before the clock so a wrong signature is always reported as such.
4. **Wrote the synthetic event generator** with deterministic fault patterns (tampered payloads, stale events, sink failures) so every code path has an event that exercises it. Fictional companies and ids only; a test scans the payloads for card-number, SSN and email shapes.
5. **Wrote the delivery layer:** in-process mock sink, at most 3 attempts per round, backoff 1 s, 2 s, ... capped at 30 s (computed and reported, never slept), idempotency ledger keyed by event id plus key, a delivered set so a new key cannot double-deliver, dead letters, and bounded requeues (3, then abandoned).
6. **Wrote the audit log** (JSONL, one line per call, sequence numbers, never the secret) and made writes **fail closed**: a `write_intent` line is written before the sink is touched, and if it cannot be written the write is refused.
7. **Wrote the protocol layer:** initialize with version negotiation, `notifications/*` (never answered), ping, tools/list, tools/call; error codes -32700 (parse), -32600 (invalid request, including batches), -32601 (unknown method), -32602 (bad params or arguments), -32002 (not initialized); tool failures are results with `isError: true`, not protocol errors; a small schema validator for tool arguments.
8. **Wrote 103 tests** in five files: signing and data (17), delivery (12), tools and audit (20), protocol conformance (48), real-subprocess end-to-end through `tests/mock_client.py` (6; a reader thread and a timeout so a hung server fails the test instead of hanging it). All pass.
9. **Mutation-checked the tests:** 10 deliberate breaks, applied to a copy, one at a time. The script reports "pattern missed" if a break did not apply. All 10 were caught on the first run, and I printed which test caught each to make sure it was a meaningful failure and not a crash.
10. **Wrote the README and this file**, then committed locally. (The push and the first CI run happen afterwards; the README says the CI had not run when it was written.)

Problems and choices along the way:
- Windows text-mode stdout would turn `\n` into `\r\n`, so `__main__` reconfigures stdio to UTF-8 with plain newlines.
- I left two pieces of junk in the first draft of the tests (a dead `if False else` in the mock client's command line and a nonsense first assertion in one test); caught on re-reading and replaced before the first run.
- `-x` in the mutation runner hides how many tests catch a break, so the numbers above are "caught", not "how many tests caught it".

---

## Part B: steps for you

### Run it
1. Needs Python 3.11 or newer. No packages to install (pytest only for the tests).
2. From the repo folder: `python -m webhookreplay` (read-only) or `python -m webhookreplay --allow-writes`.
3. It waits for JSON-RPC lines on stdin. To try it by hand, paste these one at a time:
   ```
   {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"hand","version":"0"}}}
   {"jsonrpc":"2.0","method":"notifications/initialized"}
   {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_events","arguments":{"status":"bad_signature"}}}
   ```
   Press Ctrl+Z then Enter (Windows) or Ctrl+D (macOS/Linux) to end the session.

### Run the tests
```
python -m pip install pytest
python -m pytest
```
Expect `103 passed`.

### Connect it to a client (not tested by me)
- **Claude Desktop:** add the `mcpServers` entry from the README to `claude_desktop_config.json` (Settings → Developer → Edit Config), use absolute paths, restart the app, and look for `webhook-replay` in the tools list.
- **Claude Code:** from inside the repo folder run `claude mcp add webhook-replay -- python -m webhookreplay`, then `claude mcp list`.
- If a client reports a protocol or schema error, send me its message: the protocol is hand-written and this is the first real client it would meet.

### Try a safe replay
1. Ask the client to list events with status `bad_signature`, then to verify one.
2. Ask for a **dry run** replay of a valid event (`evt_0001`): it reports what would happen and changes nothing.
3. Restart with `--allow-writes`, then replay `evt_0001` with `dry_run` false and an idempotency key. Repeat with the same key (same result, no second delivery), then with a new key (`already_delivered`).
4. Replay `evt_0011` for a dead letter, then requeue it. Check the audit file (`webhook_audit.jsonl` by default) for `write_intent` lines.
