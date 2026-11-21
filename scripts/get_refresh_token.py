#!/usr/bin/env python3
"""One-time helper that mints a Google OAuth refresh token for YouTube.

Run this on your own computer (not in GitHub Actions):

    python scripts/get_refresh_token.py \
        --client-id YOUR_CLIENT_ID \
        --client-secret YOUR_CLIENT_SECRET

It opens your browser, you pick the channel and click "Allow", and the script
prints the refresh token plus the exact GitHub secrets to create.

The redirect URI used is ``http://localhost:8080/callback`` - register that
exact URL under "Authorized redirect URIs" in your Google Cloud OAuth client.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser

from google_oauth import (
    DEFAULT_SCOPES,
    GoogleOAuthError,
    build_auth_url,
    exchange_code,
)

REDIRECT_PATH = "/callback"
SUCCESS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>AutoShortSync</title></head>
<body style="font-family:system-ui;text-align:center;padding-top:4rem">
<h1>Connected</h1>
<p>You can close this tab and return to the terminal.</p>
</body></html>"""


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != REDIRECT_PATH:
            self.send_response(404)
            self.end_headers()
            return

        query = urllib.parse.parse_qs(parsed.query)
        CallbackHandler.result = {
            "code": (query.get("code") or [None])[0],
            "error": (query.get("error") or [None])[0],
        }
        body = SUCCESS_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence request logging
        pass


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mint a Google OAuth refresh token")
    parser.add_argument("--client-id", default=os.environ.get("YOUTUBE_CLIENT_ID"))
    parser.add_argument("--client-secret", default=os.environ.get("YOUTUBE_CLIENT_SECRET"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--scope",
        action="append",
        default=None,
        help="OAuth scope (repeatable). Defaults to YouTube read-only.",
    )
    parser.add_argument("--output", default=None, help="Write tokens to this JSON file")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not args.client_id or not args.client_secret:
        print(
            "ERROR: --client-id and --client-secret are required "
            "(or set YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET).",
            file=sys.stderr,
        )
        return 2

    if not port_is_free(args.port):
        print(f"Port {args.port} is already in use. Pick another with --port.", file=sys.stderr)
        return 2

    redirect_uri = f"http://localhost:{args.port}{REDIRECT_PATH}"
    scopes = args.scope or DEFAULT_SCOPES

    CallbackHandler.result = {}
    server = http.server.HTTPServer(("127.0.0.1", args.port), CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    auth_url = build_auth_url(args.client_id, redirect_uri, scopes)
    print("\n1. Make sure this exact redirect URI is registered in Google Cloud:")
    print(f"   {redirect_uri}\n")
    print("2. Open this URL and approve access:\n")
    print(f"   {auth_url}\n")
    if not args.no_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    print("Waiting for the browser callback ...")
    deadline = time.time() + 300
    while not CallbackHandler.result:
        if time.time() > deadline:
            print("Timed out waiting for authorization.", file=sys.stderr)
            server.shutdown()
            return 1
        time.sleep(0.2)
    server.shutdown()

    result = CallbackHandler.result
    if result.get("error"):
        print(f"Authorization failed: {result['error']}", file=sys.stderr)
        return 1
    if not result.get("code"):
        print("Authorization response did not contain a code.", file=sys.stderr)
        return 1

    try:
        tokens = exchange_code(
            args.client_id, args.client_secret, result["code"], redirect_uri
        )
    except GoogleOAuthError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not tokens.get("refresh_token"):
        print(
            "Google did not return a refresh token. Delete any existing consent at "
            "https://myaccount.google.com/permissions and run again; the consent "
            "screen must be shown (prompt=consent).",
            file=sys.stderr,
        )
        return 1

    output = {
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "refresh_token": tokens["refresh_token"],
        "scope": tokens.get("scope", " ".join(scopes)),
    }

    print("\nSUCCESS. Add these GitHub secrets (Settings -> Secrets and variables -> Actions):\n")
    print(f"   YOUTUBE_CLIENT_ID      = {output['client_id']}")
    print(f"   YOUTUBE_CLIENT_SECRET  = {output['client_secret']}")
    print(f"   YOUTUBE_REFRESH_TOKEN  = {output['refresh_token']}\n")
    print("Scope granted:", output["scope"])

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(output, handle, indent=2)
            handle.write("\n")
        print(f"Tokens written to {args.output} (keep it secret, do not commit it).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
