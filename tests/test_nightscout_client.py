#!/usr/bin/env python3
"""Tests for NightscoutApi's cold-start retry behavior (502/503/504)."""

import unittest
from unittest.mock import patch

import requests_mock

from tconnectsync.nightscout import NightscoutApi, COLD_START_RETRY_DELAYS_SECONDS
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


if __name__ == '__main__':
    unittest.main()
