import json
from urllib.parse import parse_qs,urlsplit
from playwright.sync_api import expect

from test_browser import browser,site,page


def found(slug='dynamics',title='Learning physical regimes from short contexts'):
    return {'title':title,'authors':['Ada Researcher','Another Author'],'year':2026,'venue':'Test Proceedings',
            'abstract':'Transfer across physical parameters. <script>alert(1)</script>',
            'doi':'10.5555/capture-'+slug,'arxiv_id':None,'canonical_url':'https://doi.org/10.5555/capture-'+slug,
            'artifacts':[],'existing':None}


def mock_search(page,records=None):
    records=records or [found()]
    def search(route):
        params=parse_qs(urlsplit(route.request.url).query)
        source=params['source'][0]
        if source=='library': return route.continue_()
        route.fulfill(json={'papers':records,'source':source,'has_more':False,'cached':False})
    page.route('**/api/catalog/search?*',search)


def query(page,text='physical regimes'):
    page.get_by_role('link',name='Add',exact=True).click()
    page.get_by_role('searchbox',name='Find papers',exact=True).fill(text)
    page.get_by_role('button',name='SEARCH',exact=True).click()
    expect(page.locator('.capture-result')).to_have_count(1)


def test_search_adds_once_to_multiple_projects_and_remembers_choices(page,site):
    mock_search(page)
    query(page)
    projects=page.get_by_role('complementary',name='Projects to add papers to')
    projects.get_by_role('checkbox',name='Diffusion composition',exact=True).check()
    page.get_by_role('button',name='Add '+found()['title'],exact=True).click()
    expect(page.get_by_role('button',name='Already filed '+found()['title'],exact=True)).to_be_disabled()
    papers=page.request.get(site+'/api/papers',params={'q':'project:diff-compose'}).json()['papers']
    saved=next(p for p in papers if p['doi']==found()['doi'])
    assert set(saved['projects'])=={'sbi-pretrain','diff-compose'}
    assert saved['state']=='unreviewed' and saved['rel'] is None
    expect(page.locator('.capture-preview').get_by_text(found()['abstract'],exact=True)).to_be_visible()
    assert page.locator('.capture-preview script').count()==0
    page.reload()
    expect(page.get_by_role('complementary',name='Projects to add papers to').get_by_role('checkbox',name='Diffusion composition',exact=True)).to_be_checked()


def test_bulk_paste_deduplicates_links_and_shows_failed_lookups(page,site):
    papers=[found('first'),found('second','Numerical methods for astronomical inverse problems')]
    page.route('**/api/catalog/resolve',lambda route:route.fulfill(json={'results':[{'input':'first','paper':papers[0]},{'input':'duplicate','paper':papers[0]},{'input':'second','paper':papers[1]},{'input':'bad','error':'No identifier found'}]}))
    page.get_by_role('link',name='Add',exact=True).click()
    page.get_by_role('button',name='Paste links',exact=True).click()
    page.locator('#capture-links').fill('2501.00001\nhttps://arxiv.org/abs/2501.00001\n10.5555/second\nbad')
    page.get_by_role('button',name='FIND PAPERS',exact=True).click()
    expect(page.locator('.capture-result')).to_have_count(2)
    expect(page.get_by_role('alert')).to_contain_text('No identifier found')
    page.get_by_role('button',name='ADD SELECTED (2)',exact=True).click()
    expect(page.get_by_role('button',name='ADD SELECTED (0)',exact=True)).to_be_disabled()
    assert page.request.get(site+'/api/papers').json()['library_total']==5


def test_fast_results_remain_usable_when_another_source_is_slow(page,site):
    def search(route):
        source=parse_qs(urlsplit(route.request.url).query)['source'][0]
        if source=='library':return route.continue_()
        if source=='arxiv':return route.fulfill(status=502,json={'detail':'Temporarily unavailable'})
        route.fulfill(json={'papers':[found()],'source':source,'has_more':False})
    page.route('**/api/catalog/search?*',search)
    query(page)
    page.get_by_role('button',name='Add '+found()['title'],exact=True).click()
    expect(page.get_by_role('button',name='Already filed '+found()['title'],exact=True)).to_be_disabled()


