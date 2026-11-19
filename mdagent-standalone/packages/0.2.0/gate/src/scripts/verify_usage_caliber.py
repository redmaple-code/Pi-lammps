#!/usr/bin/env python3
"""Verify the relay's usage caliber before trusting compaction thresholds.

Mode 1 — two identical requests (network, needs a key):
    python scripts/verify_usage_caliber.py --probe --base-url https://host/v1 \
        --model deepseek-v4.1-flash --key-env DEEPSEEK_API_KEY

    If the second response has cache_read_input_tokens > 0 while input_tokens is
    unchanged, the endpoint reports OpenAI-style inclusive input. The harness must
    subtract cacheRead before summing disjoint buckets.

Mode 2 — offline session check (no key):
    python scripts/verify_usage_caliber.py --session session.jsonl

    Each line must carry a usage object with any of input/input_tokens and
    cache_read/cache_read_input_tokens. Prints negative adjacent increments under
    both calibers; a real transcript has none under the correct caliber.
"""
import argparse
import json
import os
import sys
import urllib.request


def _post(url, key, payload):
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": key,
        "authorization": "Bearer " + key,
        "anthropic-version": "2023-06-01",
    })
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def _usage(payload):
    usage = payload.get("usage") or {}
    return {"input": usage.get("input_tokens", usage.get("input", 0)) or 0,
            "cache": usage.get("cache_read_input_tokens", usage.get("cache_read", 0)) or 0,
            "output": usage.get("output_tokens", usage.get("output", 0)) or 0}


def probe(args):
    key = os.environ.get(args.key_env, "")
    if not key:
        print("missing key in env %s" % args.key_env, file=sys.stderr)
        return 2
    url = args.base_url.rstrip("/") + "/messages"
    payload = {"model": args.model, "max_tokens": 16,
               "messages": [{"role": "user", "content": args.prompt}]}
    first = _usage(_post(url, key, payload))
    second = _usage(_post(url, key, payload))
    print(json.dumps({"first": first, "second": second}, ensure_ascii=False, indent=2))
    inclusive = (second["cache"] > 0 and abs(second["input"] - first["input"]) <= 8)
    print("caliber:", "openai-inclusive (subtract cacheRead)"
          if inclusive else "anthropic-disjoint or no cache hit")
    return 0


def session(args):
    rows = []
    with open(args.session, encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            usage = _usage(row)
            if usage["input"] or usage["cache"]:
                rows.append(usage)
    if len(rows) < 2:
        print("not enough usage rows: %d" % len(rows), file=sys.stderr)
        return 2

    def negatives(fn):
        values = [fn(row) for row in rows]
        return sum(1 for previous, current in zip(values, values[1:]) if current < previous)

    disjoint = negatives(lambda row: row["input"] + row["output"])
    inclusive = negatives(lambda row: row["input"] + row["cache"] + row["output"])
    print(json.dumps({"rows": len(rows),
                      "negative_steps_openai_caliber": disjoint,
                      "negative_steps_anthropic_caliber": inclusive,
                      "verdict": ("endpoint is openai-inclusive; patch mapUsage"
                                  if disjoint == 0 < inclusive else "inconclusive")},
                     ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--session")
    parser.add_argument("--base-url", default="https://aispot.swj0227.icu/v1")
    parser.add_argument("--model", default="deepseek-v4.1-flash")
    parser.add_argument("--key-env", default="DEEPSEEK_API_KEY")
    parser.add_argument("--prompt", default="Reply with the single word: ok")
    args = parser.parse_args(argv)
    if args.session:
        return session(args)
    if args.probe:
        return probe(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
