"""Minimal example. Configure with environment variables, e.g.
    export STREAMSAFE_BASE_URL=https://your-gateway.example.com/v1
    export STREAMSAFE_API_KEY=...        # or STREAMSAFE_KEY_FILE=~/.config/streamsafe/key
"""
import sys

from streamsafe import Client

c = Client()
d = c.chat_completion({"model": sys.argv[1] if len(sys.argv) > 1 else "gpt-4o-mini",
                       "messages": [{"role": "user", "content": "Say hi in five words."}]})
print(d["choices"][0]["message"]["content"])
print("usage:", d["usage"])
