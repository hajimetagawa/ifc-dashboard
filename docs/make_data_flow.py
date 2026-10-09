"""
データの流れの図（docs/data_flow.svg）を作る。ダッシュボードの注記タブと README が、この SVG を表示する。

使い方（プロジェクトのフォルダで実行。処理やタブを変えたら、下の LANES を直して実行し直す）:
    uv run python docs/make_data_flow.py
"""
from pathlib import Path
from html import escape

OUT = Path(__file__).resolve().parent / "data_flow.svg"
W, H = 1240, 650
LANES = [
    # 色, 出どころ, 説明, 処理, 処理の説明, 保存先, 説明, 使うタブ
    ("#2a78d6", "IFC investment projects", "Finances One API (DS00499)\n~7,100 projects",
     "Clean and classify", "one row per project · business line · stage ·\nprogram-level amounts set aside",
     "IFC projects", "amounts by instrument", {1, 2, 3, 4, 5}),
    ("#4a3aa7", "World Bank country data", "regions, income groups (API)",
     "Code countries", "ISO3 country codes · IFC regions",
     "Countries", "joined to every project", {1, 2, 3, 4, 5}),
    ("#1c5cab", "OECD country risk", "classification list (PDF)",
     "Read the risk list", "category 0 (low) – 7 (high) per country",
     "Country risk", "latest category per country", {3, 4, 5}),
    ("#eb6834", "EBRD investments", "Excel and project list (CSV) · monthly",
     "Archive and link", "dated copies · Excel linked to CSV\nby project name and country",
     "EBRD operations", "amounts, sector, status", {4, 5}),
    ("#1baf7a", "IFC reports", "country studies (PDF)",
     "Extract and tag text", "paragraphs, boilerplate removed ·\ncountry, industry and theme tags",
     "Report paragraphs", "with page numbers", {5}),
]
TABS = ["Overview", "Portfolio", "Risk", "vs EBRD", "Country"]
COLS = [(24, 270), (330, 620), (680, 900), (960, 1216)]
HEADS = ["1  Where the data comes from", "2  How it is processed", "3  Where it is stored",
         "4  What the dashboard shows"]
TOP, LANE_H, BOX_H = 136, 92, 74

o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" '
     'font-family="Helvetica, Arial, sans-serif">']
o.append('<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
         'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#8a8984"/></marker></defs>')
o.append(f'<rect width="{W}" height="{H}" rx="16" fill="#ffffff"/>')
o.append('<text x="24" y="40" font-size="22" font-weight="700" fill="#1f1e1b">IFC Portfolio Explorer — how the data flows</text>')
o.append('<text x="24" y="64" font-size="13" fill="#6b6a65">Five public sources are processed into one database. '
         'The dashboard reads from it; country codes (ISO3) link everything together.</text>')
for (x0, x1), h in zip(COLS, HEADS):
    o.append(f'<rect x="{x0}" y="84" width="{x1 - x0}" height="{TOP - 84 + len(LANES) * LANE_H}" rx="12" fill="#f7f7f5"/>')
    o.append(f'<text x="{x0 + 14}" y="106" font-size="13" font-weight="700" fill="#5f5e5a">{escape(h)}</text>')

def box(x0, x1, y, color, title, sub, filled=False):
    fill = color if filled else "#ffffff"
    tc = "#ffffff" if filled else "#1f1e1b"
    sc = "#eef4fc" if filled else "#6b6a65"
    o.append(f'<rect x="{x0 + 10}" y="{y}" width="{x1 - x0 - 20}" height="{BOX_H}" rx="10" fill="{fill}" '
             f'stroke="{color}" stroke-width="2"/>')
    if not filled:
        o.append(f'<rect x="{x0 + 10}" y="{y}" width="8" height="{BOX_H}" rx="4" fill="{color}"/>')
    lines = sub.split("\n")
    ty = y + (BOX_H - 18 - 16 * len(lines)) / 2 + 16
    o.append(f'<text x="{x0 + 28}" y="{ty}" font-size="15" font-weight="700" fill="{tc}">{escape(title)}</text>')
    for k, ln in enumerate(lines):
        o.append(f'<text x="{x0 + 28}" y="{ty + 19 + 16 * k}" font-size="12" fill="{sc}">{escape(ln)}</text>')

