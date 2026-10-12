"""Robots-permitted pages, stable visible snapshots, and bounded rendering."""

import base64
import hashlib
import json
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import tldextract
from bs4 import BeautifulSoup

from pipeline.discovery import KNOWN_ATS_DOMAINS, CareersDiscovery
from pipeline.http import HTTPClient, checked_url

DOMAIN = tldextract.TLDExtract(suffix_list_urls=())


class UnsafeRedirect(ValueError):
    """A listing redirected outside its permitted domains."""


ATS_DOMAINS = KNOWN_ATS_DOMAINS
NOISE = (
    "script,style,noscript,nav,footer,body>header,[hidden],[aria-hidden='true'],"
    ".hidden,.sr-only,.visually-hidden,"
    "[role='navigation'],[role='banner'],.cookie-banner,#cookie-banner,"
    "[id*='cookie'],[class*='cookie'],[class*='consent'],.banner,.announcement,"
    ".carousel,.rotating,.ticker,.timestamp,.last-updated,time"
)
MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
DATE = re.compile(
    r"\b(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
    r"Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+)?"
    r"(?:\d{4}-\d{2}-\d{2}(?:[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?|"
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|" + MONTH + r"\s+\d{1,2}(?:,?\s+\d{4})?|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+" + MONTH + r"(?:,?\s+\d{4})?|"
    r"\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?|"
    r"\d+\s+(?:seconds?|minutes?|hours?|days?)\s+ago)\b",
    re.I,
)


def clean_url(url: str) -> str:
    checked_url(url)
    parsed = urlsplit(url)
    query = sorted(
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid"}
    )
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            urlencode(query),
            "",
        )
    )


def permitted_domain(url: str, company_domain: str) -> bool:
    try:
        host = checked_url(url)
    except ValueError:
        return False
    domain = DOMAIN(host).top_domain_under_public_suffix
    company = DOMAIN(company_domain).top_domain_under_public_suffix
    return bool(domain and company and (domain == company or domain in ATS_DOMAINS))


def clean_text(text: str) -> str:
    # Date-shaped requisition IDs in actual links are meaningful, not noise.
    parts = re.split(r"(https?://[^\s<>\]]+)", text)
    cleaned = "".join(
        part if part.startswith(("https://", "http://")) else DATE.sub("", part)
        for part in parts
    )
    return " ".join(cleaned.split())


def visible_text(text: str) -> str:
    return re.sub(r"\[https?://[^\]]+\]", "", text).strip()


@dataclass(frozen=True)
class Snapshot:
    lines: list[str]
    links: list[str]
    link_context: list[dict[str, str]]
    content_hash: str
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def visible_text(self) -> str:
        return visible_text(self.text)

    def record(self) -> dict[str, Any]:
        return {
            "lines": self.lines,
            "links": self.links,
            "link_context": self.link_context,
            "hash": self.content_hash,
        }


def snapshot(html: str, page_url: str) -> Snapshot:
    soup = BeautifulSoup(html, "html.parser")
    links: set[str] = set()
    for tag in soup.select("a[href]"):
        try:
            links.add(clean_url(urljoin(page_url, str(tag["href"]))))
        except ValueError:
            continue
    for tag in soup.select(NOISE):
        if not tag.decomposed:
            tag.decompose()
    for tag in soup.select("[style]"):
        if tag.decomposed:
            continue
        if re.search(
            r"(?:display\s*:\s*none|visibility\s*:\s*hidden)",
            str(tag.get("style")),
            re.I,
        ):
            tag.decompose()
    root = soup.select_one("main, [role='main']") or soup.body or soup
    contexts: list[dict[str, str]] = []
    for tag in root.select("a[href]"):
        href = str(tag.get("href", ""))
        if href.startswith("#"):
            continue
        try:
            url = clean_url(urljoin(page_url, href))
        except ValueError:
            continue
        label = clean_text(tag.get_text(" ", strip=True))
        container = tag.find_parent(["article", "li"]) or tag.parent
        context = clean_text(container.get_text(" ", strip=True))[:1500]
        contexts.append({"url": url, "text": label, "context": context})
        tag.replace_with(f"{label} [{url}]")
    lines = list(
        dict.fromkeys(
            line
            for raw in root.get_text("\n").splitlines()
            if (line := clean_text(raw))
        )
    )
    # Listing reordering does not change the semantic snapshot.
    digest = hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()
    return Snapshot(lines, sorted(links), contexts, digest)


def changed_context(previous: list[str], current: Snapshot) -> str | None:
    from difflib import SequenceMatcher

    regions: list[str] = []
    links: list[dict[str, str]] = []
    for op, _a, _b, start, end in SequenceMatcher(
        None, previous, current.lines, autojunk=False
    ).get_opcodes():
        if op not in {"insert", "replace"}:
            continue
        region = "\n".join(current.lines[max(0, start - 2) : end + 2])
        regions.append(region)
        links.extend(link for link in current.link_context if link["url"] in region)
    if not regions:
        return None
    payload = json.dumps({"changed_regions": regions, "nearby_links": links})
    if len(payload) > 60_000:
        raise ValueError("Changed page region exceeds extraction budget")
    return payload


def bounded(call: Callable[[], Any], deadline: float) -> Any:
    """Enforce wall time even if a synchronous transport stops making progress."""
    result: queue.Queue[tuple[bool, Any]] = queue.Queue()

    def worker() -> None:
        try:
            result.put((True, call()))
        except Exception as exc:
            result.put((False, exc))

    threading.Thread(target=worker, daemon=True).start()
    try:
        ok, value = result.get(timeout=max(0, deadline - time.monotonic()))
    except queue.Empty:
        raise TimeoutError("Total page timeout exceeded") from None
    if not ok:
        raise value
    return value


