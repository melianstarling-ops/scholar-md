import json, re, os

WORK = r"D:\Projects\Project_scholar-md\03_Output\textbooks\_realrun_100page_test\Paul_p1-100_scan\_work"
MD_PATH = r"D:\Projects\Project_scholar-md\03_Output\textbooks\_realrun_100page_test\Paul_p1-100_scan\Paul_p1-100_scan.md"

def _probe(content):
    s = re.sub(r"[\s$]", "", content or "")
    return s[:12]

with open(MD_PATH, encoding="utf-8") as f:
    md = f.read()
md_flat = re.sub(r"[\s$]", "", md)

all_blocks = []  # (page, block)
for page in range(1, 101):
    p = os.path.join(WORK, f"page_{page:04d}_res.json")
    if not os.path.exists(p):
        continue
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    for b in data.get("parsing_res_list", []):
        all_blocks.append((page, b))

rows = []
missing_idx = 0
for page, b in all_blocks:
    if b.get("block_order") is None:
        continue
    content = b.get("block_content", "")
    if b.get("block_label") == "formula_number":
        content = content.strip().strip("()")
    probe = _probe(content)
    status = "IN_MD" if (probe and probe in md_flat) else "MISSING"
    if status == "MISSING":
        missing_idx += 1
        rows.append({
            "missing_seq": missing_idx,
            "page": page,
            "block_id": b.get("block_id"),
            "block_order": b.get("block_order"),
            "block_label": b.get("block_label"),
            "block_content_raw": b.get("block_content"),
            "probe": probe,
        })

print(f"total missing found: {len(rows)}")
for r in rows:
    print("----")
    print(f"seq={r['missing_seq']} page={r['page']} block_id={r['block_id']} order={r['block_order']} label={r['block_label']}")
    print(f"content={r['block_content_raw']!r}")

out_path = os.path.join(os.path.dirname(__file__), "missing_map.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)
print(f"wrote {out_path}")
