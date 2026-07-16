from __future__ import annotations

import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


AUDIT_PATH = Path(__file__).with_name("formula_pressure_audit.py")
SPEC = spec_from_file_location("formula_pressure_audit", AUDIT_PATH)
assert SPEC is not None and SPEC.loader is not None
audit = module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class LatexNormalizationTests(unittest.TestCase):
    def test_ignores_math_whitespace_but_preserves_text_content(self) -> None:
        self.assertTrue(hasattr(audit, "normalize_latex"))
        left = r"R = \frac{L}{\sigma w t}+\text{transverse current}"
        right = r"R=\frac{L}{\sigma wt}+\text{transverse current}"
        changed_text = r"R=\frac{L}{\sigma wt}+\text{transversecurrent}"

        self.assertEqual(audit.normalize_latex(left), audit.normalize_latex(right))
        self.assertNotEqual(
            audit.normalize_latex(left), audit.normalize_latex(changed_text)
        )

    def test_removes_only_complete_outer_math_wrapper(self) -> None:
        self.assertTrue(hasattr(audit, "normalize_latex"))
        self.assertEqual(audit.normalize_latex(r"$$ x + y $$"), "x+y")
        self.assertEqual(audit.normalize_latex(r"\[x+y\]"), "x+y")
        self.assertNotEqual(audit.normalize_latex(r"x$+y"), "x+y")


class GroupingTests(unittest.TestCase):
    def test_groups_content_while_retaining_confidence_and_notes(self) -> None:
        self.assertTrue(hasattr(audit, "group_candidate_outputs"))
        rows = [
            ("model-a", {"classification": "formula", "latex": "x + y", "confidence": "high", "note": ""}),
            ("model-b", {"classification": "formula", "latex": "x+y", "confidence": "medium", "note": "check"}),
            ("model-c", {"classification": "formula", "latex": "x-y", "confidence": "high", "note": ""}),
        ]

        groups = audit.group_candidate_outputs(rows)

        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[0]["supporters"], ["model-a", "model-b"])
        self.assertEqual(groups[0]["min_confidence"], "medium")
        self.assertEqual(groups[0]["notes"], ["check"])

    def test_comparison_marks_candidates_with_more_than_one_group(self) -> None:
        self.assertTrue(hasattr(audit, "build_comparison"))
        baseline = [{"candidate_id": "a", "classification": "formula", "latex": "x+y", "confidence": "high"}]
        models = {
            "same": [{"candidate_id": "a", "classification": "formula", "latex": "x + y", "confidence": "high", "note": ""}],
            "different": [{"candidate_id": "a", "classification": "formula", "latex": "x-y", "confidence": "medium", "note": "check"}],
        }

        result = audit.build_comparison(baseline, models)

        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["disagreement_count"], 1)
        self.assertEqual(result["candidates"][0]["candidate_id"], "a")
        self.assertEqual(result["candidates"][0]["groups"][0]["supporters"], ["ROOT", "same"])


class ReportRenderingTests(unittest.TestCase):
    def test_report_places_root_review_before_model_scores(self) -> None:
        self.assertTrue(hasattr(audit, "render_first_round_report"))
        baseline = [{"candidate_id": "a", "classification": "formula", "latex": "x+y", "confidence": "high"}]
        grades = {"models": [{"label": "Model A", "correct": 1, "errors": []}]}
        summary = {"finished": 1, "valid": 1, "failed": 0, "statuses": []}

        report = audit.render_first_round_report(baseline, grades, summary)

        self.assertLess(report.index("ROOT 完整审阅"), report.index("模型严格计分"))
        self.assertIn("`x+y`", report)
        self.assertIn("1/1", report)


if __name__ == "__main__":
    unittest.main()
