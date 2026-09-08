"""Corpus query grammar (case-insensitive boolean operators).
query := or_expr ; or_expr := and_expr ('OR' and_expr)*
and_expr := unary (('AND' | adjacency) unary)*
unary := 'NOT' unary | '(' or_expr ')' | atom
atom := WORD | STRING | WORD ':' [COMPARISON] (WORD | STRING)
Sort directives are allowed only in a top-level conjunction and apply globally.
All SQL values are bound parameters; FTS input is quoted as literal text.
"""
from dataclasses import dataclass

class QueryError(ValueError):
    def __init__(self, message, position=0):
        super().__init__(message)
        self.message, self.position = message, position

@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    pos: int

@dataclass(frozen=True)
class Node:
    kind: str
    value: object = None
    left: object = None
    right: object = None
    pos: int = 0


def lex(source):
    if not isinstance(source,str): raise QueryError('Query must be text')
    if len(source) > 4000:
        raise QueryError('Query exceeds 4,000 characters', 4000)
    out, i = [], 0
    while i < len(source):
        c, start = source[i], i
        if c.isspace():
            i += 1
            continue
        if c in '():':
            out.append(Token(c, c, i)); i += 1
        elif c in '<>=':
            i += 1
            if i < len(source) and source[i] == '=' and c != '=':
                i += 1
            out.append(Token('CMP', source[start:i], start))
        elif c == '"':
            i += 1; chars = []
            while i < len(source) and source[i] != '"':
                if source[i] == '\\':
                    i += 1
                    if i >= len(source) or source[i] not in ('"', '\\'):
                        raise QueryError('Only \\" and \\\\ escapes are supported', i)
                chars.append(source[i]); i += 1
            if i == len(source):
                raise QueryError('Unclosed quoted phrase', start)
            i += 1
            if not chars:
                raise QueryError('A search phrase cannot be empty', start)
            out.append(Token('STRING', ''.join(chars), start))
        else:
            while i < len(source) and not source[i].isspace() and source[i] not in '():<>="':
                i += 1
            value = source[start:i]
            out.append(Token(value.upper() if value.upper() in ('AND','OR','NOT') else 'WORD', value, start))
    if len(out) > 300:
        raise QueryError('Query is too complex (maximum 300 tokens)')
    return out + [Token('EOF', '', len(source))]

class Parser:
    def __init__(self, source):
        self.tokens, self.i, self.depth = lex(source), 0, 0
    @property
    def cur(self): return self.tokens[self.i]
    def take(self):
        t = self.cur; self.i += 1; return t
    def parse(self):
        if self.cur.kind == 'EOF': return Node('true')
        node = self.or_expr()
        if self.cur.kind != 'EOF':
            raise QueryError(f'Unexpected {self.cur.value!r}', self.cur.pos)
        return node
    def or_expr(self):
        node = self.and_expr()
        while self.cur.kind == 'OR':
            op = self.take(); node = Node('or', left=node, right=self.and_expr(), pos=op.pos)
        return node
    def and_expr(self):
        node = self.unary()
        while self.cur.kind in ('AND','NOT','(','WORD','STRING'):
            pos = self.cur.pos
            if self.cur.kind == 'AND': self.take()
            node = Node('and', left=node, right=self.unary(), pos=pos)
        return node
    def unary(self):
        self.depth += 1
        if self.depth > 40: raise QueryError('Maximum nesting depth is 40', self.cur.pos)
        try:
            t = self.cur
            if t.kind == 'NOT':
                self.take(); return Node('not', left=self.unary(), pos=t.pos)
            if t.kind == '(':
                self.take(); node = self.or_expr()
                if self.cur.kind != ')': raise QueryError('Expected closing parenthesis', self.cur.pos)
                self.take(); return node
            if t.kind not in ('WORD','STRING'):
                raise QueryError('Expected a search term', t.pos)
            self.take()
            if self.cur.kind != ':': return Node('term', t.value, pos=t.pos)
            if t.kind != 'WORD': raise QueryError('Expected field name before colon', t.pos)
            self.take(); cmp = '='
            if self.cur.kind == 'CMP': cmp = self.take().value
            if self.cur.kind not in ('WORD','STRING'):
                raise QueryError('Expected a value after colon', self.cur.pos)
            v = self.take()
            return Node('filter', (t.value.lower(), cmp, v.value), pos=t.pos)
        finally: self.depth -= 1

SCORES = ('rel','cred','qual','reimpl')
SORTS = {'rel':'rel','cred':'cred','qual':'qual','reimpl':'reimpl','added':'added','modified':'modified','year':'year','title':'title','venue':'venue'}

