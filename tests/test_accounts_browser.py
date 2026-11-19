"""A real browser follows the admin invitation and member onboarding flow."""
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright, expect
from corpus.app import create_app
from corpus.config import Settings

PASSWORD = 'browser-admin-password'


@pytest.fixture
def account_site(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
    origin = f'http://localhost:{port}'
    settings = Settings(data_dir=tmp_path,password=PASSWORD,pdf_jobs=False,public_origin=origin)
    server = uvicorn.Server(uvicorn.Config(create_app(settings),host='127.0.0.1',port=port,log_level='error'))
    thread = threading.Thread(target=server.run,daemon=True); thread.start()
    for _ in range(100):
        if server.started: break
        time.sleep(.02)
    assert server.started
    yield origin
    server.should_exit = True; thread.join(5)


def test_admin_invites_friend_who_sets_password_and_gets_private_library(account_site, tmp_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True,args=['--no-sandbox'])
        owner_context = browser.new_context(viewport={'width':1440,'height':900})
        friend_context = browser.new_context(viewport={'width':390,'height':844})
        admin = owner_context.new_page(); friend = friend_context.new_page()
        errors = []
        admin.on('pageerror',lambda error:errors.append(str(error)))
        friend.on('pageerror',lambda error:errors.append(str(error)))
        admin.goto(account_site)
        admin.locator('#password').fill(PASSWORD)
        admin.get_by_role('button',name='ENTER',exact=True).click()
        admin.get_by_role('button',name='PEOPLE',exact=True).click()
        expect(admin.get_by_role('heading',name='People & invitations')).to_be_visible()
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
        expect(friend.get_by_role('button',name='PEOPLE',exact=True)).to_have_count(0)
        assert friend.request.get(account_site+'/api/papers').json()['total'] == 0
        friend.get_by_role('button',name='ACCOUNT',exact=True).click()
        expect(friend.get_by_label('EMAIL',exact=True)).to_have_value('friend@example.org')
        friend.get_by_role('button',name='LOG OUT',exact=True).click()
        expect(friend.get_by_label('EMAIL',exact=True)).to_have_value('friend@example.org')
        friend.locator('#password').fill('friend-private-password')
        friend.get_by_role('button',name='ENTER',exact=True).click()
        expect(friend.get_by_role('heading',name='Your account')).to_be_visible()
        admin.reload()
        expect(admin.locator('.account-row').filter(has_text='friend@example.org').first).to_contain_text('Active')
        admin.get_by_role('button',name='PASSWORD RESET LINK',exact=True).click()
        expect(admin.get_by_role('textbox',name='Invitation link')).to_be_visible()
        friend.goto(url)
        expect(friend.get_by_role('alert')).to_contain_text('invalid, expired, or already used')
        assert not errors
        browser.close()
