"""Eastmoney dated-suspension adapter for M2 daily recovery.

These adapters deliberately expose only facts that the upstream endpoints can
support point in time.  The Eastmoney ST board is a current snapshot, so it is
not used here to label a historical session.
"""

from __future__ import annotations

from datetime import date
import socket
import time

from scripts.market_data.contracts import normalize_symbol, parse_date


class EastmoneySuspensionSource:
    """Dated Eastmoney suspension facts, loaded once for a target session."""

    name = "akshare_eastmoney_dated_suspension"

    def __init__(
        self,
        attempts: int = 3,
        backoff_seconds: float = 1.0,
        timeout_seconds: float = 25.0,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.attempts = attempts
        self.backoff_seconds = backoff_seconds
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _optional_date(value: object) -> date | None:
        text = str(value).strip()
        if not text or text.lower() in {"nat", "nan", "none"}:
            return None
        return parse_date(value)

    def fetch(self, target: date) -> frozenset[str]:
        try:
            import akshare as ak
        except ImportError as error:
            raise RuntimeError("akshare is not installed") from error
        previous_socket_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(self.timeout_seconds)
        failures: list[str] = []
        try:
            for attempt in range(1, self.attempts + 1):
                try:
                    frame = ak.stock_tfp_em(date=target.strftime("%Y%m%d"))
                    if frame is None or len(frame.columns) < 5:
                        raise RuntimeError("Eastmoney suspension response has an invalid schema")
                    suspended: set[str] = set()
                    for row in frame.itertuples(index=False, name=None):
                        code = normalize_symbol(row[1])
                        start = self._optional_date(row[3])
                        end = self._optional_date(row[4])
                        if start is not None and start <= target and (end is None or end >= target):
                            suspended.add(code)
                    return frozenset(sorted(suspended))
                except Exception as error:
                    failures.append(f"attempt {attempt}: {type(error).__name__}: {error}")
                    if attempt == self.attempts:
                        raise RuntimeError(
                            f"Eastmoney suspension facts unavailable after {self.attempts} attempts: "
                            f"{'; '.join(failures)}"
                        ) from error
                    if self.backoff_seconds:
                        time.sleep(self.backoff_seconds * attempt)
            raise RuntimeError("Eastmoney suspension facts unavailable")
        finally:
            socket.setdefaulttimeout(previous_socket_timeout)
