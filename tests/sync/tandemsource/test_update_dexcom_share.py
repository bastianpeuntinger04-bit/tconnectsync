#!/usr/bin/env python3

import unittest
import arrow

from tconnectsync.sync.tandemsource.update_dexcom_share import UpdateDexcomShare
from tconnectsync import features

from ...api.fake import TConnectApi
from ...nightscout_fake import NightscoutApi

DEVICE_ID = 'irrelevant-device-id'
DEXCOM_DEVICE = 'Dexcom Share (tconnectsync)'


class FakeDexcomShareApi:
    def __init__(self, readings=None):
        self._readings = readings if readings is not None else []
        self.calls = []

    def get_latest_glucose_values(self, minutes=1440, max_count=288):
        self.calls.append((minutes, max_count))
        return self._readings


def reading(value, wt_ms, trend="Flat"):
    return {
        "WT": "Date(%d)" % wt_ms,
        "ST": "Date(%d)" % wt_ms,
        "DT": "Date(%d)" % wt_ms,
        "Value": value,
        "Trend": trend,
    }


class TestUpdateDexcomShareEnabled(unittest.TestCase):
    def test_disabled_by_default(self):
        u = UpdateDexcomShare(TConnectApi(), NightscoutApi(), DEVICE_ID, False, features=features.DEFAULT_FEATURES)
        self.assertFalse(u.enabled())

    def test_enabled_when_feature_set(self):
        u = UpdateDexcomShare(TConnectApi(), NightscoutApi(), DEVICE_ID, False, features=[features.CGM_DEXCOM_SHARE])
        self.assertTrue(u.enabled())


class TestUpdateDexcomShareUpdate(unittest.TestCase):
    def _build(self, readings, last_upload=None):
        tconnect = TConnectApi()
        tconnect._dexcomshare = FakeDexcomShareApi(readings)
        nightscout = NightscoutApi()
        nightscout.last_uploaded_bg_entry = lambda *args, **kwargs: last_upload
        u = UpdateDexcomShare(tconnect, nightscout, DEVICE_ID, False, features=[features.CGM_DEXCOM_SHARE])
        return u, nightscout

    def test_uploads_new_readings_when_none_uploaded_yet(self):
        readings = [reading(120, 1700000000000), reading(125, 1700000300000)]
        u, nightscout = self._build(readings, last_upload=None)

        result = u.update(pretend=False)

        self.assertTrue(result)
        uploaded = nightscout.uploaded_entries['entries']
        self.assertEqual(len(uploaded), 2)
        self.assertEqual(uploaded[0]['sgv'], 120)
        self.assertEqual(uploaded[0]['device'], DEXCOM_DEVICE)
        self.assertEqual(uploaded[0]['type'], 'sgv')
        self.assertEqual(uploaded[1]['sgv'], 125)

    def test_skips_readings_not_after_last_upload(self):
        readings = [reading(120, 1700000000000), reading(125, 1700000300000)]
        u, nightscout = self._build(readings, last_upload={'date': 1700000000000})

        result = u.update(pretend=False)

        self.assertTrue(result)
        uploaded = nightscout.uploaded_entries['entries']
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(uploaded[0]['sgv'], 125)

    def test_no_new_readings_returns_false(self):
        readings = [reading(120, 1700000000000)]
        u, nightscout = self._build(readings, last_upload={'date': 1700000000000})

        result = u.update(pretend=False)

        self.assertFalse(result)
        self.assertEqual(nightscout.uploaded_entries['entries'], [])

    def test_pretend_mode_does_not_upload(self):
        readings = [reading(120, 1700000000000)]
        u, nightscout = self._build(readings, last_upload=None)

        result = u.update(pretend=True)

        self.assertTrue(result)
        self.assertEqual(nightscout.uploaded_entries['entries'], [])

    def test_trend_mapped_to_direction(self):
        readings = [reading(120, 1700000000000, trend="FortyFiveUp")]
        u, nightscout = self._build(readings, last_upload=None)

        u.update(pretend=False)

        uploaded = nightscout.uploaded_entries['entries']
        self.assertEqual(uploaded[0]['direction'], 'FortyFiveUp')

    def test_out_of_range_trend_mapped_to_spelled_out_direction(self):
        readings = [reading(400, 1700000000000, trend="RateOutOfRange")]
        u, nightscout = self._build(readings, last_upload=None)

        u.update(pretend=False)

        uploaded = nightscout.uploaded_entries['entries']
        self.assertEqual(uploaded[0]['direction'], 'RATE OUT OF RANGE')

    def test_unrecognized_trend_omits_direction(self):
        readings = [reading(120, 1700000000000, trend="SomeFutureTrendValue")]
        u, nightscout = self._build(readings, last_upload=None)

        u.update(pretend=False)

        uploaded = nightscout.uploaded_entries['entries']
        self.assertNotIn('direction', uploaded[0])

    def test_last_upload_dateString_fallback(self):
        ts_ms = 1700000000000
        readings = [reading(120, ts_ms), reading(125, ts_ms + 300000)]
        last_upload_datestring = arrow.get(ts_ms / 1000.0).strftime('%Y-%m-%dT%H:%M:%S%z')
        u, nightscout = self._build(readings, last_upload={'dateString': last_upload_datestring})

        result = u.update(pretend=False)

        uploaded = nightscout.uploaded_entries['entries']
        self.assertEqual(len(uploaded), 1)
        self.assertEqual(uploaded[0]['sgv'], 125)

    def test_readings_uploaded_oldest_first(self):
        readings = [reading(125, 1700000300000), reading(120, 1700000000000)]
        u, nightscout = self._build(readings, last_upload=None)

        u.update(pretend=False)

        uploaded = nightscout.uploaded_entries['entries']
        self.assertEqual([e['sgv'] for e in uploaded], [120, 125])


if __name__ == "__main__":
    unittest.main()
