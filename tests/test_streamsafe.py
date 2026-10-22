"""Tests against a local fake OpenAI-compatible SSE server (no network)."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import streamsafe as S  # noqa: E402

FAKE_KEY = "test-key-not-a-secret"


class Fake(http.server.BaseHTTPRequestHandler):
    script = []   # list of behaviours consumed per request
    seen = []
    protocol_version = "HTTP/1.0"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        kind = Fake.script.pop(0) if Fake.script else "ok"
        if kind.startswith("http"):
            code = int(kind[4:])
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"nope ' + FAKE_KEY.encode() + b'"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if kind == "cut":
            return  # stream ends with no content
        if self.path.endswith("/chat/completions"):
            evs = [{"id": "c1", "model": "m", "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": "think"}}]},
                   {"choices": [{"index": 0, "delta": {"content": "Hel"}}]},
                   {"choices": [{"index": 0, "delta": {"content": "lo", "tool_calls": [
                       {"index": 0, "id": "t1", "function": {"name": "f", "arguments": "{\"a\""}}]}}]},
                   {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}]},
                                 "finish_reason": "stop"}]},
                   {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}]
        else:
            evs = [{"type": "response.output_text.delta", "delta": "Hi "},
                   {"type": "response.output_text.delta", "delta": "there"},
                   {"type": "response.completed", "response": {"id": "r1", "model": "m", "output": [],
                                                               "usage": {"input_tokens": 1}}}]
        self.wfile.write(b": keepalive\n\n")
        for e in evs:
            self.wfile.write(b"data: " + json.dumps(e).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *a):
        pass


class StreamsafeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Fake.script, Fake.seen = [], []
        self.usage = []
        self.c = S.Client(base_url=self.base, api_key=FAKE_KEY, policy=S.RetryPolicy(delays=(0,), total_cap=30),
                          on_usage=lambda *a: self.usage.append(a), concurrency_backoff=0)

    def test_chat_reassembles_non_stream_shape(self):
        d = self.c.chat_completion({"model": "m", "messages": [], "stream": False})
        self.assertTrue(Fake.seen[0]["body"]["stream"])
        self.assertTrue(Fake.seen[0]["body"]["stream_options"]["include_usage"])
        self.assertEqual(Fake.seen[0]["auth"], "Bearer " + FAKE_KEY)
        msg = d["choices"][0]["message"]
        self.assertEqual(msg["content"], "Hello")
        self.assertEqual(msg["reasoning_content"], "think")
        self.assertEqual(msg["tool_calls"][0]["function"], {"name": "f", "arguments": "{\"a\":1}"})
        self.assertEqual(d["choices"][0]["finish_reason"], "stop")
        self.assertEqual(d["usage"]["completion_tokens"], 2)
        self.assertEqual(self.usage[-1][3], True)

    def test_responses(self):
        d = self.c.responses_create({"model": "m", "input": "x"})
        self.assertEqual(S.response_text(d), "Hi there")
        self.assertEqual(d["id"], "r1")

    def test_retry_on_503_and_cut(self):
        Fake.script = ["http503", "cut"]
        text, _, _ = self.c.chat_stream({"model": "m", "messages": []})
        self.assertEqual(text, "Hello")
        self.assertEqual(len(Fake.seen), 3)

    def test_no_retry_on_401_and_key_redacted(self):
        Fake.script = ["http401"]
        with self.assertRaises(RuntimeError) as cm:
            self.c.chat_completion({"model": "m", "messages": []})
        self.assertEqual(len(Fake.seen), 1)
        self.assertNotIn(FAKE_KEY, str(cm.exception))
        self.assertIn("***", str(cm.exception))

    def test_gives_up_after_max_attempts(self):
        Fake.script = ["http502"] * 5
        with self.assertRaises(S.RetryableHTTP):
            self.c.responses_stream({"model": "m", "input": "x"})
        self.assertEqual(len(Fake.seen), 3)

    def test_nonidempotent_policy(self):
        self.assertFalse(S.NONIDEMPOTENT_RETRY.status_retryable(500))
        self.assertTrue(S.NONIDEMPOTENT_RETRY.status_retryable(429))

    def test_download_only_sends_key_to_api_host(self):
        self.assertTrue(self.c.is_trusted("/files/x"))
        self.assertFalse(self.c.is_trusted("https://cdn.example.com/x.png"))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            body = os.path.join(d, "b.json")
            with open(body, "w") as f:
                json.dump({"model": "m", "messages": [], "stream": False}, f)
            env = dict(os.environ, STREAMSAFE_API_KEY=FAKE_KEY, PYTHONPATH=os.path.dirname(os.path.dirname(__file__)))
            r = subprocess.run([sys.executable, "-m", "streamsafe", "chat", body, "-", "--base-url", self.base],
                               capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["choices"][0]["message"]["content"], "Hello")
            self.assertNotIn(FAKE_KEY, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
