import os
import unittest

import proof

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


class DedupeTests(unittest.TestCase):
    def test_repeats_collapse(self):
        out = "\n".join(["error: boom 12"] * 5 + ["error: other 3"])
        groups = proof.dedupe_lines(out)
        counts = {sample: count for count, sample in groups}
        self.assertTrue(any(c == 5 for c in counts.values()))

    def test_empty_output(self):
        self.assertEqual(proof.dedupe_lines(""), [])


class AnalyzeHtmlTests(unittest.TestCase):
    def test_empty_body_is_white_screen(self):
        html = "<html><head><title>T</title></head><body></body></html>"
        status, detail, _ = proof.analyze_html(html, "fixture")
        self.assertEqual(status, "FAIL")
        self.assertIn("white screen", detail)

    def test_healthy_page_passes(self):
        html = ("<html><head><title>Ok</title></head><body>"
                "<h1>Hello</h1><p>Plenty of visible text on this page "
                "for a real user to read and use every day.</p>"
                "</body></html>")
        status, _, warnings = proof.analyze_html(html, "fixture")
        self.assertEqual(status, "PASS")
        self.assertEqual(warnings, [])

    def test_missing_title_is_warning_not_failure(self):
        html = ("<html><body><p>Visible text that is long enough to "
                "pass the heuristics without any title at all.</p>"
                "</body></html>")
        status, _, warnings = proof.analyze_html(html, "fixture")
        self.assertEqual(status, "PASS")
        self.assertTrue(any("title" in w for w in warnings))

    def test_visible_text_strips_scripts(self):
        html = ("<html><body><script>var secret_words = 'lots of text "
                "hidden in a script tag should not count';</script>"
                "</body></html>")
        self.assertEqual(proof.visible_text(html), "")


class DetectTests(unittest.TestCase):
    def test_detects_node_scripts(self):
        info = proof.detect_project(os.path.join(FIXTURES, "broken-node"))
        self.assertTrue(info["node"])
        self.assertIn("build", info["scripts"])
        self.assertNotIn("test", info["scripts"])

    def test_detects_static_index(self):
        info = proof.detect_project(os.path.join(FIXTURES, "healthy-static"))
        self.assertTrue(info["static_index"])
        self.assertFalse(info["node"])


class EndToEndTests(unittest.TestCase):
    """The owner's agreed planted-error acceptance tests."""

    def test_broken_node_build_fails(self):
        report, verdict = proof.build_report(
            os.path.join(FIXTURES, "broken-node"))
        self.assertEqual(verdict, "FAIL")
        self.assertIn("npm run build", report)
        self.assertIn("deliberate build failure", report)

    def test_white_screen_fails(self):
        report, verdict = proof.build_report(
            os.path.join(FIXTURES, "white-screen"))
        self.assertEqual(verdict, "FAIL")
        self.assertIn("white screen", report.lower())

    def test_healthy_project_passes(self):
        report, verdict = proof.build_report(
            os.path.join(FIXTURES, "healthy-static"))
        self.assertEqual(verdict, "PASS")
        self.assertIn("## Verdict: PASS", report)

    def test_report_structure(self):
        report, _ = proof.build_report(os.path.join(FIXTURES, "healthy-static"))
        for needle in ("# proof report", "## Verdict:", "## Checks",
                       "| Check | Status |"):
            self.assertIn(needle, report)

    def test_missing_scripts_are_not_failures(self):
        # broken-node has no lint/typecheck/test scripts: they must
        # never appear as checks, and never as failures.
        report, _ = proof.build_report(os.path.join(FIXTURES, "broken-node"))
        self.assertNotIn("npm run lint", report)
        self.assertNotIn("npm run test", report)


if __name__ == "__main__":
    unittest.main()
