#!/usr/bin/env python3
"""Devario scheduler to CoolerControl Mode synchronization, v0.2."""
import json
import logging
from pathlib import Path
import re
import signal
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
API = 'http://127.0.0.1:11987'
LOG = logging.getLogger('devario_scheduler_sync')
STOP = False
SCHEDULER_ID = re.compile(r'^(?:default|scx_[a-zA-Z0-9_+.-]+)$')

def stop(*_):
    global STOP
    STOP = True

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

def request(path, token, method='GET'):
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json'}
    data = None
    if method == 'POST':
        data = b'{}'
        headers['Content-Type'] = 'application/json'
    req = Request(API + path, headers=headers, data=data, method=method)
    with urlopen(req, timeout=5) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}

def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_success = None
    last_observed = None
    last_catalog_attempt = 0
    transition_logged = False
    while not STOP:
        interval = 2
        try:
            now = time.monotonic()
            if last_catalog_attempt == 0 or now - last_catalog_attempt >= 30:
                last_catalog_attempt = now
                try:
                    publish_catalog(discover_schedulers())
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    LOG.warning('Scheduler discovery unavailable: %s', exc)
            config, interval, mapping = load_config()
            if not config.get('enabled', True):
                last_success = None
                last_observed = None
            else:
                active = get_scheduler()
                if active is None:
                    if not transition_logged:
                        LOG.info('Scheduler transition detected; waiting for stable state')
                        transition_logged = True
                    # Keep the last successful state until a stable one appears.
                else:
                    transition_logged = False
                    target = mapping.get(active)
                    if active != last_observed:
                        LOG.info('Active scheduler: %s', active)
                        last_observed = active
                    state = (active, target)
                    if state != last_success:
                        if target:
                            if not isinstance(target, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', target):
                                raise ValueError('Invalid Mode UID for ' + active)
                            token = (ROOT / 'api_token').read_text().strip()
                            if not token:
                                raise ValueError('API token is empty')
                            current = request('/modes-active', token).get('current_mode_uid')
                            if current != target:
                                request('/modes-active/' + target, token, 'POST')
                                LOG.info('Scheduler %s: activated CoolerControl mode %s', active, target)
                            else:
                                LOG.info('Scheduler %s: target mode already active', active)
                        else:
                            LOG.info('Scheduler %s: no mapping; leaving cooling unchanged', active)
                        last_success = state
        except (OSError, ValueError, subprocess.SubprocessError, HTTPError, URLError) as exc:
            LOG.error('Sync failed (will retry): %s', exc)
            last_success = None
        for _ in range(interval * 10):
            if STOP:
                break
            time.sleep(0.1)
    LOG.info('Stopped')

if __name__ == '__main__':
    main()