def test_mobile_project_picker_and_inline_abstract(page,site):
    mock_search(page)
    page.set_viewport_size({'width':390,'height':844})
    query(page)
    page.get_by_role('button',name='PROJECTS (1)',exact=True).click()
    page.get_by_role('complementary',name='Projects to add papers to').get_by_role('checkbox',name='Diffusion composition',exact=True).check()
    page.get_by_role('button',name='PROJECTS (2)',exact=True).click()
    page.get_by_role('button',name='Inspect '+found()['title'],exact=True).click()
    expect(page.locator('.capture-mobile-abstract')).to_contain_text('Transfer across physical parameters.')
    assert page.evaluate('document.documentElement.scrollWidth')<=390
    page.get_by_role('button',name='Add '+found()['title'],exact=True).click()
    expect(page.get_by_role('button',name='Already filed '+found()['title'],exact=True)).to_be_disabled()


def test_bookmark_capture_prefills_link_and_requires_an_explicit_add(page,site):
    paper=found()
    page.context.route('**/api/catalog/resolve',lambda route:route.fulfill(json={'results':[{'input':'source','paper':paper}]}))
    page.get_by_role('link',name='Add',exact=True).click()
    bookmark=page.get_by_role('link',name='SAVE TO CORPUS',exact=True).get_attribute('href')
    assert bookmark.startswith('javascript:')
    source=page.context.new_page()
    source.route('https://example.org/article',lambda route:route.fulfill(content_type='text/html',body='<title>A paper page</title><p>Paper.</p>'))
    source.goto('https://example.org/article')
    with source.expect_popup() as opened:
        source.evaluate(bookmark[len('javascript:'):])
    capture=opened.value
    expect(capture.get_by_role('searchbox',name='Find papers')).to_have_value('https://example.org/article')
    expect(capture.locator('.capture-result')).to_have_count(1)
    assert capture.request.get(site+'/api/papers').json()['library_total']==3
    capture.get_by_role('button',name='Add '+paper['title'],exact=True).click()
    expect(capture.get_by_role('button',name='Already filed '+paper['title'],exact=True)).to_be_disabled()
    source.close();capture.close()


def test_saved_paper_without_identifiers_can_be_filed_without_network(page,site):
    page.get_by_role('link',name='Add',exact=True).click()
    page.get_by_role('searchbox',name='Find papers').fill('Alpha scientific posterior')
    page.route('**/api/catalog/search?*',lambda route:route.continue_() if 'source=library' in route.request.url else route.fulfill(json={'papers':[],'has_more':False}))
    page.get_by_role('button',name='SEARCH',exact=True).click()
    expect(page.locator('.capture-result')).to_have_count(1)
    picker=page.get_by_role('complementary',name='Projects to add papers to')
    picker.get_by_role('checkbox',name='Diffusion composition',exact=True).check()
    page.get_by_role('button',name='Add Alpha scientific posterior',exact=True).click()
    expect(page.get_by_role('button',name='Already filed Alpha scientific posterior',exact=True)).to_be_disabled()
    assert page.request.get(site+'/api/papers').json()['library_total']==3


def test_bookmark_title_fallback_searches_when_link_has_no_identifier(page,site):
    from urllib.parse import urlencode
    mock_search(page)
    page.route('**/api/catalog/resolve',lambda route:route.fulfill(json={'results':[{'input':'link','error':'No identifier'}]}))
    page.goto(site+'/?'+urlencode({'screen':'add','url':'https://openreview.net/forum?id=example','title':found()['title']+' | OpenReview'}))
    expect(page.get_by_role('searchbox',name='Find papers')).to_have_value(found()['title'])
    expect(page.locator('.capture-result')).to_have_count(1)
    assert page.request.get(site+'/api/papers').json()['library_total']==3
