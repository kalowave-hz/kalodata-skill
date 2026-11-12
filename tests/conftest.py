import json
import base64
import sys
import threading
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "skills" / "kalodata" / "scripts")
)


class MockKalo(BaseHTTPRequestHandler):
    """Scriptable KaloData mock: responses keyed by path in server.responses."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw
            )
            body = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                content = part.get_payload(decode=True)
                body[name] = (
                    {"filename": part.get_filename(), "content_type": part.get_content_type(),
                     "content": content}
                    if part.get_filename() is not None else content.decode("utf-8")
                )
        else:
            body = json.loads(raw or b"{}")
        self._respond(body)

    def do_GET(self):
        self._respond({})

    def _respond(self, body):
        headers = {k.lower(): v for k, v in self.headers.items()}
        path = self.path.split("?")[0]
        self.server.requests.append((path, headers, body))
        payload = self.server.responses.get(path, {"success": True, "data": []})
        if callable(payload):
            payload = payload(body)
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


@pytest.fixture
def mock_server():
    server = HTTPServer(("127.0.0.1", 0), MockKalo)
    server.responses = {}
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


@pytest.fixture
def env(monkeypatch, mock_server, tmp_path):
    monkeypatch.setenv("KALODATA_API_KEY", "test-key")
    monkeypatch.setenv("KALODATA_BASE_URL", f"http://127.0.0.1:{mock_server.server_address[1]}")
    monkeypatch.setenv("KALODATA_CONFIG_DIR", str(tmp_path / "cfg"))
    return mock_server


@pytest.fixture
def image_file(tmp_path):
    image = tmp_path / "product.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    ))
    return image
