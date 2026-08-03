#!/usr/bin/env python3

import unittest
import requests_mock

from tconnectsync.api.dexcomshare import DexcomShareApi, parse_dexcom_date
from tconnectsync.api.common import ApiException, ApiLoginException

ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"
SESSION_ID = "66666666-7777-8888-9999-000000000000"
EMPTY_SESSION_ID = "00000000-0000-0000-0000-000000000000"

US_AUTH_URL = "https://share2.dexcom.com/ShareWebServices/Services/General/AuthenticatePublisherAccount"
US_LOGIN_URL = "https://share2.dexcom.com/ShareWebServices/Services/General/LoginPublisherAccountById"
US_VALUES_URL = "https://share2.dexcom.com/ShareWebServices/Services/Publisher/ReadPublisherLatestGlucoseValues"

OUS_AUTH_URL = "https://shareous1.dexcom.com/ShareWebServices/Services/General/AuthenticatePublisherAccount"
OUS_LOGIN_URL = "https://shareous1.dexcom.com/ShareWebServices/Services/General/LoginPublisherAccountById"


def register_login(m, auth_url=US_AUTH_URL, login_url=US_LOGIN_URL, session_id=SESSION_ID):
    m.post(auth_url, json=ACCOUNT_ID)
    m.post(login_url, json=session_id)


class TestParseDexcomDate(unittest.TestCase):
    def test_parses_plain_epoch(self):
        self.assertEqual(parse_dexcom_date("/Date(1462404576000)/"), 1462404576000)

    def test_parses_epoch_with_timezone_offset(self):
        self.assertEqual(parse_dexcom_date("/Date(1462404576000-0700)/"), 1462404576000)

    def test_parses_epoch_with_positive_offset(self):
        self.assertEqual(parse_dexcom_date("/Date(1462404576000+0200)/"), 1462404576000)

    def test_raises_on_unparseable_value(self):
        with self.assertRaises(ApiException):
            parse_dexcom_date("not-a-date")


class TestDexcomShareApiLogin(unittest.TestCase):
    def test_login_success_us_region(self):
        with requests_mock.Mocker() as m:
            register_login(m)
            api = DexcomShareApi("user", "pass", "US")
            self.assertEqual(api.session_id, SESSION_ID)
            self.assertEqual(api.base_url, "https://share2.dexcom.com/ShareWebServices/Services")

    def test_login_success_ous_region(self):
        with requests_mock.Mocker() as m:
            register_login(m, auth_url=OUS_AUTH_URL, login_url=OUS_LOGIN_URL)
            api = DexcomShareApi("user", "pass", "OUS")
            self.assertEqual(api.session_id, SESSION_ID)
            self.assertEqual(api.base_url, "https://shareous1.dexcom.com/ShareWebServices/Services")

    def test_login_raises_on_empty_session_id(self):
        with requests_mock.Mocker() as m:
            register_login(m, session_id=EMPTY_SESSION_ID)
            with self.assertRaises(ApiLoginException):
                DexcomShareApi("user", "wrongpass", "US")

    def test_login_raises_on_authenticate_http_error(self):
        with requests_mock.Mocker() as m:
            m.post(US_AUTH_URL, status_code=500, text="Internal Server Error")
            with self.assertRaises(ApiLoginException):
                DexcomShareApi("user", "pass", "US")

    def test_login_raises_on_login_by_id_http_error(self):
        with requests_mock.Mocker() as m:
            m.post(US_AUTH_URL, json=ACCOUNT_ID)
            m.post(US_LOGIN_URL, status_code=401, text="Unauthorized")
            with self.assertRaises(ApiLoginException):
                DexcomShareApi("user", "pass", "US")


class TestDexcomShareApiGetLatestGlucoseValues(unittest.TestCase):
    def test_returns_readings(self):
        readings = [
            {"WT": "/Date(1462404576000)/", "ST": "/Date(1462404576000)/", "DT": "/Date(1462404576000-0700)/", "Value": 120, "Trend": "Flat"},
        ]
        with requests_mock.Mocker() as m:
            register_login(m)
            m.post(US_VALUES_URL, json=readings)

            api = DexcomShareApi("user", "pass", "US")
            result = api.get_latest_glucose_values(minutes=1440, max_count=288)

            self.assertEqual(result, readings)

    def test_relogs_in_once_on_500_then_succeeds(self):
        readings = [{"WT": "/Date(1462404576000)/", "ST": "/Date(1462404576000)/", "DT": "/Date(1462404576000)/", "Value": 100, "Trend": "Flat"}]
        with requests_mock.Mocker() as m:
            register_login(m)
            m.post(US_VALUES_URL, [
                {"status_code": 500, "text": "session expired"},
                {"status_code": 200, "json": readings},
            ])

            api = DexcomShareApi("user", "pass", "US")
            result = api.get_latest_glucose_values()

            self.assertEqual(result, readings)
            # 2 login calls at construction + 2 more from the automatic relogin
            self.assertEqual(m.call_count, 6)

    def test_raises_if_still_failing_after_relogin(self):
        with requests_mock.Mocker() as m:
            register_login(m)
            m.post(US_VALUES_URL, status_code=503, text="still down")

            api = DexcomShareApi("user", "pass", "US")
            with self.assertRaises(ApiException):
                api.get_latest_glucose_values()

    def test_empty_response_returns_empty_list(self):
        with requests_mock.Mocker() as m:
            register_login(m)
            # requests_mock doesn't serialize json=None to the body "null", so
            # set it explicitly to exercise the `r.json() or []` null case.
            m.post(US_VALUES_URL, text="null", headers={"Content-Type": "application/json"})

            api = DexcomShareApi("user", "pass", "US")
            self.assertEqual(api.get_latest_glucose_values(), [])


if __name__ == "__main__":
    unittest.main()
