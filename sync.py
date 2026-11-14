#!/usr/bin/env python3
"""Devario scheduler to CoolerControl Mode synchronization."""
import json
import logging
from pathlib import Path
import re
import signal
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

ROOT = Path(__file__).resolve().parent
DEFAULT_API_URL = 'http://127.0.0.1:11987'
LOG = logging.getLogger('devario_scheduler_sync')
STOP = False
LOOP = None
SYSTEMD_NAME = 'org.freedesktop.systemd1'
SCX_UNIT_PATH = '/org/freedesktop/systemd1/unit/scx_2eservice'
PROPERTIES_IFACE = 'org.freedesktop.DBus.Properties'
DEFAULT_STABILIZATION_MS = 750
SCHEDULER_ID = re.compile(r'^(?:default|scx_[a-zA-Z0-9_+.-]+)$')

def stop(*_):
    global STOP
    STOP = True
    if LOOP is not None:
        GLib.idle_add(LOOP.quit)

def command(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=6, check=True).stdout

def get_scheduler():
    fields = dict(line.split(':', 1) for line in command('scxcli', 'get').splitlines() if ':' in line)
    state = fields.get('state', '').strip()
    running = fields.get('running', '').strip()
    if running and running != 'nothing' and SCHEDULER_ID.fullmatch(running):
        return running
    if state.startswith('default') and running == 'nothing':
        return 'default'
    # During a switch, 'configured' can already name the next scheduler while
    # the kernel still has the previous one attached. Never act on that value.
    return None

def discover_schedulers():
    result = []
    for line in command('scxcli', 'list').splitlines():
        m = re.match(r'^\s*([* -])\s+(default|scx_[\w+.-]+)\s{2,}(\S.*?)\s{2,}(.*)$', line)
        if not m:
            continue
        marker, sid, name, _description = m.groups()
        if not SCHEDULER_ID.fullmatch(sid):
            continue
        result.append({'id': sid, 'name': name.strip(), 'installed': marker != '-'})
    if not result:
        raise ValueError('No schedulers parsed from scxcli list; keeping previous catalog')
    return result

def load_config():
    config = json.loads((ROOT / 'config.json').read_text())
    mapping = config.get('scheduler_modes', {})
    if not isinstance(mapping, dict):
        raise ValueError('scheduler_modes must be an object')
    interval = max(1, min(60, int(config.get('poll_interval', 2))))
    return config, interval, mapping

def publish_catalog(catalog):
    # The plugin UI only has CoolerControl's config API; expose discovered
    # schedulers through config.json. Re-read before updating to reduce races.
    path = ROOT / 'config.json'
    config = json.loads(path.read_text())
    if config.get('discovered_schedulers') == catalog:
        return
    config['discovered_schedulers'] = catalog
    temp = ROOT / '.config.json.tmp'
    try:
        temp.write_text(json.dumps(config, indent=2) + '\n')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    LOG.info('Discovered %d schedulers (%d installed)', len(catalog), sum(s['installed'] for s in catalog))

def api_url_from_config(config):
    value = config.get('api_url', DEFAULT_API_URL)
    if not isinstance(value, str):
        raise ValueError('api_url must be a URL string')
    value = value.strip().rstrip('/')
    parts = urlsplit(value)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError('api_url must be an http(s) URL without credentials or query')
    try:
        _ = parts.port
    except ValueError as exc:
        raise ValueError('Invalid port in api_url') from exc
    return value


def request(path, token, api_url, method='GET'):
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    data = None
    if method == 'POST':
        data = b'{}'
        headers['Content-Type'] = 'application/json'
    req = Request(api_url + path, headers=headers, data=data, method=method)
    with urlopen(req, timeout=5) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}

