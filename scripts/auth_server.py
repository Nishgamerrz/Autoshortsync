#!/usr/bin/env python3
"""One-page authorization server for Google/YouTube AND Instagram/Facebook.

Run it on your computer:

    python scripts/auth_server.py

Then open http://localhost:8080 in your browser:

1. Paste your Google OAuth client ID + secret and your Meta app ID + secret.
2. Click "Authorize Google / YouTube"  -> Google consent screen -> done.
3. Click "Authorize Instagram"         -> Facebook Login -> done.

The page then shows the exact values to paste into GitHub Secrets.

Redirect URIs that must be registered:
    Google : http://localhost:8080/google/callback
    Meta   : http://localhost:8080/instagram/callback
"""

from __future__ import annotations

import argparse
import html
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"

META_AUTH_URL = "https://www.facebook.com/v21.0/dialog/oauth"
META_GRAPH_URL = "https://graph.facebook.com/v21.0"
META_SCOPE = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement"


class AuthState:
    google_client_id = ""
    google_client_secret = ""
    meta_app_id = ""
    meta_app_secret = ""
    port = 8080


STATE = AuthState()


def http_get_json(url: str, params: dict) -> dict:
    separator = "&" if "?" in url else "?"
    request = urllib.request.Request(url + separator + urllib.parse.urlencode(params))
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def http_post_json(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def google_redirect_uri() -> str:
    return f"http://localhost:{STATE.port}/google/callback"


def meta_redirect_uri() -> str:
    return f"http://localhost:{STATE.port}/instagram/callback"


def google_auth_link() -> str:
    params = urllib.parse.urlencode(
        {
            "client_id": STATE.google_client_id,
            "redirect_uri": google_redirect_uri(),
            "response_type": "code",
            "scope": GOOGLE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": "google",
        }
    )
    return f"{GOOGLE_AUTH_URL}?{params}"


def meta_auth_link() -> str:
    params = urllib.parse.urlencode(
        {
            "client_id": STATE.meta_app_id,
            "redirect_uri": meta_redirect_uri(),
            "response_type": "code",
            "scope": META_SCOPE,
            "state": "instagram",
        }
    )
    return f"{META_AUTH_URL}?{params}"


def exchange_google(code: str) -> dict:
    return http_post_json(
        GOOGLE_TOKEN_URL,
        {
            "client_id": STATE.google_client_id,
            "client_secret": STATE.google_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": google_redirect_uri(),
        },
    )


def exchange_meta(code: str) -> dict:
    short = http_get_json(
        f"{META_GRAPH_URL}/oauth/access_token",
        {
            "client_id": STATE.meta_app_id,
            "client_secret": STATE.meta_app_secret,
            "redirect_uri": meta_redirect_uri(),
            "code": code,
        },
    )
    long_lived = http_get_json(
        f"{META_GRAPH_URL}/oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": STATE.meta_app_id,
            "client_secret": STATE.meta_app_secret,
            "fb_exchange_token": short["access_token"],
        },
    )
    user_token = long_lived["access_token"]
    pages = http_get_json(
        f"{META_GRAPH_URL}/me/accounts",
        {"fields": "id,name,instagram_business_account", "access_token": user_token},
    )

    results = []
    for page in pages.get("data", []):
        ig = page.get("instagram_business_account") or {}
        page_token = ""
        try:
            token_info = http_get_json(
                f"{META_GRAPH_URL}/{page['id']}",
                {"fields": "access_token", "access_token": user_token},
            )
            page_token = token_info.get("access_token", "")
        except Exception as exc:  # noqa: BLE001 - show the error on the page
            page_token = f"error: {exc}"
        results.append(
            {
                "page_id": page.get("id", ""),
                "page_name": page.get("name", ""),
                "instagram_account_id": ig.get("id", ""),
                "page_access_token": page_token,
            }
        )
    return {"user_token_expires": long_lived.get("expires_in"), "pages": results}


PAGE_STYLE = """
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e6e8ee;
      margin:0;padding:2rem;display:flex;justify-content:center}
 .card{max-width:760px;width:100%;background:#171a21;border:1px solid #262b36;border-radius:14px;padding:2rem}
 h1{font-size:1.5rem;margin-top:0} h2{font-size:1.1rem;margin-bottom:.4rem}
 code,.val{background:#0b0d12;border:1px solid #2a3040;border-radius:6px;padding:.15rem .4rem;
           font-family:ui-monospace,Menlo,Consolas,monospace;word-break:break-all}
 .val{display:block;padding:.7rem;margin:.4rem 0 1rem}
 button,a.btn{display:inline-block;background:#2f80ed;color:#fff;border:0;border-radius:8px;
              padding:.8rem 1.1rem;font-size:1rem;text-decoration:none;cursor:pointer;margin:.3rem .3rem .3rem 0}
 button.secondary{background:#262b36}
 input{width:100%;box-sizing:border-box;background:#0b0d12;border:1px solid #2a3040;color:#e6e8ee;
       border-radius:8px;padding:.6rem;margin:.3rem 0 .8rem;font-size:.95rem}
 label{font-size:.85rem;color:#9aa3b2}
 .ok{color:#3fb950}.err{color:#f85149}.muted{color:#9aa3b2;font-size:.9rem}
 .row{display:flex;gap:1rem;flex-wrap:wrap}.row>div{flex:1;min-width:260px}
</style>
"""


