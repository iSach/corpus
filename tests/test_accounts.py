"""Invite lifecycle, administrator boundaries, and private-library isolation."""
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, parse_qs
import time

import pytest
from fastapi.testclient import TestClient

from corpus.app import create_app
from corpus.config import Settings
from corpus.db import connection, initialize, add_run, validate_paper, write_paper
from corpus import accounts as account_module, jobs, sources
from helpers import Authenticator, pdf_bytes

ORIGIN = 'https://corpus.example'
PASSWORD = 'existing-owner-password'
MEMBER_PASSWORD = 'my-private-member-password'


@pytest.fixture
def site(tmp_path):
    settings = Settings(data_dir=tmp_path, password=PASSWORD, ingest_token='owner-ingest', pdf_jobs=False,
                        public_origin=ORIGIN, passkey_origin=ORIGIN, secure_cookie=True)
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN) as admin:
        assert admin.post('/api/login', json={'password':PASSWORD}).status_code == 200
        yield app, admin, settings


def invitation(admin, email='friend@example.org', delivery='link'):
    response = admin.post('/api/admin/invitations', json={'email':email,'delivery':delivery})
    assert response.status_code == 200, response.text
    return response.json()


def token(invite):
    return parse_qs(urlsplit(invite['url']).fragment)['invite'][0]


def join(app, invite):
    client = TestClient(app, base_url=ORIGIN)
    response = client.post('/api/invitations/accept', json={'token':token(invite), 'password':MEMBER_PASSWORD})
    assert response.status_code == 200, response.text
    return client, response.json()['user']


def test_existing_library_becomes_admin_without_moving_or_replacing_data(tmp_path):
    settings = Settings(data_dir=tmp_path, password=PASSWORD, ingest_token='owner-ingest', pdf_jobs=False)
    initialize(settings)
    with connection(settings) as db:
        run = add_run(db, 'fixture', 'Existing library')
        paper_id = write_paper(db, validate_paper(db, {'title':'Existing private paper','notes':'Keep me'}), run)
    with TestClient(create_app(settings)) as client:
        result = client.post('/api/login', json={'password':PASSWORD}).json()
        assert result['user']['id'] == 'owner' and result['user']['role'] == 'admin'
        assert client.get('/api/paper', params={'id':paper_id}).json()['notes'] == 'Keep me'
        assert settings.database == tmp_path / 'corpus.sqlite3'


def test_invite_is_bound_to_email_and_only_sets_up_on_password_submission(site):
    app, admin, settings = site
    invite = invitation(admin, 'Friend@Example.org')
    key = token(invite)
    assert urlsplit(invite['url']).query == ''
    assert invite['email'] == 'friend@example.org'
    visitor = TestClient(app, base_url=ORIGIN)
    assert visitor.get(invite['url']).status_code == 200  # Email scanners do not consume the link.
    for _ in range(2):
        result = visitor.post('/api/invitations/inspect', json={'token':key})
        assert result.status_code == 200 and result.json()['email'] == 'friend@example.org'
    assert visitor.post('/api/invitations/accept', json={'token':key,'password':'short'}).status_code == 400
    result = visitor.post('/api/invitations/accept', json={'token':key,'password':MEMBER_PASSWORD,'role':'admin','email':'attacker@example.org'})
    assert result.status_code == 200
    user = result.json()['user']
    assert user['role'] == 'member' and user['email'] == 'friend@example.org'
    assert visitor.get('/api/papers').json()['total'] == 0
    assert visitor.get('/api/config').json()['fields'] == [{'id':'unfiled','label':'Unfiled','symbol':'','color':''}]
    assert visitor.post('/api/invitations/accept', json={'token':key,'password':MEMBER_PASSWORD}).status_code == 400
    with app.state.accounts.registry() as db:
        stored = db.execute('SELECT * FROM invitations').fetchone()
        assert stored['token_hash'] != key and stored['used']
    assert key.encode() not in (settings.data_dir/'accounts.sqlite3').read_bytes()


def test_expired_revoked_and_replaced_links_cannot_be_used(site):
    app, admin, _ = site
    old = invitation(admin)
    current = invitation(admin)
    visitor = TestClient(app, base_url=ORIGIN)
    assert visitor.post('/api/invitations/inspect', json={'token':token(old)}).status_code == 400
    assert admin.post(f"/api/admin/invitations/{current['id']}/revoke").status_code == 200
    assert visitor.post('/api/invitations/inspect', json={'token':token(current)}).status_code == 400
    expired = invitation(admin)
    with app.state.accounts.registry() as db:
        db.execute('UPDATE invitations SET expires=? WHERE id=?', (time.time()-1,expired['id']))
    assert visitor.post('/api/invitations/accept', json={'token':token(expired),'password':MEMBER_PASSWORD}).status_code == 400


