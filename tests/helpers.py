"""Shared test data and small helpers. Fixtures live in conftest.py."""
import base64
import hashlib
import io
import json
import secrets
import socket
import threading
import time
from contextlib import contextmanager

import cbor2
import uvicorn
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from pypdf import PdfReader

from corpus.app import create_app

TOPIC_FIELDS = [
    {'id': 'sbi-pretrain', 'label': 'SBI pretraining'},
    {'id': 'diff-compose', 'label': 'Diffusion composition'},
    {'id': 'unfiled', 'label': 'Unfiled'},
]

INBOX_TOKEN = 'inbox-test-token'
INBOX_FIELDS = [{'id': 'unfiled', 'label': 'Unfiled'}, {'id': 'physics', 'label': 'Physics'}]


def paper_body(slug, *, field='sbi-pretrain', rel=7, doi=None, arxiv_id=None, **extra):
    doi = doi or f'10.5555/{slug}'
    return {
        'title': f'Paper {slug} on posterior inference', 'authors': ['Ada Lovelace', 'Alan Turing'],
        'venue': 'ICML', 'year': 2024, 'abstract': f'Abstract for {slug}.',
        'canonical_url': f'https://doi.org/{doi}', 'doi': doi, 'arxiv_id': arxiv_id, 'field': field,
        'rel': rel, 'cred': 6, 'qual': 8, 'reimpl': 5, 'notes': f'Notes for {slug}',
        'tags': ['score-matching'], 'artifacts': [], **extra,
    }


def pdf_bytes():
    """A one-page, valid PDF whose extracted text is 'posterior collapse'."""
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
        b'<< /Length 59 >>\nstream\nBT\n/F1 12 Tf\n72 720 Td\n(posterior collapse) Tj\nET\nendstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    output = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(output)
    output.extend(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
    output.extend(b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets))
    output.extend(f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    data = bytes(output)
    assert 'posterior collapse' in PdfReader(io.BytesIO(data)).pages[0].extract_text()
    return data


# Inbox: agent proposals arrive through the ingest token.

def proposal(slug='regimes', **changes):
    return {'title': 'Instability boundaries in reaction diffusion equations', 'authors': ['Research Author'],
            'doi': '10.5555/' + slug, 'abstract': 'Generalization to unseen physical regimes.',
            'field': 'physics', 'tags': ['regimes'], 'state': 'reviewed', 'rel': 10, 'admission': 'approved',
            'artifacts': [{'kind': 'pdf', 'url': 'https://example.org/' + slug + '.pdf'},
                          {'kind': 'code', 'url': 'https://example.org/solver'}],
            'proposed_scores': {'rel': 9, 'reason': 'Measures transition boundaries across parameter space.'},
            **changes}


def ingest(client, paper, agent='automatic-test'):
    response = client.post('/api/ingest', headers={'Authorization': 'Bearer ' + INBOX_TOKEN},
                           json={'agent': agent, 'query': 'physical forecasting benchmarks', 'papers': [paper]})
    assert response.status_code == 200, response.text
    return response.json()


def decision(client, paper_id, action, **extra):
    return client.post('/api/inbox/decision', json={'id': paper_id, 'action': action, **extra})


# Review

def assessment(**changes):
    return {'stage': 'abstract', 'priority': 'high', 'role': 'evaluation', 'confidence': 'moderate',
            'explanation': 'The abstract reports transfer across physical regimes.',
            'limitation': 'The information supplied to the model is unclear.',
            'questions': [], **changes}


def ai_result(topics=('physics',), **changes):
    """A well-formed Claude review, as the model returns it."""
    return {'digest': {'tldr': 'Maps where reaction-diffusion solvers stop generalizing.',
                       'contributions': ['A benchmark of unseen physical regimes.', 'A boundary-detection metric.'],
                       'method': 'Trains surrogates on one regime and tests on others.',
                       'results': 'Reports error growth beyond the training regime.',
                       'limitations': 'Only two equation families.', 'kind': 'benchmark'},
            'topics': [{'topic': topic, 'relevance': 8, 'priority': 'high', 'role': 'evaluation', 'confidence': 'moderate',
                        'explanation': 'The abstract evaluates generalization to unseen physical regimes.',
                        'questions': ['Which regimes are held out?']} for topic in topics],
            'suggested_topics': [], 'scores': {'cred': 6, 'qual': 7, 'reimpl': 5},
            'score_reasons': {'cred': 'Abstract only.', 'qual': 'Needs methods inspection.', 'reimpl': 'No code details.'},
            'evidence': [], 'limitation': 'Experiments were not inspected.', **changes}


def paper(client, pid):
    return client.get('/api/paper', params={'id': pid}).json()


# Passkeys

ORIGIN = 'https://corpus.example'


def encoded(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


class Authenticator:
    """A software authenticator, including zero-counter synced passkeys."""
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.user_handle = None

    def registration(self, options, *, verified=True, origin=ORIGIN):
        self.user_handle = options['user']['id']
        public = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1:2, 3:-7, -1:1, -2:public.x.to_bytes(32,'big'), -3:public.y.to_bytes(32,'big')})
        flags = 0x59 | (0x04 if verified else 0)
        auth_data = hashlib.sha256(options['rp']['id'].encode()).digest() + bytes([flags]) + bytes(4)
        auth_data += bytes(16) + len(self.credential_id).to_bytes(2,'big') + self.credential_id + cose
        client = json.dumps({'type':'webauthn.create','challenge':options['challenge'],'origin':origin,'crossOrigin':False}).encode()
        return {
            'id':encoded(self.credential_id), 'rawId':encoded(self.credential_id), 'type':'public-key',
            'response':{'attestationObject':encoded(cbor2.dumps({'fmt':'none','attStmt':{},'authData':auth_data})),
                        'clientDataJSON':encoded(client),'transports':['internal','hybrid']},
        }

    def assertion(self, options, *, count=0, verified=True, present=True, origin=ORIGIN,
                  rp_id=None, challenge=None, cross_origin=False, user_handle=None):
        client = json.dumps({'type':'webauthn.get','challenge':challenge or options['challenge'],
                             'origin':origin,'crossOrigin':cross_origin}).encode()
        flags = 0x18 | (0x04 if verified else 0) | (0x01 if present else 0)
        auth_data = hashlib.sha256((rp_id or options['rpId']).encode()).digest() + bytes([flags]) + count.to_bytes(4,'big')
        signature = self.key.sign(auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            'id':encoded(self.credential_id), 'rawId':encoded(self.credential_id), 'type':'public-key',
            'response':{'clientDataJSON':encoded(client),'authenticatorData':encoded(auth_data),
                        'signature':encoded(signature),'userHandle':user_handle or self.user_handle},
        }


# Browser

@contextmanager
def serve(settings_for, setup=None):
    """Run the app on a free local port. settings_for(origin) builds its Settings; setup(app) may patch the app."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    origin = f'http://localhost:{port}'
    app = create_app(settings_for(origin))
    if setup:
        setup(app)
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(.02)
    assert server.started
    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(5)


@contextmanager
def open_page(browser, width=1440, height=900):
    """A fresh browser context whose page must finish without JavaScript errors."""
    context = browser.new_context(viewport={'width': width, 'height': height})
    page = context.new_page()
    page.set_default_timeout(7000)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        yield page
        assert not errors
    finally:
        context.close()