def fts_literal(value):
    if not any(c.isalnum() for c in value): raise QueryError('Search terms must contain a letter or number')
    return '"' + value.replace('"', '""') + '"'

@dataclass
class Compiled:
    where: str
    params: list
    order: str
    sort: str
    pdf_terms: list


def compile_query(source='', facets=None):
    ast = Parser(source).parse()
    params, sorts, pdf_terms = [], [], []
    def bound(v): params.append(v); return '?'
    def walk(n, allow_sort=True, negated=False):
        if n.kind == 'true': return '1'
        if n.kind in ('and','or'):
            allow = allow_sort and n.kind == 'and'
            return '(' + walk(n.left, allow, negated) + ' ' + n.kind.upper() + ' ' + walk(n.right, allow, negated) + ')'
        if n.kind == 'not': return '(NOT ' + walk(n.left, False, not negated) + ')'
        if n.kind == 'term':
            return 'p.id IN (SELECT paper_id FROM paper_fts WHERE paper_fts MATCH ' + bound(fts_literal(n.value)) + ')'
        key, cmp, value = n.value
        if key in (*SCORES, 'year'):
            try: num = int(value)
            except ValueError: raise QueryError(f'{key} requires an integer', n.pos)
            if key in SCORES and not 0 <= num <= 10: raise QueryError('Scores must be between 0 and 10', n.pos)
            return f'COALESCE(p.{key} {cmp} {bound(num)}, 0)'
        if cmp != '=': raise QueryError(f'Comparisons are not supported for {key}', n.pos)
        if key == 'sort':
            if not allow_sort: raise QueryError('sort must be outside OR and NOT', n.pos)
            name = value.lstrip('-')
            if name not in SORTS: raise QueryError('Unknown sort column: ' + name, n.pos)
            if sorts: raise QueryError('Use only one sort directive', n.pos)
            sorts.append(value); return '1'
        if key in ('field','venue','state'):
            if key == 'state' and value not in ('unreviewed','reviewed','archived'):
                raise QueryError('Unknown review state', n.pos)
            return f'p.{key} = {bound(value)} COLLATE NOCASE'
        if key == 'tag':
            return 'p.id IN (SELECT pt.paper_id FROM paper_tags pt JOIN tags t ON t.id=pt.tag_id WHERE t.name = '+bound(value)+' COLLATE NOCASE)'
        if key == 'author':
            return 'p.id IN (SELECT paper_id FROM paper_fts WHERE paper_fts MATCH '+bound('authors : '+fts_literal(value))+')'
        if key == 'has':
            if value not in ('pdf','code','data','slides','video','other','cached'):
                raise QueryError('Unknown artifact kind', n.pos)
            predicate = "a.fetch_status='cached' AND a.kind='pdf'" if value == 'cached' else 'a.kind='+bound(value)
            return 'EXISTS (SELECT 1 FROM artifacts a WHERE a.paper_id=p.id AND '+predicate+')'
        if key == 'is':
            if value != 'starred': raise QueryError('Expected is:starred', n.pos)
            return 'p.starred=1'
        if key == 'pdf':
            if not negated: pdf_terms.append(value)
            return 'p.id IN (SELECT paper_id FROM pdf_fts WHERE pdf_fts MATCH '+bound(fts_literal(value))+')'
        raise QueryError('Unknown search field: '+key, n.pos)
    where = walk(ast)
    facets = {} if facets is None else facets
    allowed = {'field','state','has','venue_class','year',*SCORES}
    if not isinstance(facets,dict) or set(facets)-allowed: raise QueryError('Unknown facet')
    for key, values in facets.items():
        if key in SCORES:
            if type(values) is not int or not 0 <= values <= 10: raise QueryError('Score floor must be an integer 0–10')
            if values: where += f' AND p.{key} >= '+bound(values)
            continue
        if not isinstance(values,list) or len(values)>100 or any(not isinstance(v,str) for v in values): raise QueryError('Facet values must be a list of strings')
        if not values: continue
        if key in ('field','state','year','venue_class'):
            if key == 'year':
                try: values = [int(v) for v in values]
                except ValueError: raise QueryError('Year facets must be integers')
            where += f' AND p.{key} IN (' + ','.join(bound(v) for v in values) + ')'
        else:
            where += ' AND (' + ' OR '.join(walk(Node('filter',('has','=',v))) for v in values) + ')'
    sort = sorts[0] if sorts else '-added'
    name = sort.lstrip('-')
    direction = 'DESC' if sort.startswith('-') or (not sort.startswith('-') and name in SCORES) else 'ASC'
    return Compiled(where, params, f'p.{SORTS[name]} {direction} NULLS LAST, p.id ASC', sort, pdf_terms)