def test_only_admin_can_invite_list_revoke_or_disable(site):
    app, admin, _ = site
    member, user = join(app, invitation(admin))
    visitor = TestClient(app, base_url=ORIGIN)
    for client, expected in ((visitor,401),(member,403)):
        assert client.get('/api/admin/accounts').status_code == expected
        assert client.post('/api/admin/invitations',json={'email':'other@example.org'}).status_code == expected
        assert client.post('/api/admin/invitations/anything/revoke').status_code == expected
        assert client.post(f"/api/admin/accounts/{user['id']}/status",json={'status':'disabled'}).status_code == expected
    assert admin.post('/api/admin/accounts/owner/status',json={'status':'disabled'}).status_code == 400
    assert admin.get('/api/session').json()['user']['role'] == 'admin'


def test_library_isolation_including_guessed_ids_sql_exports_and_pdf_files(site, monkeypatch):
    app, admin, settings = site
    alice, a = join(app, invitation(admin,'alice@example.org'))
    bob, b = join(app, invitation(admin,'bob@example.org'))
    clients = [(admin,'owner','Admin confidential'), (alice,a['id'],'Alice confidential'), (bob,b['id'],'Bob confidential')]
    monkeypatch.setattr(sources,'safe_download',lambda url,max_bytes:pdf_bytes())
    for client, user_id, title in clients:
        response = client.post('/api/papers',json={'title':title,'doi':'10.5555/same','notes':title,'artifacts':[{'kind':'pdf','url':'https://example.org/same.pdf'}]})
        assert response.status_code == 200, response.text
        paper = response.json()['paper']
        with connection(app.state.accounts.library(user_id)) as db:
            artifact = dict(db.execute('SELECT * FROM artifacts').fetchone())
        jobs.fetch_one(app.state.accounts.library(user_id),artifact)
        assert client.get(f"/api/artifacts/{artifact['id']}/file").status_code == 200
        assert client.get('/api/paper',params={'id':paper['id']}).json()['title'] == title
        assert client.get('/api/papers').json()['total'] == 1
        result = client.post('/api/sql',json={'sql':'SELECT title FROM papers'}).json()
        assert result['rows'] == [{'title':title}]
        assert client.post('/api/sql',json={'sql':'SELECT * FROM users'}).status_code == 400
        assert client.post('/api/sql',json={'sql':"ATTACH DATABASE '../accounts.sqlite3' AS other"}).status_code == 400
        exported = client.post('/api/export',json={'scope':'all','format':'json'}).json()
        assert title in str(exported)
        assert client.get('/api/stats').json()['total'] == 1
    for client, _, title in clients:
        assert client.get('/api/papers',params={'q':title.split()[0]}).json()['total'] == 1
        for other in ('Admin','Alice','Bob'):
            if other not in title:
                assert client.get('/api/papers',params={'q':other}).json()['total'] == 0
    assert len({app.state.accounts.library(uid).cache_dir for _,uid,_ in clients}) == 3
    # An ID that exists only in another library cannot be read, changed, or deleted.
    admin_only = admin.post('/api/papers',json={'title':'Only the owner has this paper'}).json()['paper']['id']
    assert alice.get('/api/paper',params={'id':admin_only}).status_code == 404
    assert alice.post('/api/papers',json={'id':admin_only,'notes':'overwrite'}).status_code == 404
    assert alice.delete('/api/paper',params={'id':admin_only}).status_code == 404
    assert alice.post('/api/topics',json={'id':'alice-only','label':'Alice topic'}).status_code == 200
    assert all(topic['id'] != 'alice-only' for topic in admin.get('/api/topics').json()['topics'])


def test_owner_ingest_token_always_targets_owner_even_with_member_cookie(site):
    app, admin, _ = site
    member, _ = join(app,invitation(admin))
    response = member.post('/api/ingest', headers={'Authorization':'Bearer owner-ingest'},json={'papers':[{'title':'Owner automatic discovery'}]})
    assert response.status_code == 200, response.text
    assert admin.get('/api/inbox').json()['total'] == 1
    assert member.get('/api/inbox').json()['total'] == 0
    assert member.post('/api/ingest',json={'papers':[{'title':'Wrong token'}]}).status_code == 401


