"""http_get: retry rules, checked with a fake network boundary."""
import unittest
import urllib.error

from hazemy.common import ATTEMPTS, Budget, PipelineError, http_get
from hazemy.gfs import fetch_wind_messages
from tests.test_data_sources import utc
from tests.test_job import FakeResponse


class Network:
    """Replays a list of outcomes: an int status, an exception, or a FakeResponse."""

    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, int):
            raise urllib.error.HTTPError(req.full_url, outcome, "x", {}, None)
        return outcome


class HttpGet(unittest.TestCase):
    def get(self, outcomes, **kw):
        net, sleeps = Network(outcomes), []
        try:
            body = http_get("http://x/y", label="T", budget=Budget(), urlopen=net,
                            sleep=sleeps.append, log=lambda _: None, **kw)
        except PipelineError as err:
            return net, sleeps, err
        return net, sleeps, body

    def test_retries_5xx_then_succeeds(self):
        net, sleeps, body = self.get([503, 500, FakeResponse("ok")])
        self.assertEqual((body, net.calls, len(sleeps)), (b"ok", 3, 2))

    def test_retries_429(self):
        net, _, body = self.get([429, FakeResponse("ok")])
        self.assertEqual((body, net.calls), (b"ok", 2))

    def test_gives_up_after_all_attempts(self):
        net, _, err = self.get([502] * ATTEMPTS)
        self.assertIsInstance(err, PipelineError)
        self.assertEqual(net.calls, ATTEMPTS)
        self.assertIn("failed after", str(err))

    def test_other_4xx_fails_at_once(self):
        for status in (400, 401, 403, 404):
            net, sleeps, err = self.get([status, FakeResponse("never")])
            self.assertIsInstance(err, PipelineError, status)
            self.assertEqual((net.calls, sleeps), (1, []), status)
            self.assertIn(f"HTTP {status}", str(err))

    def test_unexpected_success_status_is_retried_as_failure(self):
        _, _, err = self.get([FakeResponse("x", status=200)] * ATTEMPTS, ok=(206,))
        self.assertIsInstance(err, PipelineError)

    def test_short_range_response_fails(self):
        idx = ("1:0:d=2026102000:UGRD:10 m above ground:anl:\n"
               "2:100:d=2026102000:VGRD:10 m above ground:anl:\n"
               "3:200:d=2026102000:TMP:2 m above ground:anl:\n")

        def urlopen(req, timeout=None):
            if req.full_url.endswith(".idx"):
                return FakeResponse(idx)
            return FakeResponse("x" * 10, status=206)  # asked for 100 bytes
        with self.assertRaises(PipelineError) as ctx:
            fetch_wind_messages(utc(2026, 10, 20), 0, Budget(), urlopen=urlopen,
                                sleep=lambda s: None, log=lambda _: None)
        self.assertIn("range returned 10 bytes, expected 100", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
