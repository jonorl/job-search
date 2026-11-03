import json
from pathlib import Path

import typst

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"


def render(template: str, data: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    typst.compile(
        TEMPLATES / f"{template}.typ",
        output=output,
        root=TEMPLATES,
        # Only the bundled Carlito, so a PDF built on the VPS looks the same as one built here.
        font_paths=[ROOT / "fonts"],
        ignore_system_fonts=True,
        sys_inputs={"data": json.dumps(data)},
    )
