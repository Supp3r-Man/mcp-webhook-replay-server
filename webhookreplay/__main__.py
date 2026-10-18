import argparse
import sys

from .server import build_server, serve


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m webhookreplay",
                                     description="MCP server (stdio) for inspecting and replaying synthetic webhook events.")
    parser.add_argument("--allow-writes", action="store_true",
                        help="allow real replays and requeues (to the in-process mock sink); dry runs always work")
    parser.add_argument("--audit-file", default="webhook_audit.jsonl", help="JSONL audit log (default: %(default)s)")
    parser.add_argument("--events", type=int, default=40, help="number of synthetic events (default: %(default)s)")
    parser.add_argument("--tolerance", type=int, default=300, help="signature timestamp tolerance in seconds (default: %(default)s)")
    parser.add_argument("--fixed-now", type=int, default=None, help="testing: pin the clock to this epoch second")
    args = parser.parse_args(argv)

    # Newline-delimited JSON: keep "\n" as written (no CRLF translation on Windows) and UTF-8 on both pipes.
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    sys.stdin.reconfigure(encoding="utf-8")

    server = build_server(args.allow_writes, args.audit_file, args.events, args.tolerance, args.fixed_now)
    # Human-readable status goes to stderr: stdout is reserved for protocol messages. The secret is never printed.
    print(f"webhook-replay MCP server ready: {args.events} synthetic events, "
          f"{'writes enabled' if args.allow_writes else 'read-only (dry runs only)'}, audit log {args.audit_file}",
          file=sys.stderr)
    serve(server, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
