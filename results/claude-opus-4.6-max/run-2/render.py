"""Digest rendering."""

import textwrap


def digest(items, chan_cfg):
    lines = [f"{chan_cfg.get('title', 'Digest')} — {len(items)} item(s)", ""]
    for item in items:
        lines.append(f"* {item['title']}  [{item['source']}]")
        summary = (item.get("summary") or "").strip()
        if summary:
            lines.extend(textwrap.wrap(summary, width=76, initial_indent="  ", subsequent_indent="  "))
        lines.append(f"  {item['link']}")
        lines.append("")
    return "\n".join(lines)
