#!/usr/bin/env python3
"""Tests for NightscoutApi's retry behavior (429/502/503/504)."""

import unittest
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests_mock

from tconnectsync.nightscout import (
    NightscoutApi,
    COLD_START_RETRY_DELAYS_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
)
from tconnectsync.api.common import ApiException

NS_URL = 'https://my-nightscout.example.com/'
NS_SECRET = 'testsecret'


class TestNightscoutColdStartRetry(unittest.TestCase):
    def setUp(self):
        self.ns = NightscoutApi(NS_URL, NS_SECRET)
        # Retries sleep for real by default; patch it out so these tests run
        # in milliseconds instead of the ~37s the real backoff schedule sums to.
        sleep_patcher = patch('tconnectsync.nightscout.time.sleep')
        self.mock_sleep = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_immediate_success_makes_one_request(self):
        with requests_mock.Mocker() as m:
            m.get(requests_mock.ANY, status_code=200, json={})
            self.ns.api_status()
            self.assertEqual(m.call_count, 1)
        self.mock_sleep.assert_not_called()

    def test_502_then_200_retries_once(self):
        with requests_mock.Mocker() as m:
            m.get(requests_mock.ANY, [
                {'status_code': 502, 'text': '<html>Bad Gateway</html>'},
                {'status_code': 200, 'json': {}},
            ])
            self.ns.api_status()
            self.assertEqual(m.call_count, 2)
        self.mock_sleep.assert_called_once_with(COLD_START_RETRY_DELAYS_SECONDS[0])

    def test_503_and_504_are_also_retried(self):
        for status in (503, 504):
            with self.subTest(status=status):
                with requests_mock.Mocker() as m:
                    m.get(requests_mock.ANY, [
                        {'status_code': status, 'text': '<html></html>'},
                        {'status_code': 200, 'json': {}},
                    ])
                    self.ns.api_status()
                    self.assertEqual(m.call_count, 2)

    def test_persistent_502_exhausts_all_retries_then_raises(self):
        with requests_mock.Mocker() as m:
            m.post(requests_mock.ANY, status_code=502, text='<html>Bad Gateway</html>')
            with self.assertRaises(ApiException):
                self.ns.upload_entry({'foo': 'bar'})
            # 1 initial attempt + 4 retries = 5 total requests
            self.assertEqual(m.call_count, len(COLD_START_RETRY_DELAYS_SECONDS) + 1)
        self.assertEqual(self.mock_sleep.call_count, len(COLD_START_RETRY_DELAYS_SECONDS))
        for delay in COLD_START_RETRY_DELAYS_SECONDS:
            self.mock_sleep.assert_any_call(delay)

    def test_non_retryable_status_returns_immediately(self):
        with requests_mock.Mocker() as m:
            m.post(requests_mock.ANY, status_code=404, text='not found')
            with self.assertRaises(ApiException):
                self.ns.upload_entry({'foo': 'bar'})
            self.assertEqual(m.call_count, 1)
        self.mock_sleep.assert_not_called()

    def test_upload_entry_retries_on_502_then_succeeds(self):
        with requests_mock.Mocker() as m:
            m.post(requests_mock.ANY, [
                {'status_code': 502, 'text': '<html></html>'},
                {'status_code': 200, 'json': {}},
            ])
            self.ns.upload_entry({'foo': 'bar'})
            self.assertEqual(m.call_count, 2)

    def test_429_without_retry_after_uses_fixed_schedule(self):
        with requests_mock.Mocker() as m:
            m.post(requests_mock.ANY, [
                {'status_code': 429, 'text': 'Too Many Requests'},
                {'status_code': 200, 'json': {}},
            ])
            self.ns.upload_entry({'foo': 'bar'})
            self.assertEqual(m.call_count, 2)
        self.mock_sleep.assert_called_once_with(COLD_START_RETRY_DELAYS_SECONDS[0])

    def test_429_with_integer_retry_after_is_respected(self):
        with requests_mock.Mocker() as m:
            m.post(requests_mock.ANY, [
                {'status_code': 429, 'text': 'Too Many Requests', 'headers': {'Retry-After': '17'}},
                {'status_code': 200, 'json': {}},
            ])
            self.ns.upload_entry({'foo': 'bar'})
        self.mock_sleep.assert_called_once_with(17.0)

    def test_429_with_http_date_retry_after_is_respected(self):
        target = datetime.now(timezone.utc) + timedelta(seconds=12)
        with requests_mock.Mocker() as m:
            m.post(requests_mock.ANY, [
                {'status_code': 429, 'text': 'Too Many Requests', 'headers': {'Retry-After': format_datetime(target, usegmt=True)}},
                {'status_code': 200, 'json': {}},
            ])
            self.ns.upload_entry({'foo': 'bar'})
        self.assertEqual(self.mock_sleep.call_count, 1)
        (slept_seconds,), _ = self.mock_sleep.call_args
        # Allow a couple seconds of slack for test execution time.
        self.assertAlmostEqual(slept_seconds, 12.0, delta=3.0)

    def test_429_retry_after_is_capped_at_max(self):
        with requests_mock.Mocker() as m:
            m.post(requests_mock.ANY, [
                {'status_code': 429, 'text': 'Too Many Requests', 'headers': {'Retry-After': str(int(MAX_RETRY_AFTER_SECONDS) + 600)}},
                {'status_code': 200, 'json': {}},
            ])
            self.ns.upload_entry({'foo': 'bar'})
        self.mock_sleep.assert_called_once_with(MAX_RETRY_AFTER_SECONDS)

    def test_persistent_429_exhausts_retries_then_raises(self):
        with requests_mock.Mocker() as m:
            m.post(requests_mock.ANY, status_code=429, text='Too Many Requests')
            with self.assertRaises(ApiException):
                self.ns.upload_entry({'foo': 'bar'})
            self.assertEqual(m.call_count, len(COLD_START_RETRY_DELAYS_SECONDS) + 1)

    def test_rate_limit_gate_applies_to_next_unrelated_call(self):
        # A sync cycle makes many sequential, unrelated Nightscout calls
        # (one dedup GET per event processor, then per-entry upload POSTs).
        # Once one of them has been told by Retry-After that Nightscout is
        # rate-limited, the next one -- even for a completely different
        # entity -- should wait out the remainder up front instead of also
        # hitting a 429 and retrying independently.
        with requests_mock.Mocker() as m:
            m.post(requests_mock.ANY, [
                {'status_code': 429, 'text': 'Too Many Requests', 'headers': {'Retry-After': '5'}},
                {'status_code': 200, 'json': {}},
            ])
            self.ns.upload_entry({'foo': 'bar'})
            self.mock_sleep.assert_called_once_with(5.0)
            self.mock_sleep.reset_mock()
            calls_before_second = m.call_count

            m.post(requests_mock.ANY, [{'status_code': 200, 'json': {}}])
            self.ns.upload_entry({'baz': 'qux'})

        self.assertEqual(self.mock_sleep.call_count, 1)
        (slept_seconds,), _ = self.mock_sleep.call_args
        self.assertAlmostEqual(slept_seconds, 5.0, delta=1.0)
        # Exactly one POST for the second call -- it waited via the gate
        # instead of hitting 429 again.
        self.assertEqual(m.call_count - calls_before_second, 1)

    def test_expired_rate_limit_gate_does_not_wait(self):
        with requests_mock.Mocker() as m:
            m.post(requests_mock.ANY, [
                {'status_code': 429, 'text': 'Too Many Requests', 'headers': {'Retry-After': '5'}},
                {'status_code': 200, 'json': {}},
            ])
            self.ns.upload_entry({'foo': 'bar'})
            # Simulate the gate having already expired by the time of the
            # next call, rather than actually waiting 5 real seconds.
            self.ns._rate_limited_until = 0
            self.mock_sleep.reset_mock()

            m.post(requests_mock.ANY, [{'status_code': 200, 'json': {}}])
            self.ns.upload_entry({'baz': 'qux'})

        self.mock_sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
