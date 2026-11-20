"""A real browser follows the admin invitation and member onboarding flow."""
import pytest
from playwright.sync_api import expect

from corpus.config import Settings
from helpers import open_page, serve

PASSWORD = 'browser-admin-password'


@pytest.fixture
def account_site(tmp_path):
    with serve(lambda origin: Settings(data_dir=tmp_path, password=PASSWORD, pdf_jobs=False, public_origin=origin)) as origin:
        yield origin


def test_admin_invites_friend_who_sets_password_and_gets_private_library(account_site, browser, tmp_path):
    with open_page(browser) as admin, open_page(browser, 390, 844) as friend:
        admin.goto(account_site)
        admin.locator('#password').fill(PASSWORD)
        admin.get_by_role('button',name='ENTER',exact=True).click()
        admin.get_by_role('button',name='ADMIN',exact=True).click()
        expect(admin.get_by_role('heading',name='People & services')).to_be_visible()
        # The credential handoff works from a phone without showing the key.
        admin.set_viewport_size({'width':390,'height':844})
        admin.route('**/api/admin/mail/resend', lambda route: route.fulfill(status=200,content_type='application/json',body='{"saved":true}'))
        key_input = admin.get_by_label('RESEND API KEY',exact=True)
        assert key_input.get_attribute('type') == 'password'
        key_input.fill('re_browser_setup_test_key')
        admin.get_by_role('button',name='SAVE RESEND KEY',exact=True).click()
        expect(key_input).to_have_value('')
        expect(admin.get_by_text('Your Resend key is saved securely.',exact=False)).to_be_visible()
        admin.set_viewport_size({'width':1440,'height':900})
        admin.get_by_label('FRIEND’S EMAIL').fill('friend@example.org')
        admin.get_by_role('button',name='CREATE INVITATION LINK',exact=True).click()
        link = admin.get_by_role('textbox',name='Invitation link')
        expect(link).to_be_visible()
        url = link.input_value()
        assert '#invite=' in url
        assert 'mailto:friend%40example.org' in admin.get_by_role('link',name='OPEN IN EMAIL APP').get_attribute('href')
        admin.screenshot(path=str(tmp_path/'admin.png'))
        friend.goto(url)
        expect(friend.get_by_role('heading',name='Your research library awaits')).to_be_visible()
        assert '#invite=' not in friend.url
        friend.get_by_label('PASSWORD',exact=True).fill('friend-private-password')
        friend.get_by_label('CONFIRM PASSWORD',exact=True).fill('a-different-password')
        friend.get_by_role('button',name='CREATE MY ACCOUNT',exact=True).click()
        expect(friend.get_by_role('alert')).to_contain_text('passwords do not match')
        friend.get_by_label('CONFIRM PASSWORD',exact=True).fill('friend-private-password')
        friend.get_by_role('button',name='CREATE MY ACCOUNT',exact=True).click()
        expect(friend.get_by_role('button',name='LOG OUT',exact=True)).to_be_visible()
        expect(friend.get_by_role('button',name='ADMIN',exact=True)).to_have_count(0)
        assert friend.request.get(account_site+'/api/papers').json()['total'] == 0
        friend.get_by_role('button',name='ACCOUNT',exact=True).click()
        expect(friend.get_by_label('EMAIL',exact=True)).to_have_value('friend@example.org')
        friend.get_by_role('button',name='LOG OUT',exact=True).click()
        expect(friend.get_by_label('EMAIL',exact=True)).to_have_value('friend@example.org')
        friend.locator('#password').fill('friend-private-password')
        friend.get_by_role('button',name='ENTER',exact=True).click()
        expect(friend.get_by_role('heading',name='Your account')).to_be_visible()
        friend.get_by_role('button',name='GENERATE TOKEN',exact=True).click()
        secret = friend.locator('#ingest-token-value').input_value()
        assert secret.startswith('cps_')
        saved = friend.request.post(account_site+'/api/ingest',headers={'Authorization':'Bearer '+secret},data={'papers':[{'title':'Saved from the extension'}]})
        assert saved.json()['queued'] == 1
        friend.once('dialog', lambda dialog: dialog.accept())
        friend.get_by_role('button',name='REVOKE',exact=True).click()
        expect(friend.get_by_role('button',name='GENERATE TOKEN',exact=True)).to_be_visible()
        expect(friend.get_by_role('heading',name='Passkeys')).to_be_visible()
        friend.get_by_label('THEME').select_option('oled')
        assert friend.locator('html').get_attribute('data-theme') == 'oled'
        friend.reload()
        assert friend.locator('html').get_attribute('data-theme') == 'oled'
        expect(friend.get_by_label('THEME')).to_have_value('oled')
        friend.get_by_label('THEME').select_option('midnight')
        assert friend.locator('html').get_attribute('data-theme') == 'midnight'
        admin.reload()
        expect(admin.locator('.account-row').filter(has_text='friend@example.org').first).to_contain_text('Active')
        admin.get_by_role('button',name='PASSWORD RESET LINK',exact=True).click()
        expect(admin.get_by_role('textbox',name='Invitation link')).to_be_visible()
        friend.goto(url)
        expect(friend.get_by_role('alert')).to_contain_text('invalid, expired, or already used')


def test_admin_sets_claude_keys_and_switches_people(account_site, browser, tmp_path):
    with open_page(browser) as admin:
        admin.goto(account_site)
        admin.locator('#password').fill(PASSWORD)
        admin.get_by_role('button',name='ENTER',exact=True).click()
        admin.get_by_role('button',name='ADMIN',exact=True).click()
        expect(admin.get_by_text('Claude reviews are off for your library',exact=False)).to_be_visible()
        token = admin.get_by_label('CLAUDE SUBSCRIPTION TOKEN',exact=True)
        assert token.get_attribute('type') == 'password'
        token.fill('sk-ant-oat01-browser-token')
        token.press('Enter')
        expect(admin.get_by_text('Your library is reviewed with your Claude subscription',exact=False)).to_be_visible()
        expect(token).to_have_value('')
        assert 'sk-ant-oat01' not in admin.content()
        admin.get_by_label('REVIEWS PER DAY (0: NO LIMIT)',exact=True).fill('5')
        admin.get_by_role('button',name='SAVE CLAUDE SETTINGS',exact=True).click()
        expect(admin.get_by_role('button',name='USE SERVER DEFAULTS',exact=True)).to_be_visible()
        assert admin.request.get(account_site+'/api/admin/services').json()['settings']['ai_daily_limit']['value'] == 5
        expect(admin.get_by_role('button',name='CLAUDE: ON',exact=True)).to_be_visible()
        admin.get_by_role('button',name='CLAUDE: ON',exact=True).click()
        expect(admin.get_by_role('button',name='CLAUDE: OFF',exact=True)).to_be_visible()
        # Revoking the only link of someone who never joined removes them from People.
        admin.get_by_label('FRIEND’S EMAIL').fill('maybe@example.org')
        admin.get_by_role('button',name='CREATE INVITATION LINK',exact=True).click()
        expect(admin.get_by_text('Invited · waiting for setup')).to_be_visible()
        admin.get_by_role('button',name='REVOKE',exact=True).click()
        expect(admin.get_by_text('Invited · waiting for setup')).to_have_count(0)
        expect(admin.get_by_text('No pending invitations.')).to_be_visible()
        admin.screenshot(path=str(tmp_path/'services.png'), full_page=True)