def test_personal_ingest_tokens_target_their_own_library_and_can_be_revoked(site):
    app, admin, _ = site
    member, user = join(app, invitation(admin))
    anonymous = TestClient(app, base_url=ORIGIN)
    assert anonymous.post('/api/account/ingest-token').status_code == 401
    assert member.get('/api/account/ingest-token').json() == {'exists': False, 'created': None}
    created = member.post('/api/account/ingest-token').json()
    secret = created['token']
    assert created['exists'] and secret.startswith('cps_')
    assert 'token' not in member.get('/api/account/ingest-token').json(), 'the secret is shown only once'
    with account_module.Accounts.registry(app.state.accounts) as db:
        assert secret not in str(db.execute('SELECT * FROM ingest_tokens').fetchall()), 'only a hash is stored'

    def send(token, title):
        return anonymous.post('/api/ingest', headers={'Authorization': 'Bearer ' + token}, json={'papers': [{'title': title}]})
    assert send(secret, 'Member paper').status_code == 200
    assert member.get('/api/inbox').json()['total'] == 1
    assert admin.get('/api/inbox').json()['total'] == 0, 'a member token never writes to the owner library'
    assert send('cps_' + 'x' * 43, 'Guess').status_code == 401

    replaced = member.post('/api/account/ingest-token').json()['token']
    assert send(secret, 'Old').status_code == 401 and send(replaced, 'New').status_code == 200
    assert member.delete('/api/account/ingest-token').json()['exists'] is False
    assert send(replaced, 'Revoked').status_code == 401

    again = member.post('/api/account/ingest-token').json()['token']
    admin.post(f"/api/admin/accounts/{user['id']}/status", json={'status': 'disabled'})
    assert send(again, 'Disabled').status_code == 401


def test_member_login_disable_and_recovery_revoke_sessions_and_keep_library(site):
    app, admin, _ = site
    member, user = join(app,invitation(admin))
    member.post('/api/papers',json={'title':'Keep my library'})
    second = TestClient(app,base_url=ORIGIN)
    assert second.post('/api/login',json={'email':user['email'],'password':MEMBER_PASSWORD}).status_code == 200
    reset = invitation(admin)
    recovering, _ = join(app,reset)
    assert member.get('/api/papers').status_code == 401
    assert second.get('/api/papers').status_code == 401
    assert recovering.get('/api/papers').json()['total'] == 1
    assert admin.post(f"/api/admin/accounts/{user['id']}/status",json={'status':'disabled'}).status_code == 200
    assert recovering.get('/api/papers').status_code == 401
    assert second.post('/api/login',json={'email':user['email'],'password':MEMBER_PASSWORD}).status_code == 401
    assert admin.post(f"/api/admin/accounts/{user['id']}/status",json={'status':'active'}).status_code == 200
    assert recovering.get('/api/papers').status_code == 401
    assert second.post('/api/login',json={'email':user['email'],'password':MEMBER_PASSWORD}).status_code == 200


def test_account_email_and_password_change_are_authenticated_and_private(site):
    app, admin, _ = site
    member, user = join(app,invitation(admin))
    assert member.post('/api/account',json={'email':'new@example.org','current_password':'wrong'}).status_code == 401
    assert member.post('/api/account',json={'email':'new@example.org','current_password':MEMBER_PASSWORD,'password':'a-new-member-password'}).status_code == 200
    member.post('/api/logout')
    assert member.post('/api/login',json={'email':user['email'],'password':MEMBER_PASSWORD}).status_code == 401
    assert member.post('/api/login',json={'email':'new@example.org','password':'a-new-member-password'}).status_code == 200
    assert admin.post('/api/account',json={'current_password':PASSWORD,'email':'admin@example.org'}).status_code == 200
    assert member.post('/api/account',json={'email':'admin@example.org','current_password':'a-new-member-password'}).status_code == 400


def test_invitation_double_submission_only_one_wins(site):
    app, admin, _ = site
    key = token(invitation(admin))
    def accept():
        client = TestClient(app,base_url=ORIGIN)
        return client.post('/api/invitations/accept',json={'token':key,'password':MEMBER_PASSWORD}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _:accept(),range(2))) == [200,400]


def test_email_failure_does_not_revoke_the_previous_working_link(site, monkeypatch):
    app, admin, settings = site
    settings.smtp_host = 'smtp.example.org'
    settings.smtp_from = 'corpus@example.org'
    old = invitation(admin)
    def fail(*args):
        raise account_module.HTTPException(502,'The email server could not send the invitation.')
    monkeypatch.setattr(app.state.accounts,'send_invitation',fail)
    assert admin.post('/api/admin/invitations',json={'email':'friend@example.org','delivery':'email'}).status_code == 502
    visitor = TestClient(app,base_url=ORIGIN)
    assert visitor.post('/api/invitations/inspect',json={'token':token(old)}).status_code == 200


