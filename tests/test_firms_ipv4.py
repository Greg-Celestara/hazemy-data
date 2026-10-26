"""FIRMS IPv4-only diagnostic attempt: network and name lookup are faked."""
import os
import socket
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone

from hazemy.common import Budget, PipelineError, RetriesExhausted
from hazemy.firms import SOURCES, fetch_detection_rows, rows_with_fallback
from tests.test_data_sources import HEADER, row
from tests.test_job import FakeResponse, is_dated_firms_url

NOW = datetime(2026, 10, 19, 10, 30, tzinfo=timezone.utc)
UNREACHABLE = urllib.error.URLError("Network is unreachable")


class Network:
    """Fails the first `failures` calls with `error`, then answers; records lookup and timeout."""

    def __init__(self, failures, error=UNREACHABLE, body=HEADER + row(0.5, 101.5)):
        self.failures, self.error, self.body = failures, error, body
        self.calls, self.timeouts, self.lookups = 0, [], []

    def __call__(self, req, timeout=None):
        self.calls += 1
        self.timeouts.append(timeout)
        self.lookups.append(socket.getaddrinfo)
        if self.calls <= self.failures:
            raise self.error
        return FakeResponse(HEADER if is_dated_firms_url(req.full_url) else self.body)


class Ipv4Attempt(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.lines, self.original = [], socket.getaddrinfo

    def http(self, net):
        return {"urlopen": net, "sleep": lambda s: None}

    def test_ipv4_success_after_three_failures_is_used_and_saved(self):
        net = Network(failures=3)
        rows, fetched = rows_with_fallback(
            "k", NOW, lambda: NOW, Budget(), self._dir.name, log=self.lines.append,
            **self.http(net))
        self.assertEqual(len(rows), len(SOURCES))
        self.assertEqual(fetched, NOW)
        self.assertTrue(os.path.exists(os.path.join(self._dir.name, "firms_last.json")))
        self.assertIn("FIRMS VIIRS_SNPP_NRT: succeeded over IPv4 after 3 failed attempts",
                      self.lines)
        self.assertEqual(net.timeouts[3], 60)
        self.assertEqual(net.calls, 3 + 1 + (2 * len(SOURCES) - 1))  # two requests per source

    def test_ipv4_failure_logs_the_error_and_reraises_for_the_fallback(self):
        net = Network(failures=99)
        rows, fetched = rows_with_fallback(
            "k", NOW, lambda: NOW, Budget(), self._dir.name, log=self.lines.append,
            **self.http(net))
        self.assertEqual((rows, fetched), ([], None))
        text = "\n".join(self.lines)
        self.assertIn("FIRMS VIIRS_SNPP_NRT: IPv4-only attempt failed (RetriesExhausted:", text)
        self.assertIn("Network is unreachable", text)
        self.assertEqual(net.calls, 4)  # 3 normal + 1 IPv4, no more retries

    def test_lookup_is_limited_only_during_the_attempt_and_restored_after(self):
        for failures in (3, 99):
            net = Network(failures=failures)
            try:
                fetch_detection_rows("k", NOW, Budget(), log=lambda _: None, **self.http(net))
            except RetriesExhausted:
                pass
            self.assertIs(socket.getaddrinfo, self.original)
            self.assertTrue(all(fn is self.original for fn in net.lookups[:3]))
            self.assertIsNot(net.lookups[3], self.original)

    def test_lookup_is_restored_when_the_attempt_raises_unexpectedly(self):
        def boom(req, timeout=None):
            if socket.getaddrinfo is not self.original:
                raise KeyError("unexpected")
            raise UNREACHABLE
        with self.assertRaises(KeyError):
            fetch_detection_rows("k", NOW, Budget(), log=lambda _: None, urlopen=boom,
                                 sleep=lambda s: None)
        self.assertIs(socket.getaddrinfo, self.original)

    def test_4xx_is_fatal_with_no_ipv4_attempt(self):
        net = Network(failures=99, error=urllib.error.HTTPError("u", 401, "x", {}, None))
        with self.assertRaises(PipelineError) as ctx:
            rows_with_fallback("k", NOW, lambda: NOW, Budget(), self._dir.name,
                               log=self.lines.append, **self.http(net))
        self.assertNotIsInstance(ctx.exception, RetriesExhausted)
        self.assertEqual(net.calls, 1)
        self.assertNotIn("IPv4", "\n".join(self.lines))
