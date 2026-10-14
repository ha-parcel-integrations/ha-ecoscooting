"""Tests for the Ecoscooting API client."""
import json
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.ecoscooting.api import (
    EcoscootingApiClient,
    EcoscootingApiError,
)
from custom_components.ecoscooting.const import (
    REQUEST_MSG_TYPE,
    REQUEST_TO_CODE,
    TRACKING_API_URL,
)

from .payloads import (
    DELIVERED_CODE,
    QUERY_ERROR,
    XML_ERROR,
    delivered_sample,
)

CODE = DELIVERED_CODE


def _session_returning(status: int, body: object = None) -> MagicMock:
    response = AsyncMock()
    response.status = status
    if isinstance(body, str):
        response.json = AsyncMock(side_effect=json.JSONDecodeError("x", body, 0))
    else:
        response.json = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


async def test_get_parcel_posts_the_exact_form_the_gateway_expects():
    """The gateway rejects the request outright if any field is missing."""
    session = _session_returning(200, delivered_sample())
    client = EcoscootingApiClient(session)

    await client.async_get_parcel(CODE)

    url = session.post.call_args[0][0]
    form = session.post.call_args[1]["data"]
    assert url == TRACKING_API_URL
    assert json.loads(form["logistics_interface"]) == {
        "mailNo": CODE,
        "locale": "en_US",
        "role": "endUser",
    }
    assert form["msg_type"] == REQUEST_MSG_TYPE
    assert form["to_code"] == REQUEST_TO_CODE
    # Present and non-empty is all the gateway checks, but it does check that.
    assert form["data_digest"]
    assert form["logistic_provider_id"]


async def test_get_parcel_returns_the_whole_reply_on_success():
    sample = delivered_sample()
    client = EcoscootingApiClient(_session_returning(200, sample))

    assert await client.async_get_parcel(CODE) == sample


async def test_get_parcel_accepts_a_case_insensitive_reference_echo():
    sample = delivered_sample("CNPRT00000000000000000009")
    client = EcoscootingApiClient(_session_returning(200, sample))

    assert await client.async_get_parcel("cnprt00000000000000000009") == sample


async def test_get_parcel_returns_none_when_the_lookup_fails():
    """Unknown and unavailable share one error code, so this is not-found."""
    client = EcoscootingApiClient(_session_returning(200, QUERY_ERROR))

    assert await client.async_get_parcel("000000000000000099") is None


async def test_get_parcel_branches_on_the_body_not_the_status():
    """Every reply is HTTP 200 — success must never be inferred from that."""
    session = _session_returning(200, QUERY_ERROR)
    client = EcoscootingApiClient(session)

    assert await client.async_get_parcel(CODE) is None


async def test_get_parcel_rejects_a_reply_about_another_reference():
    client = EcoscootingApiClient(
        _session_returning(200, delivered_sample("000000000000000098"))
    )

    with pytest.raises(EcoscootingApiError, match="different reference"):
        await client.async_get_parcel(CODE)


async def test_get_parcel_tolerates_a_success_without_a_reference():
    """A reply that omits the echo is still about the code we asked for."""
    sample = delivered_sample()
    del sample["packageParam"]["trackingNumber"]
    client = EcoscootingApiClient(_session_returning(200, sample))

    assert await client.async_get_parcel(CODE) == sample


async def test_get_parcel_raises_on_xml_error_reply(caplog):
    """A rejected request answers XML; it must not look like not-found."""
    client = EcoscootingApiClient(_session_returning(200, XML_ERROR))

    with pytest.raises(EcoscootingApiError):
        await client.async_get_parcel(CODE)


async def test_get_parcel_warns_once_on_an_unknown_error_code(caplog):
    client = EcoscootingApiClient(
        _session_returning(200, {"success": "false", "errorCode": "S99"})
    )

    for _ in range(2):
        with pytest.raises(EcoscootingApiError, match="S99"):
            await client.async_get_parcel(CODE)

    assert caplog.text.count("S99") == 1  # warned once, not once per poll
    assert "issues/new" in caplog.text


async def test_get_parcel_raises_on_error_envelope_without_a_code():
    client = EcoscootingApiClient(_session_returning(200, {"success": "false"}))

    with pytest.raises(EcoscootingApiError):
        await client.async_get_parcel(CODE)


async def test_get_parcel_raises_on_non_object_body():
    client = EcoscootingApiClient(_session_returning(200, ["not", "a", "dict"]))

    with pytest.raises(EcoscootingApiError):
        await client.async_get_parcel(CODE)


async def test_get_parcel_raises_on_error_status():
    client = EcoscootingApiClient(_session_returning(500, {}))

    with pytest.raises(EcoscootingApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.status_code == 500


async def test_get_parcel_reads_retry_after_on_429():
    session = _session_returning(429, {})
    session.post.return_value.__aenter__.return_value.headers = {"Retry-After": "90"}
    client = EcoscootingApiClient(session)

    with pytest.raises(EcoscootingApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.retry_after == 90.0


async def test_get_parcel_ignores_an_http_date_retry_after():
    session = _session_returning(429, {})
    session.post.return_value.__aenter__.return_value.headers = {
        "Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"
    }
    client = EcoscootingApiClient(session)

    with pytest.raises(EcoscootingApiError) as err:
        await client.async_get_parcel(CODE)
    assert err.value.retry_after is None


async def test_get_parcel_propagates_network_error():
    """ClientError is left alone — DataUpdateCoordinator already wraps it."""
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    client = EcoscootingApiClient(session)

    with pytest.raises(aiohttp.ClientError):
        await client.async_get_parcel(CODE)
