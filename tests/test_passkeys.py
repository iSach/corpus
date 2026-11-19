"""Verify real WebAuthn signatures, challenge binding, and password recovery."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from corpus.app import create_app
from corpus.config import Settings
from corpus.db import connection
from helpers import Authenticator, encoded

ORIGIN = 'https://corpus.example'
PASSWORD = 'test-owner-password'


def post(client, path, body=None, origin=ORIGIN):
    return client.post('/api/passkeys/'+path, json=body or {}, headers={'Origin':origin})


def enroll(client, authenticator=None):
    authenticator = authenticator or Authenticator()
    assert client.post('/api/login', json={'password':PASSWORD}).status_code == 200
    response = post(client, 'register/options', {'name':'iCloud Keychain','password':PASSWORD})
    assert response.status_code == 200, response.text
    challenge = response.json()
    options = challenge['options']
    assert options['authenticatorSelection']['residentKey'] == 'required'
    assert options['authenticatorSelection']['userVerification'] == 'required'
    response = post(client, 'register/verify', {'challenge_id':challenge['challenge_id'],
                                             'credential':authenticator.registration(options)})
    assert response.status_code == 200, response.text
    return authenticator


def authenticate(client, authenticator, **kwargs):
    response = post(client, 'authenticate/options')
    assert response.status_code == 200, response.text
    challenge = response.json()
    assert challenge['options']['userVerification'] == 'required'
    body = {'challenge_id':challenge['challenge_id'], 'credential':authenticator.assertion(challenge['options'], **kwargs)}
    return post(client, 'authenticate/verify', body), body


@pytest.fixture
def owner(tmp_path):
    settings = Settings(data_dir=tmp_path, password=PASSWORD, ingest_token='test-ingest',
                        pdf_jobs=False, secure_cookie=True, passkey_origin=ORIGIN)
    app = create_app(settings)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, app, settings


def test_synced_passkey_login_issues_session_and_prevents_replay(owner):
    client, app, settings = owner
    authenticator = enroll(client)
    assert client.get('/api/config').json()['passkeys'] == {'enabled':True,'registered':True}
    client.post('/api/logout')
    assert client.get('/api/papers').status_code == 401
    response, body = authenticate(client, authenticator)
    assert response.status_code == 200, response.text
    assert 'HttpOnly' in response.headers['set-cookie'] and 'Secure' in response.headers['set-cookie']
    assert client.get('/api/session').json()['authenticated']
    assert client.get('/api/papers').status_code == 200
    client.post('/api/logout')
    assert post(client, 'authenticate/verify', body).status_code == 400
    assert not client.get('/api/session').json()['authenticated']
    # Synced iCloud-style authenticators may continue to report a zero counter.
    assert authenticate(client, authenticator)[0].status_code == 200
    with connection(settings, True) as db:
        key = db.execute('SELECT * FROM passkeys').fetchone()
        assert key['sign_count'] == 0 and key['backed_up'] == 1
        assert key['device_type'] == 'multi_device' and key['last_used']
        assert bytes(key['public_key']) and bytes(key['credential_id']) == authenticator.credential_id


@pytest.mark.parametrize('changes', [
    {'verified':False}, {'present':False}, {'origin':'https://attacker.example'},
    {'rp_id':'attacker.example'}, {'challenge':encoded(b'wrong-challenge')},
    {'user_handle':encoded(b'another-user')}, {'cross_origin':True},
])
def test_invalid_signed_assertions_cannot_create_sessions(owner, changes):
    client, _, _ = owner
    authenticator = enroll(client)
    client.post('/api/logout')
    response, _ = authenticate(client, authenticator, **changes)
    assert response.status_code in (400,401), response.text
    assert not client.get('/api/session').json()['authenticated']


def test_invalid_signature_and_unknown_credential_are_rejected(owner):
    client, _, _ = owner
    authenticator = enroll(client)
    client.post('/api/logout')
    challenge = post(client, 'authenticate/options').json()
    credential = authenticator.assertion(challenge['options'])
    credential['response']['signature'] = encoded(b'not-a-signature')
    assert post(client, 'authenticate/verify', {'challenge_id':challenge['challenge_id'],'credential':credential}).status_code == 401
    unknown = Authenticator()
    unknown.user_handle = authenticator.user_handle
    assert authenticate(client, unknown)[0].status_code == 401
    assert not client.get('/api/session').json()['authenticated']


def test_counter_rollback_is_rejected(owner):
    client, _, _ = owner
    authenticator = enroll(client)
    client.post('/api/logout')
    assert authenticate(client, authenticator, count=2)[0].status_code == 200
    client.post('/api/logout')
    assert authenticate(client, authenticator, count=2)[0].status_code == 401
    assert authenticate(client, authenticator, count=1)[0].status_code == 401


def test_enrollment_and_removal_require_session_and_password(owner):
    client, _, _ = owner
    assert client.get('/api/passkeys').status_code == 401
    assert post(client, 'register/options', {'password':PASSWORD}).status_code == 401
    assert post(client, 'register/verify').status_code == 401
    assert post(client, '1/remove', {'password':PASSWORD}).status_code == 401
    assert client.post('/api/login', json={'password':PASSWORD}).status_code == 200
    assert post(client, 'register/options', {'password':'wrong'}).status_code == 401
    assert client.get('/api/passkeys').json()['passkeys'] == []
    enroll(client)
    key = client.get('/api/passkeys').json()['passkeys'][0]
    assert set(key) == {'id','name','created','last_used'}
    assert post(client, str(key['id'])+'/remove', {'password':'wrong'}).status_code == 401
    assert post(client, str(key['id'])+'/remove', {'password':PASSWORD}, origin='https://attacker.example').status_code == 403
    assert post(client, str(key['id'])+'/remove', {'password':PASSWORD}).status_code == 200
    assert not client.get('/api/config').json()['passkeys']['registered']
    assert post(client, 'authenticate/options').status_code == 400
    client.post('/api/logout')
    assert client.post('/api/login', json={'password':PASSWORD}).status_code == 200


def test_registration_requires_user_verification_and_the_original_session(owner):
    client, _, _ = owner
    client.post('/api/login', json={'password':PASSWORD})
    challenge = post(client, 'register/options', {'password':PASSWORD}).json()
    credential = Authenticator().registration(challenge['options'], verified=False)
    assert post(client, 'register/verify', {'challenge_id':challenge['challenge_id'],'credential':credential}).status_code == 400
    challenge = post(client, 'register/options', {'password':PASSWORD}).json()
    credential = Authenticator().registration(challenge['options'])
    client.post('/api/logout')
    client.post('/api/login', json={'password':PASSWORD})
    assert post(client, 'register/verify', {'challenge_id':challenge['challenge_id'],'credential':credential}).status_code == 400


def test_challenges_are_browser_bound_expire_and_have_a_bound(owner, monkeypatch):
    client, app, _ = owner
    authenticator = enroll(client)
    client.post('/api/logout')
    challenge = post(client, 'authenticate/options').json()
    credential = authenticator.assertion(challenge['options'])
    client.cookies.delete('corpus_webauthn')
    assert post(client, 'authenticate/verify', {'challenge_id':challenge['challenge_id'],'credential':credential}).status_code == 400
    challenge = post(client, 'authenticate/options').json()
    app.state.passkeys.pending[challenge['challenge_id']].expires = 0
    assert post(client, 'authenticate/verify', {'challenge_id':challenge['challenge_id'], 'credential':authenticator.assertion(challenge['options'])}).status_code == 400
    monkeypatch.setattr('corpus.passkeys.MAX_CHALLENGES', 1)
    challenge = post(client, 'authenticate/options').json()
    assert post(client, 'authenticate/options').status_code == 429
    app.state.passkeys.pending[challenge['challenge_id']].expires = 0
    assert post(client, 'authenticate/options').status_code == 200


def test_credentials_survive_restart_but_pending_ceremonies_do_not(tmp_path):
    settings = Settings(data_dir=tmp_path, password=PASSWORD, pdf_jobs=False, passkey_origin=ORIGIN)
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        authenticator = enroll(client)
        old = post(client, 'authenticate/options').json()
    with TestClient(create_app(settings), base_url=ORIGIN) as client:
        assert post(client, 'authenticate/verify', {'challenge_id':old['challenge_id'],'credential':authenticator.assertion(old['options'])}).status_code == 400
        assert authenticate(client, authenticator)[0].status_code == 200


def test_passkeys_use_only_the_configured_origin(owner):
    client, _, _ = owner
    assert client.post('/api/passkeys/authenticate/options').status_code == 403
    assert post(client, 'authenticate/options', origin='https://attacker.example').status_code == 403
    assert client.get('/api/config', headers={'Host':'attacker.example'}).json()['passkeys']['enabled'] is False
    assert client.post('/api/passkeys/authenticate/options', headers={'Host':'attacker.example','Origin':'https://attacker.example'}).status_code == 400


@pytest.mark.parametrize('change', ['origin','challenge','crossOrigin'])
def test_registration_rejects_wrong_client_origin_or_challenge(owner, change):
    client, _, _ = owner
    client.post('/api/login', json={'password':PASSWORD})
    challenge = post(client, 'register/options', {'password':PASSWORD}).json()
    credential = Authenticator().registration(challenge['options'])
    client_data = json.loads(base64.urlsafe_b64decode(credential['response']['clientDataJSON']+'=='))
    client_data[change] = {'origin':'https://attacker.example','challenge':encoded(b'wrong'),'crossOrigin':True}[change]
    credential['response']['clientDataJSON'] = encoded(json.dumps(client_data).encode())
    assert post(client, 'register/verify', {'challenge_id':challenge['challenge_id'],'credential':credential}).status_code == 400
    assert client.get('/api/passkeys').json()['passkeys'] == []


@pytest.mark.parametrize('origin', ['http://corpus.example','https://corpus.example/path','https://a:b@corpus.example','https://corpus.example?query=1','https://corpus.example#fragment'])
def test_invalid_passkey_origin_is_rejected(origin):
    with pytest.raises(ValueError):
        Settings(passkey_origin=origin)
