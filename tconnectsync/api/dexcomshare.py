import logging
import re

from typing import List, Optional, TypedDict

import requests

from .common import ApiException, ApiLoginException, base_headers

logger = logging.getLogger(__name__)

# Public application id used by third-party Dexcom Share integrations
# (e.g. nightscout-connect, xDrip+). Not a secret -- Dexcom Share
# authenticates the *account*, this only identifies the calling app.
APPLICATION_ID = "d8665ade-9673-4e27-9ff6-92db4ce13d13"

EMPTY_SESSION_ID = "00000000-0000-0000-0000-000000000000"

_DATE_RE = re.compile(r"/Date\((\d+)(?:[+-]\d+)?\)/")


def parse_dexcom_date(value: str) -> int:
    """Parses a Dexcom Share WT/ST/DT field (e.g. '/Date(1462404576000)/' or
    '/Date(1462404576000-0700)/') into a millisecond Unix timestamp."""
    m = _DATE_RE.match(value)
    if not m:
        raise ApiException(0, "Could not parse Dexcom Share date: %s" % value)
    return int(m.group(1))


class DexcomGlucoseReading(TypedDict):
    """One entry from ReadPublisherLatestGlucoseValues.

    WT/ST/DT are '/Date(<epoch_ms>[+-]tz)/'-wrapped timestamps (Wall Time,
    System Time, Display Time; WT is used here). Value is in mg/dL. Trend is
    one of Dexcom's named trend strings (e.g. 'Flat', 'FortyFiveUp',
    'DoubleDown', 'NotComputable', 'RateOutOfRange').
    """
    WT: str
    ST: str
    DT: str
    Value: int
    Trend: str


class DexcomShareApi:
    """Client for the undocumented Dexcom Share API (the same API used by the
    Dexcom Follow app and reused by most third-party Nightscout bridges).
    Not affiliated with or verified by Dexcom; endpoints may change without
    notice.
    """

    _US_BASE_URL = "https://share2.dexcom.com/ShareWebServices/Services"
    _OUS_BASE_URL = "https://shareous1.dexcom.com/ShareWebServices/Services"

    def __init__(self, username: str, password: str, region: str = "US") -> None:
        self.username = username
        self.password = password
        self.region = (region or "US").upper()
        self.base_url = self._US_BASE_URL if self.region == "US" else self._OUS_BASE_URL
        self.session_id: Optional[str] = None
        self.login()

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **base_headers(),
        }

    def login(self) -> str:
        logger.info("Logging in to Dexcom Share (%s region)..." % self.region)
        account_id = self._authenticate_account()
        session_id = self._login_by_account_id(account_id)

        if not session_id or session_id == EMPTY_SESSION_ID:
            raise ApiLoginException(0, "Dexcom Share login did not return a valid session id")

        self.session_id = session_id
        logger.info("Logged in to Dexcom Share successfully")
        return session_id

    def _authenticate_account(self) -> str:
        r = requests.post(
            self.base_url + "/General/AuthenticatePublisherAccount",
            json={
                "accountName": self.username,
                "password": self.password,
                "applicationId": APPLICATION_ID,
            },
            headers=self._headers(),
        )
        if r.status_code != 200:
            raise ApiLoginException(r.status_code, "Dexcom Share AuthenticatePublisherAccount failed: %s" % r.text)
        return r.json()

    def _login_by_account_id(self, account_id: str) -> str:
        r = requests.post(
            self.base_url + "/General/LoginPublisherAccountById",
            json={
                "accountId": account_id,
                "password": self.password,
                "applicationId": APPLICATION_ID,
            },
            headers=self._headers(),
        )
        if r.status_code != 200:
            raise ApiLoginException(r.status_code, "Dexcom Share LoginPublisherAccountById failed: %s" % r.text)
        return r.json()

    def get_latest_glucose_values(self, minutes: int = 1440, max_count: int = 288) -> List[DexcomGlucoseReading]:
        """Fetches the most recent up-to-max_count glucose readings from the
        last `minutes` minutes, newest first (as returned by the API)."""
        if not self.session_id:
            self.login()

        def _request() -> requests.Response:
            return requests.post(
                self.base_url + "/Publisher/ReadPublisherLatestGlucoseValues",
                params={
                    "sessionId": self.session_id,
                    "minutes": minutes,
                    "maxCount": max_count,
                },
                headers=self._headers(),
            )

        r = _request()
        if r.status_code in (500, 401, 403):
            # Session tokens expire; re-login once and retry.
            logger.info("Dexcom Share session appears expired (HTTP %d), re-logging in" % r.status_code)
            self.login()
            r = _request()

        if r.status_code != 200:
            raise ApiException(r.status_code, "Dexcom Share ReadPublisherLatestGlucoseValues failed: %s" % r.text)

        return r.json() or []
