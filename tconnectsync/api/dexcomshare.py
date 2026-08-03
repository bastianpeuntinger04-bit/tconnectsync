import logging
import re

from typing import List, Optional, TypedDict

import requests

from .common import ApiException, ApiLoginException

logger = logging.getLogger(__name__)

# Dexcom Share application ids, keyed by region. Verified against the
# actively-maintained pydexcom client (github.com/gagebenne/pydexcom), the
# de facto reference implementation for this undocumented API: the same id
# is used for both US and OUS (incl. Europe/Germany) accounts, while Japan
# has always used a separate one. Using the wrong id does not produce a
# clear "wrong application" error -- Dexcom rejects it as an ordinary
# AccountPasswordInvalid, indistinguishable from a real bad password.
APPLICATION_ID_DEFAULT = "d89443d2-327c-4a6f-89e5-496bbb0317db"  # US, OUS
APPLICATION_ID_JP = "d8665ade-9673-4e27-9ff6-92db4ce13d13"  # Japan only

EMPTY_SESSION_ID = "00000000-0000-0000-0000-000000000000"

# The live API returns bare 'Date(<ms>[+-]<tz>)', with no leading/trailing
# slash (confirmed against a real account and pydexcom's own captured
# example, both slash-less) -- unlike the classic ASP.NET '/Date(...)/ '
# wrapper this was originally, incorrectly, modeled on. Slashes are matched
# optionally so either form parses.
_DATE_RE = re.compile(r"/?Date\((\d+)(?:[+-]\d+)?\)/?")


def parse_dexcom_date(value: str) -> int:
    """Parses a Dexcom Share WT/ST/DT field (e.g. 'Date(1462404576000)' or
    'Date(1462404576000-0700)') into a millisecond Unix timestamp."""
    m = _DATE_RE.match(value)
    if not m:
        raise ApiException(0, "Could not parse Dexcom Share date: %s" % value)
    return int(m.group(1))


class DexcomGlucoseReading(TypedDict):
    """One entry from ReadPublisherLatestGlucoseValues.

    WT/ST/DT are 'Date(<epoch_ms>[+-]tz)'-wrapped timestamps (Wall Time,
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

    Deliberately mirrors pydexcom (github.com/gagebenne/pydexcom) -- the
    actively-maintained reference implementation of this API -- in the
    details that are easy to get subtly wrong and hard to diagnose from the
    outside: a single persistent `requests.Session` for the whole login
    flow (Dexcom's infrastructure may key off cookie continuity between the
    two login calls) and the exact header set (a browser-spoofing
    User-Agent, present in this project's other API clients, is a
    plausible bot-detection signal that has no reason to be sent here).
    """

    _BASE_URLS = {
        "US": "https://share2.dexcom.com/ShareWebServices/Services",
        "OUS": "https://shareous1.dexcom.com/ShareWebServices/Services",
        "JP": "https://share.dexcom.jp/ShareWebServices/Services",
    }

    _HEADERS = {"Accept-Encoding": "application/json"}

    def __init__(self, username: str, password: str, region: str = "US") -> None:
        self.username = username
        self.password = password
        self.region = (region or "US").upper()
        if self.region not in self._BASE_URLS:
            raise ValueError("Invalid Dexcom Share region '%s'. Must be one of %s." % (region, sorted(self._BASE_URLS)))
        self.base_url = self._BASE_URLS[self.region]
        self.application_id = APPLICATION_ID_JP if self.region == "JP" else APPLICATION_ID_DEFAULT
        self.session_id: Optional[str] = None
        self._session = requests.Session()
        self.login()

    def _post(self, path: str, json: Optional[dict] = None, params: Optional[dict] = None) -> requests.Response:
        return self._session.post(self.base_url + path, headers=self._HEADERS, json=json, params=params)

    @staticmethod
    def _error_detail(r: requests.Response) -> str:
        """Dexcom Share returns structured {"Code": ..., "Message": ...}
        error bodies. Surface the Code prominently -- it's the single most
        useful diagnostic (e.g. AccountPasswordInvalid, SessionNotValid,
        SSO_AuthenticateMaxAttemptsExceeded) and easy to miss buried in a
        raw response body."""
        try:
            body = r.json()
            if isinstance(body, dict) and "Code" in body:
                return "%s: %s" % (body.get("Code"), body.get("Message"))
        except ValueError:
            pass
        return r.text

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
        r = self._post(
            "/General/AuthenticatePublisherAccount",
            json={
                "accountName": self.username,
                "password": self.password,
                "applicationId": self.application_id,
            },
        )
        if r.status_code != 200:
            raise ApiLoginException(r.status_code, "Dexcom Share AuthenticatePublisherAccount failed: %s" % self._error_detail(r))
        return r.json()

    def _login_by_account_id(self, account_id: str) -> str:
        r = self._post(
            "/General/LoginPublisherAccountById",
            json={
                "accountId": account_id,
                "password": self.password,
                "applicationId": self.application_id,
            },
        )
        if r.status_code != 200:
            raise ApiLoginException(r.status_code, "Dexcom Share LoginPublisherAccountById failed: %s" % self._error_detail(r))
        return r.json()

    def get_latest_glucose_values(self, minutes: int = 1440, max_count: int = 288) -> List[DexcomGlucoseReading]:
        """Fetches the most recent up-to-max_count glucose readings from the
        last `minutes` minutes, newest first (as returned by the API)."""
        if not self.session_id:
            self.login()

        def _request() -> requests.Response:
            return self._post(
                "/Publisher/ReadPublisherLatestGlucoseValues",
                params={
                    "sessionId": self.session_id,
                    "minutes": minutes,
                    "maxCount": max_count,
                },
            )

        r = _request()
        if r.status_code in (500, 401, 403):
            # Session tokens expire; re-login once and retry.
            logger.info("Dexcom Share session appears expired (HTTP %d), re-logging in" % r.status_code)
            self.login()
            r = _request()

        if r.status_code != 200:
            raise ApiException(r.status_code, "Dexcom Share ReadPublisherLatestGlucoseValues failed: %s" % self._error_detail(r))

        return r.json() or []
