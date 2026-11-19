"""Daily review with an actual server, worker, database, and browser."""
import time

from playwright.sync_api import expect


def test_full_text_comparison_and_approval_evidence(review_page):
    page,site,pid=review_page
    expect(page.locator('.review-explanation')).to_have_text('The abstract reports transfer across physical regimes.')
    expect(page.locator('.review-limitation')).to_contain_text('information supplied')
    expect(page.get_by_role('button',name='APPROVE FROM ABSTRACT',exact=True)).to_be_visible()
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        status=page.request.get(site+'/api/paper',params={'id':pid}).json()['review']['check']['status']
        if status=='ready': break
        time.sleep(.1)
    assert status=='ready'
    page.get_by_role('button',name='REFRESH',exact=True).click()
    page.get_by_role('button',name='RECORD FULL-TEXT CHECK',exact=True).click()
    expect(page.locator('.review-page-text')).to_contain_text('posterior collapse')
    page.locator('#assessment-priority').select_option('medium')
    page.locator('#assessment-role').select_option('supporting')
    page.locator('#assessment-confidence').select_option('high')
    page.locator('#assessment-explanation').fill('The checked sections provide supporting diagnostic evidence.')
    page.locator('#assessment-limitation').fill('This fixture does not establish long-horizon accuracy.')
    page.locator('#assessment-sections').fill('Results')
    page.locator('#assessment-quote').fill('posterior collapse')
    page.get_by_role('button',name='SAVE ASSESSMENT',exact=True).click()
    expect(page.locator('.review-explanation')).to_have_text('The checked sections provide supporting diagnostic evidence.')
    expect(page.locator('.evidence-level')).to_have_text('Full text checked')
    page.locator('.review-comparison summary').click()
    expect(page.locator('.abstract-assessment')).to_have_text('The abstract reports transfer across physical regimes.')
    expect(page.locator('.review-change')).to_contain_text('High → Medium')
    page.get_by_role('button',name='REFRESH',exact=True).click()
    expect(page.locator('.abstract-assessment')).to_be_visible()
    page.get_by_role('button',name='APPROVE & NEXT',exact=True).click()
    expect(page.get_by_role('button',name='Approved (1)',exact=True)).to_be_visible()
    approved=page.request.get(site+'/api/paper',params={'id':pid}).json()
    assert approved['admission']=='approved' and approved['state']=='unreviewed'
    assert approved['last_decision']['assessment_id']==approved['review']['current']['id']
