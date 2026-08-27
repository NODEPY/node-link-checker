import argparse
import json
import tempfile
import unittest
from pathlib import Path

import requests

from link_checker import (
    check_site,
    fetch_page,
    find_links,
    is_same_site,
    non_negative_float,
    normalize_start_url,
    normalize_url,
    positive_float,
    save_report,
)


class FakeResponse:
    def __init__(
        self,
        url: str,
        status: int = 200,
        html: str = "",
        content_type: str = "text/html; charset=utf-8",
        redirects: int = 0,
    ):
        self.url = url
        self.status_code = status
        self.text = html
        self.content = html.encode("utf-8")
        self.encoding = "utf-8"
        self.headers = {"Content-Type": content_type}
        self.history = [object()] * redirects
        self.closed = False
        self.iterated = False

    def iter_content(self, chunk_size: int):
        self.iterated = True

        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses: dict):
        self.responses = responses
        self.requested = []

    def get(self, url: str, **kwargs):
        self.requested.append(url)
        response = self.responses[url]

        if isinstance(response, Exception):
            raise response

        return response


class UrlTest(unittest.TestCase):
    def test_start_url_adds_https_and_root_path(self):
        self.assertEqual(
            normalize_start_url("Example.COM"),
            "https://example.com/",
        )

    def test_start_url_rejects_unsupported_scheme(self):
        with self.assertRaises(ValueError):
            normalize_start_url("ftp://example.com")

    def test_link_is_normalized_and_fragment_is_removed(self):
        self.assertEqual(
            normalize_url("https://example.com/docs/page", "../about#team"),
            "https://example.com/about",
        )

    def test_special_links_are_ignored(self):
        self.assertIsNone(normalize_url("https://example.com", "mailto:me@test.com"))
        self.assertIsNone(normalize_url("https://example.com", "#section"))

    def test_malformed_link_is_ignored(self):
        self.assertIsNone(normalize_url("https://example.com", "http://[::1"))

    def test_find_links_removes_duplicates(self):
        html = """
        <a href="/about">About</a>
        <a href="/about#team">Team</a>
        <a href="https://other.test/page">External</a>
        """

        self.assertEqual(
            find_links(html, "https://example.com/"),
            ["https://example.com/about", "https://other.test/page"],
        )

    def test_http_and_https_on_same_host_are_internal(self):
        self.assertTrue(
            is_same_site("http://example.com/about", "https://example.com/")
        )

    def test_numeric_options_reject_non_finite_values(self):
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    positive_float(value)
                with self.assertRaises(argparse.ArgumentTypeError):
                    non_negative_float(value)


class CrawlerTest(unittest.TestCase):
    def test_checker_uses_canonical_host_after_start_redirect(self):
        responses = {
            "https://example.com/": FakeResponse(
                "https://www.example.com/",
                html='<a href="/about">About</a>',
                redirects=1,
            ),
            "https://www.example.com/about": FakeResponse(
                "https://www.example.com/about"
            ),
        }

        report = check_site(
            "https://example.com",
            delay=0,
            session=FakeSession(responses),
            quiet=True,
        )

        self.assertEqual(report["canonical_target"], "https://www.example.com/")
        self.assertEqual(report["summary"]["checked"], 2)
        self.assertEqual(report["summary"]["external_skipped"], 0)

    def test_non_html_response_is_not_downloaded(self):
        response = FakeResponse(
            "https://example.com/archive.zip",
            html="x" * 100_000,
            content_type="application/zip",
        )
        session = FakeSession({"https://example.com/archive.zip": response})

        result, html = fetch_page(
            session,
            "https://example.com/archive.zip",
            timeout=2,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(html, "")
        self.assertFalse(response.iterated)
        self.assertTrue(response.closed)

    def test_large_html_response_is_truncated(self):
        response = FakeResponse(
            "https://example.com/",
            html="a" * 100,
        )
        session = FakeSession({"https://example.com/": response})

        result, html = fetch_page(
            session,
            "https://example.com/",
            timeout=2,
            html_limit=20,
        )

        self.assertEqual(len(html), 20)
        self.assertTrue(result["html_truncated"])

    def test_checker_crawls_internal_links_and_finds_broken_page(self):
        start_html = """
        <a href="/about">About</a>
        <a href="/missing">Missing</a>
        <a href="https://external.test/">External</a>
        """
        responses = {
            "https://example.com/": FakeResponse(
                "https://example.com/",
                html=start_html,
            ),
            "https://example.com/about": FakeResponse(
                "https://example.com/about",
                html='<a href="/">Home</a>',
            ),
            "https://example.com/missing": FakeResponse(
                "https://example.com/missing",
                status=404,
                html="Not found",
            ),
        }
        session = FakeSession(responses)

        report = check_site(
            "https://example.com",
            limit=10,
            delay=0,
            session=session,
            quiet=True,
        )

        self.assertEqual(report["summary"]["checked"], 3)
        self.assertEqual(report["summary"]["working"], 2)
        self.assertEqual(report["summary"]["broken"], 1)
        self.assertEqual(report["summary"]["external_skipped"], 1)
        self.assertEqual(
            report["external_links"],
            ["https://external.test/"],
        )

    def test_request_error_is_saved_as_broken(self):
        session = FakeSession(
            {"https://example.com/": requests.Timeout("request timed out")}
        )

        report = check_site(
            "example.com",
            delay=0,
            session=session,
            quiet=True,
        )

        self.assertEqual(report["summary"]["broken"], 1)
        self.assertEqual(report["results"][0]["error"], "request timed out")

    def test_page_limit_stops_the_crawl(self):
        responses = {
            "https://example.com/": FakeResponse(
                "https://example.com/",
                html='<a href="/one">One</a><a href="/two">Two</a>',
            ),
            "https://example.com/one": FakeResponse("https://example.com/one"),
            "https://example.com/two": FakeResponse("https://example.com/two"),
        }

        report = check_site(
            "example.com",
            limit=1,
            delay=0,
            session=FakeSession(responses),
            quiet=True,
        )

        self.assertTrue(report["limit_reached"])
        self.assertEqual(report["summary"]["checked"], 1)

    def test_json_report_is_written(self):
        report = {
            "target": "https://example.com/",
            "summary": {"checked": 1, "working": 1, "broken": 0},
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "reports" / "report.json"
            save_report(report, path)
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved, report)


if __name__ == "__main__":
    unittest.main()
