#!/usr/bin/env python3
"""Read headless Claude Code's output (`claude -p --output-format stream-json --verbose`, or plain `json`) and
print the answer's text, the JSON "result". Raw output that isn't JSON (an older call, a crash) is printed as is.

    claude-result.py RAW [--model-file FILE]

--model-file: write the model that wrote the answer there, e.g. "claude-opus-5-5": the `message.model` of the last
top-level assistant message in the stream (a subagent's messages carry a parent_tool_use_id and are skipped), so a
turn that moved to a fallback model names the one that answered. Nothing is written when the stream doesn't say
(plain json output, or no assistant message): a reply is never labelled with a model it may not have come from.

Exit 3 when the call ended in an API error that belongs to the account (a usage or spend limit, HTTP 401/402/429),
so claude-failover.sh can move to the next account; otherwise 0.
"""
import json
import re
import sys

LIMIT = re.compile(r"hit your limit|usage limit|spend limit|limit reached|credit balance|out of credits|billing_error"
                   r"|rate_limit", re.I)


def parse(raw: str) -> tuple[dict | None, str | None]:
    """The result object and the answering model from claude's output; (None, None) when it isn't claude's JSON."""
    result, model = None, None
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("type") == "result":
            result = d
        elif d.get("type") == "assistant" and not d.get("parent_tool_use_id"):
            m = (d.get("message") or {}).get("model")
            if isinstance(m, str) and m.strip() and m.strip() != "<synthetic>":
                model = m.strip()
    return result, model


def main() -> int:
    args = sys.argv[1:]
    model_file = None
    if "--model-file" in args:
        i = args.index("--model-file")
        model_file = args[i + 1]
        del args[i:i + 2]
    raw = open(args[0], encoding="utf-8", errors="replace").read()
    r, model = parse(raw)
    if r is None:
        sys.stdout.write(raw)
        return 0
    text = r.get("result") or ""
    sys.stdout.write(text + ("\n" if text and not text.endswith("\n") else ""))
    if model_file and model and not r.get("is_error"):
        with open(model_file, "w", encoding="utf-8") as f:
            f.write(model + "\n")
    acct = r.get("api_error_status") in (401, 402, 429) or LIMIT.search(text)
    return 3 if r.get("is_error") and acct else 0


if __name__ == "__main__":
    sys.exit(main())