def arrow(x0, x1, y):
    o.append(f'<line x1="{x0}" y1="{y}" x2="{x1}" y2="{y}" stroke="#8a8984" stroke-width="2" marker-end="url(#ar)"/>')

for i, (c, s1, s2, p1, p2, d1, d2, tabs) in enumerate(LANES):
    y = TOP + i * LANE_H
    mid = y + BOX_H / 2
    box(*COLS[0], y, c, s1, s2)
    box(*COLS[1], y, c, p1, p2)
    box(*COLS[2], y, c, d1, d2)
    arrow(COLS[0][1] - 8, COLS[1][0] + 8, mid)
    arrow(COLS[1][1] - 8, COLS[2][0] + 8, mid)
    arrow(COLS[2][1] - 8, COLS[3][0] + 8, mid)
    # どのタブで使うか（使うタブは色付き）
    cw, gap = 44, 4
    x = COLS[3][0] + 14
    for t in range(1, 6):
        on = t in tabs
        o.append(f'<rect x="{x}" y="{mid - 17}" width="{cw}" height="34" rx="8" '
                 f'fill="{c if on else "#ffffff"}" stroke="{c if on else "#d3d2cc"}" stroke-width="1.5"/>')
        o.append(f'<text x="{x + cw / 2}" y="{mid + 5}" font-size="14" font-weight="700" text-anchor="middle" '
                 f'fill="{"#ffffff" if on else "#c3c2b7"}">{t}</text>')
        x += cw + gap

# データベースのまとまり
y0, y1 = TOP - 6, TOP + (len(LANES) - 1) * LANE_H + BOX_H + 6
o.append(f'<rect x="{COLS[2][0] + 4}" y="{y0}" width="{COLS[2][1] - COLS[2][0] - 8}" height="{y1 - y0}" rx="12" '
         'fill="none" stroke="#1baf7a" stroke-width="1.5" stroke-dasharray="6 4"/>')
# タブ名（チップの列の上）
for k, name in enumerate(TABS):
    cx = COLS[3][0] + 14 + k * 48 + 22
    o.append(f'<text x="{cx}" y="{TOP - 8}" font-size="10.5" text-anchor="middle" fill="#5f5e5a">{escape(name)}</text>')
ly = TOP + len(LANES) * LANE_H + 10
o.append(f'<text x="{COLS[3][0] + 14}" y="{ly}" font-size="12" fill="#4a3aa7">Coloured = the tab uses this data</text>')
o.append(f'<text x="{COLS[3][0] + 14}" y="{ly + 18}" font-size="12" fill="#4a3aa7">(Streamlit app, app/app.py)</text>')
o.append(f'<text x="{COLS[2][0] + 14}" y="{ly}" font-size="12" fill="#0f7a52">One database file (DuckDB),</text>')
o.append(f'<text x="{COLS[2][0] + 14}" y="{ly + 18}" font-size="12" fill="#0f7a52">rebuilt by re-running the scripts</text>')
o.append(f'<text x="{COLS[1][0] + 14}" y="{ly}" font-size="12" fill="#b4501d">Python scripts in src/, using</text>')
o.append(f'<text x="{COLS[1][0] + 14}" y="{ly + 18}" font-size="12" fill="#b4501d">hand-edited reference tables in sources/</text>')
o.append(f'<text x="{COLS[0][0] + 14}" y="{ly}" font-size="12" fill="#1c5cab">All sources are public and free;</text>')
o.append(f'<text x="{COLS[0][0] + 14}" y="{ly + 18}" font-size="12" fill="#1c5cab">raw files are kept with their date</text>')
o.append("</svg>")
OUT.write_text("\n".join(o), encoding="utf-8")
print(f"保存: {OUT}")
