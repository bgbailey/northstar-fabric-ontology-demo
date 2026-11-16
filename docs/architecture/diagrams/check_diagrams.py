"""Regenerates the architecture diagrams into a temporary folder and compares their text with
the committed SVG files. Run from anywhere: python docs/architecture/diagrams/check_diagrams.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMAGES = HERE.parent / "images"
SCRIPTS = {
    "unified_context_layer.py": "unified-context-layer.svg",
    "service_assurance_ontology.py": "service-assurance-ontology.svg",
    "question_flow_two_arms.py": "question-flow-two-arms.svg",
}


def texts(svg: Path) -> list[str]:
    body = svg.read_text(encoding="utf-8")
    found = re.findall(r"<(?:text|title|desc)[^>]*>(.*?)</(?:text|title|desc)>", body, flags=re.S)
    return [re.sub(r"<[^>]+>", "", t).strip() for t in found]


def main() -> int:
    failed = False
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, DIAGRAM_OUT=tmp)
        for script, svg in SCRIPTS.items():
            run = subprocess.run([sys.executable, str(HERE / script)], env=env, capture_output=True, text=True)
            if run.returncode != 0:
                print(f"FAIL {script}: {run.stderr.strip()}")
                failed = True
                continue
            # joined, so that a line wrapped at a different word does not count as a difference
            new, old = " ".join(texts(Path(tmp) / svg)), " ".join(texts(IMAGES / svg))
            if new != old:
                print(f"FAIL {svg}: text differs from the committed file; regenerate and commit")
                failed = True
            else:
                print(f"ok   {svg}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
