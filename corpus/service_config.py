"""Service keys and Claude review settings that the administrator manages in the app.

Values saved from the admin page override the server environment; clearing one
falls back to the environment again. They live in ``service-config.json`` in the
data folder (mode 0600), not in a database, so nightly backups never copy them.
Secrets are write-only: the API reports whether a key is set, never its value.
"""
import json
import os
import re
import tempfile
import threading
from pathlib import Path

from .config import EFFORTS

SECRETS = ('claude_oauth_token', 'anthropic_api_key', 's2_api_key', 'openalex_api_key')
OPTIONS = ('ai_backend', 'ai_model', 'ai_effort', 'ai_min_interval', 'ai_daily_limit')
KEYS = SECRETS + OPTIONS


def clean(key, value):
    """Validate one incoming value; None means "use the environment"."""
    if value is None or value == '':
        return None
    if key in SECRETS:
        if not isinstance(value, str) or not 8 <= len(value.strip()) <= 2000 or re.search(r'\s', value.strip()):
            raise ValueError('Enter the key as a single line without spaces')
        return value.strip()
    if key == 'ai_backend':
        if value not in ('auto', 'api', 'cli', 'off'):
            raise ValueError('Choose auto, api, cli or off')
        return value
    if key == 'ai_model':
        if not isinstance(value, str) or not re.fullmatch(r'claude-[a-z0-9][a-z0-9.-]{1,80}', value.strip()):
            raise ValueError('Enter a Claude model ID, such as claude-opus-5-5')
        return value.strip()
    if key == 'ai_effort':
        if value not in EFFORTS:
            raise ValueError('Choose low, medium, high, xhigh or max')
        return value
    if key == 'ai_min_interval':
        if type(value) not in (int, float) or not 0 <= value <= 86400:
            raise ValueError('Seconds between reviews must be 0–86,400')
        return float(value)
    if key == 'ai_daily_limit':
        if type(value) is not int or not 0 <= value <= 10000:
            raise ValueError('The daily limit must be a whole number from 0 to 10,000')
        return value
    raise ValueError('Unknown setting')


class ServiceConfig:
    def __init__(self, owner):
        self.path = Path(owner.data_dir) / 'service-config.json'
        self.environment = {key: getattr(owner, key) for key in KEYS}
        self.lock = threading.Lock()

    def stored(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        result = {}
        for key, value in (data.items() if isinstance(data, dict) else []):
            try:
                if key in KEYS and clean(key, value) is not None:
                    result[key] = clean(key, value)
            except ValueError:
                pass
        return result

    def values(self):
        return {**self.environment, **self.stored()}

    def save(self, changes):
        if not isinstance(changes, dict) or not changes or set(changes) - set(KEYS):
            raise ValueError('Choose settings to change')
        validated = {key: clean(key, value) for key, value in changes.items()}
        with self.lock:
            stored = self.stored()
            for key, value in validated.items():
                if value is None:
                    stored.pop(key, None)
                else:
                    stored[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.service-config-')
            try:
                with os.fdopen(fd, 'w') as output:
                    json.dump(stored, output)
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise

    def describe(self):
        stored = self.stored()
        result = {}
        for key in KEYS:
            source = 'admin' if key in stored else 'environment' if self.environment[key] not in ('', None) else None
            if key in SECRETS:
                result[key] = {'set': source is not None, 'source': source}
            else:
                result[key] = {'value': stored.get(key, self.environment[key]), 'source': source, 'default': self.environment[key]}
        return result