class PageFetcher:
    def __init__(
        self,
        http: HTTPClient,
        timeout: float = 45,
        renderer: Callable[[str, float], tuple[str, str]] | None = None,
    ) -> None:
        self.http = http
        self.timeout = timeout
        self.policy = CareersDiscovery(http)
        self.lock = threading.Lock()
        self.renderer = renderer or self._render
        self._local = threading.local()

    def _response(self, url: str, deadline: float) -> httpx.Response:
        # Cache access is serialized, transport requests use the shared host limiter.
        with self.lock:
            if not self.policy._allowed(url, deadline):
                raise ValueError("robots.txt disallows this page")
        response = self.http.get(url, deadline=deadline)
        if time.monotonic() >= deadline:
            raise TimeoutError("Total page timeout exceeded")
        return response

    def _plain(
        self, url: str, deadline: float, domain: str | None = None
    ) -> tuple[str, str]:
        for _ in range(6):
            if domain and not permitted_domain(url, domain):
                raise UnsafeRedirect("Page redirect left company or known ATS domains")
            response = self._response(url, deadline)
            if response.is_redirect:
                url = urljoin(url, response.headers["location"])
                continue
            response.raise_for_status()
            return response.text, url
        raise ValueError("Too many page redirects")

    def plain(self, url: str, domain: str | None = None) -> tuple[str, str]:
        deadline = time.monotonic() + self.timeout
        return bounded(lambda: self._plain(url, deadline, domain), deadline)

    def fetch(
        self, url: str, method: str | None = None, domain: str | None = None
    ) -> tuple[Snapshot, str]:
        deadline = time.monotonic() + self.timeout

        def work() -> tuple[Snapshot, str]:
            self._local.warnings = []
            if domain and not permitted_domain(url, domain):
                raise ValueError("Careers URL is outside company or known ATS domains")
            if method == "playwright":
                html, final = self.renderer(url, deadline)
                used = "playwright"
            else:
                html, final = self._plain(url, deadline, domain)
                page = snapshot(html, final)
                # A short legitimate no-vacancies page needs no browser.
                no_jobs = re.search(
                    r"no (?:open |current )?(?:jobs|vacancies|positions|openings)",
                    page.visible_text,
                    re.I,
                )
                shell = re.search(
                    r"enable javascript|loading(?:\.\.\.)?", page.visible_text, re.I
                )
                if not no_jobs and (len(page.visible_text) < 100 or shell):
                    html, final = self.renderer(final, deadline)
                    used = "playwright"
                else:
                    used = "http"
            if domain and not permitted_domain(final, domain):
                raise UnsafeRedirect("Rendered page left permitted domains")
            page = snapshot(html, final)
            page.warnings.extend(self._local.warnings)
            if not page.visible_text or re.search(
                r"^(?:loading[. ]*|(?:please )?enable javascript.*)$",
                page.visible_text,
                re.I,
            ):
                raise ValueError("Page text is empty or still a JavaScript shell")
            return page, used

        return bounded(work, deadline)

    def _render(self, url: str, deadline: float) -> tuple[str, str]:
        """Browser requests travel over IPC through the same polite HTTP client.

        Chromium gets no unmediated network access: even scripts and redirects
        need robots permission and host pacing. A subprocess bounds browser hangs.
        """
        if time.monotonic() >= deadline:
            raise TimeoutError("Chromium total page timeout exceeded")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pipeline.sources.render_worker",
                url,
                str(max(1, int((deadline - time.monotonic()) * 1000))),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        messages: queue.Queue[str] = queue.Queue()

        def stop() -> None:
            if process.poll() is None:
                process.terminate()

        watchdog = threading.Timer(max(0, deadline - time.monotonic()), stop)
        watchdog.daemon = True
        watchdog.start()

        def read() -> None:
            assert process.stdout
            for line in process.stdout:
                messages.put(line)
            messages.put("")

        threading.Thread(target=read, daemon=True).start()
        try:
            while True:
                try:
                    line = messages.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    raise TimeoutError("Chromium total page timeout exceeded") from None
                if not line:
                    raise ValueError("Chromium renderer stopped; install its browser")
                message = json.loads(line)
                if message["type"] == "done":
                    return message["html"], message["url"]
                if message["type"] == "error":
                    raise ValueError("Chromium rendering failed")
                try:
                    if message.get("document") and not permitted_domain(
                        message["url"], checked_url(url)
                    ):
                        raise UnsafeRedirect("Browser document left permitted domains")
                    response = self._response(message["url"], deadline)
                    if response.status_code >= 400 and len(self._local.warnings) < 50:
                        self._local.warnings.append(
                            f"Browser resource returned HTTP {response.status_code}"
                        )
                    reply = {
                        "status": response.status_code,
                        "headers": {
                            k: v
                            for k, v in response.headers.items()
                            if k.lower() not in {"content-encoding", "content-length"}
                        },
                        "body": base64.b64encode(response.content).decode(),
                    }
                except UnsafeRedirect:
                    raise
                except Exception as exc:
                    # Block forbidden subresources; main-page failures propagate.
                    if len(self._local.warnings) < 50:
                        self._local.warnings.append(
                            "Browser resource blocked/unavailable ("
                            + type(exc).__name__
                            + ")"
                        )
                    reply = {"abort": True}
                assert process.stdin
                process.stdin.write(json.dumps(reply) + "\n")
                process.stdin.flush()
        finally:
            watchdog.cancel()
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(timeout=max(0, min(2, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                stop()
                process.wait(timeout=2)
            if process.stdout:
                process.stdout.close()
