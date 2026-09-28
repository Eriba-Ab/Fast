"""
Build Taraba Central Billing and Collections (taraba-billing.html).

Reads the single combined registry workbook, packs every record into a compressed
dataset, and inlines it with the fonts, coat of arms and PDF library into one
offline HTML file. The workbook's own sheet layout (sheet name, headers, column
order) is recorded too, so the platform can regenerate the statutory breakdown
exactly as the spreadsheet is designed.

Usage:
    python build.py                          # uses the default workbook below
    python build.py "path/to/workbook.xlsx"  # a newer workbook with the same kind of columns

Requires: openpyxl  (pip install openpyxl)
"""
import base64
import datetime
import gzip
import json
import re
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent
DEFAULT_WORKBOOK = ROOT / "Taraba_Combined_Registry_2025.xlsx"
TEMPLATE = ROOT / "src" / "template.html"
OUTPUT = ROOT / "taraba-billing.html"
SEAL = ROOT / "Taraba_State_Coat_of_Arms.png"
JSPDF = ROOT / "assets" / "jspdf.umd.min.js"  # PDF export library, inlined so it works offline
FONTS = {"__FONT_LATIN__": ROOT / "assets" / "nunito-latin.woff2",
         "__FONT_LATIN_EXT__": ROOT / "assets" / "nunito-latin-ext.woff2"}

