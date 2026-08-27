import argparse
import json
import math
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

user_agent = "NODE-Link-Checker/1.0"
skipped_schemes = ("mailto:", "tel:", "javascript:", "data:")
max_html_bytes = 2 * 1024 * 1024


def clean_netloc(url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()

    if ":" in host:
        host = f"[{host}]"

    try:
        port = parsed.port
    except ValueError:
        return ""

    default_port = (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    )

    if port and not default_port:
        return f"{host}:{port}"

    return host


def normalize_start_url(value: str) -> str:
    value = value.strip()

    if not value:
        raise ValueError("URL не може бути порожнім")

    if "://" not in value:
        value = f"https://{value}"

    parsed = urlsplit(value)

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Підтримуються тільки http та https URL")

    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Введи звичайну адресу сайту без логіна і пароля")

    netloc = clean_netloc(value)

    if not netloc:
        raise ValueError("Не вдалося розпізнати адресу сайту")

    return urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def normalize_url(page_url: str, href: str | None) -> str | None:
    if not href:
        return None

    href = href.strip()

    if not href or href.startswith("#") or href.casefold().startswith(skipped_schemes):
        return None

    try:
        absolute_url = urljoin(page_url, href)
        parsed = urlsplit(absolute_url)
    except ValueError:
        return None

    if parsed.scheme not in {"http", "https"}:
        return None

    if not parsed.hostname or parsed.username or parsed.password:
        return None

    netloc = clean_netloc(absolute_url)

    if not netloc:
        return None

    return urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path or "/",
            parsed.query,
            "",
        )
    )


def is_same_site(url: str, start_url: str) -> bool:
    return clean_netloc(url) == clean_netloc(start_url)


def find_links(html: str, page_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    for tag in soup.find_all("a", href=True):
        url = normalize_url(page_url, tag.get("href"))

        if url:
            links.add(url)

    return sorted(links)


def fetch_page(
    session,
    url: str,
    timeout: float,
    html_limit: int = max_html_bytes,
) -> tuple[dict, str]:
    started_at = time.perf_counter()

    try:
        response = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
            headers={"User-Agent": user_agent},
        )
    except requests.RequestException as error:
        elapsed_ms = round((time.perf_counter() - started_at) * 1000)
        return (
            {
                "url": url,
                "final_url": None,
                "status": None,
                "ok": False,
                "response_time_ms": elapsed_ms,
                "redirect_count": 0,
                "content_type": None,
                "html_truncated": False,
                "error": str(error) or error.__class__.__name__,
            },
            "",
        )

    elapsed_ms = round((time.perf_counter() - started_at) * 1000)
    content_type = response.headers.get("Content-Type", "")
    is_html = "text/html" in content_type.casefold()
    status = response.status_code
    html = ""
    html_truncated = False

    try:
        if is_html and 200 <= status < 400:
            content = bytearray()

            for chunk in response.iter_content(chunk_size=65_536):
                if not chunk:
                    continue

                remaining = html_limit - len(content)

                if remaining <= 0:
                    html_truncated = True
                    break

                content.extend(chunk[:remaining])

                if len(chunk) > remaining:
                    html_truncated = True
                    break

            encoding = response.encoding or "utf-8"
            html = content.decode(encoding, errors="replace")
    finally:
        response.close()

    return (
        {
            "url": url,
            "final_url": response.url,
            "status": status,
            "ok": 200 <= status < 400,
            "response_time_ms": elapsed_ms,
            "redirect_count": len(response.history),
            "content_type": content_type or None,
            "html_truncated": html_truncated,
            "error": None,
        },
        html,
    )


def print_result(result: dict):
    if result["error"]:
        print(f"❌ ERR  {result['url']} ({result['error']})")
        return

    if result["redirect_count"]:
        print(
            f"↪️  {result['status']}  {result['url']} "
            f"→ {result['final_url']} ({result['response_time_ms']} ms)"
        )
        return

    icon = "✅" if result["ok"] else "❌"
    print(
        f"{icon} {result['status']}  {result['url']} ({result['response_time_ms']} ms)"
    )


