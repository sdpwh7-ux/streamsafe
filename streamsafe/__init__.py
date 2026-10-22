"""streamsafe - an OpenAI-compatible client that always streams.

Why: many OpenAI-compatible gateways sit behind a reverse proxy (Cloudflare, nginx, a load
balancer) with a fixed idle/read timeout, e.g. Cloudflare's ~100-125 s limit that turns into
"error code: 524". A *non-streamed* request to a reasoning model that thinks longer than that
dies even though the upstream would have answered. Streaming keeps bytes flowing, so the proxy
never times out.

This module:
  * forces ``stream: true`` on /chat/completions and /responses;
  * reassembles the stream into exactly the JSON shape a ``stream: false`` call returns
    (``chat_completion`` / ``responses_create`` are drop-in replacements);
  * retries only what is safe to retry (network errors, 429, 5xx incl. 520-524, mid-stream
    cuts), with exponential backoff, jitter and a total wall-clock cap;
  * never prints or logs the API key (error text is redacted).

Configuration (constructor arguments override environment variables):
  STREAMSAFE_BASE_URL  or OPENAI_BASE_URL   default https://api.openai.com/v1
  STREAMSAFE_API_KEY   or OPENAI_API_KEY    or a file named by STREAMSAFE_KEY_FILE
  STREAMSAFE_RETRY_MAX_ATTEMPTS (3), STREAMSAFE_RETRY_TOTAL_CAP (900 s),
  STREAMSAFE_CONCURRENCY_BACKOFF (20 s)
"""
import json
import os
import random
import sys
import time
import urllib.parse

import requests

__version__ = "0.1.0"
__all__ = ["Client", "RetryPolicy", "RetryableHTTP", "NONIDEMPOTENT_RETRY", "with_retry", "response_text"]

DEFAULT_BASE_URL = "https://api.openai.com/v1"
# Some CDNs block the default python-requests User-Agent; send a neutral one.
DEFAULT_UA = f"streamsafe/{__version__}"
# Substrings in an error body that mean "too many concurrent requests on this account":
# treated as 429 with a longer backoff.
CONCURRENCY_MARKERS = ("concurrency_limit", "too many concurrent")


# ---------------------------------------------------------------------------
# Retry policy
#   * max 3 attempts, backoff 2 s / 6 s / 18 s with +-25 % jitter
#   * retry ONLY on: network errors (connect/read timeout, reset, broken chunked stream),
#     HTTP 429, HTTP 5xx (incl. Cloudflare 520-524)
#   * NEVER retry other 4xx (401/403 auth, 400/404/413/422 validation ...)
#   * total wall-clock cap; a retry is skipped if the next sleep would exceed it
# ---------------------------------------------------------------------------
class RetryPolicy:
    def __init__(self, max_attempts=None, delays=(2.0, 6.0, 18.0), jitter=0.25,
                 total_cap=None, retry_statuses=None, label="streamsafe"):
        self.max_attempts = int(max_attempts or os.environ.get("STREAMSAFE_RETRY_MAX_ATTEMPTS", 3))
        self.delays = tuple(delays)
        self.jitter = jitter
        self.total_cap = float(total_cap if total_cap is not None
                               else os.environ.get("STREAMSAFE_RETRY_TOTAL_CAP", 900))
        # None = default rule (429 + any 5xx); otherwise an explicit set of codes
        self.retry_statuses = set(retry_statuses) if retry_statuses is not None else None
        self.label = label

    def status_retryable(self, code):
        if code is None:
            return True  # network error
        if self.retry_statuses is not None:
            return code in self.retry_statuses
        return code == 429 or 500 <= code <= 599

    def delay(self, attempt_idx):
        d = self.delays[min(attempt_idx, len(self.delays) - 1)]
        return max(0.0, d * (1 + random.uniform(-self.jitter, self.jitter)))


DEFAULT_RETRY = RetryPolicy()
# For paid, non-idempotent submissions without an idempotency key (e.g. video or image
# generation): do NOT retry 500/504/52x, because the upstream job may already exist and a
# retry could double-charge.
NONIDEMPOTENT_RETRY = RetryPolicy(retry_statuses={429, 502, 503}, label="submit")


