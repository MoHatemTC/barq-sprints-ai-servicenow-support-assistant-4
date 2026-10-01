"""Tests for the transient-vs-permanent retry classifier (no infrastructure needed)."""
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from barq_ai_support.retry_policy import is_transient_error


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://example.test/api")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_rate_limit_and_5xx_are_transient(status):
    assert is_transient_error(_status_error(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_4xx_are_permanent(status):
    assert is_transient_error(_status_error(status)) is False


def test_network_errors_are_transient():
    assert is_transient_error(httpx.ConnectError("connection refused")) is True
    assert is_transient_error(httpx.ReadTimeout("timed out")) is True
    assert is_transient_error(TimeoutError("slow")) is True
    assert is_transient_error(ConnectionError("reset")) is True


def test_unrelated_errors_are_permanent():
    assert is_transient_error(ValueError("malformed payload")) is False
    assert is_transient_error(KeyError("sys_id")) is False
    assert is_transient_error(None) is False


def test_wrapped_network_error_is_transient():
    """A library that wraps a network error in its own exception type
    should still be recognised as transient via the cause chain."""

    class WrapperError(Exception):
        pass

    try:
        try:
            raise httpx.ConnectError("underlying")
        except httpx.ConnectError as inner:
            raise WrapperError("client wrapper") from inner
    except WrapperError as wrapped:
        assert is_transient_error(wrapped) is True


def test_wrapped_permanent_status_error_stays_permanent():
    class WrapperError(Exception):
        pass

    try:
        try:
            raise _status_error(404)
        except httpx.HTTPStatusError as inner:
            raise WrapperError("client wrapper") from inner
    except WrapperError as wrapped:
        assert is_transient_error(wrapped) is False
