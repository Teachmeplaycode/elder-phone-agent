"""Minimal OpenAI-compatible chat client (DeepSeek by default). Stdlib only."""
import json
import os
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")


def _key():
    k = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY")
    if not k:
        raise RuntimeError("Set DEEPSEEK_API_KEY in your shell (e.g. in ~/.zshrc)")
    return k


def chat(messages, json_mode=True, temperature=0.0, max_tokens=800, model=None, retries=3):
    """Return (text, usage_dict, latency_seconds)."""
    body = {"model": model or MODEL, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(BASE_URL.rstrip("/") + "/chat/completions", data=data,
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + _key()})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.loads(r.read().decode("utf-8"))
            latency = time.time() - t0
            text = resp["choices"][0]["message"]["content"]
            return text, resp.get("usage", {}), latency
        except (urllib.error.URLError, KeyError, ValueError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("LLM call failed: %r" % last)


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])
