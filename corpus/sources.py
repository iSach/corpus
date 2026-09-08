"""Metadata adapters and guarded downloads for the Corpus ingest worker.

The module deliberately has no third-party dependencies.  Metadata is fetched from
Crossref for DOI identifiers and from arXiv's Atom API for arXiv identifiers.
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
from typing import Any, Mapping


DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
MAX_REDIRECTS = 5
_METADATA_MAX_BYTES = 4 * 1024 * 1024
_USER_AGENT = "Corpus/1.0 (metadata ingest; +https://crossref.org/)"


class SourceError(ValueError):
    """A clear, user-facing failure while recognizing or fetching a source."""


class MetadataFetchError(SourceError):
    """A transport or response-parsing failure after identifier recognition."""


_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
_ARXIV_NEW_RE = re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b", re.IGNORECASE)
_ARXIV_OLD_RE = re.compile(r"\b[a-z][a-z0-9-]*(?:\.[a-z]{2})?/\d{7}(?:v\d+)?\b", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]*>")


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

    raise SourceError(
        "URL does not contain a recognizable DOI or arXiv identifier; generic URLs cannot be used for metadata"
    )


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
            raise SourceError(f"source returned HTTP {response.status}")

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


def fetch_metadata(identifier: str) -> dict[str, Any]:
    """Fetch normalized metadata for a DOI or arXiv identifier.

    Generic URLs are intentionally rejected unless they encode one of those two
    identifiers.  Network and parsing failures after recognition are raised as
    ``MetadataFetchError`` so callers can distinguish them from invalid input.
    """

    source = _recognize(identifier)
    try:
        if source.kind == "doi":
            endpoint = "https://api.crossref.org/works/" + urllib.parse.quote(source.value, safe="")
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
            return _crossref_metadata(decoded, source.value)

        endpoint = "https://export.arxiv.org/api/query?id_list=" + urllib.parse.quote(source.value, safe="")
        payload = _fetch_url(
            endpoint,
            max_bytes=_METADATA_MAX_BYTES,
            headers={"Accept": "application/atom+xml, application/xml;q=0.9"},
        )
        return _arxiv_metadata(payload, source.value)
    except MetadataFetchError:
        raise
    except Exception as exc:
        raise MetadataFetchError(str(exc) or "metadata source failed") from exc


__all__ = [
    "DEFAULT_MAX_BYTES",
    "MetadataFetchError",
    "SourceError",
    "fetch_metadata",
    "safe_download",
]
