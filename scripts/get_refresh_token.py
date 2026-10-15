#!/usr/bin/env python3
"""Exchange a one-time SolarEdge OAuth authorization code for tokens.

Home Assistant's built-in OAuth2 flow cannot be used with SolarEdge (the
authorization callback does not return the `state` parameter), so the first
refresh token has to be obtained by hand. Run this right after approving
access in the browser; authorization codes expire quickly.

Guided (recommended) - prompts for anything not given, opens the browser,
and accepts the full redirect URL you land on (the code is extracted for you):
    python3 get_refresh_token.py

Non-interactive, if you already have a code:
    python3 get_refresh_token.py \
        --client-id YOUR_CLIENT_ID \
        --client-secret YOUR_CLIENT_SECRET \
        --redirect-uri https://the.redirect/uri/registered/with/your/app \
        --code THE_CODE_FROM_THE_REDIRECT_URL

Only the Python standard library is required.
"""
import argparse
import base64
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

TOKEN_URL = "https://monitoringapi.solaredge.com/v2/oauth2/token"


def ask(value, prompt, secret=False):
    if value:
        return value
    return (getpass.getpass if secret else input)(f"{prompt}: ").strip()


def extract_code(text: str) -> str:
    """Accept either a bare code or a full redirect URL containing ?code=..."""
    text = text.strip()
    if "code=" in text:
        query = urllib.parse.urlparse(text).query or text.split("?", 1)[-1]
        codes = urllib.parse.parse_qs(query).get("code")
        if codes:
            return codes[0]
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--redirect-uri")
    parser.add_argument("--code", help="code or full redirect URL")
    parser.add_argument(
        "--authorize-url",
        help="authorize endpoint from the SolarEdge developer guide "
        "(only needed for the guided flow)",
    )
    args = parser.parse_args()

    args.client_id = ask(args.client_id, "Client ID")
    args.client_secret = ask(args.client_secret, "Client secret", secret=True)
    args.redirect_uri = ask(args.redirect_uri, "Redirect URI registered with your app")

    if not args.code:
        authorize = ask(
            args.authorize_url,
            "Authorize endpoint URL (from the SolarEdge guide)",
        )
        sep = "&" if "?" in authorize else "?"
        url = authorize + sep + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": args.client_id,
                "redirect_uri": args.redirect_uri,
            }
        )
        print(f"\nOpening your browser. Log in and approve access:\n\n{url}\n")
        webbrowser.open(url)
        print(
            "You will be redirected to your redirect URI (the page may fail to load).\n"
            "Copy the full address from the browser's address bar and paste it here."
        )
        args.code = input("Redirect URL (or code): ")
    args.code = extract_code(args.code)
    if not args.code:
        print("No code provided.", file=sys.stderr)
        return 1

    basic = base64.b64encode(
        f"{args.client_id}:{args.client_secret}".encode()
    ).decode()
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": args.code,
            "redirect_uri": args.redirect_uri,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            tokens = json.load(resp)
    except urllib.error.HTTPError as err:
        print(f"Token exchange failed ({err.code}): {err.read().decode()}", file=sys.stderr)
        return 1

    refresh = tokens.get("refresh_token")
    if not refresh:
        print(f"No refresh_token in response: {tokens}", file=sys.stderr)
        return 1
    print("Refresh token (paste this into Home Assistant):\n")
    print(refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
