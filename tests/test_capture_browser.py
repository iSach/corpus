from urllib.parse import parse_qs,urlsplit
from playwright.sync_api import expect


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
    projects=page.get_by_role('complementary',name='Topics to add papers to')
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
    expect(page.get_by_role('complementary',name='Topics to add papers to').get_by_role('checkbox',name='Diffusion composition',exact=True)).to_be_checked()
