"""Metadata adapters and guarded downloads for the Corpus ingest worker.

The module deliberately has no third-party dependencies.  Metadata is fetched from
Crossref for DOI identifiers and from arXiv's Atom API for arXiv identifiers.
Other links are read from the page itself: NeurIPS/ICML/ICLR conference pages
through their paper template, and publisher pages through the Highwire
``citation_*`` meta tags most of them carry.  OpenReview refuses automated
clients, so its links are recognized but cannot be fetched.
``safe_download`` is shared by background artifact workers and keeps all network
access behind the same SSRF and response-size checks.
"""

from __future__ import annotations

import html
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Mapping


DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
MAX_REDIRECTS = 5
_METADATA_MAX_BYTES = 4 * 1024 * 1024
_USER_AGENT = "Corpus/1.0 (metadata ingest; +https://crossref.org/)"


class SourceError(ValueError):
    """A clear, user-facing failure while recognizing or fetching a source."""


class HTTPStatusError(SourceError):
    """A non-success HTTP status; keeps the status and any Retry-After header."""

    def __init__(self, status: int, retry_after: str | None = None):
        super().__init__(f"source returned HTTP {status}")
        self.status = status
        self.retry_after = retry_after


class MetadataFetchError(SourceError):
    """A transport or response-parsing failure after identifier recognition."""


class PageError(SourceError):
    """A fetched page that does not describe a paper (the input's fault, not an outage)."""


_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_ARXIV_NEW_RE = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b", re.IGNORECASE)
_ARXIV_OLD_RE = re.compile(r"\b[a-z][a-z0-9-]*(?:\.[a-z]{2})?/\d{7}(?:v\d+)?\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")
_OPENREVIEW_ID_RE = re.compile(r"^[A-Za-z0-9_-]{5,40}$")
# Hosts of the shared NeurIPS/ICML/ICLR conference site: short and full names.
_CONFERENCES = {
    "neurips.cc": ("NeurIPS", "Advances in Neural Information Processing Systems"),
    "icml.cc": ("ICML", "International Conference on Machine Learning"),
    "iclr.cc": ("ICLR", "International Conference on Learning Representations"),
}
OPENREVIEW_BLOCKED = (
    "OpenReview blocks automated lookups. Paste the paper's NeurIPS, ICML or ICLR page, "
    "its arXiv id or DOI, or add it by hand."
)


@dataclass(frozen=True)
class _Identifier:
    kind: str
    value: str


@dataclass(frozen=True)
class _ResolvedAddress:
    host: str
    port: int
    ip: str


def _clean_text(value: Any) -> str:
    """Collapse XML/HTML whitespace while preserving word boundaries."""

    if value is None:
        return ""
    text = html.unescape(str(value))
    text = _TAG_RE.sub(" ", text)
    return " ".join(text.split())


def _clean_doi(value: str) -> str | None:
    candidate = urllib.parse.unquote(value.strip())
    candidate = re.sub(r"^doi:\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", candidate, flags=re.IGNORECASE)
    candidate = candidate.strip().strip("<>")
    # DOI citations commonly include terminal punctuation outside the identifier.
    candidate = candidate.rstrip(".,;:)]}")
    if _DOI_RE.fullmatch(candidate):
        return candidate.lower()
    return None


def _clean_arxiv(value: str) -> str | None:
    candidate = urllib.parse.unquote(value).strip()
    candidate = re.sub(r"^arxiv:\s*", "", candidate, flags=re.IGNORECASE)
    candidate = candidate.strip().strip("<>")
    candidate = candidate.rstrip(".,;:)]}")
    match = _ARXIV_NEW_RE.fullmatch(candidate) or _ARXIV_OLD_RE.fullmatch(candidate)
    if not match:
        return None
    # A version identifies a revision of the same work.  The stable identifier is
    # useful for deduplication, so omit it from the normalized value.
    return re.sub(r"v\d+$", "", candidate, flags=re.IGNORECASE).lower()


