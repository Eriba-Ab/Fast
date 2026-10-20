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
DEFAULT_WORKBOOK = ROOT / "Taraba State_Consolidated Revenue Collections Report_2025.xlsx"
TEMPLATE = ROOT / "src" / "template.html"
OUTPUT = ROOT / "taraba-billing.html"
PUBLIC_TEMPLATE = ROOT / "src" / "public.html"
BUILD = ROOT / "build"
SEAL = ROOT / "Taraba_State_Coat_of_Arms.png"
JSPDF = ROOT / "assets" / "jspdf.umd.min.js"  # PDF export library, inlined so it works offline
QRCODE = ROOT / "assets" / "qrcode.js"  # QR code generator (qrcode-generator 1.4.4, MIT), inlined

# Demand notices: one assessment date for the tax year (the notice text allows 30 days to pay).
ASSESSMENT_DATE = datetime.date(2025, 12, 23)
PAYMENT_DAYS = 30
FONTS = {"__FONT_LATIN__": ROOT / "assets" / "nunito-latin.woff2",
         "__FONT_LATIN_EXT__": ROOT / "assets" / "nunito-latin-ext.woff2"}

# Every revenue head the platform knows, in the order a demand notice lists them.
# The workbook may order its columns differently (they are matched by name) and may
# carry only some of the later heads; the platform uses the ones present. The first
# seven are the fees that make up "Sub-Total Fees" and must always be there.
HEADS = [
    "Presumptive Turnover Tax",
    "Business Premises Registration",
    "Development Levy",
    "Environmental Sanitation Levy",
    "Waste Collection Fee",
    "Fire Safety Certificate",
    "Produce & Commodity Market Fee",
    "Licences",
    "Fines",
    "Sales",
    "Earnings",
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
    "taraba tax identification number": "tin",
    "tirs tin": "tin",
    "ttin": "tin",
    "tin": "tin",
    "business size": "size",
    "sub-total fees": "sub",
    "total harmonized assessment": "total",
}
HEAD_NAMES = {h.lower(): h for h in HEADS}
HEAD_NAMES["specialized licences"] = "Licences"
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
    # a column's data style is its most common style across the sampled rows, not just row 2
    from collections import Counter
    votes = {}
    for m in re.finditer(r'<row\b[^>]*\br="(\d+)"[^>]*>(.*?)</row>', head, re.S):
        if m.group(1) == "1":
            continue
        for c in re.finditer(r'<c\b[^>]*?\br="([A-Z]+)\d+"([^>]*?)(?:/>|>)', m.group(2)):
            votes.setdefault(col_index(c.group(1)), Counter())[int(attr(c.group(2), "s", "0"))] += 1
    for i, v in votes.items():
        d.setdefault(i, {"s": 0, "f": None})["s"] = v.most_common(1)[0][0]
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


def demand_notice_number(reg_id, year):
    """A stable 15-digit notice number per business and tax year, in the GovPayHub style
    (DN + 15 digits): year, class digit, registration serial, five check digits."""
    import hashlib
    parts = reg_id.split("/")
    klass = {"INST": "1", "MSME": "2"}.get(parts[1] if len(parts) > 1 else "", "9")
    serial = int(re.sub(r"\D", "", parts[-1]) or 0) % 100000
    check = int(hashlib.sha256(f"{reg_id}|{year}".encode()).hexdigest(), 16) % 100000
    return f"DN{year}{klass}{serial:05d}{check:05d}"


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
    keys = [re.sub(r"\s*\(NGN\)$", "", h, flags=re.I).strip().lower() for h in raw_header]
    found = {HEAD_NAMES[k] for k in keys if k in HEAD_NAMES}
    heads_present = [h for h in HEADS if h in found]
    missing_fees = [h for h in HEADS[:FEE_HEADS] if h not in found]
    if missing_fees:
        raise SystemExit("The workbook is missing fee columns: " + ", ".join(missing_fees))
    fields = []
    for i, h in enumerate(raw_header):
        key = re.sub(r"\s*\(NGN\)$", "", h, flags=re.I).strip().lower()
        if i in month_index:
            fields.append(f"month:{month_index[i]}")
        elif TOTAL_BILLED.match(key):
            fields.append("billed")
        elif key in HEAD_NAMES:
            fields.append(f"head:{heads_present.index(HEAD_NAMES[key])}")
        elif key in FIELDS:
            fields.append(FIELDS[key])
        else:
            raise SystemExit(f'Column "{h}" is not recognised, so the sheet layout cannot be reproduced.')
    col = {f: i for i, f in enumerate(fields)}
    for need in ("id", "name", "lga", "addr", "cat", "size", "total"):
        if need not in col:
            raise SystemExit(f'The workbook has no "{need}" column.')
    c_heads = [col[f"head:{h}"] for h in range(len(heads_present))]

    lgas, cats, entities = [], [], []
    out = {k: [] for k in ("id", "tin", "name", "lga", "addr", "phone", "cat", "size", "cls", "total")}
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
        out["tin"].append(clean(r[col["tin"]]) if "tin" in col else "")
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
    year = int(month_cols[0][1][:4])
    out["dn"] = [demand_notice_number(i, year) for i in out["id"]]
    if len(set(out["dn"])) != count:
        raise SystemExit("Generated demand notice numbers are not unique.")
    blank_tin = sum(1 for t in out["tin"] if not t)
    if "tin" in col and blank_tin:
        print(f"  note: {blank_tin:,} businesses have no TIRS TIN")
    layout = {"key": "stat", "title": "Statutory breakdown", "sheet": ws.title,
              "headers": raw_header, "fields": fields, "rows": list(range(count)),
              "phoneNumeric": numeric_phones > text_phones, "design": read_design(workbook_path)}
    formulas = [f"{raw_header[i]} = {f.replace('{r}', 'n')}" for i, f in enumerate(layout["design"]["formulas"]) if f]
    if formulas:
        print("  formulas kept in the statutory breakdown: " + "; ".join(formulas))
    return {
        "source": Path(workbook_path).name,
        "taxYear": year,
        "assessmentDate": ASSESSMENT_DATE.isoformat(),
        "dueDate": (ASSESSMENT_DATE + datetime.timedelta(days=PAYMENT_DAYS)).isoformat(),
        "built": datetime.date.today().isoformat(),
        "months": [m for _, m in month_cols],
        "lgas": lgas,
        "cats": cats,
        "sizes": SIZES,
        "entities": entities,
        "heads": heads_present,
        "feeHeads": FEE_HEADS,
        "count": count,
        "cols": out,
        "stat": [layout],
    }


