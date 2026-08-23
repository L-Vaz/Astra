from __future__ import annotations

import argparse
from pathlib import Path

import fitz
from PIL import Image, ImageDraw


def render_pdf(pdf_path: Path, output_dir: Path, scale: float = 2.0) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    document = fitz.open(pdf_path)
    rendered: list[Path] = []
    matrix = fitz.Matrix(scale, scale)
    for index, page in enumerate(document):
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        output_path = output_dir / f"page-{index + 1:02d}.png"
        pixmap.save(output_path)
        rendered.append(output_path)
    document.close()
    return rendered


def create_contact_sheet(images: list[Path], output_path: Path) -> None:
    opened = [Image.open(path).convert("RGB") for path in images]
    thumb_width = 600
    margin = 24
    label_height = 34
    thumbnails: list[Image.Image] = []
    for image in opened:
        ratio = thumb_width / image.width
        thumbnails.append(image.resize((thumb_width, round(image.height * ratio))))

    columns = 2
    rows = (len(thumbnails) + columns - 1) // columns
    cell_height = max(image.height for image in thumbnails) + label_height
    sheet = Image.new(
        "RGB",
        (
            columns * thumb_width + (columns + 1) * margin,
            rows * cell_height + (rows + 1) * margin,
        ),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    for index, image in enumerate(thumbnails):
        column = index % columns
        row = index // columns
        x = margin + column * (thumb_width + margin)
        y = margin + row * cell_height
        sheet.paste(image, (x, y))
        draw.text((x, y + image.height + 6), f"Page {index + 1:02d}", fill="black")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the Astra portfolio PDF for visual QA.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    pages = render_pdf(args.pdf.resolve(), args.output_dir.resolve())
    create_contact_sheet(pages, args.output_dir.resolve() / "contact-sheet.png")
    print(f"Rendered {len(pages)} pages to {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
