"""Delivery channels.

`webhook` posts the digest body as JSON. `stdout` just prints it, which is
what we use locally.

Delivery is best effort: a channel that is down raises DeliveryError, which
digest.run_once catches per channel. The undelivered items are left out of the
`deliveries` ledger, so cron picks them up on the next tick and the other
channels still get their digests. Raise DeliveryError (not a bare exception)
for anything a retry could plausibly fix.
"""

import json
import urllib.error
import urllib.request


class DeliveryError(Exception):
    pass


def send(chan_cfg, body):
    kind = chan_cfg.get("type", "stdout")

    if kind == "stdout":
        print(f"=== {chan_cfg['name']} ===")
        print(body)
        return

    if kind == "webhook":
        payload = json.dumps({"text": body}).encode()
        req = urllib.request.Request(
            chan_cfg["url"],
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status >= 300:
                    raise DeliveryError(f"{chan_cfg['name']}: HTTP {resp.status}")
        except urllib.error.URLError as exc:
            raise DeliveryError(f"{chan_cfg['name']}: {exc}") from exc
        return

    raise DeliveryError(f"unknown channel type {kind!r}")
