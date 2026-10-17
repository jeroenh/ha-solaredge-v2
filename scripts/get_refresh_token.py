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

Monitoring (for cron): SolarEdge rotates the refresh token on every use and the
integration stores the newest one, so do NOT refresh the token from cron
yourself - that would invalidate Home Assistant's copy. Instead check that the
integration is still healthy through the Home Assistant REST API:
    HA_TOKEN=long_lived_token python3 get_refresh_token.py --check-ha \
        --ha-url http://homeassistant.local:8123 --entity sensor.solaredge_site_<SITEID>_energy_today
It prints nothing and exits 0 when healthy; otherwise it prints how to replace
the token and exits 1. The guided flow ends by printing a ready-made crontab entry (the
HA token is embedded as an argument); --print-cron prints one with placeholders.

Only the Python standard library is required.
"""
import argparse
import base64
import getpass
import json
import os
import shlex
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


CRON_TEMPLATE = """\
# crontab -e   (cron mails any output of a job to MAILTO; this check is silent when healthy)
MAILTO={mailto}
# Every 6 hours:
0 */6 * * * python3 {script} --check-ha --ha-url {ha_url} --ha-token {ha_token} --entity sensor.solaredge_site_{site_id}_energy_today

# Create the HA token in HA: Profile -> Security -> Long-lived access tokens.
# The cron host needs a working local mailer (e.g. msmtp or postfix) for MAILTO.
"""


def build_cron(site_id, ha_url, ha_token, mailto):
    return CRON_TEMPLATE.format(
        mailto=mailto or "you@example.com",
        script=shlex.quote(os.path.abspath(__file__)),
        ha_url=shlex.quote(ha_url),
        ha_token=shlex.quote(ha_token or "YOUR_HA_LONG_LIVED_ACCESS_TOKEN"),
        site_id=site_id or "SITEID",
    )


REPLACE_HELP = """\
The SolarEdge integration looks unhealthy; its refresh token has probably expired or been revoked.
To replace it:
  1. python3 {script}        (guided: log in, approve, paste the redirect URL)
  2. Home Assistant -> Settings -> Devices & Services -> SolarEdge ONE (v2 API)
     -> Re-authenticate (or Reconfigure) and paste the new refresh token.
"""


def check_ha(args) -> int:
    token = args.ha_token or os.environ.get("HA_TOKEN")
    if not (token and args.ha_url and args.entity):
        print("--check-ha needs --ha-url, --entity and HA_TOKEN (or --ha-token)", file=sys.stderr)
        return 2
    req = urllib.request.Request(
        f"{args.ha_url.rstrip('/')}/api/states/{args.entity}",
        headers={"Authorization": "Bearer " + token},
    )
    script = os.path.abspath(__file__)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            state = json.load(resp).get("state")
    except (urllib.error.URLError, OSError) as err:
        print(f"Could not query Home Assistant: {err}\n")
        return 1
    if state in ("unavailable", "unknown", None):
        print(f"{args.entity} is '{state}'.\n")
        print(REPLACE_HELP.format(script=script))
        return 1
    return 0


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
    parser.add_argument("--check-ha", action="store_true", help="health check for cron")
    parser.add_argument("--ha-url", help="e.g. http://homeassistant.local:8123")
    parser.add_argument("--ha-token", help="long-lived token (prefer env HA_TOKEN)")
    parser.add_argument("--entity", help="e.g. sensor.solaredge_site_<SITEID>_energy_today")
    parser.add_argument("--site-id", help="SolarEdge site ID")
    parser.add_argument("--mailto", help="email address for cron output")
    parser.add_argument("--print-cron", action="store_true", help="show crontab suggestion")
    args = parser.parse_args()

    if args.print_cron:
        print(
            build_cron(
                args.site_id,
                args.ha_url or "http://homeassistant.local:8123",
                args.ha_token,
                args.mailto,
            )
        )
        return 0
    if args.check_ha:
        return check_ha(args)

    args.site_id = ask(args.site_id, "Site ID")
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
    print("\nValues to paste into Home Assistant (Re-authenticate/Reconfigure):\n")
    print(f"  Site ID:        {args.site_id}")
    print(f"  Client ID:      {args.client_id}")
    print(f"  Client secret:  {args.client_secret}")
    print(f"  Refresh token:  {refresh}")
    print(
        "\nKeep these private; the integration rotates the refresh token on every use, "
        "so do not reuse it elsewhere."
    )

    print("\nNow let's generate a crontab that emails you when the token needs replacing.")
    ha_url = input("Home Assistant URL [http://homeassistant.local:8123]: ").strip()
    ha_token = getpass.getpass("HA long-lived access token (empty to skip): ").strip()
    if ha_token:
        mailto = input("Email for alerts [you@example.com]: ").strip()
        print("\nAdd this with `crontab -e`:\n")
        print(
            build_cron(
                args.site_id,
                ha_url or "http://homeassistant.local:8123",
                ha_token,
                mailto,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
