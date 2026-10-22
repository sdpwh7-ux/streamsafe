# streamsafe

**An OpenAI-compatible client that always streams, so long reasoning calls survive proxy timeouts.**

Many OpenAI-compatible endpoints (self-hosted gateways, relays, one-api/new-api style proxies,
corporate egress) sit behind a reverse proxy with a fixed read timeout. Behind Cloudflare that is
roughly 100-125 seconds, after which the client gets `error code: 524`. A *non-streamed* call to
a reasoning model that thinks for three minutes therefore fails even though the model would have
answered, and you pay for the tokens anyway.

Streaming keeps bytes flowing, so the proxy never times out. But most scripts (and most agents
writing scripts) want the simple `stream: false` JSON. streamsafe gives you both:

- `chat_completion(body)` / `responses_create(body)` are drop-in replacements for
  non-streamed calls. Internally they **force `stream: true`** and **reassemble the stream** into
  the exact JSON shape a `stream: false` call returns: text, reasoning content, merged tool-call
  deltas, finish reason and usage (`stream_options.include_usage` is set automatically).
- `chat_stream` / `responses_stream` give you `(text, final_json, status)` plus an `on_delta`
  callback for live output.
- A conservative **retry policy**: only network errors, mid-stream cuts, 429 and 5xx (incl.
  520-524) are retried. Other 4xx errors are never retried. Backoff is 2/6/18 s with jitter,
  under a total wall-clock cap. A separate `NONIDEMPOTENT_RETRY` policy for paid job submissions
  does not retry 500/504/52x, so the same job is not charged twice.
- "Too many concurrent requests" errors (common on single-seat relay accounts) are treated as 429
  with a longer backoff.
- The API key is never printed: error text is redacted, and `download()` only attaches the key
  when the URL's host is the API host.

One small module; the only dependency is `requests`.

## Install

```bash
pip install git+https://github.com/sdpwh7-ux/streamsafe.git
# or just copy streamsafe/ into your project
```

## Usage

```python
from streamsafe import Client

c = Client()  # reads STREAMSAFE_BASE_URL / STREAMSAFE_API_KEY (or OPENAI_BASE_URL / OPENAI_API_KEY)
d = c.chat_completion({"model": "my-reasoning-model",
                       "messages": [{"role": "user", "content": "Prove it step by step."}]})
print(d["choices"][0]["message"]["content"])
print(d["choices"][0]["message"].get("reasoning_content"))
print(d["usage"])

r = c.responses_create({"model": "my-model", "input": "hello", "tools": [{"type": "web_search"}]})

text, final, _ = c.chat_stream({"model": "m", "messages": [...]}, on_delta=lambda s: print(s, end=""))
```

Command line: a streaming drop-in for `curl -d @body.json -o out.json`:

```bash
python -m streamsafe chat examples/body.chat.json out.json        # or: streamsafe chat ...
python -m streamsafe responses body.json - --read-timeout 1200    # '-' = stdout
```

It prints the HTTP-like status to stderr (`200` or the error status) and exits 0 on success, 1 on
failure.

### Usage accounting hook

```python
def on_usage(endpoint, model, final_json, ok, status, error):
    ...  # e.g. append to a JSONL ledger; called once per attempt, exceptions are swallowed

c = Client(on_usage=on_usage)
```

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `STREAMSAFE_BASE_URL` / `OPENAI_BASE_URL` | `https://api.openai.com/v1` | API base URL (include `/v1`) |
| `STREAMSAFE_API_KEY` / `OPENAI_API_KEY` | - | API key |
| `STREAMSAFE_KEY_FILE` | - | read the key from this file instead (recommended, `chmod 600`) |
| `STREAMSAFE_RETRY_MAX_ATTEMPTS` | `3` | attempts per call |
| `STREAMSAFE_RETRY_TOTAL_CAP` | `900` | total seconds across retries |
| `STREAMSAFE_CONCURRENCY_BACKOFF` | `20` | minimum wait after a concurrency-limit error |

All of these can also be passed to `Client(...)` / `RetryPolicy(...)`.

## Security notes for agent tooling

- **Credentials.** Prefer `STREAMSAFE_KEY_FILE` over putting the key on a command line. The key
  is only sent to the configured base URL's host; error messages and retry logs are redacted.
  Redaction only covers *this* key, so do not pass other secrets in prompts.
- **Network.** streamsafe only talks to the base URL you configure (plus any URL you explicitly
  `download()`). Point it only at endpoints you trust: the gateway sees every prompt and response.
- **Retries cost money.** A retried call is a new, billable call. The default policy only retries
  failures where the request most likely produced nothing; use `NONIDEMPOTENT_RETRY` (or
  `RetryPolicy(max_attempts=1)`) for expensive or side-effecting requests.
- **No shell or file access**: the CLI only reads the body file you give it and writes the
  output file you name.

## Tests

```bash
python -m unittest discover -s tests -v   # runs against a local fake SSE server, no network
```

## License

MIT
