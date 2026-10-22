#!/usr/bin/env python3
"""Detect whether the local QQ client has restored exactly one logged-in account.

This mirrors the PT-login probe SnowLuma itself uses.  It is intentionally
standalone so the supervisord wrapper and the host-side watchdog can use the
same signal without requiring SnowLuma's WebUI or its admin password.

Exit code 0 means "one account is available"; exit code 1 means "not ready".
No account identifiers or credentials are printed.
"""
import json
import ssl
import sys
import urllib.request

PORTS = (4301, 4303, 4305, 4307, 4309)
TIMEOUT_SECONDS = 3


def fetch_accounts(port):
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(
        f"https://127.0.0.1:{port}/pt_get_uins?callback=ptui_getuins_CB&pt_local_tk=0",
        headers={
            "Host": "localhost.ptlogin2.qq.com",
            "Referer": "https://xui.ptlogin2.qq.com/",
            "Cookie": "pt_local_token=0",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=context) as response:
        text = response.read().decode("utf-8", "replace")
    if "[" not in text or "]" not in text:
        return []
    inner = text.split("[", 1)[1].split("]", 1)[0]
    try:
        value = json.loads("[" + inner + "]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def main():
    for port in PORTS:
        try:
            accounts = fetch_accounts(port)
        except Exception:
            continue
        if len(accounts) == 1:
            print(f"login probe: one account on port {port}")
            return 0
        # SnowLuma probes twice and keeps the shorter answer; a single empty
        # read during QQ's own state change should not be treated as final.
        try:
            accounts = fetch_accounts(port)
        except Exception:
            accounts = []
        if len(accounts) == 1:
            print(f"login probe: one account on port {port}")
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
