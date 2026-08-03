import logging

from .tandemsource import TandemSourceApi
from .dexcomshare import DexcomShareApi
from .. import secret

logger = logging.getLogger(__name__)

"""A wrapper for the Tandem Source API."""
class TConnectApi:
    email = None
    password = None

    def __init__(self, email, password, region=None):
        self.email = email
        self.password = password
        # A caller which does not pass a region (e.g. tconnectsync-heroku)
        # must get the configured TCONNECT_REGION, not a hardcoded US
        # default which would send EU accounts to the US endpoints (#152).
        self.region = region or secret.TCONNECT_REGION
        self._tandemsource = None
        self._dexcomshare = None

    @property
    def tandemsource(self):
        if self._tandemsource and not self._tandemsource.needs_relogin():
            return self._tandemsource

        logger.debug(f"Instantiating new TandemSourceApi for region {self.region}")

        self._tandemsource = TandemSourceApi(self.email, self.password, self.region)
        return self._tandemsource

    @property
    def dexcomshare(self):
        # Unrelated Dexcom account credentials, only used by the optional
        # CGM_DEXCOM_SHARE feature. Cached for the process lifetime like
        # tandemsource above -- get_latest_glucose_values() re-logs-in on its
        # own if the session has expired, so no relogin check is needed here.
        if self._dexcomshare:
            return self._dexcomshare

        logger.debug(f"Instantiating new DexcomShareApi for region {secret.DEXCOM_SHARE_REGION}")

        self._dexcomshare = DexcomShareApi(secret.DEXCOM_SHARE_USERNAME, secret.DEXCOM_SHARE_PASSWORD, secret.DEXCOM_SHARE_REGION)
        return self._dexcomshare
