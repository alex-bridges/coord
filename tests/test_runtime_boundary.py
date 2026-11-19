"""Opt-in contract probe of the installed Codex CLI; no auth or model/API spend.

COORD_TEST_CODEX_RUNTIME=1 python3 -m unittest discover -s Tools/coord/tests -p test_runtime_boundary.py

Only an empty temporary home and local fake Responses endpoint are used. Request
bodies and headers are discarded; the assertion retains only advertised tool names.
Run this when upgrading the CLI: configuration names alone cannot prove isolation.
"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from coordlib import runtime


@unittest.skipUnless(os.environ.get("COORD_TEST_CODEX_RUNTIME") == "1", "opt-in installed Codex capability probe")
class CodexBoundaryTests(unittest.TestCase):
    def test_actual_model_request_advertises_no_tools(self):
        executable = shutil.which("codex")
        self.assertIsNotNone(executable, "the opt-in probe requires Codex on PATH")
        captured = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                try:
                    request = json.loads(body)
                    captured.append([tool.get("name", tool.get("type")) for tool in request.get("tools", [])])
                except (ValueError, TypeError, AttributeError):
                    captured.append(["unparseable request"])
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"offline capability probe complete","type":"invalid_request_error"}}')

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory(prefix="coord-runtime-probe-") as scratch:
                home, work = os.path.join(scratch, "home"), os.path.join(scratch, "work")
                os.mkdir(home)
                os.mkdir(work)
                env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "CODEX_HOME": home, "TMPDIR": scratch}
                configs = ['model_provider="coordprobe"', 'model_providers.coordprobe.name="coordprobe"',
                           f'model_providers.coordprobe.base_url="http://127.0.0.1:{server.server_port}/v1"',
                           'model_providers.coordprobe.wire_api="responses"',
                           "model_providers.coordprobe.requires_openai_auth=false",
                           "model_providers.coordprobe.request_max_retries=0",
                           "model_providers.coordprobe.stream_max_retries=0",
                           "features.enable_request_compression=false"]
                command = runtime.codex_command(executable, "Return OK; do not use tools.", "gpt-6-sol")
                index = command.index("--")
                command[index:index] = [part for config in configs for part in ("--config", config)]
                subprocess.run(command, cwd=work, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=25)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
        self.assertTrue(captured, "CLI did not send a request to the local fake endpoint")
        for tools in captured:
            self.assertEqual(tools, [], "diagnosis advertised tools despite its runtime restrictions")


_EXAMPLE_CONFIG_PATH = None


def setUpModule():
    global _EXAMPLE_CONFIG_PATH
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import conductor_config_fixture
    _EXAMPLE_CONFIG_PATH = conductor_config_fixture.use_example_config()


def tearDownModule():
    import conductor_config_fixture
    conductor_config_fixture.restore(_EXAMPLE_CONFIG_PATH)


if __name__ == "__main__":
    unittest.main()
