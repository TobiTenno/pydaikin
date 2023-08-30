"""Pydaikin base appliance, represent a Daikin device."""
from collections import defaultdict
from datetime import datetime, timedelta
import logging
import socket
from typing import Dict, Optional
from urllib.parse import unquote

from aiohttp import ClientSession
from aiohttp.web_exceptions import HTTPForbidden
from retry import retry

from .discovery import get_name
from .models import base
from .power import ATTR_COOL, ATTR_HEAT, ATTR_TOTAL, TIME_TODAY, DaikinPowerMixin
from .values import ApplianceValues

_LOGGER = logging.getLogger(__name__)


class Appliance(DaikinPowerMixin):  # pylint: disable=too-many-public-methods
    """Daikin main appliance class."""

    base_url: str
    http_resources: Dict[str, Optional[base.DaikinResponse]]
    session: Optional[ClientSession]

    TRANSLATIONS = {}

    VALUES_TRANSLATION = {}

    VALUES_SUMMARY = []

    @classmethod
    def daikin_to_human(cls, dimension, value):
        """Return converted values from Daikin to Human."""
        return cls.TRANSLATIONS.get(dimension, {}).get(value, str(value))

    @classmethod
    def human_to_daikin(cls, dimension, value):
        """Return converted values from Human to Daikin."""
        translations_rev = {
            dim: {v: k for k, v in item.items()}
            for dim, item in cls.TRANSLATIONS.items()
        }
        return translations_rev.get(dimension, {}).get(value, value)

    @classmethod
    def daikin_values(cls, dimension):
        """Return sorted list of translated values."""
        return sorted(list(cls.TRANSLATIONS.get(dimension, {}).values()))

    @staticmethod
    def translate_mac(value):
        """Return translated MAC address."""
        return ':'.join(value[i : i + 2] for i in range(0, len(value), 2))

    @staticmethod
    def discover_ip(device_id):
        """Return translated name to ip address."""
        try:
            socket.inet_aton(device_id)
            device_ip = device_id  # id is an IP
        except socket.error:
            device_ip = None

        if device_ip is None:
            # id is a common name, try discovery
            device_name = get_name(device_id)
            if device_name is None:
                # try DNS
                try:
                    device_ip = socket.gethostbyname(device_id)
                except socket.gaierror as exc:
                    raise ValueError(f"no device found for {device_id}") from exc
            else:
                device_ip = device_name.ip_addr
        return device_id

    async def __new__(cls, *a, **kw):  # pylint: disable=invalid-overridden-method
        instance = super().__new__(cls)
        await instance.__init__(*a, **kw)
        return instance

    async def __init__(self, device_id, session: Optional[ClientSession] = None):
        """Init the pydaikin appliance, representing one Daikin device."""
        self.values = ApplianceValues()
        self.http_resources = {}

        self.session = session
        self._energy_consumption_history = defaultdict(list)
        if session:
            self.device_ip = device_id
        else:
            self.device_ip = self.discover_ip(device_id)

        self.base_url = f"http://{self.device_ip}"

    async def _get_resource(self, model: type[base.DaikinResponse], params: Optional[dict] = None):
        "Get a DaikinResponse model"
        if params is None:
            params = {}

        path = model.get_url()
        response_text = await self.get(path, params)

        outmodel = model(_response=response_text)  # type: ignore

        return outmodel

    @retry(tries=3, delay=1)
    async def get(self, path: str, params: Optional[dict] = None):
        """Make the http request."""
        if params is None:
            params = {}

        if self.session is None:
            session = ClientSession()
        else:
            session = self.session

        async with session as client_session:
            async with client_session.get(
                f'{self.base_url}/{path}', params=params
            ) as resp:
                if resp.status == 403:
                    raise HTTPForbidden
                assert resp.status == 200, f"Response code is {resp.status}"

                return await resp.text()

    async def connect(self):
        """Update status from resources."""

        resources = [
            resource
            for _, resource in self.http_resources.items()
            if resource.is_stale  # pylint: disable=using-constant-test
        ]

        _LOGGER.debug("Updating %s", resources)
        for resource in resources:
            try:
                self.values[resource.get_url()] = await self._get_resource(resource)
            except ValueError:
                self.values[resource.get_url()] = None

        self._register_energy_consumption_history()

    def show_values(self, only_summary=False):
        """Print values."""
        if only_summary:
            keys = self.VALUES_SUMMARY
        else:
            keys = sorted(self.values.keys())

        for key in keys:
            if key in self.values:
                (k, val) = self.represent(key)
                print(f"{k : >20}: {val}")

    def log_sensors(self, file):
        """Log sensors to a file."""
        data = [
            ('datetime', datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')),
            ('in_temp', self.inside_temperature),
        ]
        if self.support_outside_temperature:
            data.append(('out_temp', self.outside_temperature))
        if self.support_compressor_frequency:
            data.append(('cmp_freq', self.compressor_frequency))
        if self.support_energy_consumption:
            data.append(
                ('total_today', self.energy_consumption(ATTR_TOTAL, TIME_TODAY))
            )
            data.append(('cool_today', self.energy_consumption(ATTR_COOL, TIME_TODAY)))
            data.append(('heat_today', self.energy_consumption(ATTR_HEAT, TIME_TODAY)))
            data.append(('total_power', self.current_total_power_consumption))
            data.append(('cool_energy', self.last_hour_cool_energy_consumption))
            data.append(('heat_energy', self.last_hour_heat_energy_consumption))
        if file.tell() == 0:
            file.write(','.join(k for k, _ in data))
            file.write('\n')
        file.write(','.join(str(v) for _, v in data))
        file.write('\n')
        file.flush()

    def show_sensors(self):
        """Print sensors."""
        data = [
            datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            f'in_temp={int(self.inside_temperature)}°C',
        ]
        if self.support_outside_temperature:
            data.append(f'out_temp={int(self.outside_temperature)}°C')
        if self.support_compressor_frequency:
            data.append(f'cmp_freq={int(self.compressor_frequency)}Hz')
        if self.support_energy_consumption:
            data.append(
                f'total_today={self.energy_consumption(ATTR_TOTAL, TIME_TODAY):.01f}kWh'
            )
            data.append(
                f'cool_today={self.energy_consumption(ATTR_COOL, TIME_TODAY):.01f}kWh'
            )
            data.append(
                f'heat_today={self.energy_consumption(ATTR_HEAT, TIME_TODAY):.01f}kWh'
            )
            data.append(f'total_power={self.current_total_power_consumption:.02f}kW')
            data.append(f'cool_energy={self.last_hour_cool_energy_consumption:.01f}kW')
            data.append(f'heat_energy={self.last_hour_heat_energy_consumption:.01f}kW')
        print('  '.join(data))

    def represent(self, key):
        """Return translated value from key."""
        k = self.VALUES_TRANSLATION.get(key, key)

        # adapt the value
        val = self.values.get(key)

        if key == 'mode' and self.values['pow'] == '0':
            val = 'off'
        elif key == 'mac':
            val = self.translate_mac(val)
            val = unquote(self.values[key]).split(';')
        else:
            val = self.daikin_to_human(key, val)

        _LOGGER.log(logging.NOTSET, 'Represent: %s, %s, %s', key, k, val)
        return (k, val)

    def _parse_number(self, dimension) -> Optional[float]:
        """Parse float number."""
        try:
            return float(self.values.get(dimension))
        except (TypeError, ValueError):
            return None

    @property
    def mac(self) -> str:
        """Return device's MAC address."""
        return self.values.get('mac', self.device_ip)

    @property
    def support_away_mode(self) -> bool:
        """Return True if the device support away_mode."""
        return 'en_hol' in self.values

    @property
    def support_fan_rate(self) -> bool:
        """Return True if the device support setting fan_rate."""
        return 'f_rate' in self.values

    @property
    def support_swing_mode(self) -> bool:
        """Return True if the device support setting swing_mode."""
        return 'f_dir' in self.values

    @property
    def support_outside_temperature(self) -> bool:
        """Return True if the device is not an AirBase unit."""
        return self.outside_temperature is not None

    @property
    def support_humidity(self) -> bool:
        """Return True if the device has humidity sensor."""
        return False

    @property
    def support_advanced_modes(self) -> bool:
        """Return True if the device supports advanced modes."""
        return 'adv' in self.values

    @property
    def support_compressor_frequency(self) -> bool:
        """Return True if the device supports compressor frequency."""
        return 'cmpfreq' in self.values

    @property
    def support_energy_consumption(self) -> bool:
        """Return True if the device supports energy consumption monitoring."""
        return super().support_energy_consumption

    @property
    def outside_temperature(self) -> Optional[float]:
        """Return current outside temperature."""
        return self._parse_number('otemp')

    @property
    def inside_temperature(self) -> Optional[float]:
        """Return current inside temperature."""
        return self._parse_number('htemp')

    @property
    def target_temperature(self) -> Optional[float]:
        """Return current target temperature."""
        return self._parse_number('stemp')

    @property
    def compressor_frequency(self) -> Optional[float]:
        """Return current compressor frequency."""
        return self._parse_number('cmpfreq')

    @property
    def humidity(self) -> Optional[float]:
        """Return current humidity."""
        return self._parse_number('hhum')

    @property
    def target_humidity(self) -> Optional[float]:
        """Return target humidity."""
        return self._parse_number('shum')

    @property
    def current_total_power_consumption(self):
        """Return the current total (heating+cooling, all devices) power consumption in kW."""
        # We tolerate a 50% delay in consumption measure
        return self.current_power_consumption(
            mode=ATTR_TOTAL, exp_diff_time_margin_factor=0.5
        )

    @property
    def last_hour_cool_energy_consumption(self):
        """Return the last hour cool power consumption of a given mode in kW."""
        # We tolerate a 5 minutes delay in consumption measure
        return self.current_power_consumption(
            mode=ATTR_COOL,
            exp_diff_time_value=timedelta(minutes=60),
            exp_diff_time_margin_factor=timedelta(minutes=5),
        )

    @property
    def last_hour_heat_energy_consumption(self):
        """Return the last hour heat power consumption of a given mode in kW."""
        # We tolerate a 5 minutes margin in consumption measure
        return self.current_power_consumption(
            mode=ATTR_HEAT,
            exp_diff_time_value=timedelta(minutes=60),
            exp_diff_time_margin_factor=timedelta(minutes=5),
        )

    @property
    def today_cool_energy_consumption(self):
        """Return today's cooling energy consumption in kWh."""
        return self.energy_consumption(
            mode=ATTR_COOL,
            time=TIME_TODAY,
        )

    @property
    def today_heat_energy_consumption(self):
        """Return today's heating energy consumption in kWh."""
        return self.energy_consumption(
            mode=ATTR_HEAT,
            time=TIME_TODAY,
        )

    @property
    def today_total_energy_consumption(self):
        """Return today's total (all devices) energy consumption in kWh."""
        return self.energy_consumption(
            mode=ATTR_TOTAL,
            time=TIME_TODAY,
        )

    @property
    def today_energy_consumption(self):
        """Return today's energy consumption in kWh."""
        return (self.today_cool_energy_consumption or 0) + (
            self.today_heat_energy_consumption or 0
        )

    @property
    def fan_rate(self) -> list:
        """Return list of supported fan rates."""
        return list(map(str.title, self.TRANSLATIONS.get('f_rate', {}).values()))

    @property
    def swing_modes(self) -> list:
        """Return list of supported swing modes."""
        return list(map(str.title, self.TRANSLATIONS.get('f_dir', {}).values()))

    async def set(self, settings):
        """Set settings on Daikin device."""
        raise NotImplementedError

    async def set_holiday(self, mode):
        """Set holiday mode."""
        raise NotImplementedError

    async def set_advanced_mode(self, mode, value):
        """Enable or disable advanced modes."""
        raise NotImplementedError

    async def set_streamer(self, mode):
        """Enable or disable the streamer."""
        raise NotImplementedError

    @property
    def zones(self):
        """Return list of zones."""
        return

    async def set_zone(self, zone_id, key, value):
        """Set zone status."""
        raise NotImplementedError