class RetryableHTTP(Exception):
    """Raise inside a retried callable to signal a retryable HTTP status."""

    def __init__(self, status, detail="", min_delay=0.0):
        super().__init__(f"HTTP {status} {detail}"[:500])
        self.status = status
        self.min_delay = float(min_delay or 0)


def _is_network_error(e):
    import http.client
    import socket
    import urllib.error
    net = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
           ConnectionError, TimeoutError, socket.timeout, http.client.IncompleteRead,
           http.client.RemoteDisconnected)
    if isinstance(e, urllib.error.HTTPError):
        return False
    if isinstance(e, urllib.error.URLError):
        return True
    return isinstance(e, net)


def with_retry(fn, policy=None, label=None, log=True, redact=str):
    """Call fn() under the retry policy.

    fn may return a requests.Response (its status is checked here), return any other value
    (= success), raise RetryableHTTP(status) for a retryable status it detected itself, or raise
    a network exception. Non-retryable responses are returned unchanged; non-network exceptions
    propagate immediately.
    """
    p = policy or DEFAULT_RETRY
    label = label or p.label
    t0 = time.time()
    last_exc, final = None, None
    for att in range(p.max_attempts):
        min_d = 0.0
        try:
            res = fn()
            code = res.status_code if isinstance(res, requests.Response) else None
            if code is None or not p.status_retryable(code):
                return res
            reason, last_exc, final = f"HTTP {code}", None, res
        except RetryableHTTP as e:
            if not p.status_retryable(e.status):
                raise
            reason, last_exc, final = f"HTTP {e.status}", e, None
            min_d = e.min_delay
        except Exception as e:  # noqa: BLE001
            if not _is_network_error(e):
                raise
            reason, last_exc, final = f"network: {type(e).__name__}", e, None
        if att >= p.max_attempts - 1:
            break
        d = max(p.delay(att), min_d * (1 + random.uniform(0, p.jitter)) * (att + 1))
        if time.time() - t0 + d > p.total_cap:
            if log:
                print(f"[retry:{label}] {reason}; wall-clock cap {p.total_cap:.0f}s reached, giving up", file=sys.stderr)
            break
        if log:
            print(f"[retry:{label}] attempt {att + 1}/{p.max_attempts} failed ({redact(reason)}); sleeping {d:.1f}s",
                  file=sys.stderr)
        time.sleep(d)
    if final is not None:
        return final
    raise last_exc


def response_text(resp):
    """Concatenate output_text from a /responses JSON (message items only)."""
    out = []
    for it in (resp or {}).get("output", []) or []:
        if it.get("type") == "message":
            for c in it.get("content", []) or []:
                if c.get("type") == "output_text":
                    out.append(c.get("text", ""))
    return "".join(out)


def _is_concurrency(obj):
    try:
        s = (obj if isinstance(obj, str) else json.dumps(obj)).lower()
    except Exception:
        return False
    return any(m in s for m in CONCURRENCY_MARKERS)


def _sse_events(r):
    """Yield parsed JSON objects from an SSE response; skips comments/keepalives."""
    r.encoding = "utf-8"  # SSE has no charset; requests would fall back to latin-1
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        d = line[5:].strip()
        if d == "[DONE]":
            break
        try:
            yield json.loads(d)
        except ValueError:
            continue


