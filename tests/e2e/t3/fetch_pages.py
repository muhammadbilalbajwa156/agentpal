"""Re-fetch public page evidence into /tmp, preserving frozen acceptance fixtures.

From agentpal/: python tests/e2e/t3/fetch_pages.py --out /tmp/t3-pages-fresh
No authentication, cookies, browser execution, or paid API calls.
"""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("/tmp/t3-pages-fresh"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    frozen = json.loads((Path(__file__).parent / "pages/manifest.json").read_text())
    rows = []
    for index, source in enumerate(frozen, 1):
        url = source["url"]
        row = {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat()}
        try:
            # Separate client per public page; no cookie state crosses retailers.
            with httpx.Client(timeout=20, follow_redirects=True) as client:
                with client.stream("GET", url) as response:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > 2_000_000:
                            break
                    row.update(status=response.status_code, final_url=str(response.url), bytes=len(body))
                    if response.status_code == 200 and len(body) <= 2_000_000:
                        name = f"page_{index}.html.gz"
                        (args.out / name).write_bytes(gzip.compress(bytes(body), mtime=0))
                        row.update(fixture=name, sha256=hashlib.sha256(body).hexdigest())
        except httpx.HTTPError:
            row["status"] = "BLOCKED"
        rows.append(row)
        print(index, row["status"])
    (args.out / "manifest.json").write_text(json.dumps(rows, indent=2) + "\n")


if __name__ == "__main__":
    main()
