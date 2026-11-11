from dataclasses import dataclass, field
from pathlib import Path
import os, json
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv('CORPUS_DATA_DIR', ROOT/'data')).resolve())
    cache_dir: Path | None = None
    cache_max_bytes: int = field(default_factory=lambda: int(os.getenv('CORPUS_CACHE_MAX_BYTES',str(10*1024**3))))
    pdf_max_bytes: int = 50*1024**2
    dedupe_threshold: float = field(default_factory=lambda: float(os.getenv('CORPUS_DEDUPE_THRESHOLD','0.91')))
    password: str = field(default_factory=lambda: os.getenv('CORPUS_PASSWORD',''))
    ingest_token: str = field(default_factory=lambda: os.getenv('CORPUS_INGEST_TOKEN',''))
    sql_local_only: bool = field(default_factory=lambda: os.getenv('CORPUS_SQL_LOCAL_ONLY','0')=='1')
    secure_cookie: bool = field(default_factory=lambda: os.getenv('CORPUS_SECURE_COOKIE','0')=='1')
    passkey_origin: str = field(default_factory=lambda: os.getenv('CORPUS_PASSKEY_ORIGIN',''))
    dev: bool = field(default_factory=lambda: os.getenv('CORPUS_DEV','0')=='1')
    pdf_jobs: bool = True
    venue_checks: bool = field(default_factory=lambda: os.getenv('CORPUS_VENUE_CHECKS','1')!='0')
    venue_check_interval: float = field(default_factory=lambda: float(os.getenv('CORPUS_VENUE_CHECK_INTERVAL','5')))
    s2_api_key: str = field(default_factory=lambda: os.getenv('CORPUS_S2_API_KEY',''))
    openalex_api_key: str = field(default_factory=lambda: os.getenv('CORPUS_OPENALEX_API_KEY',''))
    fields: list = field(default_factory=lambda: json.loads(os.getenv('CORPUS_FIELDS','[{"id":"unfiled","label":"Unfiled"}]')))
    def __post_init__(self):
        self.passkey_origin = self.passkey_origin.rstrip('/')
        if self.passkey_origin:
            origin = urlsplit(self.passkey_origin)
            local = origin.hostname == 'localhost'
            if (not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment
                    or (origin.scheme != 'https' and not (origin.scheme == 'http' and local))):
                raise ValueError('CORPUS_PASSKEY_ORIGIN must be an HTTPS origin (or http://localhost for development)')
            origin.port  # Validate malformed ports at startup.
        self.data_dir = Path(self.data_dir).resolve()
        self.cache_dir = Path(self.cache_dir or os.getenv('CORPUS_PDF_CACHE',self.data_dir/'pdfs')).resolve()
        if not 0.5 <= self.dedupe_threshold <= 1: raise ValueError('Dedupe threshold must be 0.5–1')
        if self.cache_max_bytes < 0: raise ValueError('Cache size cannot be negative')
        if not self.fields or any(not isinstance(f,dict) or not f.get('id') or not f.get('label') for f in self.fields):
            raise ValueError('CORPUS_FIELDS must be a nonempty list of {id,label}')
    @property
    def database(self): return self.data_dir/'corpus.sqlite3'
    @property
    def passkey_rp_id(self): return urlsplit(self.passkey_origin).hostname if self.passkey_origin else None