def build_summary(results: list[dict], external_links: set[str]) -> dict:
    broken = sum(not result["ok"] for result in results)
    redirects = sum(result["redirect_count"] > 0 for result in results)

    return {
        "checked": len(results),
        "working": len(results) - broken,
        "broken": broken,
        "redirects": redirects,
        "external_skipped": len(external_links),
    }


def check_site(
    start_url: str,
    limit: int = 30,
    timeout: float = 8.0,
    delay: float = 0.1,
    session=None,
    quiet: bool = False,
) -> dict:
    start_url = normalize_start_url(start_url)
    queue = deque([start_url])
    queued = {start_url}
    visited = set()
    sources = {start_url: set()}
    external_links = set()
    results = []
    canonical_url = start_url
    limit_reached = False
    owns_session = session is None
    http = session or requests.Session()

    try:
        while queue and len(results) < limit:
            url = queue.popleft()
            visited.add(url)

            result, html = fetch_page(http, url, timeout)
            result["sources"] = sorted(sources.get(url, set()))
            results.append(result)

            if len(results) == 1 and result["final_url"]:
                canonical_url = (
                    normalize_url(start_url, result["final_url"]) or start_url
                )

            if not quiet:
                print_result(result)

            if html and result["ok"]:
                page_url = result["final_url"] or url

                for link in find_links(html, page_url):
                    if not is_same_site(link, canonical_url):
                        external_links.add(link)
                        continue

                    if link not in visited and link not in queued:
                        if len(queued) >= limit:
                            limit_reached = True
                            continue

                        queue.append(link)
                        queued.add(link)

                    sources.setdefault(link, set()).add(url)

            if queue and delay > 0:
                time.sleep(delay)
    finally:
        if owns_session:
            http.close()

    summary = build_summary(results, external_links)

    return {
        "target": start_url,
        "canonical_target": canonical_url,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": {
            "limit": limit,
            "timeout": timeout,
            "delay": delay,
        },
        "summary": summary,
        "limit_reached": limit_reached or bool(queue),
        "external_links": sorted(external_links),
        "results": results,
    }


def save_report(report: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def print_summary(report: dict):
    summary = report["summary"]
    print("\nРезультат:")
    print(f"Перевірено: {summary['checked']}")
    print(f"Працює: {summary['working']}")
    print(f"Битих: {summary['broken']}")
    print(f"Редиректів: {summary['redirects']}")
    print(f"Зовнішніх пропущено: {summary['external_skipped']}")

    if report["limit_reached"]:
        print("⚠️ Досягнуто ліміт сторінок. Збільш його через --limit.")


def positive_int(value: str) -> int:
    number = int(value)

    if number < 1:
        raise argparse.ArgumentTypeError("значення має бути більше нуля")

    return number


def non_negative_float(value: str) -> float:
    number = float(value)

    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("значення не може бути від’ємним")

    return number


def positive_float(value: str) -> float:
    number = float(value)

    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("значення має бути більше нуля")

    return number


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find broken internal links on a website."
    )
    parser.add_argument("url", help="Website address, for example example.com")
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=30,
        help="Maximum number of pages to check (default: 30)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=8.0,
        help="Request timeout in seconds (default: 8)",
    )
    parser.add_argument(
        "--delay",
        type=non_negative_float,
        default=0.1,
        help="Delay between requests in seconds (default: 0.1)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Save a JSON report to this path",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Hide individual results and show only the summary",
    )
    args = parser.parse_args()

    try:
        report = check_site(
            args.url,
            limit=args.limit,
            timeout=args.timeout,
            delay=args.delay,
            quiet=args.quiet,
        )
    except ValueError as error:
        print(f"❌ {error}")
        return 2
    except KeyboardInterrupt:
        print("\n👋 Перевірку зупинено")
        return 130

    print_summary(report)

    if args.output:
        save_report(report, args.output)
        print(f"\n📄 Звіт збережено: {args.output}")

    return 1 if report["summary"]["broken"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
