import logging
import arrow

from typing import List, TYPE_CHECKING
if TYPE_CHECKING:
    from ...api import TConnectApi
    from ...nightscout import NightscoutApi

from ...features import DEFAULT_FEATURES
from ... import features
from ... import secret
from ...api.dexcomshare import parse_dexcom_date
from ...parser.nightscout import NightscoutEntry, DEXCOM_SHARE_ENTERED_BY

logger = logging.getLogger(__name__)

# Dexcom Share's named trend strings -> Nightscout's expected `direction`
# values. Identical except for the two out-of-range sentinels, which
# Nightscout spells with spaces.
TREND_TO_DIRECTION = {
    "None": "NONE",
    "DoubleUp": "DoubleUp",
    "SingleUp": "SingleUp",
    "FortyFiveUp": "FortyFiveUp",
    "Flat": "Flat",
    "FortyFiveDown": "FortyFiveDown",
    "SingleDown": "SingleDown",
    "DoubleDown": "DoubleDown",
    "NotComputable": "NOT COMPUTABLE",
    "RateOutOfRange": "RATE OUT OF RANGE",
}


class UpdateDexcomShare:
    """Polls the Dexcom Share API directly for near-real-time CGM readings
    and uploads any not yet in Nightscout. Independent of the Tandem pump
    event stream entirely -- ignores tconnect_device_id -- so it runs once
    per sync cycle regardless of which pump is selected.
    """

    def __init__(self, tconnect: "TConnectApi", nightscout: "NightscoutApi", tconnect_device_id: str, pretend: bool, features: List[str] = DEFAULT_FEATURES) -> None:
        self.tconnect = tconnect
        self.nightscout = nightscout
        self.pretend = pretend
        self.features = features

    def enabled(self) -> bool:
        return features.CGM_DEXCOM_SHARE in self.features

    def update(self, pretend: bool) -> bool:
        last_upload = self.nightscout.last_uploaded_bg_entry(device=DEXCOM_SHARE_ENTERED_BY)
        last_upload_time_ms = self._last_upload_time_ms(last_upload)
        logger.info("UpdateDexcomShare: last Nightscout upload (ms since epoch): %s" % last_upload_time_ms)

        readings = self.tconnect.dexcomshare.get_latest_glucose_values(
            minutes=1440, max_count=secret.DEXCOM_SHARE_MAX_COUNT)
        logger.info("UpdateDexcomShare: fetched %d readings from Dexcom Share" % len(readings))

        new_readings = []
        for reading in readings:
            ts_ms = parse_dexcom_date(reading["WT"])
            if last_upload_time_ms and ts_ms <= last_upload_time_ms:
                continue
            new_readings.append((ts_ms, reading))

        # Oldest first, so a partial failure part-way through still leaves
        # last_uploaded_bg_entry advancing monotonically on the next cycle.
        new_readings.sort(key=lambda pair: pair[0])

        count = 0
        for ts_ms, reading in new_readings:
            entry = NightscoutEntry.entry(
                sgv=reading["Value"],
                created_at=arrow.get(ts_ms / 1000.0),
                device=DEXCOM_SHARE_ENTERED_BY,
                direction=TREND_TO_DIRECTION.get(reading.get("Trend")),
            )
            if pretend:
                logger.info("Would upload to Nightscout: %s" % entry)
            else:
                logger.info("Uploading to Nightscout: %s" % entry)
                self.nightscout.upload_entry(entry, entity="entries")
            count += 1

        logger.info("UpdateDexcomShare: uploaded %d new readings" % count)
        return count > 0

    @staticmethod
    def _last_upload_time_ms(last_upload):
        if not last_upload:
            return None
        if "date" in last_upload and last_upload["date"] is not None:
            return int(last_upload["date"])
        if "dateString" in last_upload and last_upload["dateString"]:
            return int(arrow.get(last_upload["dateString"]).float_timestamp * 1000)
        return None
