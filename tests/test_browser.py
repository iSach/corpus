"""Browser regressions against an isolated real server and SQLite database."""

from playwright.sync_api import expect


def test_slow_query_typing_preserves_focus_and_pdf_hits(page):
    query=page.get_by_role('searchbox',name='Search query')
    query.press_sequentially('Alpha',delay=220)
    expect(query).to_have_value('Alpha')
    expect(query).to_be_focused()
    expect(page.locator('.paper-row')).to_have_count(1)
    expect(page.locator('.detail-title')).to_have_text('Alpha scientific posterior')
    query.fill('pdf:"posterior collapse"')
    expect(page.locator('.pdf-hit mark')).to_have_text('posterior collapse')
    assert 'p.4' in page.locator('.pdf-hit').inner_text()
    query.fill('field:')
    expect(page.locator('.notice')).to_contain_text('Expected a value')
    expect(page.locator('.paper-row')).to_have_count(1)
    assert page.locator('.detail-pane script').count()==0


def test_topic_facets_show_overlap_with_ticked_topics(page):
    page.get_by_role('link',name='Add',exact=True).click()
    page.get_by_role('button',name='Manual entry',exact=True).click()
    page.locator('#form-title').fill('Delta shared between topics')
    topics=page.get_by_role('group',name='Paper topics')
    topics.get_by_role('checkbox',name='SBI pretraining',exact=True).check()
    topics.get_by_role('checkbox',name='Diffusion composition',exact=True).check()
    page.get_by_role('button',name='SAVE',exact=True).click()
    expect(page.locator('.detail-title')).to_have_text('Delta shared between topics')
    page.get_by_role('link',name='Library',exact=True).click()
    expect(page.locator('.paper-row')).to_have_count(4)
    rail=page.locator('.facet-rail')
    sbi=rail.locator('.facet-option',has_text='SBI pretraining')
    diff=rail.locator('.facet-option',has_text='Diffusion composition')
    unfiled=rail.locator('.facet-option',has_text='Unfiled')
    expect(sbi.locator('.facet-count')).to_have_text('3')
    expect(diff.locator('.facet-count')).to_have_text('2')
    diff.locator('input').check()
    expect(page.locator('.paper-row')).to_have_count(2)
    # The ticked topic keeps its plain count; others show how many of their papers it shares.
    expect(diff.locator('.facet-count')).to_have_text('2')
    expect(sbi.locator('.facet-count')).to_have_text('1 / 3')
    expect(sbi).to_have_attribute('title','1 of 3 papers match the current filters')
    expect(sbi).not_to_have_class('facet-option facet-option-empty')
    expect(unfiled).to_have_class('facet-option facet-option-empty')
    expect(unfiled).to_have_attribute('title','No matching papers')
    diff.locator('input').uncheck()
    expect(page.locator('.paper-row')).to_have_count(4)
    expect(sbi.locator('.facet-count')).to_have_text('3')
    expect(sbi).to_have_attribute('title','')


def test_triage_save_and_export(page,site):
    page.get_by_role('searchbox',name='Search query').fill('Beta')
    expect(page.locator('.paper-row')).to_have_count(1)
    expect(page.locator('.detail-title')).to_have_text('Beta local diffusion')
    page.get_by_role('button',name='EDIT',exact=True).click()
    expect(page.locator('.entry-form')).to_be_visible()
    page.get_by_role('button',name='ACCEPT DRAFT SCORES',exact=True).click()
    page.locator('#form-notes').fill('Triaged in browser')
    page.get_by_role('button',name='SAVE & NEXT UNREVIEWED',exact=False).click()
    expect(page.locator('[data-focus-key="form-title"]')).to_have_value('Gamma neural inference')
    response=page.request.get(site+'/api/papers',params={'q':'Beta state:reviewed rel:8'})
    assert response.json()['total']==1
    page.get_by_role('link',name='Export',exact=True).click()
    expect(page.locator('.export-paper')).to_have_count(3)
    expect(page.locator('.export-output .export-count')).to_contain_text('3 entries')
    page.get_by_role('button',name='JSON',exact=True).click()
    expect(page.locator('.preview')).not_to_contain_text('Triaged in browser')
    page.get_by_role('checkbox',name='notes',exact=True).check()
    expect(page.locator('.preview')).to_contain_text('Triaged in browser')
    with page.expect_download() as download:
        page.get_by_role('button',name='DOWNLOAD .json',exact=True).click()
    assert download.value.suggested_filename=='corpus.json'
