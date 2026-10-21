"""Shared plumbing: errors, time helpers, HTTP with retries, download budget, log redaction."""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# A generic agent string: request headers must never carry a person's name or email.
USER_AGENT = "hazemy-data/1.0"
ATTEMPTS = 3
WAITS_S = (10, 30)
REQUEST_TIMEOUT_S = 120
MAX_DOWNLOAD_BYTES = 500_000_000
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MYT = timezone(timedelta(hours=8))
STEP = timedelta(hours=3)


class PipelineError(Exception):
    """A failure that must stop the job before the published file is touched."""


class RetriesExhausted(PipelineError):
    """A server stayed unreachable or kept failing (5xx/429) through every attempt."""


class Budget:
    """Download cap in bytes; add() refuses the download that would cross it."""

    def __init__(self, limit: int = MAX_DOWNLOAD_BYTES):
        self.limit = limit
        self.total = 0

    def add(self, nbytes: int) -> None:
        if self.total + nbytes > self.limit:
            raise PipelineError(
                f"Download budget of {self.limit} bytes would be exceeded "
                f"({self.total} used, {nbytes} more). Stopping.")
        self.total += nbytes


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIME_FORMAT)


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc)


def floor_hours(moment: datetime, hours: int) -> datetime:
    """Round down to a multiple of `hours` within the UTC day."""
    moment = moment.astimezone(timezone.utc)
    return moment.replace(hour=moment.hour // hours * hours, minute=0, second=0, microsecond=0)


def myt_day_bounds(moment: datetime, day_offset: int = 0):
    """(start, end) in UTC of the MYT calendar day containing `moment`, shifted by day_offset.

    MYT is UTC+8, so a MYT day starts at 16:00 UTC the previous day.
    """
    local = moment.astimezone(MYT).replace(hour=0, minute=0, second=0, microsecond=0)
    start = local + timedelta(days=day_offset)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def steps_between(start: datetime, end: datetime) -> list:
    """3-hourly UTC instants t with start <= t < end (on multiples of 3 h)."""
    t = floor_hours(start, 3)
    if t < start:
        t += STEP
    out = []
    while t < end:
        out.append(t)
        t += STEP
    return out


def redact(text: str, secrets) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def make_logger(secrets, emit=print):
    def log(line) -> None:
        emit(redact(str(line), secrets))
    return log


def require_env(env, names) -> None:
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise PipelineError(f"Missing environment variable(s): {', '.join(missing)}.")


def http_get(url, *, label, budget, headers=None, ok=(200,), urlopen=urllib.request.urlopen,
             sleep=time.sleep, log=print) -> bytes:
    """GET with retries on network errors, 5xx and 429; other statuses fail at once."""
    reason = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
            with urlopen(req, timeout=REQUEST_TIMEOUT_S) as res:
                body = res.read()
                status = res.status
        except urllib.error.HTTPError as err:
            status, body = err.code, b""
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as err:
            status, body, reason = 0, b"", f"{type(err).__name__}: {err}"
        if status in ok:
            budget.add(len(body))
            return body
        if status and status < 500 and status != 429:
            raise PipelineError(f"{label}: HTTP {status}.")
        reason = reason if status == 0 else f"HTTP {status}"
        if attempt < ATTEMPTS:
            log(f"{label}: attempt {attempt} failed ({reason}), retrying")
            sleep(WAITS_S[min(attempt - 1, len(WAITS_S) - 1)])
    raise RetriesExhausted(f"{label}: failed after {ATTEMPTS} attempts ({reason}).")
