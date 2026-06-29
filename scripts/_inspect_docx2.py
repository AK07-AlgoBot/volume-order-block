from pathlib import Path
from docx import Document
from docx.oxml.ns import qn

p = Path(r"c:\Users\pavan\OneDrive\Documents\Pavani Kengana_Testing Profile.docx")
doc = Document(str(p))
table = doc.tables[0]

for ri, row in enumerate(table.rows):
    print(f"\n=== Row {ri} ===")
    seen = set()
    for ci, cell in enumerate(row.cells):
        tc_id = id(cell._tc)
        if tc_id in seen:
            continue
        seen.add(tc_id)
        grid_span = cell._tc.get(qn("w:gridSpan"))
        text = cell.text.strip()
        if text:
            print(f"--- Cell col{ci} span={grid_span} ---")
            print(text[:500])
            if len(text) > 500:
                print("...")

tbl = table._tbl
grid = tbl.find(qn("w:tblGrid"))
if grid is not None:
    cols = grid.findall(qn("w:gridCol"))
    widths = [c.get(qn("w:w")) for c in cols]
    print("\nGrid widths:", widths)