def _arxiv_from_path(path: str, query: str = "") -> str | None:
    decoded = urllib.parse.unquote(path)
    # The API can itself be supplied as a URL.  It is still a recognizable arXiv
    # identifier if id_list contains exactly one valid id.
    params = urllib.parse.parse_qs(query)
    for key in ("id_list", "arxiv", "arxiv_id"):
        values = params.get(key, [])
        if len(values) == 1:
            candidate = _clean_arxiv(values[0].split(",")[0])
            if candidate:
                return candidate
    # Restrict path extraction to arXiv URL shapes; this avoids treating an
    # arbitrary URL containing a number that looks like an arXiv id as an id.
    if not re.search(r"/(?:abs|pdf|html)/", decoded, flags=re.IGNORECASE):
        return None
    match = _ARXIV_NEW_RE.search(decoded) or _ARXIV_OLD_RE.search(decoded)
    return _clean_arxiv(match.group(0)) if match else None


def _recognize(identifier: str) -> _Identifier:
    if not isinstance(identifier, str) or not identifier.strip():
        raise SourceError("identifier must be a non-empty DOI, arXiv id, or source URL")

    raw = identifier.strip()
    direct_doi = _clean_doi(raw)
    if direct_doi:
        return _Identifier("doi", direct_doi)
    direct_arxiv = _clean_arxiv(raw)
    if direct_arxiv:
        return _Identifier("arxiv", direct_arxiv)

    parsed = urllib.parse.urlsplit(raw)
    if not parsed.scheme:
        raise SourceError(
            "unrecognized identifier; expected a DOI (10.xxxx/...), an arXiv id, or an http(s) URL"
        )
    if parsed.scheme.lower() not in {"http", "https"}:
        raise SourceError("source URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise SourceError("source URL must not contain embedded credentials")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise SourceError("source URL has no hostname")

    if host in {"doi.org", "dx.doi.org", "www.doi.org"}:
        doi = _clean_doi(parsed.path.lstrip("/"))
        if doi:
            return _Identifier("doi", doi)
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        arxiv_id = _arxiv_from_path(parsed.path, parsed.query)
        if arxiv_id:
            return _Identifier("arxiv", arxiv_id)
    bare = host.removeprefix("www.")
    if bare in {"openreview.net", "api.openreview.net", "api2.openreview.net"}:
        values = urllib.parse.parse_qs(parsed.query).get("id", [])
        if len(values) == 1 and _OPENREVIEW_ID_RE.fullmatch(values[0]):
            return _Identifier("openreview", values[0])
        raise SourceError("OpenReview link has no paper id")
    if bare in _CONFERENCES and re.match(r"/virtual/\d{4}/.", parsed.path):
        return _Identifier("conference", urllib.parse.urlunsplit(("https", bare, parsed.path.rstrip("/"), "", "")))
    # Copernicus journals encode their DOI in the article path:
    # gmd.copernicus.org/articles/12/345/2025/ is 10.5194/gmd-12-345-2025.
    copernicus = re.fullmatch(r"([a-z]+)\.copernicus\.org", host)
    article = re.match(r"/articles/(\d+)/(\d+)/(\d{4})(?:/|$)", parsed.path) if copernicus else None
    if copernicus and article:
        return _Identifier("doi", "10.5194/{}-{}-{}-{}".format(copernicus.group(1), *article.groups()))

    # Recognize DOI-bearing publisher URLs without fetching arbitrary web pages.
    match = re.search(r"(?:^|/)(10\.\d{4,9}/[^?#]+)", urllib.parse.unquote(parsed.path), re.IGNORECASE)
    if match:
        doi = _clean_doi(match.group(1))
        if doi:
            return _Identifier("doi", doi)

    # Some publishers expose the DOI in a query parameter.  This is still
    # recognizable; an otherwise generic URL should never be fetched as metadata.
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("doi", "DOI"):
        values = query.get(key, [])
        if len(values) == 1:
            doi = _clean_doi(values[0])
            if doi:
                return _Identifier("doi", doi)
    for key in ("arxiv", "arxiv_id", "id_list"):
        values = query.get(key, [])
        if len(values) == 1:
            arxiv_id = _clean_arxiv(values[0].split(",")[0])
            if arxiv_id:
                return _Identifier("arxiv", arxiv_id)

    # Anything else is read from the page itself (see page_metadata).
    return _Identifier("page", urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, "")))