class SchedulerMonitor:
    """React to systemd unit changes; never poll scxcli for scheduler changes."""

    def __init__(self):
        self.bus = None
        self.receiver = None
        self.loop = GLib.MainLoop()
        self.sync_source = None
        self.retry_source = None
        self.last_success = None
        self.last_observed = None
        self.last_api_url = None
        self.last_settings = None
        self.transition_logged = False
        self.default_since = None

    def connect(self):
        try:
            bus = dbus.SystemBus()
            # add_signal_receiver subscribes to ordinary signals. Do not use
            # busctl monitor, which requires privileged eavesdropping access.
            receiver = bus.add_signal_receiver(
                self.on_properties_changed,
                signal_name='PropertiesChanged',
                dbus_interface=PROPERTIES_IFACE,
                path=SCX_UNIT_PATH,
                bus_name=SYSTEMD_NAME,
            )
            # Request systemd change notifications if supported. Some systems
            # emit PropertiesChanged without this; failure is non-fatal.
            try:
                manager = dbus.Interface(
                    bus.get_object(SYSTEMD_NAME, '/org/freedesktop/systemd1'),
                    'org.freedesktop.systemd1.Manager',
                )
                manager.Subscribe()
            except dbus.DBusException as exc:
                LOG.debug('systemd Subscribe unavailable: %s', exc)
            self.bus = bus
            self.receiver = receiver
            LOG.info('Subscribed to scx.service D-Bus property changes')
            self.queue_sync(0)
        except dbus.DBusException as exc:
            LOG.warning('D-Bus connection unavailable (retrying): %s', exc)
            self.disconnect()

    def disconnect(self):
        if self.receiver is not None:
            try:
                self.receiver.remove()
            except Exception:
                pass
        self.receiver = None
        self.bus = None

    def on_properties_changed(self, interface, changed, invalidated):
        if interface not in ('org.freedesktop.systemd1.Unit',
                             'org.freedesktop.systemd1.Service'):
            return
        if any(key in changed or key in invalidated
               for key in ('ActiveState', 'SubState', 'MainPID', 'ExecMainPID')):
            # Service restarts emit several notifications. Coalesce into one
            # scxcli read after the state has settled.
            self.queue_sync(350)

    def queue_sync(self, delay_ms=350):
        if self.sync_source is not None:
            GLib.source_remove(self.sync_source)
        self.sync_source = GLib.timeout_add(delay_ms, self.run_sync)

    def schedule_retry(self, seconds=5):
        if self.retry_source is not None:
            GLib.source_remove(self.retry_source)
        self.retry_source = GLib.timeout_add_seconds(seconds, self.retry)

    def retry(self):
        self.retry_source = None
        self.queue_sync(0)
        return GLib.SOURCE_REMOVE

    def run_sync(self):
        self.sync_source = None
        if STOP:
            return GLib.SOURCE_REMOVE
        try:
            config, _interval, mapping = load_config()
            api_url = api_url_from_config(config)
            settings = (bool(config.get('enabled', True)),
                        tuple(sorted(mapping.items())), api_url)
            if settings != self.last_settings:
                self.last_success = None
                self.last_settings = settings
            if not config.get('enabled', True):
                self.last_observed = None
                self.default_since = None
                return GLib.SOURCE_REMOVE
            active = get_scheduler()
            # A direct scx -> scx switch briefly passes through the kernel's
            # default scheduler. Only apply its mapping if it stays active.
            if active == 'default':
                now = time.monotonic()
                if self.default_since is None:
                    self.default_since = now
                remaining = DEFAULT_STABILIZATION_MS - int(
                    (now - self.default_since) * 1000
                )
                if remaining > 0:
                    # A single fixed deadline: duplicate systemd signals must
                    # not extend the delay indefinitely.
                    self.queue_sync(remaining)
                    return GLib.SOURCE_REMOVE
            else:
                self.default_since = None
            if active is None:
                if not self.transition_logged:
                    LOG.info('Scheduler transition detected; waiting for stable state')
                    self.transition_logged = True
                self.schedule_retry(2)
                return GLib.SOURCE_REMOVE
            self.transition_logged = False
            if active != self.last_observed:
                LOG.info('Active scheduler: %s', active)
                self.last_observed = active
            target = mapping.get(active)
            state = (active, target, api_url)
            if state == self.last_success:
                return GLib.SOURCE_REMOVE
            if target:
                if not isinstance(target, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', target):
                    raise ValueError('Invalid Mode UID for ' + active)
                token = (ROOT / 'api_token').read_text().strip()
                if not token:
                    raise ValueError('API token is empty')
                current = request('/modes-active', token, api_url).get('current_mode_uid')
                if current != target:
                    request('/modes-active/' + target, token, api_url, 'POST')
                    LOG.info('Scheduler %s: activated CoolerControl mode %s', active, target)
                else:
                    LOG.info('Scheduler %s: target mode already active', active)
            else:
                LOG.info('Scheduler %s: no mapping; leaving cooling unchanged', active)
            self.last_success = state
        except (OSError, ValueError, TypeError, subprocess.SubprocessError,
                HTTPError, URLError) as exc:
            LOG.error('Sync failed (will retry): %s', exc)
            self.last_success = None
            self.schedule_retry(5)
        return GLib.SOURCE_REMOVE

    def refresh_settings(self):
        if STOP:
            return GLib.SOURCE_REMOVE
        try:
            config, _interval, mapping = load_config()
            settings = (bool(config.get('enabled', True)),
                        tuple(sorted(mapping.items())), api_url_from_config(config))
            if settings != self.last_settings:
                self.queue_sync(0)
        except (OSError, ValueError, TypeError) as exc:
            LOG.warning('Configuration check failed: %s', exc)
        return GLib.SOURCE_CONTINUE

    def refresh_catalog(self):
        if STOP:
            return GLib.SOURCE_REMOVE
        try:
            publish_catalog(discover_schedulers())
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            LOG.warning('Scheduler discovery unavailable: %s', exc)
        return GLib.SOURCE_CONTINUE

    def check_bus(self):
        if STOP:
            return GLib.SOURCE_REMOVE
        if self.bus is None:
            self.connect()
        else:
            try:
                self.bus.get_name_owner(SYSTEMD_NAME)
            except dbus.DBusException as exc:
                LOG.warning('Lost systemd D-Bus connection: %s', exc)
                self.disconnect()
                self.connect()
        return GLib.SOURCE_CONTINUE

    def run(self):
        global LOOP
        LOOP = self.loop
        self.connect()
        self.refresh_catalog()
        GLib.timeout_add_seconds(2, self.refresh_settings)
        GLib.timeout_add_seconds(30, self.refresh_catalog)
        GLib.timeout_add_seconds(10, self.check_bus)
        try:
            self.loop.run()
        finally:
            self.disconnect()
            LOG.info('Stopped')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    DBusGMainLoop(set_as_default=True)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    SchedulerMonitor().run()


if __name__ == '__main__':
    main()
