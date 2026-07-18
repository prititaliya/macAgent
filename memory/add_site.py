"""Interactive CLI: register a purpose → URL site mapping."""

from __future__ import annotations

import sys

from memory.sqlite import ContextMemory, normalize_url


def main() -> int:
    print("MacAgent — add a purpose-tagged website")
    print("-" * 40)
    raw_url = input("Website URL: ").strip()
    if not raw_url:
        print("URL is required.", file=sys.stderr)
        return 1
    purpose = input("Purpose (what this site is for): ").strip()
    if not purpose:
        print("Purpose is required.", file=sys.stderr)
        return 1

    url = normalize_url(raw_url)
    memory = ContextMemory()
    site = memory.add_purpose_site(url, purpose)
    print(f"Saved site id={site['id']}")
    print(f"  url:     {site['url']}")
    print(f"  purpose: {site['purpose']}")
    print("Speak naturally about this purpose; MacAgent will open the URL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
