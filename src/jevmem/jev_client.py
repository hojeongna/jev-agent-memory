"""Minimal client for TypeSafe's Jev ("System One") decision model.

Jev does not write text. It answers three question types with probabilities:
  - noul   : yes/no probability
  - choice : pick one of fixed options; every option gets a probability
  - score  : graded score
This package only uses `choice`.

Safety rules kept from the original implementation:
  - Errors never contain the API key (a timeout's repr can include the full request).
  - Anything that echoes server/transport text passes through `_mask`.

`timeout` is a hard deadline for the whole call (DNS, connect, sending, a slowly trickling
response): the request runs in a daemon thread and the caller stops waiting when the time
is up. urllib's own timeout only bounds each socket operation, so a server sending one byte
every half second would otherwise keep the hook waiting far past its 4 s budget.
BillingError means HTTP status 402 and nothing else (no guessing from body text).
Redirects are never followed: urllib would resend the Authorization header to wherever a 3xx
points (another host, plain http). A redirect is reported as an error naming [jev] url.

Any object with a `.call(state, questions, timeout) -> (answers, usage)` method can
replace this client (the tests use a fake one; no real API call is ever made there).
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__


class JevError(RuntimeError):
    """Any Jev failure. Message is guaranteed key-free."""


class BillingError(JevError):
    """HTTP 402. Callers open a circuit breaker on this."""


class JevUnavailable(JevError):
    """The service could not answer: timeout, connection refused/reset, DNS failure, TLS or
    transport error, HTTP 5xx or 429. Not the request's fault, so labeling must not count it
    against a chunk. (A response that came back but is unusable stays a plain JevError.)"""


class JevTimeout(JevUnavailable):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError for the 3xx itself


def _open(req, timeout: float):
    """urlopen without redirects (the key must never travel to another address)."""
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def load_api_key(cfg) -> str:
    env = cfg["jev"].get("api_key_env") or "TYPESAFE_API_KEY"
    key = os.environ.get(env, "").strip()
    if key:
        return key
    kf = cfg["jev"].get("api_key_file")
    if kf:
        p = Path(kf).expanduser()
        if p.exists():
            key = p.read_text(encoding="utf-8").strip()
            if key:
                return key
    raise JevError(f"No Jev API key: set ${env} (or [jev].api_key_file)")


class JevClient:
    # Cloudflare in front of the API rejects the default Python-urllib user agent.
    USER_AGENT = f"jev-agent-memory/{__version__}"

    def __init__(self, api_key: str, url: str, model: str = "jev-latest"):
        self._key = api_key
        self.url = url
        self.model = model

    @classmethod
    def from_config(cls, cfg) -> "JevClient":
        return cls(load_api_key(cfg), cfg["jev"]["url"], cfg["jev"].get("model", "jev-latest"))

    def _mask(self, text: str) -> str:
        text = str(text)
        if self._key:
            text = text.replace(self._key, "***")
        return text

    def call(self, state: str, questions: dict, timeout: float = 2.0):
        """Returns (answers: dict, usage: dict). Raises JevError subclasses; JevTimeout once
        `timeout` seconds have passed, however the server behaves."""
        timeout = max(0.05, float(timeout))
        body = json.dumps(
            {"state": state, "model": self.model, "questions": questions}, ensure_ascii=False
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "User-Agent": self.USER_AGENT,
            },
        )
        box: dict = {}

        def work():
            try:
                box["raw"] = self._request(req, timeout)
            except BaseException as e:  # handed to the caller; never printed by the thread
                box["error"] = e

        t = threading.Thread(target=work, name="jev-call", daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            # The thread ends on its own when its socket times out; nobody waits for it.
            raise JevTimeout(f"Jev timeout after {timeout:.2f}s")
        if "error" in box:
            raise box.pop("error")
        raw = box["raw"]
        try:
            data = json.loads(raw)
        except ValueError:
            raise JevError(self._mask(f"Jev returned non-JSON: {raw[:200]}")) from None
        if not isinstance(data, dict):
            raise JevError("Jev returned JSON that is not an object")
        if "detail" in data:
            raise JevError(self._mask(f"Jev API error: {data['detail']}"))
        return data["answers"], data.get("usage", {})

    def _request(self, req, timeout: float) -> str:
        try:
            with _open(req, timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                try:
                    e.close()
                except Exception:
                    pass
                raise JevError(f"Jev HTTP {e.code}: a redirect, not followed (the API key would go along); "
                               "check [jev] url") from None
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            finally:
                try:
                    e.close()
                except Exception:
                    pass
            msg = self._mask(f"Jev HTTP {e.code}: {detail}")
            if e.code == 402:
                raise BillingError(msg) from None
            if e.code == 429 or e.code >= 500:
                raise JevUnavailable(msg) from None
            raise JevError(msg) from None
        except (socket.timeout, TimeoutError):
            raise JevTimeout(f"Jev timeout after {timeout:.2f}s") from None
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise JevTimeout(f"Jev timeout after {timeout:.2f}s") from None
            raise JevUnavailable(self._mask(f"Jev connection error: {reason}")) from None
        except (OSError, http.client.HTTPException) as e:  # reset, TLS, cut-off response
            raise JevUnavailable(self._mask(f"Jev connection error: {type(e).__name__}: {e}")) from None
        except Exception as e:  # never let a key-bearing repr escape
            raise JevError(self._mask(f"Jev error: {type(e).__name__}: {e}")) from None


def is_billing_error(exc: BaseException) -> bool:
    """HTTP 402 from Jev (BillingError). Nothing is guessed from message text: a 502 page
    whose body happens to contain "402" (e.g. a Cloudflare Ray ID) is not a billing error."""
    return isinstance(exc, BillingError)


def is_outage(exc: BaseException) -> bool:
    """Jev could not answer (JevUnavailable: timeout, connection or DNS error, HTTP 5xx/429).
    Decided by exception class only, never by message text."""
    return isinstance(exc, JevUnavailable)
