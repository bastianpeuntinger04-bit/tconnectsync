import arrow
import logging

from ...api.tandemsource import naive_local_to_utc

logger = logging.getLogger(__name__)

class ChooseDevice:
    def __init__(self, secret, tconnect):
        self.secret = secret
        self.tconnect = tconnect

    def choose(self):
        tconnect = self.tconnect

        pumpEventMetadata = tconnect.tandemsource.get_pumper().get('pumps', [])

        if not pumpEventMetadata:
            raise NoDevicesFound('No pumps are present on your Tandem Source account')

        serialNumberToPump = {p['serialNumber']: p for p in pumpEventMetadata}
        pump_summary = ', '.join(
            f"{p.get('serialNumber')} [{p.get('modelName', 'unknown model')}] (last seen: {p.get('maxDateOfEvents')})"
            for p in pumpEventMetadata
        )
        logger.info(f'Found {len(serialNumberToPump)} pumps on account: {pump_summary}')

        tconnectDevice = None

        if self.secret.PUMP_SERIAL_NUMBER and str(self.secret.PUMP_SERIAL_NUMBER) != '11111111':
            if not str(self.secret.PUMP_SERIAL_NUMBER) in serialNumberToPump.keys():
                raise InvalidSerialNumber(f'Serial number {self.secret.PUMP_SERIAL_NUMBER} is not present on your account: choose one of {", ".join(serialNumberToPump.keys())}')

            tconnectDevice = serialNumberToPump[str(self.secret.PUMP_SERIAL_NUMBER)]

            # Warn if pump is stale (no events in >3 days)
            try:
                max_event_date = arrow.get(naive_local_to_utc(tconnectDevice["maxDateOfEvents"]))
                age_days = (arrow.utcnow() - max_event_date).days

                if age_days > 3:
                    logger.warning(
                        f"The selected pump (serial {tconnectDevice['serialNumber']}) has no events in the last {age_days} days "
                        f"(last seen: {tconnectDevice['maxDateOfEvents']}). "
                        "You may have switched to a new pump. Consider removing or updating PUMP_SERIAL_NUMBER in your config."
                    )
            except Exception as e:
                logger.debug(f"Could not parse maxDateOfEvents to check for staleness: {e}")

            logger.info(f'Using pump with serial: {tconnectDevice["serialNumber"]} [{tconnectDevice.get("modelName", "unknown model")}] (deviceId: {tconnectDevice["assignmentId"]}, last seen: {tconnectDevice["maxDateOfEvents"]}) -- selected because PUMP_SERIAL_NUMBER is explicitly configured')
        else:
            # The BFF device list includes pumps that have never uploaded
            # (maxDateOfEvents is None); skip those when picking the most
            # recent one, and only fall back to one of them if nothing else.
            maxDateSeen = None
            for pump in pumpEventMetadata:
                if not pump.get('maxDateOfEvents'):
                    continue
                pumpMaxDate = arrow.get(naive_local_to_utc(pump['maxDateOfEvents']))
                if not tconnectDevice or pumpMaxDate > maxDateSeen:
                    maxDateSeen = pumpMaxDate
                    tconnectDevice = pump

            # If no pump has any events yet, fall back to the first one.
            if not tconnectDevice:
                tconnectDevice = pumpEventMetadata[0]

            logger.info(f'Using most recent pump (serial: {tconnectDevice["serialNumber"]} [{tconnectDevice.get("modelName", "unknown model")}], deviceId: {tconnectDevice["assignmentId"]}, last seen: {tconnectDevice["maxDateOfEvents"]}) -- selected because PUMP_SERIAL_NUMBER is not set (auto-select by most recent activity)')


        return tconnectDevice



class InvalidSerialNumber(RuntimeError):
    def __str__(self):
        return "%s: %s" % (self.__class__.__name__, super().__str__())


class NoDevicesFound(RuntimeError):
    def __str__(self):
        return "%s: %s" % (self.__class__.__name__, super().__str__())