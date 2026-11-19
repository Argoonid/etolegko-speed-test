import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import unittest
import urllib.error

from measure_speed import (
    EmptyResponseError,
    IncompleteDownloadError,
    main,
    run_benchmark,
    validate_url,
)

PAYLOAD = b"x" * 128_000


class _StubHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/fail":
            self.send_response(500)
            self.end_headers()
            return
        if self.path == "/empty":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"x" * 100)
            return

        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, format, *args):
        return


class MeasureSpeedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_ten_sequential_requests(self):
        url = f"http://127.0.0.1:{self.port}/file.bin"
        res = run_benchmark(url, count=10, timeout=5.0)
        self.assertEqual(res.requests_count, 10)
        self.assertEqual(res.total_bytes, len(PAYLOAD) * 10)
        self.assertGreater(res.total_time_s, 0)
        self.assertGreaterEqual(res.avg_time_s, res.avg_ttfb_s)
        self.assertGreater(res.speed_mbps, 0)

    def test_truncated_response_raises(self):
        url = f"http://127.0.0.1:{self.port}/truncated"
        with self.assertRaises(IncompleteDownloadError):
            run_benchmark(url, count=1, timeout=5.0)

    def test_empty_response_raises(self):
        url = f"http://127.0.0.1:{self.port}/empty"
        with self.assertRaises(EmptyResponseError):
            run_benchmark(url, count=1, timeout=5.0)

    def test_invalid_url_rejected(self):
        with self.assertRaises(ValueError):
            validate_url("ftp://example.com/file.jpg")

    def test_http_error_propagates(self):
        url = f"http://127.0.0.1:{self.port}/fail"
        with self.assertRaises(urllib.error.HTTPError):
            run_benchmark(url, count=1, timeout=5.0)

    def test_json_cli_output(self):
        url = f"http://127.0.0.1:{self.port}/file.bin"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main([url, "--requests", "3", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["requests_count"], 3)
        self.assertEqual(data["total_bytes"], len(PAYLOAD) * 3)
        self.assertEqual(len(data["samples"]), 3)


if __name__ == "__main__":
    unittest.main()