def test_smtp_sends_the_single_use_url_with_tls(site, monkeypatch):
    app, admin, settings = site
    settings.smtp_host = 'smtp.example.org'; settings.smtp_from = 'corpus@example.org'
    settings.smtp_username = 'sender'; settings.smtp_password = 'secret'
    sent = []
    class SMTP:
        def __init__(self,**kwargs): assert kwargs['host'] == settings.smtp_host
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def starttls(self,**kwargs): sent.append('tls')
        def login(self,username,password): assert (username,password) == ('sender','secret')
        def send_message(self,message): sent.append(message)
    monkeypatch.setattr(account_module.smtplib,'SMTP',SMTP)
    result = invitation(admin,delivery='email')
    assert 'url' not in result and sent[0] == 'tls'
    message = sent[1]
    assert message['To'] == 'friend@example.org' and message['From'] == 'corpus@example.org'
    assert ORIGIN+'/#invite=' in message.get_content()


def test_cross_origin_invite_and_accept_are_rejected(site):
    app, admin, _ = site
    invite = invitation(admin)
    headers = {'Origin':'https://attacker.example'}
    assert admin.post('/api/admin/invitations',headers=headers,json={'email':'other@example.org'}).status_code == 403
    visitor = TestClient(app,base_url=ORIGIN)
    assert visitor.post('/api/invitations/accept',headers=headers,json={'token':token(invite),'password':MEMBER_PASSWORD}).status_code == 403


def test_public_config_does_not_expose_private_projects(site):
    app, admin, _ = site
    admin.post('/api/topics',json={'id':'private-project','label':'Secret project'})
    visitor = TestClient(app,base_url=ORIGIN)
    assert visitor.get('/api/config').json()['fields'] == []
    assert visitor.get('/api/config?email=friend@example.org').json()['fields'] == []


def test_request_scope_does_not_leak_between_parallel_accounts(site):
    app, admin, _ = site
    alice,_ = join(app,invitation(admin,'alice@example.org'))
    bob,_ = join(app,invitation(admin,'bob@example.org'))
    alice.post('/api/papers',json={'title':'Alice paper'})
    bob.post('/api/papers',json={'title':'Bob paper'})
    def read(item):
        client,title = item
        for _ in range(8):
            assert client.get('/api/papers').json()['papers'][0]['title'] == title
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(read,[(alice,'Alice paper'),(bob,'Bob paper')]))


def test_member_passkeys_sign_in_to_their_library_and_cannot_cross_accounts(site):
    app, admin, _ = site
    member, user = join(app,invitation(admin))
    authenticator = Authenticator()
    headers = {'Origin':ORIGIN}
    options = member.post('/api/passkeys/register/options',headers=headers,json={'password':MEMBER_PASSWORD,'name':'Friend key'}).json()
    response = member.post('/api/passkeys/register/verify',headers=headers,json={
        'challenge_id':options['challenge_id'],'credential':authenticator.registration(options['options'])})
    assert response.status_code == 200, response.text
    assert admin.get('/api/passkeys').json()['passkeys'] == []
    member.post('/api/logout')
    visitor = TestClient(app,base_url=ORIGIN)
    assert visitor.get('/api/config?email=friend@example.org').json()['passkeys']['registered']
    challenge = visitor.post('/api/passkeys/authenticate/options?email=friend@example.org',headers=headers).json()
    body = {'challenge_id':challenge['challenge_id'],'credential':authenticator.assertion(challenge['options'])}
    # A challenge cannot be consumed while selecting another library.
    assert visitor.post('/api/passkeys/authenticate/verify',headers=headers,json=body).status_code == 400
    challenge = visitor.post('/api/passkeys/authenticate/options?email=friend@example.org',headers=headers).json()
    body = {'challenge_id':challenge['challenge_id'],'credential':authenticator.assertion(challenge['options'])}
    response = visitor.post('/api/passkeys/authenticate/verify?email=friend@example.org',headers=headers,json=body)
    assert response.status_code == 200, response.text
    assert response.json()['user']['id'] == user['id']
    assert visitor.get('/api/admin/accounts').status_code == 403
    assert visitor.get('/api/passkeys').json()['passkeys'][0]['name'] == 'Friend key'


