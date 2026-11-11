"""Exercise native browser WebAuthn with a virtual platform authenticator."""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright, expect

from corpus.app import create_app
from corpus.config import Settings

PASSWORD = 'browser-owner-password'


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        yield browser
        browser.close()


@pytest.fixture
def owner_page(tmp_path, browser):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    origin = f'http://localhost:{port}'
    settings = Settings(data_dir=tmp_path, password=PASSWORD, pdf_jobs=False, passkey_origin=origin)
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(.02)
    assert server.started
    context = browser.new_context(viewport={'width':1440,'height':900})
    page = context.new_page()
    page.set_default_timeout(7000)
    errors = []
    page.on('pageerror', lambda error:errors.append(str(error)))
    cdp = context.new_cdp_session(page)
    cdp.send('WebAuthn.enable')
    cdp.send('WebAuthn.addVirtualAuthenticator', {'options':{
        'protocol':'ctap2', 'transport':'internal', 'hasResidentKey':True,
        'hasUserVerification':True, 'isUserVerified':True, 'automaticPresenceSimulation':True,
        'defaultBackupEligibility':True, 'defaultBackupState':True,
    }})
    page.goto(origin)
    yield page, origin
    assert not errors
    context.close()
    server.should_exit = True
    thread.join(5)


def password_login(page):
    page.locator('#password').fill(PASSWORD)
    page.get_by_role('button',name='ENTER',exact=True).click()
    expect(page.get_by_role('button',name='LOG OUT',exact=True)).to_be_visible()


def register(page):
    password_login(page)
    page.get_by_role('button',name='PASSKEYS',exact=True).click()
    page.get_by_label('CURRENT PASSWORD',exact=True).fill(PASSWORD)
    page.get_by_role('button',name='ADD PASSKEY',exact=True).click()
    expect(page.locator('.passkey-row strong')).to_have_text('iCloud Keychain')


def test_register_then_try_passkey_by_default_on_return(owner_page):
    page, origin = owner_page
    register(page)
    page.get_by_role('button',name='LOG OUT',exact=True).click()
    # Explicit logout stays logged out; a subsequent visit tries the passkey.
    expect(page.locator('#password')).to_be_visible()
    page.reload()
    expect(page.get_by_role('button',name='LOG OUT',exact=True)).to_be_visible()
    assert page.request.get(origin+'/api/session').json()['authenticated']
    expect(page.locator('.passkey-row')).to_contain_text('last used today')


def test_cancelled_automatic_attempt_keeps_password_and_manual_retry(owner_page):
    page, origin = owner_page
    register(page)
    page.get_by_role('button',name='LOG OUT',exact=True).click()
    page.add_init_script("""
        window.passkeyCalls = 0;
        navigator.credentials.get = async () => {
            window.passkeyCalls += 1;
            throw new DOMException('Canceled', 'NotAllowedError');
        };
    """)
    page.reload()
    expect(page.get_by_role('status')).to_contain_text('Passkey sign-in was canceled')
    assert page.evaluate('window.passkeyCalls') == 1
    page.get_by_role('button',name='SIGN IN WITH PASSKEY',exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('Passkey sign-in was canceled')
    assert page.evaluate('window.passkeyCalls') == 2
    password_login(page)
    assert page.request.get(origin+'/api/session').json()['authenticated']


def test_removing_last_passkey_returns_to_password_default(owner_page):
    page, origin = owner_page
    register(page)
    page.get_by_label('CURRENT PASSWORD',exact=True).fill(PASSWORD)
    page.get_by_role('button',name='Remove iCloud Keychain',exact=True).click()
    expect(page.locator('.passkey-row')).to_have_count(0)
    assert not page.request.get(origin+'/api/config').json()['passkeys']['registered']
    page.get_by_role('button',name='LOG OUT',exact=True).click()
    page.reload()
    expect(page.locator('#password')).to_be_visible()
    expect(page.get_by_role('button',name='SIGN IN WITH PASSKEY',exact=True)).to_have_count(0)
    password_login(page)


def test_pending_passkey_request_can_be_aborted_for_password_login(owner_page):
    page, origin = owner_page
    register(page)
    page.get_by_role('button',name='LOG OUT',exact=True).click()
    page.add_init_script("""
        navigator.credentials.get = options => new Promise((resolve, reject) => {
            options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
        });
    """)
    page.reload()
    expect(page.get_by_role('button',name='WAITING FOR PASSKEY…',exact=True)).to_be_visible()
    page.get_by_role('button',name='USE PASSWORD',exact=True).click()
    expect(page.get_by_role('button',name='SIGN IN WITH PASSKEY',exact=True)).to_be_enabled()
    password_login(page)
    assert page.request.get(origin+'/api/session').json()['authenticated']


def test_unsupported_browser_keeps_password_login(owner_page):
    page, origin = owner_page
    register(page)
    page.get_by_role('button',name='LOG OUT',exact=True).click()
    page.add_init_script("Object.defineProperty(window, 'PublicKeyCredential', {value: undefined});")
    page.reload()
    expect(page.locator('#password')).to_be_visible()
    expect(page.get_by_role('button',name='SIGN IN WITH PASSKEY',exact=True)).to_have_count(0)
    password_login(page)
