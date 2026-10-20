"""Delivery channels.

`webhook` posts the digest body as JSON. `stdout` just prints it, which is
what we use locally. Delivery is best effort: if a channel is down we let the
exception propagate and cron will pick us up again on the next tick.
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