def test_accounts_and_pending_invitations_survive_restart(tmp_path):
    settings = Settings(data_dir=tmp_path,password=PASSWORD,ingest_token='owner-ingest',pdf_jobs=False,public_origin=ORIGIN)
    app = create_app(settings)
    with TestClient(app,base_url=ORIGIN) as admin:
        admin.post('/api/login',json={'password':PASSWORD})
        member,user = join(app,invitation(admin))
        member.post('/api/papers',json={'title':'Survives a restart'})
        pending = invitation(admin,'second@example.org')
        old_cookie = member.cookies.get('corpus_session')
    restarted = create_app(settings)
    with TestClient(restarted,base_url=ORIGIN) as member:
        member.cookies.set('corpus_session',old_cookie)
        assert member.get('/api/papers').status_code == 401
        assert member.post('/api/login',json={'email':user['email'],'password':MEMBER_PASSWORD}).status_code == 200
        assert member.get('/api/papers').json()['total'] == 1
        visitor = TestClient(restarted,base_url=ORIGIN)
        assert visitor.post('/api/invitations/inspect',json={'token':token(pending)}).status_code == 200
        assert visitor.post('/api/invitations/accept',json={'token':token(pending),'password':MEMBER_PASSWORD}).status_code == 200


def test_background_workers_use_concrete_member_settings_and_resume_after_enable(tmp_path,monkeypatch):
    from corpus import app as app_module
    started = []
    async def worker(settings,stop):
        started.append(settings.database)
        await stop.wait()
    monkeypatch.setattr(app_module,'worker',worker)
    settings = Settings(data_dir=tmp_path,password=PASSWORD,ingest_token='owner-ingest',pdf_jobs=True,
                        venue_checks=False,public_origin=ORIGIN)
    app = create_app(settings)
    with TestClient(app,base_url=ORIGIN) as admin:
        admin.post('/api/login',json={'password':PASSWORD})
        member,user = join(app,invitation(admin))
        member_database = app.state.accounts.library(user['id']).database
        assert settings.database in started and member_database in started
        assert admin.post(f"/api/admin/accounts/{user['id']}/status",json={'status':'disabled'}).status_code == 200
    started.clear()
    restarted = create_app(settings)
    with TestClient(restarted,base_url=ORIGIN) as admin:
        admin.post('/api/login',json={'password':PASSWORD})
        assert started == [settings.database]
        assert admin.post(f"/api/admin/accounts/{user['id']}/status",json={'status':'active'}).status_code == 200
        assert member_database in started


def test_overlapping_resends_leave_one_valid_invitation(site):
    app,admin,_ = site
    def create(_):
        return app.state.accounts.create_invite('friend@example.org',ORIGIN)
    with ThreadPoolExecutor(max_workers=2) as pool:
        invites = list(pool.map(create,range(2)))
    visitor = TestClient(app,base_url=ORIGIN)
    statuses = [visitor.post('/api/invitations/inspect',json={'token':token(item)}).status_code for item in invites]
    assert sorted(statuses) == [200,400]


def test_resend_phone_setup_requires_admin_and_never_returns_key(site,monkeypatch):
    import io,stat
    app,admin,settings = site
    member,_ = join(app,invitation(admin))
    visitor = TestClient(app,base_url=ORIGIN)
    key = 're_a_private_setup_test_key_12345'
    assert visitor.post('/api/admin/mail/resend',json={'api_key':key}).status_code == 401
    assert member.post('/api/admin/mail/resend',json={'api_key':key}).status_code == 403
    assert admin.post('/api/admin/mail/resend',headers={'Origin':'https://attacker.example'},json={'api_key':key}).status_code == 403
    assert admin.post('/api/admin/mail/resend',json={'api_key':'invalid'}).status_code == 400
    def validate(request,timeout):
        assert request.full_url == 'https://api.resend.com/domains'
        assert request.get_header('Authorization') == 'Bearer '+key
        return io.BytesIO(b'{"data":[]}')
    monkeypatch.setattr(account_module.urllib.request,'urlopen',validate)
    response = admin.post('/api/admin/mail/resend',json={'api_key':key})
    assert response.status_code == 200 and response.json() == {'saved':True}
    assert key not in response.text
    path = settings.data_dir/'mail-setup'/'resend-key'
    assert path.read_text().strip() == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    data = admin.get('/api/admin/accounts')
    assert data.json()['mail_setup_pending'] and key not in data.text
    assert admin.get('/api/admin/mail/resend').status_code == 405


def test_resend_setup_rejects_sending_only_key_without_storing_it(site,monkeypatch):
    import urllib.error
    app,admin,settings = site
    def deny(request,timeout):
        raise urllib.error.HTTPError(request.full_url,403,'Forbidden',{},None)
    monkeypatch.setattr(account_module.urllib.request,'urlopen',deny)
    response = admin.post('/api/admin/mail/resend',json={'api_key':'re_sending_only_key_12345'})
    assert response.status_code == 400 and 'Full access' in response.json()['detail']
    assert not (settings.data_dir/'mail-setup'/'resend-key').exists()