class Client:
    """Minimal streaming client for OpenAI-compatible APIs.

    on_usage: optional callback ``on_usage(endpoint, model, event_or_final, ok, status, error)``
    called once per attempt, e.g. to feed a usage ledger. Exceptions in it are swallowed.
    """

    def __init__(self, base_url=None, api_key=None, key_file=None, user_agent=DEFAULT_UA, policy=None,
                 on_usage=None, concurrency_backoff=None):
        self.base_url = (base_url or os.environ.get("STREAMSAFE_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self._key = api_key
        self._key_file = key_file or os.environ.get("STREAMSAFE_KEY_FILE")
        self.user_agent = user_agent
        self.policy = policy or DEFAULT_RETRY
        self.on_usage = on_usage
        self.concurrency_backoff = float(concurrency_backoff if concurrency_backoff is not None
                                         else os.environ.get("STREAMSAFE_CONCURRENCY_BACKOFF", 20))
        self._trusted = {urllib.parse.urlparse(self.base_url).hostname}

    # ---- credentials -------------------------------------------------------------
    def api_key(self):
        if self._key:
            return self._key
        k = os.environ.get("STREAMSAFE_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if not k and self._key_file:
            with open(os.path.expanduser(self._key_file)) as f:
                k = f.read().strip()
        if not k:
            raise RuntimeError("no API key: set STREAMSAFE_API_KEY / OPENAI_API_KEY or STREAMSAFE_KEY_FILE")
        self._key = k
        return k

    def redact(self, s):
        s = str(s)
        try:
            k = self.api_key()
            if k:
                s = s.replace(k, "***")
        except Exception:
            pass
        return s

    def headers(self, extra=None):
        h = {"Authorization": "Bearer " + self.api_key(), "User-Agent": self.user_agent}
        h.update(extra or {})
        return h

    def url(self, path):
        return urllib.parse.urljoin(self.base_url + "/", path.lstrip("/"))

    def is_trusted(self, url):
        return urllib.parse.urlparse(self.url(url)).hostname in self._trusted

    # ---- plumbing ----------------------------------------------------------------
    def _usage(self, endpoint, model, obj, ok, status, error):
        if self.on_usage:
            try:
                self.on_usage(endpoint, model, obj, ok, status, None if error is None else self.redact(error))
            except Exception:
                pass

    def _raise_http(self, r, policy):
        detail = self.redact(r.text[:400])
        if _is_concurrency(detail):
            raise RetryableHTTP(429, "concurrency limit", min_delay=self.concurrency_backoff)
        if (policy or self.policy).status_retryable(r.status_code):
            raise RetryableHTTP(r.status_code, detail)
        raise RuntimeError(f"HTTP {r.status_code}: {detail}")

    def _post_stream(self, path, body, timeout):
        return requests.post(self.url(path), json=body, stream=True, timeout=timeout,
                             headers=self.headers({"Accept": "text/event-stream", "Content-Type": "application/json"}))

    def request(self, method, url, policy=None, label=None, **kw):
        """requests.request with auth (only for the API host), UA and the retry policy."""
        url = self.url(url)
        hdr = dict(kw.pop("headers", None) or {})
        hdr = self.headers(hdr) if self.is_trusted(url) else dict(hdr, **{"User-Agent": self.user_agent})
        return with_retry(lambda: requests.request(method, url, headers=hdr, **kw), policy or self.policy, label,
                          redact=self.redact)

    def download(self, url, timeout=600):
        """GET a URL as a stream; the API key is attached only if the host is the API host."""
        return self.request("GET", url, label="download", timeout=timeout, stream=True)

    # ---- /responses ----------------------------------------------------------------
    def responses_stream(self, body, policy=None, label="responses", timeout=(20, 600), on_delta=None):
        """POST /responses with stream:true. Returns (text, final_response_or_None, 200).

        The whole request + stream consumption is one attempt, so a mid-stream cut is retried
        from scratch. Raises RuntimeError on a non-retryable HTTP error."""
        body = dict(body, stream=True)

        def once():
            st = {"status": None, "final": None}
            try:
                r = self._post_stream("responses", body, timeout)
                st["status"] = r.status_code
                if r.status_code != 200:
                    self._raise_http(r, policy)
                parts, final, err = [], None, None
                for j in _sse_events(r):
                    t = j.get("type")
                    if t == "response.output_text.delta":
                        parts.append(j.get("delta", ""))
                        if on_delta:
                            on_delta(j.get("delta", ""))
                    elif t in ("response.completed", "response.incomplete"):
                        final = j.get("response")
                    elif t in ("response.failed", "error"):
                        err = j
                if err is not None and not parts:
                    if _is_concurrency(err):
                        raise RetryableHTTP(429, "concurrency limit", min_delay=self.concurrency_backoff)
                    raise RetryableHTTP(502, "stream error event: " + self.redact(json.dumps(err)[:300]))
                if final is None and not parts:
                    raise requests.exceptions.ChunkedEncodingError("stream ended without content")
                text = "".join(parts) or response_text(final)
                st["final"] = final
                self._usage("/responses", body.get("model"), final, True, 200, None)
                return text, final, 200
            except BaseException as e:
                self._usage("/responses", body.get("model"), None, False, st["status"], repr(e))
                raise

        return with_retry(once, policy or self.policy, label, redact=self.redact)

    def responses_create(self, body, policy=None, label="responses", timeout=(20, 600)):
        """Drop-in for a stream:false /responses call: streams internally and returns the final
        response JSON (as in response.completed). If the stream had text but no completed event,
        a minimal output[] is synthesized."""
        text, final, _ = self.responses_stream(body, policy=policy, label=label, timeout=timeout)
        msg = {"type": "message", "role": "assistant",
               "content": [{"type": "output_text", "text": text, "annotations": []}]}
        if not final:
            return {"object": "response", "model": body.get("model"), "status": "incomplete", "output": [msg]}
        if text and not response_text(final):
            final = dict(final)
            final["output"] = list(final.get("output") or []) + [msg]
        return final

    # ---- /chat/completions -----------------------------------------------------------
    def chat_stream(self, body, policy=None, label="chat", timeout=(20, 600), on_delta=None):
        """POST /chat/completions with stream:true (and stream_options.include_usage).

        Concatenates choices[0].delta.content. Reasoning deltas are kept separately in
        final["choices"][0]["message"]["reasoning_content"]; tool-call deltas are merged.
        Returns (text, final, 200) where final has the shape of a non-stream chat.completion."""
        body = dict(body, stream=True)
        so = dict(body.get("stream_options") or {})
        so["include_usage"] = True
        body["stream_options"] = so

        def once():
            st = {"status": None}
            try:
                r = self._post_stream("chat/completions", body, timeout)
                st["status"] = r.status_code
                if r.status_code != 200:
                    self._raise_http(r, policy)
                parts, rparts, tool_calls, err = [], [], {}, None
                meta = {"id": None, "model": body.get("model"), "created": None, "finish_reason": None,
                        "usage": None, "role": "assistant"}
                for j in _sse_events(r):
                    if j.get("error"):
                        err = j["error"]
                        continue
                    for k in ("id", "model", "created"):
                        if j.get(k):
                            meta[k] = j[k]
                    if j.get("usage"):
                        meta["usage"] = j["usage"]
                    for ch in j.get("choices") or []:
                        if ch.get("index", 0) != 0:
                            continue
                        dl = ch.get("delta") or {}
                        if dl.get("role"):
                            meta["role"] = dl["role"]
                        c = dl.get("content")
                        if c:
                            parts.append(c)
                            if on_delta:
                                on_delta(c)
                        rc = dl.get("reasoning_content") or dl.get("reasoning")
                        if isinstance(rc, str) and rc:
                            rparts.append(rc)
                        for tc in dl.get("tool_calls") or []:
                            cur = tool_calls.setdefault(tc.get("index", 0), {
                                "id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                            if tc.get("id"):
                                cur["id"] = tc["id"]
                            f = tc.get("function") or {}
                            cur["function"]["name"] += f.get("name") or ""
                            cur["function"]["arguments"] += f.get("arguments") or ""
                        if ch.get("finish_reason"):
                            meta["finish_reason"] = ch["finish_reason"]
                if err is not None and not parts and not tool_calls:
                    if _is_concurrency(err):
                        raise RetryableHTTP(429, "concurrency limit", min_delay=self.concurrency_backoff)
                    raise RetryableHTTP(502, "stream error event: " + self.redact(json.dumps(err)[:300]))
                if not parts and not tool_calls and meta["finish_reason"] is None:
                    raise requests.exceptions.ChunkedEncodingError("stream ended without content")
                text = "".join(parts)
                msg = {"role": meta["role"], "content": text}
                if rparts:
                    msg["reasoning_content"] = "".join(rparts)
                if tool_calls:
                    msg["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
                final = {"id": meta["id"], "object": "chat.completion", "created": meta["created"],
                         "model": meta["model"],
                         "choices": [{"index": 0, "message": msg, "finish_reason": meta["finish_reason"]}],
                         "usage": meta["usage"], "_streamed": True}
                self._usage("/chat/completions", body.get("model"), final, True, 200, None)
                return text, final, 200
            except BaseException as e:
                self._usage("/chat/completions", body.get("model"), None, False, st["status"], repr(e))
                raise

        return with_retry(once, policy or self.policy, label, redact=self.redact)

    def chat_completion(self, body, policy=None, label="chat", timeout=(20, 600)):
        """Drop-in for a stream:false /chat/completions call; returns the non-stream JSON shape."""
        return self.chat_stream(body, policy=policy, label=label, timeout=timeout)[1]