# Revenue heads in the order they appear on a demand notice (the workbook may order
# its columns differently; columns are matched by name). The sub-total is the sum
# of the first seven; the total is kept exactly as the workbook states it.
HEADS = [
    "Presumptive Turnover Tax",
    "Business Premises Registration",
    "Development Levy",
    "Environmental Sanitation Levy",
    "Waste Collection Fee",
    "Fire Safety Certificate",
    "Produce & Commodity Market Fee",
    "Licences",
    "Rent on Government Property",
    "Rent on Land",
]
FEE_HEADS = 7
SIZES = ["Nano", "Small", "Medium"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_HEADER = re.compile(r"^(%s)-(\d{4})$" % "|".join(MONTHS))

# header (lower case, without " (NGN)") -> field the platform fills that column from
FIELDS = {
    "msme registration id": "id",
    "registration id": "id",
    "entity type": "entity",
    "business name": "name",
    "institution / business name": "name",
    "business category": "cat",
    "sector category": "cat",
    "lga": "lga",
    "business address": "addr",
    "phone number": "phone",
    "business size": "size",
    "sub-total fees": "sub",
    "total harmonized assessment": "total",
    "specialized licences": "head:" + str(HEADS.index("Licences")),
}
FIELDS.update({h.lower(): f"head:{i}" for i, h in enumerate(HEADS)})
TOTAL_BILLED = re.compile(r"^total billed( \d{4})?$")  # the sum of the monthly columns


def kobo(value):
    return round((value or 0) * 100)


def clean(value):
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def phone(value):
    if value is None or clean(value).upper() in ("", "N/A", "NA", "NONE"):
        return ""
    text = clean(value)
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    if re.fullmatch(r"\d{10}", text):  # stored as a number, so Excel dropped the leading zero
        text = "0" + text
    return text


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_design(workbook_path):
    """The sheet's own look, lifted from the .xlsx package: styles, theme, column widths,
    frozen panes, row heights, each column's header and data style, and the formula
    each column uses (with the row number as {r}), so exports can match it exactly."""
    import zipfile
    z = zipfile.ZipFile(workbook_path)
    names = set(z.namelist())
    wb_xml = z.read("xl/workbook.xml").decode("utf-8")
    rels = z.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    rid = re.search(r'<sheet\b[^>]*\br:id="([^"]+)"', wb_xml).group(1)
    target = re.search(r'<Relationship\b[^>]*Id="%s"[^>]*Target="([^"]+)"' % re.escape(rid), rels) or \
        re.search(r'<Relationship\b[^>]*Target="([^"]+)"[^>]*Id="%s"' % re.escape(rid), rels)
    sheet_path = "xl/" + target.group(1).lstrip("/").removeprefix("xl/")
    with z.open(sheet_path) as f:
        head = f.read(200_000).decode("utf-8", "replace")

    def attr(tag, name, default=None):
        m = re.search(r'\b%s="([^"]*)"' % name, tag or "")
        return m.group(1) if m else default

    pane = re.search(r"<pane\b[^>]*/>", head)
    fmt = re.search(r"<sheetFormatPr\b[^>]*/>", head)
    cols = re.search(r"<cols>.*?</cols>", head, re.S)
    row1 = re.search(r'<row\b[^>]*\br="1"[^>]*>(.*?)</row>', head, re.S)
    row2 = re.search(r'<row\b[^>]*\br="2"[^>]*>(.*?)</row>', head, re.S)

    def cells(row_match):
        out = {}
        if not row_match:
            return out
        for c in re.finditer(r'<c\b([^>]*?)(?:/>|>(.*?)</c>)', row_match.group(1), re.S):
            ref = re.search(r'\br="([A-Z]+)\d+"', c.group(1)).group(1)
            f = re.search(r"<f\b[^>]*>(.*?)</f>", c.group(2) or "", re.S)  # plain or shared formula
            out[col_index(ref)] = {"s": int(attr(c.group(1), "s", "0")),
                                   "f": re.sub(r"\b([A-Z]{1,3})2\b", r"\1{r}", f.group(1)) if f else None}
        return out

    h, d = cells(row1), cells(row2)
    width = max(list(h) + list(d) + [0]) + 1
    return {
        "styles": z.read("xl/styles.xml").decode("utf-8"),
        "theme": z.read("xl/theme/theme1.xml").decode("utf-8") if "xl/theme/theme1.xml" in names else None,
        "cols": cols.group(0) if cols else "",
        "xSplit": int(float(attr(pane.group(0), "xSplit", "0"))) if pane and attr(pane.group(0), "state") == "frozen" else 0,
        "ySplit": int(float(attr(pane.group(0), "ySplit", "0"))) if pane and attr(pane.group(0), "state") == "frozen" else 0,
        "defaultColWidth": attr(fmt.group(0) if fmt else "", "defaultColWidth"),
        "defaultRowHeight": attr(fmt.group(0) if fmt else "", "defaultRowHeight", "15"),
        "headerHt": attr(re.search(r'<row\b[^>]*\br="1"[^>]*>', head).group(0), "ht") if row1 else None,
        "rowHt": attr(re.search(r'<row\b[^>]*\br="2"[^>]*>', head).group(0), "ht") if row2 else None,
        "headerS": [h.get(i, {}).get("s", 0) for i in range(width)],
        "dataS": [d.get(i, {}).get("s", 0) for i in range(width)],
        "formulas": [d.get(i, {}).get("f") for i in range(width)],
    }


def load(workbook_path):
    wb = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    raw_header = [str(h).strip() if h is not None else "" for h in next(rows)]

    # map every column to a field, in the workbook's own order
    month_cols = sorted(
        ((i, f"{m.group(2)}-{MONTHS.index(m.group(1)) + 1:02d}")
         for i, h in enumerate(raw_header) if (m := MONTH_HEADER.match(h))),
        key=lambda x: x[1])
    if not month_cols:
        raise SystemExit("No monthly columns (e.g. Jan-2025) found in the workbook header.")
    month_index = {i: j for j, (i, _) in enumerate(month_cols)}
    fields = []
    for i, h in enumerate(raw_header):
        key = re.sub(r"\s*\(NGN\)$", "", h, flags=re.I).strip().lower()
        if i in month_index:
            fields.append(f"month:{month_index[i]}")
        elif TOTAL_BILLED.match(key):
            fields.append("billed")
        elif key in FIELDS:
            fields.append(FIELDS[key])
        else:
            raise SystemExit(f'Column "{h}" is not recognised, so the sheet layout cannot be reproduced.')
    col = {f: i for i, f in enumerate(fields)}
    for need in ("id", "name", "lga", "addr", "cat", "size", "total"):
        if need not in col:
            raise SystemExit(f'The workbook has no "{need}" column.')
    c_heads = [col[f"head:{h}"] for h in range(len(HEADS))]

    lgas, cats, entities = [], [], []
    out = {k: [] for k in ("id", "name", "lga", "addr", "phone", "cat", "size", "cls", "total")}
    heads_flat, pay_flat = [], []
    numeric_phones = text_phones = 0
    checks = {"sub": 0, "billed": 0, "rounding": 0}
    for r in rows:
        if not r or not r[col["id"]]:
            continue
        lga, cat, size = clean(r[col["lga"]]), clean(r[col["cat"]]), clean(r[col["size"]]).title()
        rid = clean(r[col["id"]])
        entity = clean(r[col["entity"]]) if "entity" in col else rid.split("/")[1]
        for value, bucket in ((lga, lgas), (cat, cats), (entity, entities)):
            if value not in bucket:
                bucket.append(value)
        if size not in SIZES:
            SIZES.append(size)
        ph = r[col["phone"]] if "phone" in col else None
        if isinstance(ph, (int, float)):
            numeric_phones += 1
        elif ph not in (None, ""):
            text_phones += 1
        heads = [kobo(r[i]) for i in c_heads]
        pay = [kobo(r[i]) for i, _ in month_cols]
        total = kobo(r[col["total"]])
        if "sub" in col and sum(heads[:FEE_HEADS]) != kobo(r[col["sub"]]):
            checks["sub"] += 1
        if "billed" in col and sum(pay) != kobo(r[col["billed"]]):
            checks["billed"] += 1
        if sum(pay) != total:
            checks["rounding"] += 1
        out["id"].append(rid)
        out["name"].append(clean(r[col["name"]]))
        out["lga"].append(lga)
        out["addr"].append(clean(r[col["addr"]]))
        out["phone"].append(phone(ph))
        out["cat"].append(cat)
        out["size"].append(SIZES.index(size))
        out["cls"].append(entities.index(entity))
        out["total"].append(total)
        heads_flat.extend(heads)
        pay_flat.extend(pay)

    if checks["sub"]:
        print(f"  WARNING: {checks['sub']} rows whose Sub-Total Fees differs from the sum of the seven fees")
    if checks["billed"]:
        print(f"  WARNING: {checks['billed']} rows whose Total Billed differs from the sum of the months")
    if checks["rounding"]:
        print(f"  note: {checks['rounding']:,} rows where the monthly total differs from the assessment by a few kobo (rounding in the workbook)")

    lgas.sort()
    cats.sort()
    out["lga"] = [lgas.index(x) for x in out["lga"]]
    out["cat"] = [cats.index(x) for x in out["cat"]]
    out["heads"] = heads_flat
    out["pay"] = pay_flat
    count = len(out["id"])
    layout = {"key": "stat", "title": "Statutory breakdown", "sheet": ws.title,
              "headers": raw_header, "fields": fields, "rows": list(range(count)),
              "phoneNumeric": numeric_phones > text_phones, "design": read_design(workbook_path)}
    formulas = [f"{raw_header[i]} = {f.replace('{r}', 'n')}" for i, f in enumerate(layout["design"]["formulas"]) if f]
    if formulas:
        print("  formulas kept in the statutory breakdown: " + "; ".join(formulas))
    return {
        "source": Path(workbook_path).name,
        "built": datetime.date.today().isoformat(),
        "months": [m for _, m in month_cols],
        "lgas": lgas,
        "cats": cats,
        "sizes": SIZES,
        "entities": entities,
        "heads": HEADS,
        "feeHeads": FEE_HEADS,
        "count": count,
        "cols": out,
        "stat": [layout],
    }


def main():
    workbook = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_WORKBOOK
    print(f"Reading {workbook.name} ...")
    data = load(workbook)
    packed = base64.b64encode(
        gzip.compress(json.dumps(data, separators=(",", ":")).encode("utf-8"), 9)
    ).decode("ascii")

    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("__DATA_GZ_B64__", packed)
    html = html.replace("__SEAL__", base64.b64encode(SEAL.read_bytes()).decode("ascii"))
    for token, path in FONTS.items():
        html = html.replace(token, base64.b64encode(path.read_bytes()).decode("ascii"))
    html = html.replace("__JSPDF__", JSPDF.read_text(encoding="utf-8"))
    OUTPUT.write_text(html, encoding="utf-8")

    paid = sum(data["cols"]["pay"]) / 100
    assessed = sum(data["cols"]["total"]) / 100
    print(f"{data['count']:,} records ({', '.join(data['entities'])}) | {len(data['months'])} months | "
          f"NGN {assessed:,.2f} assessed | NGN {paid:,.2f} collected")
    print(f"Statutory breakdown layout: sheet \"{data['stat'][0]['sheet']}\", {len(data['stat'][0]['headers'])} columns")
    print(f"Wrote {OUTPUT.name} ({OUTPUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
