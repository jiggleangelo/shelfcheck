import io
import unittest
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

from demo import (
    DemoHandler,
    MAX_CONCURRENT_REPORTS,
    MAX_CONNECTIONS,
    create_server,
)


class HostedDemoLimitTests(unittest.TestCase):
    def setUp(self):
        self.server = create_server("127.0.0.1", 0)

    def tearDown(self):
        self.server.server_close()

    def test_handle_error_never_writes_client_address_or_traceback(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.server.handle_error(None, ("203.0.113.99", 45678))
        self.assertEqual("", stderr.getvalue())

    def test_handler_applies_ten_second_socket_timeout(self):
        self.assertEqual(10, DemoHandler.timeout)

    def test_connection_limit_rejects_before_spawning_another_thread(self):
        for _ in range(MAX_CONNECTIONS):
            self.assertTrue(self.server.connection_slots.acquire(blocking=False))
        request = Mock()
        try:
            with patch.object(ThreadingHTTPServer, "process_request") as parent, \
                    patch.object(self.server, "shutdown_request") as shutdown:
                self.server.process_request(request, ("203.0.113.50", 12345))
            parent.assert_not_called()
            shutdown.assert_called_once_with(request)
        finally:
            for _ in range(MAX_CONNECTIONS):
                self.server.connection_slots.release()

    def test_report_limit_refuses_work_without_building_report(self):
        for _ in range(MAX_CONCURRENT_REPORTS):
            self.assertTrue(self.server.try_start_report())
        try:
            handler = object.__new__(DemoHandler)
            handler.path = "/run"
            handler.headers = {
                "Content-Length": "7",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            handler.rfile = io.BytesIO(b"brand=x")
            handler.server = self.server
            handler._problem = Mock()

            with patch("demo._build_report") as build_report:
                handler.do_POST()

            build_report.assert_not_called()
            handler._problem.assert_called_once_with(
                503,
                "Demo busy",
                "Too many fake reports are already running. Please try again shortly.",
            )
        finally:
            for _ in range(MAX_CONCURRENT_REPORTS):
                self.server.finish_report()


if __name__ == "__main__":
    unittest.main()
