from __future__ import annotations

import json
import shutil
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO = ROOT / "portfolio"
ASSET_ROOT = (PORTFOLIO / "assets").resolve()
MANIFEST = PORTFOLIO / "source-manifest.json"


def checked_path(base: Path, relative: str) -> Path:
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError(f"path escapes repository root: {relative}")
    return path


def main() -> None:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    copied: list[str] = []
    for entry in payload["assets"]:
        source = checked_path(ROOT, entry["source"])
        output = checked_path(ROOT, entry["output"])
        if not output.is_relative_to(ASSET_ROOT):
            raise ValueError(f"output is outside portfolio/assets: {output}")
        if not source.is_file():
            raise FileNotFoundError(source)

        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)
        with Image.open(output) as image:
            image.verify()
        copied.append(output.relative_to(ROOT).as_posix())

    print(f"copied {len(copied)} portfolio assets")
    for path in copied:
        print(f"- {path}")


if __name__ == "__main__":
    main()
