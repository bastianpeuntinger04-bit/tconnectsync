import datetime
import requests
import hashlib
import time
import urllib.parse
import arrow
import logging

from urllib.parse import urljoin
from typing import Optional, Tuple, Union

from .api.common import ApiException, DEFAULT_REQUEST_TIMEOUT_SECONDS
from .parser.nightscout import ENTERED_BY

# Anything arrow.get() accepts for the date filters / timestamps passed around
# in this module (ISO strings, datetimes, or already-parsed Arrow objects).
DateLike = Union[str, datetime.datetime, arrow.Arrow]

def format_datetime(date: DateLike) -> str:
	return arrow.get(date).isoformat()

def _ms_timestamp() -> str:
	"""Millisecond-precision local time, independent of the global logging
	format's %(asctime)s (which this project configures without msecs) --
	needed to see exactly how tightly spaced a burst of Nightscout requests
	within one sync cycle actually is."""
	return datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]

def time_range(field_name: str, start_time: Optional[DateLike], end_time: Optional[DateLike]) -> str:
	def fmt(date: DateLike) -> str:
		ret = format_datetime(date)
		# URL-encode so the '+' in offsets like '+02:00' is not decoded
		# to a space by the server, which would mangle the ISO-8601 value.
		# Upstream instead retries with 'T' replaced by a space (t_to_space);
		# encoding the value fixes the cause, so that fallback is not carried.
		return urllib.parse.quote(ret, safe='')
	arg = ''
	if start_time:
		arg += '&find[%s][$gte]=%s' % (field_name, fmt(start_time))
	if end_time:
		arg += '&find[%s][$lte]=%s' % (field_name, fmt(end_time))
	return arg


# Render's free tier sleeps a service after ~15 min idle; the first request
# after that wakes it, and Render's own proxy -- not Nightscout -- answers
# with one of these as an HTML page while the app boots (typically 20-50s).
# 429 is a different failure entirely (Nightscout's own rate limiting, seen
# as text/plain "Too Many Requests" rather than Render's HTML error pages --
# most likely triggered by a burst of individual upload_entry() POSTs, one
# per new treatment, with no pacing between them) but the same retry-in-place
# approach applies, preferring the server's own Retry-After when it sends one.
RETRYABLE_STATUS_CODES = (429, 502, 503, 504)
# Total ~37s of backoff across up to 4 retries (5 attempts total), chosen to
# cover a typical Render free-tier cold start within a single sync cycle
# rather than losing already-fetched Tandem data to the outer autoupdate
# poll-cycle retry, which would also force a fresh Tandem login. Used as-is
# for 502/503/504, and as the fallback for 429 when no Retry-After is sent.
COLD_START_RETRY_DELAYS_SECONDS = (2, 5, 10, 20)
# Upper bound on a server-supplied Retry-After, so a misconfigured or
# malicious value can't stall a sync cycle for an unreasonable time.
MAX_RETRY_AFTER_SECONDS = 300.0


def _parse_retry_after(value: str) -> Optional[float]:
	"""Parses a Retry-After header value: either an integer number of
	seconds, or an HTTP-date (RFC 7231 7.1.3). Returns None if unparseable."""
	value = value.strip()
	if value.isdigit():
		return float(value)
	try:
		from email.utils import parsedate_to_datetime
		dt = parsedate_to_datetime(value)
	except (TypeError, ValueError):
		return None
	if dt is None:
		return None
	now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.utcnow()
	return max((dt - now).total_seconds(), 0.0)

