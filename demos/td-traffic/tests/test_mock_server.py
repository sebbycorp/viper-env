import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mock"))
import server  # noqa: E402


@pytest.fixture(scope="module")
def base():
    srv = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def post(base, body, headers=None, path="/v1/chat/completions"):
    req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", **(headers or {})})
    return urllib.request.urlopen(req)


def test_healthz(base):
    assert urllib.request.urlopen(base + "/healthz").status == 200


def test_usage_from_headers(base):
    h = {"x-td-input-tokens": "31000", "x-td-output-tokens": "850", "x-td-cached-tokens": "18000"}
    r = post(base, {"model": "gpt-4o", "messages": []}, h)
    d = json.load(r)
    assert d["model"] == "gpt-4o"
    assert d["object"] == "chat.completion"
    assert d["choices"][0]["finish_reason"] == "stop"
    assert d["usage"]["prompt_tokens"] == 31000
    assert d["usage"]["completion_tokens"] == 850
    assert d["usage"]["prompt_tokens_details"]["cached_tokens"] == 18000


def test_default_usage(base):
    d = json.load(post(base, {"model": "m", "messages": []}))
    assert d["usage"]["completion_tokens"] == 150


def test_status_injection(base):
    with pytest.raises(urllib.error.HTTPError) as e:
        post(base, {"model": "m"}, {"x-td-status": "429"})
    assert e.value.code == 429
    assert json.load(e.value)["error"]["type"] == "rate_limit_exceeded"


def test_other_path(base):
    assert post(base, {"model": "m"}, path="/openai/chat/completions").status == 200


def test_stream_ends_with_usage_and_done(base):
    h = {"x-td-input-tokens": "10", "x-td-output-tokens": "5"}
    raw = post(base, {"model": "m", "stream": True}, h).read().decode()
    lines = [l for l in raw.split("\n") if l.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    last = json.loads(lines[-2][6:])
    assert last["choices"] == []
    assert last["usage"]["prompt_tokens"] == 10
    assert last["usage"]["completion_tokens"] == 5
