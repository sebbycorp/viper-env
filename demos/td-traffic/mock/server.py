"""Fake OpenAI-compatible LLM. Behaviour is driven by x-td-* request headers."""
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ERR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
             404: "not_found_error", 429: "rate_limit_exceeded"}
TEXT = "Here is a summary of the requested account activity. No further action is required."


def num(h, name, default):
    try:
        return int(h.get(name, default))
    except ValueError:
        return default


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.send_json(200, {"status": "ok"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not self.path.split("?")[0].endswith("/chat/completions"):
            return self.send_json(404, {"error": {"message": "not found", "type": "not_found_error", "code": "404"}})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        h = self.headers
        time.sleep(min(num(h, "x-td-latency-ms", 0), 3000) / 1000)
        status = num(h, "x-td-status", 200)
        if status != 200:
            err = {"message": f"injected error {status}", "type": ERR_TYPES.get(status, "server_error"),
                   "code": ERR_TYPES.get(status, "server_error")}
            return self.send_json(status, {"error": err})
        pt = num(h, "x-td-input-tokens", len(raw) // 4)
        ct = num(h, "x-td-output-tokens", 150)
        usage = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
                 "prompt_tokens_details": {"cached_tokens": num(h, "x-td-cached-tokens", 0)}}
        base = {"id": "chatcmpl-" + uuid.uuid4().hex[:24], "created": int(time.time()),
                "model": body.get("model", "fake-model")}
        if body.get("stream"):
            return self.stream(base, usage)
        base.update(object="chat.completion", usage=usage, choices=[
            {"index": 0, "message": {"role": "assistant", "content": TEXT}, "finish_reason": "stop"}])
        self.send_json(200, base)

    def stream(self, base, usage):
        base["object"] = "chat.completion.chunk"
        chunks = [dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": TEXT},
                                       "finish_reason": None}]),
                  dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}]),
                  dict(base, choices=[], usage=usage)]
        out = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        data = out.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
