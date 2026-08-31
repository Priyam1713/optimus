"""Ask the provider which models this key may use. Prints names, never the key.

The key is read out of the env file by this process and handed straight to the
provider SDK. It is never printed, logged, or returned — the only output is the
list of model ids, which is what we actually need in order to pick one.

    python scripts/list_models.py --env-file E:/Test/state/harbor.env
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_key(env_file: Path, name: str) -> str:
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip("'\"")
    raise SystemExit(f"{name} not found in {env_file}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--var", default="GEMINI_API_KEY")
    parser.add_argument("--filter", default="")
    args = parser.parse_args()

    key = load_key(Path(args.env_file), args.var)
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}&pageSize=200"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            import json

            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        # Deliberately not echoing the URL: it carries the key.
        print(f"HTTP {exc.code} from the models endpoint", file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print(f"could not reach the provider: {exc.reason}", file=sys.stderr)
        return 2

    rows = []
    for model in payload.get("models", []):
        name = model.get("name", "").removeprefix("models/")
        methods = model.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        if args.filter and args.filter not in name:
            continue
        rows.append(name)

    for name in sorted(rows):
        print(name)
    print(f"\n{len(rows)} model(s) usable for generateContent", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