def write_export(data, workbook):
    """Every notice, flattened, for the payment server's database."""
    c, M, H = data["cols"], len(data["months"]), len(data["heads"])
    rows = []
    for r in range(data["count"]):
        heads = c["heads"][r * H:(r + 1) * H]
        rows.append({
            "dn": c["dn"][r], "reg_id": c["id"][r], "ttin": c["tin"][r], "name": c["name"][r],
            "entity": data["entities"][c["cls"][r]], "category": data["cats"][c["cat"][r]],
            "lga": data["lgas"][c["lga"][r]], "size": data["sizes"][c["size"][r]],
            "address": c["addr"][r], "phone": c["phone"][r],
            "heads": heads, "subtotal": sum(heads[:FEE_HEADS]), "total": c["total"][r],
            "pay": c["pay"][r * M:(r + 1) * M],
        })
    export = {"source": Path(workbook).name, "built": data["built"], "taxYear": data["taxYear"],
              "assessmentDate": data["assessmentDate"], "dueDate": data["dueDate"],
              "months": data["months"], "heads": data["heads"], "feeHeads": FEE_HEADS, "rows": rows}
    out = BUILD / "data" / "assessments.json.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(gzip.compress(json.dumps(export, separators=(",", ":")).encode("utf-8"), 9))
    print(f"Wrote build/data/{out.name} ({out.stat().st_size / 1e6:.1f} MB, {len(rows):,} notices)")


def write_public_site(staff_html):
    """The public page reuses the staff console's stylesheet (with its fonts already
    inlined) so both look identical, but carries no taxpayer data."""
    style = re.search(r"<style>.*?</style>", staff_html, re.S).group(0)
    page = PUBLIC_TEMPLATE.read_text(encoding="utf-8")
    page = page.replace("__STYLE__", style)
    page = page.replace("__SEAL__", base64.b64encode(SEAL.read_bytes()).decode("ascii"))
    page = page.replace("__JSPDF__", JSPDF.read_text(encoding="utf-8"))
    page = page.replace("__QRCODE__", QRCODE.read_text(encoding="utf-8"))
    out = BUILD / "site" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    if "__DATA_GZ_B64__" in page or "TR/MSME/" in page:
        raise SystemExit("The public page must not contain registry data.")
    print(f"Wrote build/site/{out.name} ({out.stat().st_size / 1e3:.0f} KB, no taxpayer data)")


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
    html = html.replace("__QRCODE__", QRCODE.read_text(encoding="utf-8"))
    OUTPUT.write_text(html, encoding="utf-8")

    # --- payment server artefacts (see server/ and deploy/) -------------------
    # build/staff/  the staff console, served only to signed-in officers
    # build/site/   the public page: one notice at a time from the API, no registry inside
    # build/data/   the assessments export the server loads with `python -m app.cli sync`
    (BUILD / "staff").mkdir(parents=True, exist_ok=True)
    (BUILD / "staff" / OUTPUT.name).write_text(html, encoding="utf-8")
    write_export(data, workbook)
    write_public_site(html)

    paid = sum(data["cols"]["pay"]) / 100
    assessed = sum(data["cols"]["total"]) / 100
    print(f"{data['count']:,} records ({', '.join(data['entities'])}) | {len(data['months'])} months | "
          f"NGN {assessed:,.2f} assessed | NGN {paid:,.2f} collected")
    print(f"Statutory breakdown layout: sheet \"{data['stat'][0]['sheet']}\", {len(data['stat'][0]['headers'])} columns")
    print(f"Wrote {OUTPUT.name} ({OUTPUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
