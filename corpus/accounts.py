"""Invite-only accounts and request-scoped, physically separate libraries.

The account registry is outside every library's SQL console. A request's library
is selected solely from its server-side session (or an authentication ceremony),
never from a library path or an account ID supplied by a client.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from email.message import EmailMessage
from pathlib import Path
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
import tempfile
import urllib.request
import urllib.error
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .db import connection, initialize

INVITE_TTL = 72 * 3600


def email_address(value):
    if not isinstance(value, str) or len(value) > 254:
        raise ValueError('Enter a valid email address')
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", value):
        raise ValueError('Enter a valid email address')
    return value


def validate_password(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 1000:
        raise ValueError('Use a password with 12–1,000 characters')
    return value


class LibrarySettings:
    """ContextVars propagate into FastAPI's thread pool and asyncio.to_thread."""
    def __init__(self, owner):
        self.current = ContextVar('corpus_library', default=owner)

    def __getattr__(self, key):
        return getattr(self.current.get(), key)


class Accounts:
    def __init__(self, owner):
        self.owner = owner
        self.settings = LibrarySettings(owner)
        self.user_id = ContextVar('corpus_user', default='owner')
        self.sessions = {}
        self.session_lock = threading.Lock()
        self.invite_lock = threading.Lock()
        self.libraries = {'owner': owner}

    @contextmanager
    def registry(self):
        db = sqlite3.connect(self.owner.data_dir / 'accounts.sqlite3', timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        path = self.owner.data_dir / 'accounts.sqlite3'
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.registry() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT UNIQUE COLLATE NOCASE,
                    role TEXT NOT NULL CHECK(role IN ('admin','member')),
                    status TEXT NOT NULL CHECK(status IN ('invited','active','disabled')),
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invitations (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    token_hash TEXT NOT NULL UNIQUE, expires REAL NOT NULL,
                    used REAL, revoked INTEGER NOT NULL DEFAULT 0,
                    ready INTEGER NOT NULL DEFAULT 0
                );
            ''')
            db.execute("INSERT OR IGNORE INTO users VALUES('owner',?,'admin','active',?)",
                       (email_address(self.owner.admin_email) if self.owner.admin_email else None, time.time()))

    def user(self, user_id=None):
        with self.registry() as db:
            row = db.execute('SELECT id,email,role,status,created FROM users WHERE id=?',
                             (user_id or self.user_id.get(),)).fetchone()
        return dict(row) if row else None

    def by_email(self, email):
        # The existing owner can continue using password-only sign-in.
        if email in ('', None):
            return self.user('owner')
        try:
            email = email_address(email)
        except ValueError:
            return None
        with self.registry() as db:
            row = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        return dict(row) if row else None

    def library(self, user_id):
        if user_id == 'owner':
            return self.owner
        # IDs originate in the registry; reject paths even for internal callers.
        if not re.fullmatch(r'[0-9a-f]{32}', user_id):
            raise ValueError('Invalid account')
        if user_id not in self.libraries:
            folder = self.owner.data_dir / 'users' / user_id
            self.libraries[user_id] = replace(self.owner, data_dir=folder, cache_dir=folder/'pdfs',
                fields=[{'id':'unfiled','label':'Unfiled'}], password='', ingest_token='', dev=False)
        return self.libraries[user_id]

    def session_user(self, request):
        token = request.cookies.get('corpus_session', '')
        with self.session_lock:
            record = self.sessions.get(token)
            if not record or record[1] < time.time():
                self.sessions.pop(token, None)
                return self.user('owner') if self.owner.dev else None
        user = self.user(record[0])
        return user if user and user['status'] == 'active' else None

    def issue_session(self, user_id=None):
        user_id = user_id or self.user_id.get()
        user = self.user(user_id)
        if not user or user['status'] != 'active':
            raise HTTPException(401, 'Account is unavailable')
        token = secrets.token_urlsafe(32)
        with self.session_lock:
            self.sessions = {key:value for key,value in self.sessions.items() if value[1] > time.time()}
            self.sessions[token] = (user_id, time.time() + 7*86400)
        response = JSONResponse({'authenticated':True, 'user':user})
        response.set_cookie('corpus_session', token, httponly=True, secure=self.owner.secure_cookie,
                            samesite='strict', max_age=7*86400)
        return response

    def revoke_sessions(self, user_id):
        with self.session_lock:
            self.sessions = {key:value for key,value in self.sessions.items() if value[0] != user_id}

    def require_admin(self, request):
        user = self.session_user(request)
        if not user or user['role'] != 'admin':
            raise HTTPException(403, 'Administrator access is required')
        return user

    @property
    def email_enabled(self):
        return bool(self.owner.smtp_host and self.owner.smtp_from)

    @property
    def resend_setup_file(self):
        return self.owner.data_dir / 'mail-setup' / 'resend-key'

    def save_resend_setup(self, key):
        if self.email_enabled:
            raise HTTPException(409, 'Invitation email is already connected')
        if not isinstance(key, str) or not re.fullmatch(r're_[A-Za-z0-9_-]{10,256}', key.strip()):
            raise ValueError('Enter a valid Resend API key')
        key = key.strip()
        request = urllib.request.Request('https://api.resend.com/domains', headers={
            'Authorization':'Bearer '+key, 'User-Agent':'Corpus-mail-setup/1.0'})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 403:
                raise ValueError('Use a Full access Resend key so the domain can be connected')
            raise ValueError('Resend could not validate this key. Check it and try again.')
        except (OSError, ValueError):
            raise HTTPException(502, 'Could not reach Resend. Please try again shortly.')
        folder = self.resend_setup_file.parent
        folder.mkdir(mode=0o700, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w',dir=folder,delete=False) as output:
                temporary = Path(output.name)
                output.write(key+'\n')
            os.replace(temporary,self.resend_setup_file)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def send_invitation(self, email, url):
        if not self.email_enabled:
            raise HTTPException(503, 'Email sending is not configured. Create a link to send from your email app.')
        message = EmailMessage()
        message['Subject'] = 'Your invitation to Corpus'
        message['From'] = email_address(self.owner.smtp_from)
        message['To'] = email
        message.set_content('You have been invited to Corpus, your private research library.\n\n'
                            'Set your password using this link:\n' + url + '\n\n'
                            'This link expires in 72 hours and can be used once. '
                            'If you were not expecting it, you can ignore this email.\n')
        client = smtplib.SMTP_SSL if self.owner.smtp_security == 'ssl' else smtplib.SMTP
        kwargs = {'host':self.owner.smtp_host, 'port':self.owner.smtp_port, 'timeout':15}
        if self.owner.smtp_security == 'ssl':
            kwargs['context'] = ssl.create_default_context()
        try:
            with client(**kwargs) as smtp:
                if self.owner.smtp_security == 'starttls':
                    smtp.starttls(context=ssl.create_default_context())
                if self.owner.smtp_username:
                    smtp.login(self.owner.smtp_username, self.owner.smtp_password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException):
            # Do not expose SMTP credentials, server responses, or invitation URLs.
            raise HTTPException(502, 'The email server could not send the invitation. Try again or create a link.')

    def create_invite(self, email, origin, delivery='link'):
        # Serialize sending and publication so overlapping resends cannot revoke
        # each other's newly delivered links. This is an admin-only operation.
        with self.invite_lock:
            return self._create_invite(email, origin, delivery)

    def _create_invite(self, email, origin, delivery):
        email = email_address(email)
        if delivery not in ('link','email'):
            raise ValueError('Choose link or email delivery')
        if delivery == 'email' and not self.email_enabled:
            raise HTTPException(503, 'Email sending is not configured. Create an invitation link instead.')
        token = secrets.token_urlsafe(32)
        invitation_id = uuid.uuid4().hex
        expires = time.time() + INVITE_TTL
        with self.registry() as db:
            db.execute('BEGIN IMMEDIATE')
            user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
            if user and user['role'] == 'admin':
                raise ValueError('The administrator cannot be invited')
            if user and user['status'] == 'disabled':
                raise ValueError('Enable this account before sending a new invitation')
            user_id = user['id'] if user else uuid.uuid4().hex
            if not user:
                db.execute("INSERT INTO users VALUES(?,?,'member','invited',?)", (user_id,email,time.time()))
            db.execute('INSERT INTO invitations(id,user_id,token_hash,expires) VALUES(?,?,?,?)',
                       (invitation_id,user_id,hashlib.sha256(token.encode()).hexdigest(),expires))
        url = origin.rstrip('/') + '/#invite=' + token
        if delivery == 'email':
            try:
                self.send_invitation(email, url)
            except Exception:
                with self.registry() as db:
                    db.execute('UPDATE invitations SET revoked=1 WHERE id=?', (invitation_id,))
                raise
        with self.registry() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE invitations SET revoked=1 WHERE user_id=? AND id!=? AND used IS NULL', (user_id,invitation_id))
            db.execute('UPDATE invitations SET ready=1 WHERE id=?', (invitation_id,))
        result = {'id':invitation_id, 'email':email, 'expires':expires, 'delivery':delivery}
        if delivery == 'link':
            result['url'] = url
        return result

    def invitation(self, db, token):
        if not isinstance(token, str) or not 32 <= len(token) <= 100:
            raise HTTPException(400, 'This invitation is invalid, expired, or already used. Ask for a new invitation.')
        row = db.execute('''SELECT i.*,u.email,u.status FROM invitations i JOIN users u ON u.id=i.user_id
            WHERE i.token_hash=? AND i.used IS NULL AND i.revoked=0 AND i.ready=1 AND i.expires>? AND u.status!='disabled' ''',
            (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
        if not row:
            raise HTTPException(400, 'This invitation is invalid, expired, or already used. Ask for a new invitation.')
        return row

    def accept(self, token, password, hash_password):
        password = validate_password(password)
        with self.registry() as db:
            db.execute('BEGIN IMMEDIATE')
            invite = self.invitation(db, token)
            settings = self.library(invite['user_id'])
            settings.data_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            settings.data_dir.mkdir(mode=0o700, exist_ok=True)
            initialize(settings)
            salt = secrets.token_hex(16)
            with connection(settings) as library:
                library.execute('INSERT OR REPLACE INTO credential VALUES(1,?,?)', (salt,hash_password(password,salt)))
                # A recovery link also revokes old passkeys and sessions.
                library.execute('DELETE FROM passkeys')
                library.execute('INSERT OR IGNORE INTO passkey_account VALUES(1,?)', (secrets.token_bytes(32),))
            db.execute('UPDATE invitations SET used=? WHERE id=?', (time.time(),invite['id']))
            db.execute("UPDATE users SET status='active' WHERE id=?", (invite['user_id'],))
        self.revoke_sessions(invite['user_id'])
        return invite['user_id']


def install_routes(app, accounts, limits, hash_password, start_workers):
    def limited(request, action, count=10):
        ip = request.client.host if request.client else 'unknown'
        if not limits.allow(action+':'+ip, count, 300):
            raise HTTPException(429, 'Too many attempts; try again in five minutes')

    @app.get('/api/admin/accounts')
    def users(request:Request):
        accounts.require_admin(request)
        with accounts.registry() as db:
            users = [dict(row) for row in db.execute('SELECT id,email,role,status,created FROM users ORDER BY created')]
            invitations = [dict(row) for row in db.execute('''SELECT i.id,u.email,i.expires FROM invitations i JOIN users u ON u.id=i.user_id
                WHERE i.used IS NULL AND i.revoked=0 AND i.ready=1 AND i.expires>? ORDER BY i.expires DESC''', (time.time(),))]
        return {'users':users, 'invitations':invitations, 'email_enabled':accounts.email_enabled,
                'mail_setup_pending':accounts.resend_setup_file.is_file(),
                'public_origin':accounts.owner.public_origin or accounts.owner.passkey_origin}

    @app.post('/api/admin/mail/resend')
    def connect_resend(request:Request, body:dict):
        accounts.require_admin(request)
        limited(request, 'mail-setup', 5)
        accounts.save_resend_setup(body.get('api_key'))
        return {'saved':True}

    @app.post('/api/admin/invitations')
    def invite(request:Request, body:dict):
        accounts.require_admin(request)
        limited(request, 'invitations', 20)
        origin = accounts.owner.public_origin or accounts.owner.passkey_origin
        if not origin:
            if not accounts.owner.dev:
                raise HTTPException(503, 'Set CORPUS_PUBLIC_ORIGIN to the public address of Corpus before inviting people.')
            origin = str(request.base_url).rstrip('/')
        return accounts.create_invite(body.get('email'), origin, body.get('delivery','link'))

    @app.post('/api/admin/invitations/{id}/revoke')
    def revoke(request:Request, id:str):
        accounts.require_admin(request)
        with accounts.registry() as db:
            db.execute('UPDATE invitations SET revoked=1 WHERE id=?', (id,))
        return {'revoked':True}

    @app.post('/api/admin/accounts/{id}/status')
    async def status(request:Request, id:str, body:dict):
        accounts.require_admin(request)
        if body.get('status') not in ('active','disabled'):
            raise ValueError('Choose active or disabled')
        with accounts.registry() as db:
            row = db.execute("SELECT * FROM users WHERE id=? AND role!='admin'", (id,)).fetchone()
            if not row or row['status'] == 'invited':
                raise ValueError('Choose an existing member account')
            db.execute('UPDATE users SET status=? WHERE id=?', (body['status'],id))
            db.execute('UPDATE invitations SET revoked=1 WHERE user_id=? AND used IS NULL', (id,))
        accounts.revoke_sessions(id)
        if body['status'] == 'active':
            start_workers(id)
        return {'status':body['status']}

    @app.post('/api/invitations/inspect')
    def inspect(request:Request, body:dict):
        limited(request, 'invite-accept', 30)
        with accounts.registry() as db:
            invite = accounts.invitation(db, body.get('token'))
        return {'email':invite['email'], 'expires':invite['expires'], 'recovery':invite['status']=='active'}

    @app.post('/api/invitations/accept')
    async def accept(request:Request, body:dict):
        import asyncio
        limited(request, 'invite-accept')
        user_id = await asyncio.to_thread(accounts.accept, body.get('token'), body.get('password'), hash_password)
        start_workers(user_id)
        return accounts.issue_session(user_id)

    @app.post('/api/account')
    def update_account(request:Request, body:dict):
        user = accounts.session_user(request)
        if not user:
            raise HTTPException(401, 'Sign in to Corpus')
        limited(request, 'account-update')
        settings = accounts.library(user['id'])
        password = body.get('current_password','')
        if not isinstance(password,str) or len(password)>1000:
            raise ValueError('Invalid password')
        with connection(settings,True) as db:
            credential = db.execute('SELECT * FROM credential WHERE id=1').fetchone()
        if not hmac.compare_digest(hash_password(password,credential['salt']),credential['password_hash']):
            raise HTTPException(401, 'Incorrect password')
        email = email_address(body['email']) if 'email' in body else user['email']
        new_password = validate_password(body['password']) if body.get('password') else None
        if new_password and user['id'] == 'owner' and accounts.owner.password:
            raise ValueError('The administrator password is managed by CORPUS_PASSWORD on the server')
        with accounts.registry() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('UPDATE users SET email=? WHERE id=?', (email,user['id']))
            except sqlite3.IntegrityError:
                raise ValueError('That email address is already in use')
            db.execute('UPDATE invitations SET revoked=1 WHERE user_id=? AND used IS NULL', (user['id'],))
            if new_password:
                salt = secrets.token_hex(16)
                with connection(settings) as library:
                    library.execute('UPDATE credential SET salt=?,password_hash=? WHERE id=1', (salt,hash_password(new_password,salt)))
        if new_password:
            accounts.revoke_sessions(user['id'])
        return accounts.issue_session(user['id'])
