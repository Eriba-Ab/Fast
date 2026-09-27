"""
Build Taraba Central Billing and Collections (taraba-billing.html).

Reads the TIRS CDN billing workbook, packs every record into a compressed
dataset, and inlines it with the fonts and coat of arms into one offline HTML file.

Usage:
    python build.py                          # uses the default workbook below
    python build.py "path/to/workbook.xlsx"  # any workbook with the same columns

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
DEFAULT_WORKBOOK = ROOT / "TIRS_ CDN BILLING_2025 (1).xlsx"
TEMPLATE = ROOT / "src" / "template.html"
OUTPUT = ROOT / "taraba-billing.html"
SEAL = ROOT / "Taraba_State_Coat_of_Arms.png"
FONTS = {"__FONT_LATIN__": ROOT / "assets" / "nunito-latin.woff2",
         "__FONT_LATIN_EXT__": ROOT / "assets" / "nunito-latin-ext.woff2"}

# Revenue heads in the order they appear on a demand notice. The sub-total and the
# total are not heads; the total is kept separately exactly as the workbook states it.
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
FEE_HEADS = 7  # the first seven heads make up the "Sub-Total Fees" line
SIZES = ["Nano", "Small", "Medium"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTH_HEADER = re.compile(r"^(%s)-(\d{4})$" % "|".join(MONTHS))


def kobo(value):
    return round((value or 0) * 100)


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def phone(value):
    if value is None or clean(value).upper() in ("", "N/A", "NA", "NONE"):
        return ""
    text = clean(value)
    if re.fullmatch(r"\d{10}", text):  # Excel dropped the leading zero
        text = "0" + text
    return text


def load(workbook_path):
    wb = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    ws = wb["Master Registry"] if "Master Registry" in wb.sheetnames else wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [clean(h) for h in next(rows)]

    def col(name):
        for i, h in enumerate(header):
            if h.lower().startswith(name.lower()):
                return i
        raise SystemExit(f'Column "{name}" not found in the workbook header.')

    c_id = 0
    c_name = 1  # the business-name column has no header in the source
    c_lga, c_addr, c_phone = col("LGA"), col("Business Address"), col("Phone")
    c_cat, c_size, c_total = col("Business Category"), col("Business Size"), col("Total Harmonized")
    c_heads = [col(h) for h in HEADS]
    months = []
    for i, h in enumerate(header):
        m = MONTH_HEADER.match(h)
        if m:
            months.append((i, f"{m.group(2)}-{MONTHS.index(m.group(1)) + 1:02d}"))
    months.sort(key=lambda x: x[1])
    if not months:
        raise SystemExit("No monthly columns (e.g. Jan-2025) found in the workbook header.")

    lgas, cats = [], []
    out = {k: [] for k in ("id", "name", "lga", "addr", "phone", "cat", "size", "total")}
    heads_flat, pay_flat = [], []
    for r in rows:
        if not r or not r[c_id]:
            continue
        lga, cat, size = clean(r[c_lga]), clean(r[c_cat]), clean(r[c_size]).title()
        if lga not in lgas:
            lgas.append(lga)
        if cat not in cats:
            cats.append(cat)
        if size not in SIZES:
            SIZES.append(size)
        out["id"].append(clean(r[c_id]))
        out["name"].append(clean(r[c_name]))
        out["lga"].append(lga)
        out["addr"].append(clean(r[c_addr]))
        out["phone"].append(phone(r[c_phone]))
        out["cat"].append(cat)
        out["size"].append(SIZES.index(size))
        out["total"].append(kobo(r[c_total]))
        heads_flat.extend(kobo(r[i]) for i in c_heads)
        pay_flat.extend(kobo(r[i]) for i, _ in months)

    lgas.sort()
    cats.sort()
    out["lga"] = [lgas.index(x) for x in out["lga"]]
    out["cat"] = [cats.index(x) for x in out["cat"]]
    out["heads"] = heads_flat
    out["pay"] = pay_flat
    return {
        "source": Path(workbook_path).name,
        "built": datetime.date.today().isoformat(),
        "months": [m for _, m in months],
        "lgas": lgas,
        "cats": cats,
        "sizes": SIZES,
        "heads": HEADS,
        "feeHeads": FEE_HEADS,
        "count": len(out["id"]),
        "cols": out,
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
    OUTPUT.write_text(html, encoding="utf-8")

    paid = sum(data["cols"]["pay"]) / 100
    print(f"{data['count']:,} records | {len(data['months'])} months | "
          f"NGN {paid:,.2f} collected")
    print(f"Wrote {OUTPUT.name} ({OUTPUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
