"""Request figure jobs from a running API; dry run unless --execute is set."""

import argparse
import json
import os

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paper_ids", nargs="+", help="explicit paper UUIDs; no implicit library scan"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8077")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    headers = (
        {"X-API-Key": os.environ["PAPER_API_KEY"]}
        if os.environ.get("PAPER_API_KEY")
        else {}
    )
    with httpx.Client(base_url=args.url, headers=headers, timeout=30) as client:
        for paper_id in dict.fromkeys(args.paper_ids):
            if not args.execute:
                print(
                    json.dumps(
                        {"paper_id": paper_id, "dry_run": True, "force": args.force}
                    )
                )
                continue
            response = client.post(
                f"/api/papers/{paper_id}/figures/extract", json={"force": args.force}
            )
            response.raise_for_status()
            print(json.dumps(response.json(), ensure_ascii=False))


if __name__ == "__main__":
    main()
