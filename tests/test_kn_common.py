"""Tests for kn_common helpers (run from project root via `pytest tests/`).

This is the first test module outside kn_gui/. We use a top-level
conftest.py that puts the project root on sys.path so `import kn_common`
works.
"""
from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

import kn_common
from kn_common import is_error_output, is_valid_fqdn, validate_fqdns, with_retry


# ── is_error_output ────────────────────────────────────────────────────────

@pytest.mark.parametrize('text, expected', [
    ('',                                           False),
    ('Network::RoutingTable: Added route.',        False),
    ('[ok] command succeeded',                     False),
    # False-positive guards: the old `'rror' in text` heuristic would fail these.
    ('Audio mirror mode disabled',                 False),
    ('error-correction enabled',                   False),
    ('unexpected invalid-auth flag cleared',       False),
    # True positives:
    ('Error: something broke',                     True),
    ('%% Invalid input detected',                  True),
    ('%% Error: command not permitted',            True),
    ('ERROR: permission denied',                   True),
])
def test_is_error_output(text, expected):
    assert is_error_output(text) is expected


# ── with_retry ─────────────────────────────────────────────────────────────

def test_retry_returns_on_first_success():
    called = {'n': 0}

    def fn():
        called['n'] += 1
        return 'ok'

    assert with_retry(fn, attempts=3, initial_delay=0) == 'ok'
    assert called['n'] == 1


def test_retry_succeeds_after_transient_failures():
    called = {'n': 0}

    def fn():
        called['n'] += 1
        if called['n'] < 3:
            raise ConnectionError('transient')
        return 'ok'

    with patch('kn_common.time.sleep'):  # don't actually sleep in tests
        result = with_retry(fn, attempts=5, initial_delay=0.01)
    assert result == 'ok'
    assert called['n'] == 3


def test_retry_raises_after_all_attempts_fail():
    def fn():
        raise socket.gaierror('dns broken')

    with patch('kn_common.time.sleep'):
        with pytest.raises(socket.gaierror):
            with_retry(fn, attempts=3, initial_delay=0.01)


def test_retry_does_not_retry_non_retriable():
    """PermissionError (auth failure) must propagate immediately."""
    called = {'n': 0}

    def fn():
        called['n'] += 1
        raise PermissionError('wrong password')

    with pytest.raises(PermissionError):
        with_retry(fn, attempts=5, initial_delay=0)
    assert called['n'] == 1  # no retries


# ── is_valid_fqdn / validate_fqdns ─────────────────────────────────────────

@pytest.mark.parametrize('fqdn, expected', [
    ('example.com',          True),
    ('a.b.c.example.com',    True),
    ('xn--e1afmkfd.xn--p1ai',True),  # Punycode for пример.рф
    ('example.com.',         True),  # trailing dot OK
    ('EXAMPLE.com',          True),  # case-insensitive
    ('123.example.com',      True),  # numeric label OK
    # Invalid:
    ('',                     False),
    ('bare',                 False),  # no dot
    ('.example.com',         False),  # leading dot
    ('example..com',         False),  # empty label
    ('-example.com',         False),  # leading dash
    ('example-.com',         False),  # trailing dash
    ('пример.рф',            False),  # non-ASCII (caller must Punycode)
    ('*.example.com',        False),  # wildcard — not allowed by strict parser
    ('example com',          False),  # space
    ('example;rm -rf /',     False),  # shell metacharacters
    ('a' * 64 + '.com',      False),  # label too long (>63)
    (('a' * 62 + '.') * 5 + 'com',     False),  # total > 253
])
def test_is_valid_fqdn(fqdn, expected):
    assert is_valid_fqdn(fqdn) is expected


def test_validate_fqdns_splits_by_validity():
    valid, invalid = validate_fqdns([
        'good.com', '-bad.com', 'also.good.com', '', 'third.good.net',
    ])
    assert valid == ['good.com', 'also.good.com', 'third.good.net']
    assert invalid == ['-bad.com', '']


def test_retry_calls_on_retry_callback():
    events: list[tuple[int, float]] = []

    def fn():
        raise TimeoutError('boom')

    with patch('kn_common.time.sleep'):
        with pytest.raises(TimeoutError):
            with_retry(
                fn, attempts=3, initial_delay=1.0, backoff=2.0,
                on_retry=lambda a, d, e: events.append((a, d)),
            )
    # On 3 attempts: callback fires before retry 2 (attempt=1 → delay 1.0)
    # and before retry 3 (attempt=2 → delay 2.0). Final failure does NOT
    # call the callback.
    assert events == [(1, 1.0), (2, 2.0)]
