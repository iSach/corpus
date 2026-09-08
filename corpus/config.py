from dataclasses import dataclass, field
from pathlib import Path
import os, json

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
    dev: bool = field(default_factory=lambda: os.getenv('CORPUS_DEV','0')=='1')
    pdf_jobs: bool = True
    fields: list = field(default_factory=lambda: json.loads(os.getenv('CORPUS_FIELDS','[{"id":"sbi-pretrain","label":"SBI · pretraining"},{"id":"diff-compose","label":"Diffusion · composition"},{"id":"unfiled","label":"unfiled"}]')))
    def __post_init__(self):
        self.data_dir = Path(self.data_dir).resolve()
        self.cache_dir = Path(self.cache_dir or os.getenv('CORPUS_PDF_CACHE',self.data_dir/'pdfs')).resolve()
        if not 0.5 <= self.dedupe_threshold <= 1: raise ValueError('Dedupe threshold must be 0.5–1')
        if self.cache_max_bytes < 0: raise ValueError('Cache size cannot be negative')
        if not self.fields or any(not isinstance(f,dict) or not f.get('id') or not f.get('label') for f in self.fields):
            raise ValueError('CORPUS_FIELDS must be a nonempty list of {id,label}')
    @property
    def database(self): return self.data_dir/'corpus.sqlite3'
