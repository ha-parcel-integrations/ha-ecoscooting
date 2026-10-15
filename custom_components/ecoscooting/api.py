"""Ecoscooting public tracking API client.

One form-encoded POST per reference, no account and no session. The contract
the coordinator relies on:

* ``async_get_parcel`` returns the raw reply dict on success,
* returns ``None`` when the gateway cannot find the reference (a normal,
  expected state — never an error, and never proof the parcel does not
  exist),
* raises :class:`EcoscootingApiError` for anything else, with
  ``status_code`` set on a non-2xx response and ``retry_after`` set when the
  carrier's own ``Retry-After`` header on a 429 could be parsed as seconds —
  the coordinator's backoff reads both,
* lets ``aiohttp.ClientError`` propagate untouched — ``DataUpdateCoordinator``
  already wraps those into ``UpdateFailed``.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from .const import (
    ERROR_CODE_QUERY_FAILED,
    REQUEST_DIGEST,
    REQUEST_LOCALE,
    REQUEST_MSG_TYPE,
    REQUEST_PROVIDER_ID,
    REQUEST_ROLE,
    REQUEST_TO_CODE,
    TRACKING_API_URL,
)
from .parcels import NEW_ISSUE_URL

_LOGGER = logging.getLogger(__name__)

# Pre-1.0 convention (CONVENTIONS.md § Pre-1.0 releases): an envelope we have
# never seen gets one WARNING with a copy-paste issue link, not a silent
# guess. Codes and keys only — the reply carries a destination postcode and,
# on a delivery event, door coordinates.
_envelopes_warned: set[str] = set()


def _warn_unexpected_envelope(error_code: Any, keys: list[str]) -> None:
    """Log an unrecognised reply envelope once per session."""
    key = str(error_code)
    if key in _envelopes_warned:
        return
    _envelopes_warned.add(key)
    _LOGGER.warning(
        "Unrecognised Ecoscooting reply — help us handle it. Open an issue "
        "and paste this line: %s\n  errorCode=%s keys=%s",
        NEW_ISSUE_URL,
        key,
        sorted(keys),
    )


class EcoscootingApiError(Exception):
    """Raised when an Ecoscooting API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"Ecoscooting API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


class EcoscootingApiClient:
    """Client for the public Ecoscooting tracking endpoint.

    No authentication and no session: the lookup is keyed on the tracking
    reference alone. Everything answers HTTP 200 — a found reference, an
    unknown one and a rejected request alike — so the body decides, never the
    status code.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        Returns the full reply for a known reference, or ``None`` when the
        gateway reports it as not found — which it also does for a reference
        whose order is merely unavailable, so this is "not found or
        unavailable", never proof of absence. Any other envelope or non-2xx
        status raises :class:`EcoscootingApiError`; network errors propagate
        as ``aiohttp.ClientError``.
        """
        payload = {
            "logistics_interface": json.dumps(
                {
                    "mailNo": tracking_code,
                    "locale": REQUEST_LOCALE,
                    "role": REQUEST_ROLE,
                },
                separators=(",", ":"),
            ),
            "msg_type": REQUEST_MSG_TYPE,
            "logistic_provider_id": REQUEST_PROVIDER_ID,
            "data_digest": REQUEST_DIGEST,
            "to_code": REQUEST_TO_CODE,
        }
        async with self._session.post(TRACKING_API_URL, data=payload) as response:
            if response.status == 429:
                retry_after_header = response.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_header) if retry_after_header else None
                except ValueError:
                    retry_after = None  # an HTTP-date, not seconds; let the caller's own backoff handle it
                raise EcoscootingApiError(
                    "HTTP 429", status_code=429, retry_after=retry_after
                )
            if response.status != 200:
                raise EcoscootingApiError(
                    f"HTTP {response.status}", status_code=response.status
                )
            try:
                # content_type=None: the gateway serves JSON as text/plain, and
                # aiohttp would otherwise refuse to parse it.
                body = await response.json(content_type=None)
            except ValueError as err:
                # A rejected request comes back as XML rather than JSON, which
                # lands here. Retained as an error so the coordinator keeps the
                # last-good state instead of blanking the parcel.
                raise EcoscootingApiError(f"unparseable body ({err})") from err

        if not isinstance(body, dict):
            raise EcoscootingApiError("unexpected body (not a JSON object)")

        if body.get("success") == "true":
            reported = (body.get("packageParam") or {}).get("trackingNumber")
            if reported and str(reported).upper() != tracking_code.upper():
                # The gateway answering about a different reference means the
                # reply cannot be trusted for this parcel.
                raise EcoscootingApiError("reply is for a different reference")
            return body

        error_code = body.get("errorCode")
        if error_code == ERROR_CODE_QUERY_FAILED:
            return None
        _warn_unexpected_envelope(error_code, list(body))
        raise EcoscootingApiError(str(error_code or "unknown error envelope"))
