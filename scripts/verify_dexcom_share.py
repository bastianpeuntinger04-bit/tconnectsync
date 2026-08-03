#!/usr/bin/env python3
"""
Stand-alone smoke test for tconnectsync.api.dexcomshare.DexcomShareApi
against the real Dexcom Share API.

Does not touch Nightscout or Tandem -- only logs in to Dexcom Share and
prints the most recent glucose reading, to confirm the client's login flow
and response parsing still match Dexcom's (undocumented, reverse-engineered)
API before relying on the CGM_DEXCOM_SHARE feature in production.

Credentials are read from DEXCOM_SHARE_USERNAME / DEXCOM_SHARE_PASSWORD /
DEXCOM_SHARE_REGION if set, otherwise prompted for interactively (password
via getpass, so it is never echoed or kept in shell history).

Usage:
    python3 scripts/verify_dexcom_share.py
    DEXCOM_SHARE_REGION=OUS python3 scripts/verify_dexcom_share.py
"""

import getpass
import logging
import os
import sys
from pathlib import Path

import arrow

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tconnectsync.api.dexcomshare import DexcomShareApi, parse_dexcom_date  # noqa: E402
from tconnectsync.api.common import ApiException  # noqa: E402


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")

    region = os.environ.get("DEXCOM_SHARE_REGION", "US")
    if not os.environ.get("DEXCOM_SHARE_USERNAME"):
        print("If you log in to Dexcom with a phone number, it must be in full")
        print("international format: '+', country code, number, no leading 0")
        print("(e.g. a German number 0151 23456789 -> +4915123456789).\n")
    username = os.environ.get("DEXCOM_SHARE_USERNAME") or input("Dexcom Share username: ")
    password = os.environ.get("DEXCOM_SHARE_PASSWORD") or getpass.getpass("Dexcom Share password (not echoed): ")

    print(f"\nLogging in to Dexcom Share ({region} region)...")
    try:
        api = DexcomShareApi(username, password, region)
    except ApiException as e:
        print(f"\nLOGIN FAILED: {e}")
        print("If the error code is AccountPasswordInvalid despite correct credentials, check:")
        print("  - phone number username in full international format (see above)")
        print("  - at least one follower configured under Share/Follow in the Dexcom app")
        print("  - DEXCOM_SHARE_REGION is correct for your account (OUS for Germany/Europe)")
        return 1

    print("Login succeeded.\n")

    print("Fetching latest glucose values...")
    try:
        readings = api.get_latest_glucose_values(minutes=60, max_count=12)
    except ApiException as e:
        print(f"\nFETCH FAILED: {e}")
        return 1

    if not readings:
        print("No readings returned in the last 60 minutes (client and login both work).")
        return 0

    print(f"Got {len(readings)} reading(s). Most recent:")
    latest = readings[0]
    ts_ms = parse_dexcom_date(latest["WT"])
    local_time = arrow.get(ts_ms / 1000.0).to("local")
    print(f"  Value:  {latest['Value']} mg/dL")
    print(f"  Trend:  {latest['Trend']}")
    print(f"  Time:   {local_time.format('YYYY-MM-DD HH:mm:ss ZZ')} ({local_time.humanize()}, {ts_ms} ms since epoch)")
    print("\nRaw first reading (for shape verification):")
    print(f"  {latest}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
