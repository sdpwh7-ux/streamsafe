"""streamsafe CLI - a streaming drop-in for `curl ... -d @body.json -o out.json`.

  python -m streamsafe responses BODY.json OUT.json [--read-timeout 900] [--base-url URL]
  python -m streamsafe chat      BODY.json OUT.json [--read-timeout 900] [--base-url URL]
  (BODY may be '-' for stdin, OUT may be '-' for stdout)

Reads a request body (any "stream" value is ignored), calls the endpoint WITH stream:true and
writes the JSON a stream:false call would have returned. Prints the HTTP-like status to stderr
("200" or the error status); exit 0 on success, 1 on failure. The key is never printed.
"""
import argparse
import json
import sys

from . import Client, RetryableHTTP


def main(argv=None):
    ap = argparse.ArgumentParser(prog="streamsafe", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("endpoint", choices=["responses", "chat"])
    ap.add_argument("body")
    ap.add_argument("out")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--read-timeout", type=int, default=900)
    a = ap.parse_args(argv)
    if a.body == "-":
        body = json.load(sys.stdin)
    else:
        with open(a.body, encoding="utf-8") as f:
            body = json.load(f)
    body.pop("stream", None)
    c = Client(base_url=a.base_url)
    to = (20, a.read_timeout)
    try:
        d = c.chat_completion(body, timeout=to) if a.endpoint == "chat" else c.responses_create(body, timeout=to)
        code, rc = 200, 0
    except RetryableHTTP as e:
        d, code, rc = {"error": {"message": c.redact(str(e))}}, e.status, 1
    except Exception as e:  # noqa: BLE001
        msg = c.redact(str(e))
        code = 0
        if msg.startswith("HTTP "):
            try:
                code = int(msg.split()[1].rstrip(":"))
            except (IndexError, ValueError):
                pass
        d, rc = {"error": {"message": msg}}, 1
    s = json.dumps(d, ensure_ascii=False)
    if a.out == "-":
        print(s)
    else:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(s)
    print(code, file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
