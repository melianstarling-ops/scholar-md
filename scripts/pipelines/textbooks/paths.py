"""管线产物路径的单一真相源:交付根(md+assets)/ 过程根(_work/修复/报错/自检)双目录。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class DocLayout:
    stem: str
    deliverables_root: str
    work_root: str

    # 交付侧 --------------------------------------------------------------
    @property
    def doc_deliverable_dir(self) -> str:
        return os.path.join(self.deliverables_root, self.stem)

    @property
    def md_path(self) -> str:
        return os.path.join(self.doc_deliverable_dir, f"{self.stem}.md")

    @property
    def assets_dir(self) -> str:
        return os.path.join(self.doc_deliverable_dir, f"{self.stem}.assets")

    # 过程侧 --------------------------------------------------------------
    @property
    def doc_work_dir(self) -> str:
        return os.path.join(self.work_root, self.stem)

    @property
    def work_dir(self) -> str:
        return os.path.join(self.doc_work_dir, "_work")

    @property
    def repair_dir(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_repair")

    @property
    def worklist_path(self) -> str:
        return os.path.join(self.repair_dir, "worklist.json")

    @property
    def formula_candidates_path(self) -> str:
        return os.path.join(self.repair_dir, "formula_candidates.jsonl")

    @property
    def formula_candidates_summary_path(self) -> str:
        return os.path.join(self.repair_dir, "formula_candidates_summary.json")

    @property
    def render_errors_path(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_render_errors.json")

    @property
    def corrections_path(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_corrections.json")

    @property
    def selfcheck_path(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_selfcheck.json")

    @property
    def debug_html_path(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_debug.html")

    @property
    def source_audit_path(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_source_audit.json")

    @property
    def formula_repair_path(self) -> str:
        return os.path.join(self.doc_work_dir, f"{self.stem}_formula_repair.json")

    @property
    def quality_repair_dir(self) -> str:
        # ``doc_work_dir`` already names the document.  Repeating a long stem
        # here pushes nested auto-repair snapshots past Windows MAX_PATH.
        return os.path.join(self.doc_work_dir, "_quality_repair")


def resolve_layout(stem: str, deliverables_root: str,
                   work_root: str | None = None) -> DocLayout:
    """work_root 缺省 = <deliverables_root>/_work_root(交付根下的显眼子树,好 gitignore/好删)。"""
    wr = work_root or os.path.join(deliverables_root, "_work_root")
    return DocLayout(stem=stem, deliverables_root=deliverables_root, work_root=wr)
