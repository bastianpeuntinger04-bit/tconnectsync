#!/usr/bin/env python3

import http.client
import os
import unittest
import urllib.request

from tconnectsync.util.render_healthcheck import maybe_start_health_server

# Fixed high ports, unlikely to collide with anything else on a CI runner.
TEST_PORT = 18765
TEST_PORT_HEAD = 18766


class TestMaybeStartHealthServer(unittest.TestCase):
    def setUp(self):
        self._orig_port = os.environ.get('PORT')

    def tearDown(self):
        if self._orig_port is None:
            os.environ.pop('PORT', None)
        else:
            os.environ['PORT'] = self._orig_port

    def test_noop_when_port_not_set(self):
        os.environ.pop('PORT', None)
        maybe_start_health_server()  # must not raise or hang

    def test_noop_when_port_not_an_integer(self):
        os.environ['PORT'] = 'not-a-number'
        maybe_start_health_server()  # must not raise

    def test_binds_and_serves_200_when_port_set(self):
        os.environ['PORT'] = str(TEST_PORT)
        maybe_start_health_server()

        with urllib.request.urlopen('http://127.0.0.1:%d/' % TEST_PORT, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b'tconnectsync is running', resp.read())

    def test_head_request_returns_200(self):
        os.environ['PORT'] = str(TEST_PORT_HEAD)
        maybe_start_health_server()

        conn = http.client.HTTPConnection('127.0.0.1', TEST_PORT_HEAD, timeout=5)
        try:
            conn.request('HEAD', '/')
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