def page(body: str) -> bytes:
    return f"<!doctype html><html><head><meta charset='utf-8'><title>AutoShortSync Auth</title>{PAGE_STYLE}</head><body><div class='card'>{body}</div></body></html>".encode()


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_html(self, html_text: str, status: int = 200) -> None:
        body = page(html_text)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url: str) -> None:
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self._send_html(self._home())
        elif path == "/links":
            self._send_html(self._links_page())
        elif path == "/google":
            if not STATE.google_client_id:
                self._send_html("<h1>Missing Google credentials</h1><p><a class='btn secondary' href='/'>Back</a></p>")
                return
            self._redirect(google_auth_link())
        elif path == "/google/callback":
            self._google_callback(query)
        elif path == "/instagram":
            if not STATE.meta_app_id:
                self._send_html("<h1>Missing Meta app credentials</h1><p><a class='btn secondary' href='/'>Back</a></p>")
                return
            self._redirect(meta_auth_link())
        elif path == "/instagram/callback":
            self._instagram_callback(query)
        else:
            self._send_html("<h1>Not found</h1><p><a class='btn secondary' href='/'>Home</a></p>", 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/configure":
            self._send_html("<h1>Not found</h1>", 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        STATE.google_client_id = (form.get("google_client_id", [""])[0] or "").strip()
        STATE.google_client_secret = (form.get("google_client_secret", [""])[0] or "").strip()
        STATE.meta_app_id = (form.get("meta_app_id", [""])[0] or "").strip()
        STATE.meta_app_secret = (form.get("meta_app_secret", [""])[0] or "").strip()
        self._send_html(
            "<h1>Credentials saved</h1><p class='muted'>You can now click the authorization buttons.</p>"
            "<p><a class='btn' href='/links'>Open authorization links</a></p>"
        )

    def _home(self) -> str:
        creds_ready = bool(STATE.google_client_id or STATE.meta_app_id)
        form = f"""
<h1>AutoShortSync authorization</h1>
<p class="muted">Paste your app credentials, then use the two authorization buttons.</p>
<form method="post" action="/configure">
  <h2>Google / YouTube</h2>
  <label>Client ID</label>
  <input name="google_client_id" value="{html.escape(STATE.google_client_id)}" placeholder="xxxx.apps.googleusercontent.com">
  <label>Client secret</label>
  <input name="google_client_secret" type="password" value="{html.escape(STATE.google_client_secret)}" placeholder="GOCSPX-...">
  <div class="muted">Redirect URI to register in Google Cloud: <code>{google_redirect_uri()}</code></div>
  <h2>Meta / Instagram</h2>
  <label>App ID</label>
  <input name="meta_app_id" value="{html.escape(STATE.meta_app_id)}" placeholder="1234567890">
  <label>App secret</label>
  <input name="meta_app_secret" type="password" value="{html.escape(STATE.meta_app_secret)}" placeholder="App secret">
  <div class="muted">Redirect URI to register in the Meta app (Facebook Login → Valid OAuth Redirect URIs): <code>{meta_redirect_uri()}</code></div>
  <button type="submit">Save credentials</button>
</form>
{"<p><a class='btn' href='/links'>Open authorization links</a></p>" if creds_ready else ""}
"""
        return form

    def _links_page(self) -> str:
        google_link = google_auth_link() if STATE.google_client_id else ""
        meta_link = meta_auth_link() if STATE.meta_app_id else ""
        return f"""
<h1>Authorization links</h1>
<h2>1. Google / YouTube</h2>
<p><a class="btn" href="/google">Authorize Google / YouTube</a></p>
<p class="muted">Raw link (right-click → copy):</p>
<code>{html.escape(google_link) or 'save your Google credentials first'}</code>
<h2>2. Instagram / Facebook</h2>
<p><a class="btn" href="/instagram">Authorize Instagram</a></p>
<p class="muted">Raw link (right-click → copy):</p>
<code>{html.escape(meta_link) or 'save your Meta credentials first'}</code>
<p><a class="btn secondary" href="/">Back</a></p>
"""

    def _google_callback(self, query: dict) -> None:
        if query.get("error"):
            self._send_html(f"<h1 class='err'>Google authorization failed</h1><p>{html.escape(query['error'][0])}</p><p><a class='btn secondary' href='/'>Back</a></p>")
            return
        code = (query.get("code") or [""])[0]
        if not code:
            self._send_html("<h1 class='err'>No code returned</h1><p><a class='btn secondary' href='/'>Back</a></p>")
            return
        try:
            tokens = exchange_google(code)
        except Exception as exc:  # noqa: BLE001
            self._send_html(f"<h1 class='err'>Token exchange failed</h1><p>{html.escape(str(exc))}</p><p><a class='btn secondary' href='/'>Back</a></p>")
            return

        refresh = tokens.get("refresh_token", "")
        if not refresh:
            self._send_html(
                "<h1 class='err'>No refresh token returned</h1>"
                "<p>Revoke access at https://myaccount.google.com/permissions and try again.</p>"
                "<p><a class='btn secondary' href='/'>Back</a></p>",
            )
            return

        body = f"""
<h1 class="ok">Google / YouTube connected</h1>
<p>Add these GitHub secrets (Settings → Secrets and variables → Actions):</p>
<label>YOUTUBE_CLIENT_ID</label><span class="val">{html.escape(STATE.google_client_id)}</span>
<label>YOUTUBE_CLIENT_SECRET</label><span class="val">{html.escape(STATE.google_client_secret)}</span>
<label>YOUTUBE_REFRESH_TOKEN</label><span class="val">{html.escape(refresh)}</span>
<p class="muted">Scope: {html.escape(tokens.get('scope', ''))}</p>
<p><a class="btn" href="/links">Authorize Instagram next</a></p>
"""
        self._send_html(body)

    def _instagram_callback(self, query: dict) -> None:
        if query.get("error"):
            self._send_html(f"<h1 class='err'>Instagram authorization failed</h1><p>{html.escape(query['error'][0])}</p><p><a class='btn secondary' href='/'>Back</a></p>")
            return
        code = (query.get("code") or [""])[0]
        if not code:
            self._send_html("<h1 class='err'>No code returned</h1><p><a class='btn secondary' href='/'>Back</a></p>")
            return
        try:
            result = exchange_meta(code)
        except Exception as exc:  # noqa: BLE001
            self._send_html(f"<h1 class='err'>Token exchange failed</h1><p>{html.escape(str(exc))}</p><p><a class='btn secondary' href='/'>Back</a></p>")
            return

        rows = ""
        for page in result["pages"]:
            if not page["instagram_account_id"]:
                rows += f"""
<h2>{html.escape(page['page_name'])} <span class="muted">(no Instagram Business account linked)</span></h2>
"""
                continue
            rows += f"""
<h2>{html.escape(page['page_name'])}</h2>
<label>INSTAGRAM_ACCOUNT_ID</label><span class="val">{html.escape(page['instagram_account_id'])}</span>
<label>INSTAGRAM_ACCESS_TOKEN (Page token)</label><span class="val">{html.escape(page['page_access_token'])}</span>
"""
        if not result["pages"]:
            rows = "<p class='muted'>No Facebook Pages found for this account.</p>"

        self._send_html(
            f"<h1 class='ok'>Instagram connected</h1>{rows}"
            "<p><a class='btn secondary' href='/'>Back</a></p>"
        )

    def log_message(self, *args) -> None:  # silence request logging
        pass


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoShortSync authorization server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--google-client-id", default=os.environ.get("YOUTUBE_CLIENT_ID", ""))
    parser.add_argument("--google-client-secret", default=os.environ.get("YOUTUBE_CLIENT_SECRET", ""))
    parser.add_argument("--meta-app-id", default=os.environ.get("META_APP_ID", ""))
    parser.add_argument("--meta-app-secret", default=os.environ.get("META_APP_SECRET", ""))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    STATE.port = args.port
    STATE.google_client_id = args.google_client_id.strip()
    STATE.google_client_secret = args.google_client_secret.strip()
    STATE.meta_app_id = args.meta_app_id.strip()
    STATE.meta_app_secret = args.meta_app_secret.strip()

    if not port_is_free(args.port):
        print(f"Port {args.port} is in use. Pick another with --port.", file=sys.stderr)
        return 2

    server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("\nAutoShortSync authorization server running.")
    print(f"  Open:  http://localhost:{args.port}")
    print(f"  Google redirect URI:  {google_redirect_uri()}")
    print(f"  Meta redirect URI:    {meta_redirect_uri()}")
    if not args.no_browser:
        try:
            webbrowser.open(f"http://localhost:{args.port}")
        except Exception:
            pass

    print("\nPress Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
