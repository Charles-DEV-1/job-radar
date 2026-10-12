"""Find ATS links/embeds on a bounded set of robots-permitted official pages."""

import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from pipeline.http import USER_AGENT, HTTPClient, checked_url
from pipeline.models import BoardLink, Company, DiscoveryResult
from pipeline.robots import RobotsPolicy


def owns_url(company: Company, url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    domain = company.domain.lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def parse_board_url(url: str, evidence_url: str | None = None) -> BoardLink | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    ats = None
    slug = None
    if host in {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.eu.greenhouse.io",
    }:
        ats = "greenhouse"
        if parts and parts[0] == "embed":
            slug = query.get("for", [None])[0]
        elif parts:
            slug = parts[0]
    elif host == "boards-api.greenhouse.io" and parts[:2] == ["v1", "boards"]:
        ats, slug = "greenhouse", parts[2] if len(parts) > 2 else None
    elif host in {"jobs.lever.co", "jobs.eu.lever.co"} and parts:
        ats, slug = "lever", parts[0]
    elif host == "api.lever.co" and parts[:2] == ["v0", "postings"]:
        ats, slug = "lever", parts[2] if len(parts) > 2 else None
    elif host == "jobs.ashbyhq.com" and parts:
        ats, slug = "ashby", parts[0]
    elif host == "api.ashbyhq.com" and parts[:2] == ["posting-api", "job-board"]:
        ats, slug = "ashby", parts[2] if len(parts) > 2 else None
    elif host == "apply.workable.com" and parts:
        ats = "workable"
        if parts[:4] == ["api", "v1", "widget", "accounts"]:
            slug = parts[4] if len(parts) > 4 else None
        elif parts[0] not in {"api", "j", "jobs"}:
            slug = parts[0]
    elif host.endswith(".workable.com") and host.count(".") == 2:
        prefix = host.split(".")[0]
        if prefix not in {"apply", "www", "jobs", "resources", "help", "api"}:
            ats, slug = "workable", prefix
    elif host.endswith(".recruitee.com") and host.count(".") == 2:
        prefix = host.split(".")[0]
        if prefix not in {"www", "api", "help", "support"}:
            ats, slug = "recruitee", prefix
    elif host in {"careers.smartrecruiters.com", "jobs.smartrecruiters.com"} and parts:
        ats, slug = "smartrecruiters", parts[0]
    elif host == "api.smartrecruiters.com" and parts[:2] == ["v1", "companies"]:
        ats, slug = "smartrecruiters", parts[2] if len(parts) > 2 else None
    if ats and slug and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", slug):
        return BoardLink(ats, slug, evidence_url)
    return None


def page_links(html: str, page_url: str) -> tuple[list[BoardLink], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    career_pages: list[str] = []
    for tag in soup.find_all(["a", "iframe", "script", "link"]):
        for attr in ("href", "src", "data-src"):
            value = tag.get(attr)
            if isinstance(value, str):
                urls.append(urljoin(page_url, value))
                if tag.name == "a" and re.search(
                    r"careers?|jobs|join[- ]?us|vacancies",
                    value + " " + tag.get_text(" "),
                    re.IGNORECASE,
                ):
                    career_pages.append(urljoin(page_url, value))
    # Also support inline JavaScript embeds and JSON strings with escaped slashes.
    for script in soup.find_all("script"):
        text = script.get_text().replace("\\/", "/")
        urls.extend(re.findall(r"https?://[^\s\"'<>`\\]+", text))
    found: list[BoardLink] = []
    for url in urls:
        link = parse_board_url(url, page_url)
        if link and link not in found:
            found.append(link)
    return found, list(dict.fromkeys(career_pages))


class CareersDiscovery:
    def __init__(self, http: HTTPClient) -> None:
        self.http = http
        self.robots: dict[str, RobotFileParser | bool] = {}

    def _allowed(self, url: str, deadline: float | None = None) -> bool:
        checked_url(url)
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.robots:
            robots_url = origin + "/robots.txt"
            # Follow robots redirects with host pacing; fail closed on errors.
            for _ in range(5):
                response = self.http.get(robots_url, deadline=deadline)
                if response.is_redirect:
                    robots_url = urljoin(robots_url, response.headers["location"])
                    checked_url(robots_url)
                    continue
                if response.status_code in {404, 410}:
                    self.robots[origin] = True
                elif response.status_code in {401, 403}:
                    self.robots[origin] = False
                else:
                    response.raise_for_status()
                    if (
                        "<html" in response.text.lower()
                        or "<!doctype html" in response.text.lower()
                    ):
                        raise ValueError(
                            "robots.txt returned HTML; page access not attempted"
                        )
                    parser = RobotsPolicy(origin + "/robots.txt")
                    parser.parse(response.text.splitlines())
                    interval = parser.polite_interval(USER_AGENT)
                    self.http.set_host_interval(url, interval)
                    self.robots[origin] = parser
                break
            else:
                raise ValueError("Too many robots.txt redirects")
        policy = self.robots[origin]
        return policy if isinstance(policy, bool) else policy.can_fetch(USER_AGENT, url)

    def discover(self, company: Company) -> DiscoveryResult:
        result = DiscoveryResult()
        homepage = f"https://{company.domain}/"
        queue = list(dict.fromkeys([company.careers_url or homepage, homepage]))
        visited: set[str] = set()
        page_count = 0
        while queue and page_count < 4:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            page_count += 1
            # A configured external board is a candidate, not official-site proof.
            configured = parse_board_url(url)
            if configured and not owns_url(company, url):
                result.links.append(configured)
                continue
            if not owns_url(company, url):
                result.errors.append(
                    "Configured careers URL is outside the official domain"
                )
                continue
            try:
                current = url
                for _ in range(5):
                    if not self._allowed(current):
                        raise ValueError("robots.txt disallows this page")
                    response = self.http.get(current)
                    if response.is_redirect:
                        target = urljoin(current, response.headers["location"])
                        checked_url(target)
                        if not owns_url(company, target):
                            linked = parse_board_url(target, current)
                            if linked:
                                result.links.append(linked)
                                break
                            raise ValueError("Redirect left the official domain")
                        current = target
                        continue
                    response.raise_for_status()
                    links, career_pages = page_links(response.text, current)
                    result.links.extend(links)
                    queue.extend(
                        page
                        for page in career_pages
                        if page not in visited and owns_url(company, page)
                    )
                    break
                else:
                    raise ValueError("Too many careers-page redirects")
            except httpx.TransportError as exc:
                result.errors.append(f"{url}: {type(exc).__name__}")
            except httpx.HTTPStatusError as exc:
                result.errors.append(f"{url}: HTTP {exc.response.status_code}")
            except (ValueError, KeyError) as exc:
                result.errors.append(f"{url}: {exc}")
        result.links = list(dict.fromkeys(result.links))
        return result
