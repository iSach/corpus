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
    public_origin: str = field(default_factory=lambda: os.getenv('CORPUS_PUBLIC_ORIGIN',''))
    admin_email: str = field(default_factory=lambda: os.getenv('CORPUS_ADMIN_EMAIL',''))
    smtp_host: str = field(default_factory=lambda: os.getenv('CORPUS_SMTP_HOST',''))
    smtp_port: int = field(default_factory=lambda: int(os.getenv('CORPUS_SMTP_PORT','587')))
    smtp_username: str = field(default_factory=lambda: os.getenv('CORPUS_SMTP_USERNAME',''))
    smtp_password: str = field(default_factory=lambda: os.getenv('CORPUS_SMTP_PASSWORD',''))
    smtp_from: str = field(default_factory=lambda: os.getenv('CORPUS_SMTP_FROM',''))
    smtp_security: str = field(default_factory=lambda: os.getenv('CORPUS_SMTP_SECURITY','starttls'))
    dev: bool = field(default_factory=lambda: os.getenv('CORPUS_DEV','0')=='1')
    ai_reviews: bool = field(default_factory=lambda: os.getenv('CORPUS_AI_REVIEWS','1')!='0')
    # 'auto' uses the API key when set, else the Claude subscription token.
    ai_backend: str = field(default_factory=lambda: os.getenv('CORPUS_AI_BACKEND','auto'))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv('ANTHROPIC_API_KEY',''), repr=False)
    claude_oauth_token: str = field(default_factory=lambda: os.getenv('CLAUDE_CODE_OAUTH_TOKEN',''), repr=False)
    claude_bin: str = field(default_factory=lambda: os.getenv('CORPUS_CLAUDE_BIN','claude'))
    ai_model: str = field(default_factory=lambda: os.getenv('CORPUS_AI_MODEL','claude-opus-5-5'))
    ai_min_interval: float = field(default_factory=lambda: float(os.getenv('CORPUS_AI_MIN_INTERVAL','180')))
    ai_daily_limit: int = field(default_factory=lambda: int(os.getenv('CORPUS_AI_DAILY_LIMIT','30')))
    # Member libraries are copies of the owner's settings with this unset: a
    # personal Claude subscription only ever reviews the owner's own library.
    owner_library: bool = True
    ai_poll_interval: float = 30.0
    # Queue pacing state, shared by every library (members inherit the owner's path).
    ai_state_file: Path | None = None
    pdf_jobs: bool = True
    venue_checks: bool = field(default_factory=lambda: os.getenv('CORPUS_VENUE_CHECKS','1')!='0')
    venue_check_interval: float = field(default_factory=lambda: float(os.getenv('CORPUS_VENUE_CHECK_INTERVAL','5')))
    s2_api_key: str = field(default_factory=lambda: os.getenv('CORPUS_S2_API_KEY',''))
    openalex_api_key: str = field(default_factory=lambda: os.getenv('CORPUS_OPENALEX_API_KEY',''))
    fields: list = field(default_factory=lambda: json.loads(os.getenv('CORPUS_FIELDS','[{"id":"unfiled","label":"Unfiled"}]')))
    def __post_init__(self):
        self.passkey_origin = self.passkey_origin.rstrip('/')
        self.public_origin = self.public_origin.rstrip('/')
        if self.public_origin:
            origin = urlsplit(self.public_origin)
            if (not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment
                    or (origin.scheme != 'https' and not (origin.scheme == 'http' and origin.hostname in ('localhost','127.0.0.1','::1')))):
                raise ValueError('CORPUS_PUBLIC_ORIGIN must be an HTTPS origin (or local HTTP for development)')
            origin.port
        if self.ai_backend not in ('auto','api','cli','off'):
            raise ValueError('CORPUS_AI_BACKEND must be auto, api, cli or off')
        if self.ai_min_interval < 0 or self.ai_daily_limit < 0:
            raise ValueError('AI review pacing cannot be negative')
        if self.smtp_security not in ('starttls','ssl'):
            raise ValueError('CORPUS_SMTP_SECURITY must be starttls or ssl')
        if self.passkey_origin:
            origin = urlsplit(self.passkey_origin)
            local = origin.hostname == 'localhost'
            if (not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment
                    or (origin.scheme != 'https' and not (origin.scheme == 'http' and local))):
                raise ValueError('CORPUS_PASSKEY_ORIGIN must be an HTTPS origin (or http://localhost for development)')
            origin.port  # Validate malformed ports at startup.
        self.data_dir = Path(self.data_dir).resolve()
        self.ai_state_file = Path(self.ai_state_file or self.data_dir/'ai-queue.json')
        self.cache_dir = Path(self.cache_dir or os.getenv('CORPUS_PDF_CACHE',self.data_dir/'pdfs')).resolve()
        if not 0.5 <= self.dedupe_threshold <= 1: raise ValueError('Dedupe threshold must be 0.5–1')
        if self.cache_max_bytes < 0: raise ValueError('Cache size cannot be negative')
        if not self.fields or any(not isinstance(f,dict) or not f.get('id') or not f.get('label') for f in self.fields):
            raise ValueError('CORPUS_FIELDS must be a nonempty list of {id,label}')
    @property
    def database(self): return self.data_dir/'corpus.sqlite3'
    @property
    def passkey_rp_id(self): return urlsplit(self.passkey_origin).hostname if self.passkey_origin else None
