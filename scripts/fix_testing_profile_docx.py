from pathlib import Path
import shutil

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

SRC = Path(r"c:\Users\pavan\OneDrive\Documents\Pavani Kengana_Testing Profile.docx")
BACKUP = SRC.with_name("Pavani Kengana_Testing Profile_backup.docx")

NAVY = RGBColor(0x1F, 0x49, 0x7D)
BLUE = RGBColor(0x44, 0x72, 0xC4)


def unique_row_cells(row):
    seen, out = set(), []
    for cell in row.cells:
        tid = id(cell._tc)
        if tid not in seen:
            seen.add(tid)
            out.append(cell)
    return out


def cell_text(cell) -> str:
    lines = [p.text.rstrip() for p in cell.paragraphs]
    cleaned: list[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                cleaned.append("")
            continue
        blank_run = 0
        cleaned.append(line.strip())
    return "\n".join(cleaned).strip()


def is_section_header(text: str) -> bool:
    t = text.strip()
    return t.isupper() and 2 < len(t) < 60


def fill_cell_lines(cell, text: str, *, default_align=WD_ALIGN_PARAGRAPH.LEFT, body_size=10.5):
    cell.text = ""
    chunks = text.split("\n\n")
    first = True
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = [ln.strip() for ln in chunk.split("\n") if ln.strip()]
        if not lines:
            continue
        if first:
            p = cell.paragraphs[0]
            first = False
        else:
            p = cell.add_paragraph()
        if len(lines) == 1 and is_section_header(lines[0]):
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            run = p.add_run(lines[0])
            run.bold = True
            run.font.size = Pt(11)
            run.font.color.rgb = NAVY
            continue
        p.alignment = default_align
        for i, line in enumerate(lines):
            if i == 0:
                run = p.add_run(line)
            else:
                if is_section_header(line):
                    p = cell.add_paragraph()
                    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
                    run = p.add_run(line)
                    run.bold = True
                    run.font.size = Pt(11)
                    run.font.color.rgb = NAVY
                else:
                    run = p.add_run("\n" + line)
                    run.font.size = Pt(body_size)
            if i == 0 and not is_section_header(line):
                run.font.size = Pt(body_size)


def main():
    src_doc = Document(str(SRC))
    table = src_doc.tables[0]

    header_text = cell_text(unique_row_cells(table.rows[0])[-1])
    contact_parts = []
    for cell in unique_row_cells(table.rows[1]):
        t = cell_text(cell)
        if t:
            contact_parts.append(t.replace("\n", " ").strip())
    row2 = unique_row_cells(table.rows[2])
    objective = ""
    for cell in row2:
        t = cell_text(cell)
        if t and not is_section_header(t):
            objective = t
            break

    row3 = unique_row_cells(table.rows[3])
    left_text = cell_text(row3[1]) if len(row3) > 1 else ""
    right_text = cell_text(row3[4]) if len(row3) > 4 else ""

    if not BACKUP.exists():
        shutil.copy2(SRC, BACKUP)

    doc = Document()
    for section in doc.sections:
        section.top_margin = Inches(0.5)
        section.bottom_margin = Inches(0.5)
        section.left_margin = Inches(0.6)
        section.right_margin = Inches(0.6)

    layout = doc.add_table(rows=4, cols=2)
    layout.autofit = False
    layout.columns[0].width = Inches(2.15)
    layout.columns[1].width = Inches(4.55)

    # Header
    header = layout.rows[0].cells[0].merge(layout.rows[0].cells[1])
    lines = [ln.strip() for ln in header_text.split("\n") if ln.strip()]
    name = lines[0] if lines else "PAVANI KENGANA"
    title = lines[1] if len(lines) > 1 else "SOFTWARE TESTING & QA PROFESSIONAL"
    hp = header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r1 = hp.add_run(name)
    r1.bold = True
    r1.font.size = Pt(22)
    r1.font.color.rgb = NAVY
    hp.add_run("\n")
    r2 = hp.add_run(title)
    r2.bold = True
    r2.font.size = Pt(12)
    r2.font.color.rgb = BLUE

    # Contact
    contact = layout.rows[1].cells[0].merge(layout.rows[1].cells[1])
    cp = contact.paragraphs[0]
    cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    contact_line = "  |  ".join(contact_parts)
    cr = cp.add_run(contact_line)
    cr.font.size = Pt(10)

    # Objective
    obj = layout.rows[2].cells[0].merge(layout.rows[2].cells[1])
    op = obj.paragraphs[0]
    op.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    orun = op.add_run(objective)
    orun.font.size = Pt(10.5)
    orun.italic = True

    # Two-column body
    left = layout.rows[3].cells[0]
    right = layout.rows[3].cells[1]
    fill_cell_lines(left, left_text, default_align=WD_ALIGN_PARAGRAPH.LEFT, body_size=10)
    fill_cell_lines(right, right_text, default_align=WD_ALIGN_PARAGRAPH.JUSTIFY, body_size=10.5)

    doc.save(str(SRC))
    print(f"Fixed alignment: {SRC}")
    print(f"Backup saved: {BACKUP}")


if __name__ == "__main__":
    main()
