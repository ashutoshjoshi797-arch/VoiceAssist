"""A fake cloud AI server: the VoiceAssist cloud "chat" function stream (and the older provider formats).

Used by the tests to exercise the online code path, connection loss mid-answer, outages and
recovery without real API keys. Control with .mode: "ok" | "drop" (close the socket mid-stream) |
"401" | "500".
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeCloud:
    def __init__(self, port: int = 0):
        self.mode = "ok"
        self.requests: list[dict] = []
        self.port = port
        self.srv = None

    def start(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {"_raw": len(raw)}
                fake.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if fake.mode == "401":
                    return self._json(401, {"error": {"type": "authentication_error", "message": "invalid x-api-key"}})
                if fake.mode == "500":
                    return self._json(500, {"error": {"message": "overloaded"}})
                words = ["This ", "is ", "the ", "cloud ", "model ", "answering. ", "It ", "works ", "fine."]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for i, w in enumerate(words):
                    if fake.mode == "hang" and i == 3:
                        time.sleep(60)  # Wi-Fi dropped: no data, no FIN
                        return
                    if fake.mode == "drop" and i == 3:
                        self.connection.shutdown(2)  # network dies mid-answer
                        return
                    self.wfile.write(self._event(w).encode())
                    self.wfile.flush()
                    time.sleep(0.02)
                self.wfile.write(self._end().encode())

            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _event(self, text):
                if self.path.startswith("/v1/messages") or self.path.startswith("/functions/v1/chat"):
                    d = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
                    return f"event: content_block_delta\ndata: {json.dumps(d)}\n\n"
                if "chat/completions" in self.path:
                    return f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': text}}]})}\n\n"
                if "streamGenerateContent" in self.path:
                    return f"data: {json.dumps({'candidates': [{'content': {'role': 'model', 'parts': [{'text': text}]}}]})}\n\n"
                return ""

            def _end(self):
                if self.path.startswith("/v1/messages") or self.path.startswith("/functions/v1/chat"):
                    return 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
                if "chat/completions" in self.path:
                    return "data: [DONE]\n\n"
                if "streamGenerateContent" in self.path:
                    return 'data: {"candidates": [{"content": {"role": "model", "parts": [{"text": ""}]}, "finishReason": "STOP"}]}\n\n'
                return ""

        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None