def _validate_max_bytes(max_bytes: int) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    return max_bytes


def _blocked_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise SourceError(f"DNS returned an invalid IP address: {address!r}") from exc
    # ``is_private`` includes RFC1918 and a number of non-public special ranges;
    # the explicit checks document the SSRF policy and cover IPv4-mapped IPv6.
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_reserved
    )


def _resolve(host: str, port: int) -> _ResolvedAddress:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _blocked_ip(host):
            raise SourceError(f"refusing to connect to non-public address {host}")
        return _ResolvedAddress(host, port, host)

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceError(f"could not resolve source host {host!r}") from exc
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        address = str(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise SourceError(f"could not resolve source host {host!r}")
    for address in addresses:
        if _blocked_ip(address):
            raise SourceError(f"refusing to connect to non-public address {address}")
    # Keep this exact address for the connection.  We never ask the socket layer
    # to resolve the hostname again, preventing a DNS rebinding between validation
    # and connect.  Every address returned by DNS is checked before selection.
    return _ResolvedAddress(host, port, addresses[0])


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, address: _ResolvedAddress, timeout: float) -> None:
        super().__init__(address.ip, address.port, timeout=timeout)
        self._pinned_address = address.ip

    def connect(self) -> None:  # pragma: no cover - exercised by integration users
        self.sock = socket.create_connection((self._pinned_address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, address: _ResolvedAddress, server_hostname: str, timeout: float) -> None:
        super().__init__(address.ip, address.port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_address = address.ip
        self._server_hostname = server_hostname

    def connect(self) -> None:  # pragma: no cover - exercised by integration users
        self.sock = socket.create_connection((self._pinned_address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._server_hostname)


def _parse_http_url(url: str) -> tuple[urllib.parse.SplitResult, str, int]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise SourceError("download URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise SourceError("download URL must not contain embedded credentials")
    host = (parsed.hostname or "").rstrip(".")
    if not host:
        raise SourceError("download URL has no hostname")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SourceError("download URL has an invalid port") from exc
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    if not (1 <= port <= 65535):
        raise SourceError("download URL has an invalid port")
    try:
        host_header = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SourceError("download URL has an invalid hostname") from exc
    return parsed, host_header, port


def _request_once(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    started = time.monotonic()
    parsed, host, port = _parse_http_url(url)
    address = _resolve(host, port)
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise SourceError(f"source request exceeded the {timeout:g}-second time limit")
    connection: http.client.HTTPConnection
    if parsed.scheme.lower() == "https":
        connection = _PinnedHTTPSConnection(address, host, remaining)
    else:
        connection = _PinnedHTTPConnection(address, remaining)

    request_path = parsed.path or "/"
    if parsed.query:
        request_path += "?" + parsed.query
    request_headers = {
        "Accept": "*/*",
        "User-Agent": _USER_AGENT,
        "Host": (
            f"[{host}]" if ":" in host else host
        ) + ("" if port == (443 if parsed.scheme.lower() == "https" else 80) else f":{port}"),
        "Connection": "close",
    }
    if headers:
        request_headers.update(headers)
    try:
        connection.request("GET", request_path, headers=request_headers)
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        content_length = response_headers.get("content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise SourceError("source returned an invalid Content-Length") from exc
            if declared_length < 0 or declared_length > max_bytes:
                raise SourceError(f"source response exceeds the {max_bytes}-byte limit")
        if response.status in {301, 302, 303, 307, 308}:
            # Redirect callers need only headers.  Closing the connection here also
            # prevents an unbounded redirect body from consuming memory.
            return response.status, response_headers, b""
        if response.status < 200 or response.status >= 300:
            raise HTTPStatusError(response.status, response_headers.get("retry-after"))

        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise SourceError(f"source request exceeded the {timeout:g}-second time limit")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            amount = min(64 * 1024, max_bytes - total + 1)
            # HTTPResponse.read() may wait until the requested amount is
            # available. read1() performs at most one underlying read, so a
            # trickle response cannot reset the socket timeout indefinitely.
            read1 = getattr(response, "read1", None)
            chunk = read1(amount) if read1 is not None else response.read(amount)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise SourceError(f"source response exceeds the {max_bytes}-byte limit")
            chunks.append(chunk)
        if time.monotonic() - started > timeout:
            raise SourceError(f"source request exceeded the {timeout:g}-second time limit")
        return response.status, response_headers, b"".join(chunks)
    except socket.timeout as exc:
        raise SourceError(f"source request exceeded the {timeout:g}-second time limit") from exc
    except OSError as exc:
        raise SourceError(f"source request failed: {exc}") from exc
    finally:
        connection.close()


def _fetch_url(
    url: str,
    *,
    max_bytes: int,
    timeout: float = DEFAULT_TIMEOUT,
    headers: Mapping[str, str] | None = None,
) -> bytes:
    max_bytes = _validate_max_bytes(max_bytes)
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    current = url
    seen: set[str] = set()
    deadline = time.monotonic() + timeout
    for redirect_number in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError(f"source request exceeded the {timeout:g}-second time limit")
        parsed, _, _ = _parse_http_url(current)
        # Normalize only for loop detection; the request retains the original
        # path/query bytes supplied by the server's Location header.
        loop_key = urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))
        if loop_key in seen:
            raise SourceError("source returned a redirect loop")
        seen.add(loop_key)
        status, response_headers, body = _request_once(
            current, max_bytes=max_bytes, timeout=remaining, headers=headers
        )
        if time.monotonic() > deadline:
            raise SourceError(f"source request exceeded the {timeout:g}-second time limit")
        # Keep the public helper bounded even if a custom transport implementation
        # (or a test double) does not enforce the limit itself.
        if len(body) > max_bytes:
            raise SourceError(f"source response exceeds the {max_bytes}-byte limit")
        if status not in {301, 302, 303, 307, 308}:
            return body
        location = response_headers.get("location")
        if not location:
            raise SourceError("source returned a redirect without a Location header")
        if redirect_number >= MAX_REDIRECTS:
            raise SourceError(f"source exceeded the {MAX_REDIRECTS}-redirect limit")
        current = urllib.parse.urljoin(current, location)
        # Validate scheme/credentials/hostname before the next request.  The next
        # _request_once resolves and pins the redirect target before connecting.
        _parse_http_url(current)
    raise SourceError("source redirect handling failed")


def safe_download(url: str, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    """Download one HTTP(S) artifact with SSRF, size, redirect, and time bounds."""

    _parse_http_url(url)  # fail early for unsupported schemes and malformed URLs
    return _fetch_url(url, max_bytes=max_bytes)


def _year_from_crossref(message: Mapping[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "issued", "published", "created"):
        value = message.get(key)
        if not isinstance(value, Mapping):
            continue
        date_parts = value.get("date-parts")
        if isinstance(date_parts, list) and date_parts and isinstance(date_parts[0], list) and date_parts[0]:
            try:
                return int(date_parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


def _crossref_metadata(payload: Mapping[str, Any], requested_doi: str) -> dict[str, Any]:
    message = payload.get("message")
    if not isinstance(message, Mapping):
        raise SourceError("Crossref response did not contain a metadata message")
    title_values = message.get("title")
    title = _clean_text(title_values[0]) if isinstance(title_values, list) and title_values else ""
    authors: list[str] = []
    raw_authors = message.get("author")
    if isinstance(raw_authors, list):
        for author in raw_authors:
            if not isinstance(author, Mapping):
                continue
            name = _clean_text(author.get("name"))
            if not name:
                given = _clean_text(author.get("given"))
                family = _clean_text(author.get("family"))
                name = " ".join(part for part in (given, family) if part)
            if name:
                authors.append(name)
    venues = message.get("container-title")
    venue = _clean_text(venues[0]) if isinstance(venues, list) and venues else ""
    doi = _clean_doi(str(message.get("DOI", ""))) or requested_doi
    canonical = _clean_text(message.get("URL")) or f"https://doi.org/{doi}"
    artifacts: list[dict[str, str]] = []
    links = message.get("link")
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, Mapping):
                continue
            link_url = _clean_text(link.get("URL"))
            if not link_url:
                continue
            content_type = _clean_text(link.get("content-type")).lower()
            if content_type == "application/pdf" or link_url.lower().split("?", 1)[0].endswith(".pdf"):
                if link_url in {item["url"] for item in artifacts}:
                    continue
                artifacts.append({"kind": "pdf", "url": link_url})
    abstract = _clean_text(message.get("abstract"))
    return {
        "title": title,
        "authors": authors,
        "venue": venue,
        "year": _year_from_crossref(message),
        "abstract": abstract,
        "canonical_url": canonical,
        "arxiv_id": None,
        "doi": doi,
        "artifacts": artifacts,
    }


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _first_child_text(element: ET.Element, name: str) -> str:
    for child in element.iter():
        if _local_name(child) == name:
            return _clean_text(child.text)
    return ""


def _arxiv_metadata(payload: bytes, requested_id: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise SourceError("arXiv returned malformed Atom XML") from exc
    entry = next((child for child in root if _local_name(child) == "entry"), None)
    if entry is None:
        raise SourceError("arXiv returned no entry for that identifier")
    title = _first_child_text(entry, "title")
    abstract = _first_child_text(entry, "summary")
    authors = []
    for author in entry.iter():
        if _local_name(author) != "author":
            continue
        name = _first_child_text(author, "name")
        if name:
            authors.append(name)
    published = _first_child_text(entry, "published")
    year: int | None = None
    year_match = re.match(r"(\d{4})", published)
    if year_match:
        year = int(year_match.group(1))
    journal_ref = _first_child_text(entry, "journal_ref")
    venue = journal_ref or "arXiv"
    doi = _clean_doi(_first_child_text(entry, "doi"))
    canonical = f"https://arxiv.org/abs/{requested_id}"
    artifacts = [{"kind": "pdf", "url": f"https://arxiv.org/pdf/{requested_id}.pdf"}]
    return {
        "title": title,
        "authors": authors,
        "venue": venue,
        "year": year,
        "abstract": abstract,
        "canonical_url": canonical,
        "arxiv_id": requested_id,
        "doi": doi,
        "artifacts": artifacts,
    }


_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _PageReader(HTMLParser):
    """Collects meta tags, links and the text of the first element of each
    requested class from one HTML page."""

    def __init__(self, classes: tuple[str, ...] = ()) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[str]] = {}
        self.links: list[tuple[str, str]] = []
        self.blocks: dict[str, list[str]] = {}
        self._classes = set(classes)
        self._stack: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta":
            name = (values.get("name") or values.get("property") or "").strip().lower()
            if name and "content" in values:
                self.meta.setdefault(name, []).append(values["content"])
        if tag == "a" and values.get("href"):
            self.links.append((values["href"], values.get("title", "")))
        if tag in _VOID_TAGS:
            return
        key = self._stack[-1][1] if self._stack else None
        found = next((name for name in values.get("class", "").split() if name in self._classes and name not in self.blocks), None)
        if found:
            self.blocks[found] = []
            key = found
        self._stack.append((tag, key))

    def handle_endtag(self, tag: str) -> None:
        # Tolerate unclosed elements: close back to the nearest matching tag.
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1][1]:
            self.blocks[self._stack[-1][1]].append(data)

    def text(self, name: str) -> str:
        return _clean_text(" ".join(self.blocks.get(name, [])))


def _read_page(payload: bytes, classes: tuple[str, ...] = ()) -> _PageReader:
    reader = _PageReader(classes)
    reader.feed(payload.decode("utf-8", errors="replace"))
    reader.close()
    return reader


def _http_link(value: str, base: str) -> str:
    link = urllib.parse.urljoin(base, value.strip())
    parsed = urllib.parse.urlsplit(link)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    return link


def _citation_metadata(payload: bytes, url: str) -> dict[str, Any]:
    """Metadata from Highwire ``citation_*`` meta tags (Google Scholar's format)."""

    meta = _read_page(payload).meta

    def first(*names: str) -> str:
        for name in names:
            for value in meta.get(name, []):
                if _clean_text(value):
                    return _clean_text(value)
        return ""

    title = first("citation_title", "dc.title")
    if not title:
        raise PageError(
            "That page has no citation metadata. Paste the paper's DOI or arXiv id, or add it by hand."
        )
    authors = []
    for value in meta.get("citation_author") or meta.get("dc.creator") or []:
        name = _clean_text(value)
        # Highwire names are usually "Family, Given".
        if name.count(",") == 1:
            family, given = (part.strip() for part in name.split(","))
            name = " ".join(part for part in (given, family) if part)
        if name and name not in authors:
            authors.append(name)
    year = None
    for value in (first("citation_publication_date"), first("citation_date"), first("citation_online_date"),
                  first("citation_year"), first("dc.date")):
        match = re.search(r"\b(1[5-9]\d\d|2[01]\d\d)\b", value)
        if match:
            year = int(match.group(1))
            break
    abstract = re.sub(r"^abstract[.:]?\s*", "", first("citation_abstract", "dc.description"), flags=re.IGNORECASE)
    artifacts = []
    pdf = _http_link(first("citation_pdf_url"), url)
    if pdf:
        artifacts.append({"kind": "pdf", "url": pdf})
    return {
        "title": title,
        "authors": authors,
        "venue": first("citation_journal_title", "citation_conference_title", "citation_inbook_title"),
        "year": year,
        "abstract": abstract,
        "canonical_url": _http_link(first("citation_abstract_html_url"), url) or url,
        "arxiv_id": _clean_arxiv(first("citation_arxiv_id")),
        "doi": _clean_doi(first("citation_doi", "dc.identifier")),
        "artifacts": artifacts,
    }


def _conference_metadata(payload: bytes, url: str) -> dict[str, Any]:
    """Metadata from a NeurIPS/ICML/ICLR conference-site paper page.

    These pages carry no citation tags; the title, authors, abstract and track
    are read from the site's paper template.  OpenReview is the paper's home
    when the page links to it.
    """

    parsed = urllib.parse.urlsplit(url)
    short, full = _CONFERENCES[parsed.hostname or ""]
    reader = _read_page(payload, ("event-title", "event-organizers", "abstract-text-inner", "event-type-badge"))
    title, badge = reader.text("event-title"), reader.text("event-type-badge")
    if not title or not re.search(r"\b(?:poster|oral|spotlight|paper)\b", badge, re.IGNORECASE):
        raise PageError("That conference page is not a paper")
    authors = [name for name in (part.strip() for part in re.split(r"[\u22c5\u00b7]", reader.text("event-organizers"))) if name]
    year_match = re.match(r"/virtual/(\d{4})/", parsed.path)
    year = int(year_match.group(1)) if year_match else None
    workshop = re.search(r"\bworkshop:\s*(.+)$", badge, re.IGNORECASE)
    if workshop:
        name = workshop.group(1).strip()
        # Many workshop names already say which conference they belong to.
        venue = name if re.search(rf"\b{short}\b.*\bworkshop\b", name, re.IGNORECASE) else f"{short} {year} Workshop: {name}"
        venue_class = "other"
    else:
        venue, venue_class = full, "conference"
    openreview_id = None
    for href, _ in reader.links:
        link = urllib.parse.urlsplit(href)
        values = urllib.parse.parse_qs(link.query).get("id", [])
        if (link.hostname or "").removeprefix("www.") == "openreview.net" and len(values) == 1 and _OPENREVIEW_ID_RE.fullmatch(values[0]):
            openreview_id = values[0]
            break
    return {
        "title": title,
        "authors": authors,
        "venue": venue,
        "venue_class": venue_class,
        "year": year,
        "abstract": reader.text("abstract-text-inner"),
        "canonical_url": f"https://openreview.net/forum?id={openreview_id}" if openreview_id else url,
        "arxiv_id": None,
        "doi": None,
        "openreview_id": openreview_id,
        # OpenReview serves its PDFs only to browsers, so none is attached;
        # the conference page is kept as a link.
        "artifacts": [{"kind": "other", "url": url}] if openreview_id else [],
    }


def page_metadata(kind: str, payload: bytes, url: str) -> dict[str, Any]:
    """Parse a fetched ``conference`` or ``page`` identifier's HTML."""

    return _conference_metadata(payload, url) if kind == "conference" else _citation_metadata(payload, url)


def merge_page(published: dict[str, Any], page: Mapping[str, Any]) -> dict[str, Any]:
    """Prefer the DOI record, keeping what only the landing page had."""

    merged = dict(published)
    if not merged.get("abstract") and page.get("abstract"):
        merged["abstract"] = page["abstract"]
    if not any(item.get("kind") == "pdf" for item in merged.get("artifacts", [])):
        merged["artifacts"] = [*merged.get("artifacts", []), *(item for item in page.get("artifacts", []) if item.get("kind") == "pdf")]
    return merged


def _crossref_lookup(doi: str) -> dict[str, Any]:
    endpoint = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="")
    payload = _fetch_url(
        endpoint,
        max_bytes=_METADATA_MAX_BYTES,
        headers={"Accept": "application/json"},
    )
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("Crossref returned invalid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise SourceError("Crossref returned an invalid response")
    return _crossref_metadata(decoded, doi)


def fetch_metadata(identifier: str) -> dict[str, Any]:
    """Fetch normalized metadata for a DOI, arXiv id, or paper page.

    Network and parsing failures after recognition are raised as
    ``MetadataFetchError`` so callers can distinguish them from invalid input.
    """

    source = _recognize(identifier)
    if source.kind == "openreview":
        raise SourceError(OPENREVIEW_BLOCKED)
    try:
        if source.kind == "doi":
            return _crossref_lookup(source.value)
        if source.kind in {"conference", "page"}:
            payload = _fetch_url(
                source.value,
                max_bytes=_METADATA_MAX_BYTES,
                headers={"Accept": "text/html,application/xhtml+xml;q=0.9"},
            )
            paper = page_metadata(source.kind, payload, source.value)
            if paper.get("doi"):
                try:
                    return merge_page(_crossref_lookup(paper["doi"]), paper)
                except SourceError:
                    pass
            return paper

        endpoint = "https://export.arxiv.org/api/query?id_list=" + urllib.parse.quote(source.value, safe="")
        payload = _fetch_url(
            endpoint,
            max_bytes=_METADATA_MAX_BYTES,
            headers={"Accept": "application/atom+xml, application/xml;q=0.9"},
        )
        return _arxiv_metadata(payload, source.value)
    except (MetadataFetchError, PageError):
        raise
    except Exception as exc:
        raise MetadataFetchError(str(exc) or "metadata source failed") from exc


__all__ = [
    "DEFAULT_MAX_BYTES",
    "MetadataFetchError",
    "PageError",
    "SourceError",
    "fetch_metadata",
    "safe_download",
]
