"""Single-owner WebAuthn ceremonies with one-use, browser-bound challenges."""
from dataclasses import dataclass
import hmac
import json
import secrets
import sqlite3
import threading
import time

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from webauthn import (
    generate_registration_options, generate_authentication_options,
    verify_registration_response, verify_authentication_response, options_to_json,
)
from webauthn.helpers import base64url_to_bytes, parse_authentication_credential_json
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, ResidentKeyRequirement,
    UserVerificationRequirement, PublicKeyCredentialDescriptor,
)

from .db import connection

CHALLENGE_TTL = 300
MAX_CHALLENGES = 1024
MAX_PASSKEYS = 20
COOKIE = 'corpus_webauthn'


@dataclass
class Ceremony:
    kind: str
    challenge: bytes
    browser: str
    session: str
    expires: float
    name: str = ''


class Passkeys:
    def __init__(self, settings):
        self.settings = settings
        self.pending = {}
        self.lock = threading.Lock()

    def initialize(self):
        with connection(self.settings) as db:
            db.execute('INSERT OR IGNORE INTO passkey_account(id,user_handle) VALUES(1,?)', (secrets.token_bytes(32),))

    def status(self, request):
        enabled = bool(self.settings.passkey_origin and str(request.base_url).rstrip('/') == self.settings.passkey_origin)
        with connection(self.settings, True) as db:
            registered = bool(db.execute('SELECT 1 FROM passkeys WHERE rp_id=? LIMIT 1', (self.settings.passkey_rp_id,)).fetchone())
        return {'enabled':enabled, 'registered':registered if enabled else False}

    def require_origin(self, request):
        if not self.status(request)['enabled']:
            raise HTTPException(400, 'Open Corpus at its configured secure address to use passkeys')
        if request.headers.get('origin') != self.settings.passkey_origin:
            raise HTTPException(403, 'Passkey requests must come from the configured Corpus origin')

    def begin(self, request, kind, options, name=''):
        browser = request.cookies.get(COOKIE, '')
        if not 32 <= len(browser) <= 100:
            browser = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self.lock:
            self.pending = {key:value for key,value in self.pending.items() if value.expires > now}
            if len(self.pending) >= MAX_CHALLENGES:
                raise HTTPException(429, 'Too many pending passkey requests; try again shortly')
            ceremony_id = secrets.token_urlsafe(32)
            self.pending[ceremony_id] = Ceremony(
                kind, options.challenge, browser,
                request.cookies.get('corpus_session', '') if kind == 'registration' else '',
                now + CHALLENGE_TTL, name,
            )
        response = JSONResponse({'challenge_id':ceremony_id, 'options':json.loads(options_to_json(options))})
        response.set_cookie(COOKIE, browser, httponly=True, secure=self.settings.secure_cookie, samesite='strict', max_age=CHALLENGE_TTL)
        return response

    def consume(self, request, body, kind):
        ceremony_id = body.get('challenge_id')
        if not isinstance(ceremony_id, str) or len(ceremony_id) > 100:
            raise HTTPException(400, 'Invalid passkey request')
        with self.lock:
            ceremony = self.pending.pop(ceremony_id, None)
        if (not ceremony or ceremony.expires <= time.monotonic() or ceremony.kind != kind
                or not hmac.compare_digest(ceremony.browser.encode(), request.cookies.get(COOKIE, '').encode())
                or (kind == 'registration' and not hmac.compare_digest(ceremony.session.encode(), request.cookies.get('corpus_session', '').encode()))):
            raise HTTPException(400, 'Passkey request expired or belongs to another browser; try again')
        return ceremony

    def credential(self, body):
        credential = body.get('credential')
        try:
            if not isinstance(credential, dict) or len(json.dumps(credential)) > 100000:
                raise ValueError()
            client = json.loads(base64url_to_bytes(credential['response']['clientDataJSON']))
            # Do not accept ceremonies started inside a cross-origin iframe.
            if client.get('crossOrigin', False) is not False or client.get('topOrigin') is not None:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(400, 'Invalid passkey response')
        return credential

    def registration_options(self, request, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise HTTPException(400, 'Passkey name must contain 1–80 characters')
        with connection(self.settings, True) as db:
            existing = db.execute('SELECT credential_id FROM passkeys WHERE rp_id=?', (self.settings.passkey_rp_id,)).fetchall()
            user_handle = db.execute('SELECT user_handle FROM passkey_account WHERE id=1').fetchone()[0]
        if len(existing) >= MAX_PASSKEYS:
            raise HTTPException(400, 'Remove an unused passkey before adding another')
        options = generate_registration_options(
            rp_id=self.settings.passkey_rp_id, rp_name='Corpus',
            user_id=user_handle, user_name='Corpus owner', user_display_name='Corpus',
            exclude_credentials=[PublicKeyCredentialDescriptor(id=row['credential_id']) for row in existing],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        return self.begin(request, 'registration', options, name.strip())

    def register(self, request, body):
        ceremony = self.consume(request, body, 'registration')
        credential = self.credential(body)
        try:
            verified = verify_registration_response(
                credential=credential, expected_challenge=ceremony.challenge,
                expected_rp_id=self.settings.passkey_rp_id, expected_origin=self.settings.passkey_origin,
                require_user_verification=True,
            )
        except Exception:
            raise HTTPException(400, 'Passkey registration could not be verified; try again')
        transports = credential['response'].get('transports', [])
        allowed = {'internal','hybrid','usb','nfc','ble','smart-card'}
        transports = [value for value in transports if isinstance(value, str) and value in allowed] if isinstance(transports, list) else []
        with connection(self.settings) as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM passkeys WHERE rp_id=?', (self.settings.passkey_rp_id,)).fetchone()[0] >= MAX_PASSKEYS:
                raise HTTPException(400, 'Remove an unused passkey before adding another')
            try:
                db.execute(
                    'INSERT INTO passkeys(credential_id,public_key,sign_count,rp_id,name,transports,device_type,backed_up) VALUES(?,?,?,?,?,?,?,?)',
                    (verified.credential_id, verified.credential_public_key, verified.sign_count,
                     self.settings.passkey_rp_id, ceremony.name, json.dumps(transports),
                     verified.credential_device_type.value, int(verified.credential_backed_up)),
                )
            except sqlite3.IntegrityError:
                raise HTTPException(409, 'This passkey is already registered')
        return {'registered':True}

    def authentication_options(self, request):
        with connection(self.settings, True) as db:
            rows = db.execute('SELECT credential_id FROM passkeys WHERE rp_id=?', (self.settings.passkey_rp_id,)).fetchall()
        if not rows:
            raise HTTPException(400, 'Sign in with your password and add a passkey first')
        options = generate_authentication_options(
            rp_id=self.settings.passkey_rp_id,
            allow_credentials=[PublicKeyCredentialDescriptor(id=row['credential_id']) for row in rows],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return self.begin(request, 'authentication', options)

    def authenticate(self, request, body):
        ceremony = self.consume(request, body, 'authentication')
        credential = self.credential(body)
        try:
            parsed = parse_authentication_credential_json(credential)
            with connection(self.settings) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM passkeys WHERE credential_id=? AND rp_id=?', (parsed.raw_id, self.settings.passkey_rp_id)).fetchone()
                if not row:
                    raise ValueError('Unknown passkey')
                user_handle = db.execute('SELECT user_handle FROM passkey_account WHERE id=1').fetchone()[0]
                if parsed.response.user_handle is not None and not hmac.compare_digest(parsed.response.user_handle, user_handle):
                    raise ValueError('Wrong user handle')
                verified = verify_authentication_response(
                    credential=parsed, expected_challenge=ceremony.challenge,
                    expected_rp_id=self.settings.passkey_rp_id, expected_origin=self.settings.passkey_origin,
                    credential_public_key=row['public_key'], credential_current_sign_count=row['sign_count'],
                    require_user_verification=True,
                )
                db.execute("UPDATE passkeys SET sign_count=?,device_type=?,backed_up=?,last_used=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                           (verified.new_sign_count, verified.credential_device_type.value, int(verified.credential_backed_up), row['id']))
        except Exception:
            raise HTTPException(401, 'Passkey sign-in could not be verified; try again or use your password')


def install_routes(app, service, limits, check_password, issue_session):
    def ceremony_request(request):
        service.require_origin(request)
        ip = request.client.host if request.client else 'unknown'
        if not limits.allow('passkey:'+ip, 30, 300):
            raise HTTPException(429, 'Too many passkey requests; try again in five minutes')

    @app.get('/api/passkeys')
    def list_passkeys(request:Request):
        with connection(service.settings, True) as db:
            keys = [dict(row) for row in db.execute('SELECT id,name,created,last_used FROM passkeys WHERE rp_id=? ORDER BY id', (service.settings.passkey_rp_id,))]
        return {'passkeys':keys, **service.status(request)}

    @app.post('/api/passkeys/register/options')
    def registration_options(request:Request, body:dict):
        ceremony_request(request)
        check_password(request, body.get('password', ''))
        return service.registration_options(request, body.get('name', 'iCloud Keychain'))

    @app.post('/api/passkeys/register/verify')
    def register(request:Request, body:dict):
        ceremony_request(request)
        return service.register(request, body)

    @app.post('/api/passkeys/authenticate/options')
    def authentication_options(request:Request):
        ceremony_request(request)
        return service.authentication_options(request)

    @app.post('/api/passkeys/authenticate/verify')
    def authenticate(request:Request, body:dict):
        ceremony_request(request)
        service.authenticate(request, body)
        return issue_session()

    @app.post('/api/passkeys/{id}/remove')
    def remove(request:Request, id:int, body:dict):
        ceremony_request(request)
        check_password(request, body.get('password', ''))
        with connection(service.settings) as db:
            changed = db.execute('DELETE FROM passkeys WHERE id=? AND rp_id=?', (id, service.settings.passkey_rp_id)).rowcount
        if not changed:
            raise HTTPException(404, 'Passkey not found')
        return {'removed':True}
