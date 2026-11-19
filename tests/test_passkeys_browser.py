"""Exercise native browser WebAuthn with a virtual platform authenticator."""
import pytest
from playwright.sync_api import expect

from corpus.config import Settings
from helpers import open_page, serve

PASSWORD = 'browser-owner-password'


@pytest.fixture
def owner_page(tmp_path, browser):
    """Logged-out owner page with a virtual platform authenticator; yields (page, origin)."""
    settings_for = lambda origin: Settings(data_dir=tmp_path, password=PASSWORD, pdf_jobs=False, passkey_origin=origin)
    with serve(settings_for) as origin, open_page(browser) as page:
        cdp = page.context.new_cdp_session(page)
        cdp.send('WebAuthn.enable')
        cdp.send('WebAuthn.addVirtualAuthenticator', {'options':{
            'protocol':'ctap2', 'transport':'internal', 'hasResidentKey':True,
            'hasUserVerification':True, 'isUserVerified':True, 'automaticPresenceSimulation':True,
            'defaultBackupEligibility':True, 'defaultBackupState':True,
        }})
        page.goto(origin)
        yield page, origin


def password_login(page):
    page.locator('#password').fill(PASSWORD)
    page.get_by_role('button',name='ENTER',exact=True).click()
    expect(page.get_by_role('button',name='LOG OUT',exact=True)).to_be_visible()


def register(page):
    password_login(page)
    page.get_by_role('button',name='ACCOUNT',exact=True).click()
    page.locator('#passkey-password').fill(PASSWORD)
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