logger = logging.getLogger(__name__)
class NightscoutApi:
	def __init__(self, url: str, secret: str, skip_verify: bool = False, ignore_conn_errors: bool = False) -> None:
		self.url = url
		self.secret = secret
		self.verify = False if skip_verify else None
		self.ignore_conn_errors = ignore_conn_errors
		# Total HTTP attempts sent by this instance since it was created
		# (effectively since process start, since one instance is built in
		# main() and reused for the whole --auto-update lifetime). Every
		# retry attempt counts separately, since each is a real request on
		# the wire and each one counts against Nightscout's rate limit.
		self._request_count = 0
		# Epoch seconds until which a prior 429's Retry-After told us
		# Nightscout is rate-limited. A single sync cycle makes ~12+
		# sequential Nightscout calls (see the per-processor dedup GETs);
		# without this, each one that lands inside an active rate-limit
		# window would independently hit 429 and wait out its own
		# Retry-After, potentially compounding to many times the actual
		# window length. Checked once up front instead.
		self._rate_limited_until: Optional[float] = None

	def _log_request(self, method: str, url: str) -> None:
		self._request_count += 1
		logger.info("NIGHTSCOUT REQUEST #%d @ %s: %s %s" % (
			self._request_count, _ms_timestamp(), method, url))

	@staticmethod
	def _log_response(url: str, r: requests.Response) -> None:
		logger.info("NIGHTSCOUT RESPONSE @ %s: url=%s status=%s content-type=%s" % (
			_ms_timestamp(), url, r.status_code, r.headers.get('Content-Type')))

	def _wait_if_already_rate_limited(self) -> None:
		if self._rate_limited_until is None:
			return
		remaining = self._rate_limited_until - time.time()
		if remaining > 0:
			logger.warning(
				"Nightscout is still within a previously-seen rate-limit window; "
				"waiting %.0fs before this request instead of hitting another 429" % remaining)
			time.sleep(remaining)
		self._rate_limited_until = None

	def _request(self, method: str, url: str, **kwargs) -> requests.Response:
		"""Issues one HTTP request, retrying with backoff on 429/502/503/504
		(see RETRYABLE_STATUS_CODES above). Logs every attempt, not just the
		final one, so a retry sequence is visible in the logs as handled
		rather than a bare error."""
		self._wait_if_already_rate_limited()

		kwargs.setdefault('timeout', DEFAULT_REQUEST_TIMEOUT_SECONDS)
		attempts = len(COLD_START_RETRY_DELAYS_SECONDS) + 1
		r: requests.Response
		for attempt in range(attempts):
			self._log_request(method, url)
			r = getattr(requests, method.lower())(url, **kwargs)
			self._log_response(url, r)

			if r.status_code not in RETRYABLE_STATUS_CODES:
				return r

			if attempt < attempts - 1:
				delay, delay_source = self._retry_delay(r, attempt)
				if r.status_code == 429:
					self._rate_limited_until = time.time() + delay
				logger.warning(
					"Nightscout %s %s returned HTTP %d (attempt %d/%d); retrying in %.0fs (%s)" % (
						method, url, r.status_code, attempt + 1, attempts, delay, delay_source))
				time.sleep(delay)
		return r

	@staticmethod
	def _retry_delay(r: requests.Response, attempt: int) -> Tuple[float, str]:
		"""Picks the retry delay for a retryable response: the server's own
		Retry-After when present and parseable (authoritative -- it knows its
		own rate-limit window, we don't), capped at MAX_RETRY_AFTER_SECONDS;
		otherwise the fixed cold-start backoff schedule."""
		retry_after = r.headers.get('Retry-After')
		if retry_after:
			parsed = _parse_retry_after(retry_after)
			if parsed is not None:
				capped = min(parsed, MAX_RETRY_AFTER_SECONDS)
				return capped, "server-supplied Retry-After%s" % (
					" capped at %.0fs" % MAX_RETRY_AFTER_SECONDS if parsed > MAX_RETRY_AFTER_SECONDS else "")
		return COLD_START_RETRY_DELAYS_SECONDS[attempt], "likely a Render free-tier cold start" if r.status_code != 429 else "no Retry-After header sent"

	def upload_entry(self, ns_format: dict, entity: str = 'treatments') -> None:
		url = urljoin(self.url, 'api/v1/' + entity + '?api_secret=' + self.secret)
		r = self._request('POST', url, json=ns_format, headers={
			'Accept': 'application/json',
			'Content-Type': 'application/json',
			'api-secret': hashlib.sha1(self.secret.encode()).hexdigest()
		}, verify=self.verify)
		if r.status_code != 200:
			raise ApiException(r.status_code, "Nightscout upload %s response: %s" % (r.status_code, r.text))

	def delete_entry(self, entity: str) -> None:
		url = urljoin(self.url, 'api/v1/' + entity + '?api_secret=' + self.secret)
		r = self._request('DELETE', url, json={}, headers={
			'Accept': 'application/json',
			'Content-Type': 'application/json',
			'api-secret': hashlib.sha1(self.secret.encode()).hexdigest()
		}, verify=self.verify)
		if r.status_code != 200:
			raise ApiException(r.status_code, "Nightscout delete %s response: %s" % (r.status_code, r.text))

	def put_entry(self, ns_format: dict, entity: str) -> None:
		url = urljoin(self.url, 'api/v1/' + entity + '?api_secret=' + self.secret)
		r = self._request('PUT', url, json=ns_format, headers={
			'Accept': 'application/json',
			'Content-Type': 'application/json',
			'api-secret': hashlib.sha1(self.secret.encode()).hexdigest()
		}, verify=self.verify)
		if r.status_code != 200:
			raise ApiException(r.status_code, "Nightscout put %s response: %s" % (r.status_code, r.text))

	def last_uploaded_entry(self, eventType: str, time_start: Optional[DateLike] = None, time_end: Optional[DateLike] = None) -> Optional[dict]:
		dateFilter = time_range('created_at', time_start, time_end)
		url = urljoin(self.url, 'api/v1/treatments?count=1&find[enteredBy]=' + urllib.parse.quote(ENTERED_BY) + '&find[eventType]=' + urllib.parse.quote(eventType) + dateFilter + '&ts=' + str(time.time()))
		try:
			latest = self._request('GET', url, headers={
				'api-secret': hashlib.sha1(self.secret.encode()).hexdigest()
			}, verify=self.verify)
			if latest.status_code != 200:
				raise ApiException(latest.status_code, "Nightscout last_uploaded_entry %s response: %s" % (latest.status_code, latest.text))

			j = latest.json()
			if j and len(j) > 0:
				return j[0]
			return None
		except requests.exceptions.ConnectionError as e:
			if self.ignore_conn_errors:
				logger.warn('Ignoring ConnectionError because ignore_conn_errors=true', e)
				return None
			else:
				raise e

	def last_uploaded_bg_entry(self, time_start: Optional[DateLike] = None, time_end: Optional[DateLike] = None, device: str = ENTERED_BY) -> Optional[dict]:
		dateFilter = time_range('dateString', time_start, time_end)
		url = urljoin(self.url, 'api/v1/entries.json?count=1&find[device]=' + urllib.parse.quote(device) + dateFilter + '&ts=' + str(time.time()))
		try:
			latest = self._request('GET', url, headers={
				'api-secret': hashlib.sha1(self.secret.encode()).hexdigest()
			}, verify=self.verify)
			if latest.status_code != 200:
				raise ApiException(latest.status_code, "Nightscout last_uploaded_bg_entry %s response: %s" % (latest.status_code, latest.text))

			j = latest.json()
			if j and len(j) > 0:
				return j[0]
			return None
		except requests.exceptions.ConnectionError as e:
			if self.ignore_conn_errors:
				logger.warn('Ignoring ConnectionError because ignore_conn_errors=true', e)
				return None
			else:
				raise e

	def last_uploaded_activity(self, activityType: str, time_start: Optional[DateLike] = None, time_end: Optional[DateLike] = None) -> Optional[dict]:
		dateFilter = time_range('created_at', time_start, time_end)
		url = urljoin(self.url, 'api/v1/activity?find[enteredBy]=' + urllib.parse.quote(ENTERED_BY) + '&find[activityType]=' + urllib.parse.quote(activityType) + dateFilter + '&ts=' + str(time.time()))
		try:
			latest = self._request('GET', url, headers={
				'api-secret': hashlib.sha1(self.secret.encode()).hexdigest()
			}, verify=self.verify)
			if latest.status_code != 200:
				raise ApiException(latest.status_code, "Nightscout activity %s response: %s" % (latest.status_code, latest.text))

			j = latest.json()
			if j and len(j) > 0:
				return j[0]
			return None
		except requests.exceptions.ConnectionError as e:
			if self.ignore_conn_errors:
				logger.warn('Ignoring ConnectionError because ignore_conn_errors=true', e)
				return None
			else:
				raise e

	def last_uploaded_devicestatus(self, time_start: Optional[DateLike] = None, time_end: Optional[DateLike] = None) -> Optional[dict]:
		dateFilter = time_range('created_at', time_start, time_end)
		url = urljoin(self.url, 'api/v1/devicestatus?find[device]=' + urllib.parse.quote(ENTERED_BY) + dateFilter + '&ts=' + str(time.time()))
		try:
			latest = self._request('GET', url, headers={
				'api-secret': hashlib.sha1(self.secret.encode()).hexdigest()
			}, verify=self.verify)
			if latest.status_code != 200:
				raise ApiException(latest.status_code, "Nightscout devicestatus %s response: %s" % (latest.status_code, latest.text))

			j = latest.json()
			if j and len(j) > 0:
				return j[0]
			return None
		except requests.exceptions.ConnectionError as e:
			if self.ignore_conn_errors:
				logger.warn('Ignoring ConnectionError because ignore_conn_errors=true', e)
				return None
			else:
				raise e

	"""
	Returns general status information about the Nightscout server.
	"""
	def api_status(self):
		url = urljoin(self.url, 'api/v1/status.json')
		status = self._request('GET', url, headers={
			'api-secret': hashlib.sha1(self.secret.encode()).hexdigest()
		}, verify=self.verify)
		if status.status_code != 200:
			raise Exception('HTTP error status code (%d) from Nightscout: %s' % (status.status_code, status.text))
		return status.json()

	"""
	Returns information on the currently configured Nightscout profile data store
	(contains all profiles in Nightscout under one mongo object).
	"""
	def current_profile(self, time_start=None, time_end=None):
		url = urljoin(self.url, 'api/v1/profile/current?api_secret=' + self.secret)
		r = self._request('GET', url, json={}, headers={
			'Accept': 'application/json',
			'Content-Type': 'application/json',
			'api-secret': hashlib.sha1(self.secret.encode()).hexdigest()
		}, verify=self.verify)
		if r.status_code != 200:
			raise ApiException(r.status_code, "Nightscout current_profile %s response: %s" % (r.status_code, r.text))
		return r.json()