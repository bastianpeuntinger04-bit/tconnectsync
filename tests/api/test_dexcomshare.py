#!/usr/bin/env python3

import unittest
import requests_mock

from tconnectsync.api.dexcomshare import (
    DexcomShareApi,
    parse_dexcom_date,
    APPLICATION_ID_DEFAULT,
    APPLICATION_ID_JP,
)
from tconnectsync.api.common import ApiException, ApiLoginException

ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"
SESSION_ID = "66666666-7777-8888-9999-000000000000"
EMPTY_SESSION_ID = "00000000-0000-0000-0000-000000000000"

US_AUTH_URL = "https://share2.dexcom.com/ShareWebServices/Services/General/AuthenticatePublisherAccount"
US_LOGIN_URL = "https://share2.dexcom.com/ShareWebServices/Services/General/LoginPublisherAccountById"
US_VALUES_URL = "https://share2.dexcom.com/ShareWebServices/Services/Publisher/ReadPublisherLatestGlucoseValues"

OUS_AUTH_URL = "https://shareous1.dexcom.com/ShareWebServices/Services/General/AuthenticatePublisherAccount"
OUS_LOGIN_URL = "https://shareous1.dexcom.com/ShareWebServices/Services/General/LoginPublisherAccountById"

JP_AUTH_URL = "https://share.dexcom.jp/ShareWebServices/Services/General/AuthenticatePublisherAccount"
JP_LOGIN_URL = "https://share.dexcom.jp/ShareWebServices/Services/General/LoginPublisherAccountById"


def register_login(m, auth_url=US_AUTH_URL, login_url=US_LOGIN_URL, session_id=SESSION_ID):
    m.post(auth_url, json=ACCOUNT_ID)
    m.post(login_url, json=session_id)


class TestParseDexcomDate(unittest.TestCase):
    # The live API returns the bare (slash-less) form; confirmed against a
    # real account and pydexcom's own captured example output.
    def test_parses_plain_epoch(self):
        self.assertEqual(parse_dexcom_date("Date(1462404576000)"), 1462404576000)

    def test_parses_epoch_with_timezone_offset(self):
        self.assertEqual(parse_dexcom_date("Date(1462404576000-0700)"), 1462404576000)

    def test_parses_epoch_with_positive_offset(self):
        self.assertEqual(parse_dexcom_date("Date(1462404576000+0200)"), 1462404576000)

    # The classic ASP.NET-style '/Date(...)/ ' wrapper is also accepted,
    # since the format isn't documented and could vary by endpoint/version.
    def test_also_accepts_slash_wrapped_form(self):
        self.assertEqual(parse_dexcom_date("/Date(1462404576000)/"), 1462404576000)

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

    def test_invalid_region_raises_value_error(self):
        with self.assertRaises(ValueError):
            DexcomShareApi("user", "pass", "DE")

    def test_error_body_code_and_message_surfaced(self):
        with requests_mock.Mocker() as m:
            m.post(US_AUTH_URL, status_code=500, json={"Code": "AccountPasswordInvalid", "Message": "Password does not match."})
            with self.assertRaisesRegex(ApiLoginException, "AccountPasswordInvalid: Password does not match."):
                DexcomShareApi("user", "pass", "US")

    def test_us_and_ous_use_same_application_id(self):
        # Regression test: US and OUS (incl. Germany/Europe) must use the
        # same application id -- using the JP-only id here produces a
        # misleading AccountPasswordInvalid even with correct credentials.
        for region, auth_url, login_url in [
            ("US", US_AUTH_URL, US_LOGIN_URL),
            ("OUS", OUS_AUTH_URL, OUS_LOGIN_URL),
        ]:
            with self.subTest(region=region), requests_mock.Mocker() as m:
                register_login(m, auth_url=auth_url, login_url=login_url)
                DexcomShareApi("user", "pass", region)

                auth_body = m.request_history[0].json()
                login_body = m.request_history[1].json()
                self.assertEqual(auth_body["applicationId"], APPLICATION_ID_DEFAULT)
                self.assertEqual(login_body["applicationId"], APPLICATION_ID_DEFAULT)
                self.assertNotEqual(APPLICATION_ID_DEFAULT, APPLICATION_ID_JP)

    def test_jp_region_uses_its_own_application_id_and_url(self):
        with requests_mock.Mocker() as m:
            register_login(m, auth_url=JP_AUTH_URL, login_url=JP_LOGIN_URL)
            api = DexcomShareApi("user", "pass", "JP")

            self.assertEqual(api.base_url, "https://share.dexcom.jp/ShareWebServices/Services")
            auth_body = m.request_history[0].json()
            self.assertEqual(auth_body["applicationId"], APPLICATION_ID_JP)


class TestDexcomShareApiGetLatestGlucoseValues(unittest.TestCase):
    def test_returns_readings(self):
        readings = [
            {"WT": "Date(1462404576000)", "ST": "Date(1462404576000)", "DT": "Date(1462404576000-0700)", "Value": 120, "Trend": "Flat"},
        ]
        with requests_mock.Mocker() as m:
            register_login(m)
            m.post(US_VALUES_URL, json=readings)

            api = DexcomShareApi("user", "pass", "US")
            result = api.get_latest_glucose_values(minutes=1440, max_count=288)

            self.assertEqual(result, readings)

    def test_relogs_in_once_on_500_then_succeeds(self):
        readings = [{"WT": "Date(1462404576000)", "ST": "Date(1462404576000)", "DT": "Date(1462404576000)", "Value": 100, "Trend": "Flat"}]
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
