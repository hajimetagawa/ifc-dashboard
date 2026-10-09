"""
データの流れの図（docs/data_flow.svg）を作る。開発者向けに、元データ → 処理 → テーブルとビュー（主なキーと関係）
→ 使う側（ダッシュボード、Power BI）を1枚にまとめる。ダッシュボードの注記タブと README が、この SVG を表示する。

使い方（プロジェクトのフォルダで実行。テーブルや処理を変えたら、下の定義を直して実行し直す）:
    uv run python docs/make_data_flow.py
"""

from __future__ import annotations

from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent / "data_flow.svg"
W, H = 1780, 1230
FONT = "Helvetica, Arial, sans-serif"
MONO = "Menlo, Consolas, monospace"

C = {"ifc": "#2a78d6", "cty": "#4a3aa7", "ebrd": "#eb6834", "doc": "#1baf7a", "app": "#5f5e5a",
     "line": "#8a8984", "text": "#1f1e1b", "sub": "#6b6a65", "panel": "#f7f7f5", "key": "#4a3aa7"}

o: list[str] = []


def text(x, y, s, size=12, weight=400, color=C["text"], anchor="start", font=FONT):
    o.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}" '
             f'text-anchor="{anchor}" font-family="{font}">{escape(s)}</text>')


def rect(x, y, w, h, stroke, fill="#ffffff", dash=False, rx=8, sw=1.5):
    d = ' stroke-dasharray="5 3"' if dash else ""
    o.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" '
             f'stroke-width="{sw}"{d}/>')


def line(x1, y1, x2, y2, color=C["line"], dash=False, arrow=True, label=None, label_at=0.5, label_dy=-5, width=1.5):
    d = ' stroke-dasharray="5 4"' if dash else ""
    m = ' marker-end="url(#ar)"' if arrow else ""
    if abs(y1 - y2) < 1 or abs(x1 - x2) < 1:
        o.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{width}"{d}{m}/>')
    else:   # 折れ線（横 → 縦 → 横）
        mx = (x1 + x2) / 2
        o.append(f'<path d="M{x1},{y1} H{mx} V{y2} H{x2}" fill="none" stroke="{color}" stroke-width="{width}"{d}{m}/>')
    if label:
        lx = x1 + (x2 - x1) * label_at
        ly = (y1 if label_at < 0.5 else y2) + label_dy
        text(lx, ly, label, 10, color=C["sub"], anchor="middle", font=MONO)


def table(x, y, w, name, cols, color, view=False, note=None):
    """テーブル（実線）またはビュー（破線）の箱。各列の y 座標を返す。"""
    h = 26 + 16 * len(cols) + (16 if note else 0) + 6
    rect(x, y, w, h, color, dash=view)
    o.append(f'<rect x="{x + 1}" y="{y + 1}" width="{w - 2}" height="23" rx="7" fill="{color}" '
             f'opacity="{0.10 if view else 0.16}"/>')
    text(x + 10, y + 17, name, 12, 700, font=MONO)
    if len(name) * 7.4 + 60 < w:      # 名前が長いときは種類の表示を省く
        text(x + w - 8, y + 17, "view" if view else "table", 10, color=C["sub"], anchor="end")
    pos = {}
    for i, (c, key) in enumerate(cols):
        cy = y + 40 + 16 * i
        badge = {"PK": "#0f7a52", "FK": "#b4501d", "K": C["key"]}.get(key)
        if badge:
            text(x + 10, cy, key, 9, 700, color=badge, font=MONO)
        text(x + 32, cy, c, 11, font=MONO)
        pos[c] = cy - 4
    if note:
        text(x + 10, y + 40 + 16 * len(cols), note, 10, color=C["sub"])
    return {"x": x, "y": y, "w": w, "h": h, "r": x + w, "cy": y + h / 2, "col": pos}


def card(x, y, w, title, lines, color, mono_title=True):
    h = 28 + 15 * len(lines)
    rect(x, y, w, h, color)
    o.append(f'<rect x="{x}" y="{y}" width="6" height="{h}" rx="3" fill="{color}"/>')
    text(x + 14, y + 18, title, 12, 700, font=MONO if mono_title else FONT)
    for i, ln in enumerate(lines):
        text(x + 14, y + 35 + 15 * i, ln, 10.5, color=C["sub"])
    return {"x": x, "y": y, "w": w, "h": h, "r": x + w, "cy": y + h / 2}


# ---------------------------------------------------------------- 枠
o.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="100%" font-family="{FONT}">')
o.append('<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
         f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{C["line"]}"/></marker></defs>')
rect(0, 0, W, H, "#ffffff", "#ffffff", rx=16)
text(24, 38, "IFC Portfolio Explorer — data pipeline and database schema", 22, 700)
text(24, 62, "Raw files are processed by the scripts in src/ into one DuckDB database (data/curated/ifc.duckdb). "
             "Solid boxes are tables, dashed boxes are views. Arrows join a PK or K column to the FK column of the next table.", 13,
     color=C["sub"])
for title, x, w in (("1  Raw files (data/raw/, sources/)", 20, 250), ("2  Scripts (src/) and processing", 290, 290),
                    ("3  Database: tables and views", 610, 850), ("4  Consumers", 1470, 290)):
    rect(x, 80, w, H - 100, C["panel"], C["panel"], rx=12)
    text(x + 12, 102, title, 13, 700, color="#5f5e5a")

# ---------------------------------------------------------------- 1 元データ
r_ifc = card(30, 120, 235, "IFC projects", ["Finances One API (DS00499)", "structured/ifc_projects_*.json"], C["ifc"],
             mono_title=False)
r_wb = card(30, 360, 235, "World Bank countries", ["country API", "structured/wb_countries_*.json"], C["cty"],
            mono_title=False)
r_oecd = card(30, 440, 235, "OECD country risk (PDF)", ["country/oecd_crc_*.pdf"], C["cty"], mono_title=False)
r_fx = card(30, 500, 235, "World Bank exchange rates", ["PA.NUS.FCRF", "country/wb_fx_*.json"], C["cty"],
            mono_title=False)
r_ebrd = card(30, 650, 235, "EBRD investments + project list",
              ["Excel (List sheet) and CSV, monthly", "ebrd/inbox/ → ebrd/*_YYYYMMDD.*"], C["ebrd"], mono_title=False)
r_pdf = card(30, 920, 235, "IFC reports (PDF)", ["pdf/*.pdf"], C["doc"], mono_title=False)
r_ref = card(30, 1060, 235, "Reference tables (hand-edited)",
             ["sources/*.csv: amount overrides,", "business-line rules, IFC regions,", "country aliases, report list,",
              "tag dictionary"], "#a8a7a2", mono_title=False)

# ---------------------------------------------------------------- 2 スクリプト
p_proj = card(300, 120, 270, "fetch_projects.py",
              ["page through the API (1,000 per call)", "clean text, dates and amounts",
               "business line from department name", "stage from status (active / completed /",
               "  pipeline / on hold)", "flag program-level amounts (GTFP,", "  same amount on the same day)",
               "split amounts by instrument"], C["ifc"])
p_cty = card(300, 360, 270, "build_country_ref.py",
             ["match country names → ISO3", "World Bank region, income group", "IFC region and its basis"], C["cty"])
p_risk = card(300, 450, 270, "fetch_country_data.py",
              ["OECD category 0–7 per country", "FX: 5-year average annual", "  depreciation, 10-year volatility",
               "keep history by as-of date"], C["cty"])
p_ebrd = card(300, 640, 270, "build_ebrd.py",
              ["detect file type, archive with date", "Excel rows: operation × country",
               "split debt / equity / guarantee", "link to CSV project by normalised", "  name + country",
               "status history from all CSVs"], C["ebrd"])
p_pdf = card(300, 900, 270, "extract_pdfs.py → tag_blocks.py",
             ["text blocks with page and position", "classify: paragraph, caption, toc,",
              "  reference, note, table, boilerplate", "tags: country, industry, theme", "  (keyword dictionary)"],
             C["doc"])
for a, b, dy in ((r_ifc, p_proj, 0), (r_wb, p_cty, 0), (r_oecd, p_risk, -12), (r_fx, p_risk, 12),
                 (r_ebrd, p_ebrd, 0), (r_pdf, p_pdf, 0)):
    line(a["r"], a["cy"], b["x"], b["cy"] + dy)
for b in (p_proj, p_cty, p_pdf):
    line(r_ref["r"], r_ref["y"] + 14, b["x"], b["y"] + b["h"] - 10, dash=True)

# ---------------------------------------------------------------- 3 データベース
X1, X2, X3 = 620, 905, 1190
t_proj = table(X1, 120, 245, "projects", [("project_number", "PK"), ("document_type", None), ("country", "K"),
                                          ("business_line", None), ("portfolio_stage", None), ("board_fy", None),
                                          ("amount_basis", None), ("approved_usd_m_for_sum", None)], C["ifc"],
               note="one row per disclosure document")
t_inst = table(X2, 120, 245, "project_instruments", [("project_number", "FK"), ("instrument", None),
                                                     ("approved_usd_m", None), ("approved_usd_m_for_sum", None)],
               C["ifc"])
v_uniq = table(X3, 120, 235, "projects_unique", [("project_number", "PK")], C["ifc"], view=True,
               note="2012-policy document preferred")
v_enr = table(X3, 215, 235, "projects_enriched", [("project_number", "PK"), ("iso3", "K"), ("ifc_region", None),
                                                  ("income_level", None)], C["ifc"], view=True,
              note="projects_unique + countries")
t_cty = table(X1, 360, 245, "countries", [("country", "PK"), ("iso3", "K"), ("wb_region", None),
                                          ("income_level", None), ("ifc_region", None)], C["cty"])
t_ind = table(X2, 440, 245, "country_indicators", [("iso3", "K"), ("indicator", None), ("value", None),
                                                   ("as_of", None)], C["cty"], note="oecd_crc, fx_depr_5y, fx_vol_10y")
v_risk = table(X3, 440, 235, "country_risk", [("iso3", "PK"), ("oecd_crc", None), ("fx_depr_5y", None),
                                              ("fx_vol_10y", None)], C["cty"], view=True,
               note="latest value per indicator")
t_eop = table(X1, 620, 245, "ebrd_operations", [("operation_key", "PK"), ("iso3", "K"), ("sector_group", None),
                                                ("portfolio_class", None), ("signing_year", None),
                                                ("ebrd_finance_eur_m", None), ("project_id", "FK")], C["ebrd"],
              note="one row per operation × country")
t_einst = table(X2, 620, 245, "ebrd_operation_instruments", [("operation_key", "FK"), ("instrument", None),
                                                             ("amount_eur_m", None)], C["ebrd"])
t_eproj = table(X2, 735, 245, "ebrd_projects", [("project_id", "PK"), ("status", None), ("stage", None),
                                                ("iso3", "K")], C["ebrd"])
t_ehist = table(X3, 620, 235, "ebrd_project_status_history", [("snapshot_date", "K"), ("project_id", "FK"),
                                                              ("status", None)], C["ebrd"])
v_echg = table(X3, 735, 235, "ebrd_status_changes", [("project_id", "PK")], C["ebrd"], view=True,
               note="status changed since last download")
t_docs = table(X1, 880, 245, "documents", [("doc_id", "PK"), ("title", None), ("pub_date", None)], C["doc"])
t_blk = table(X1, 995, 245, "doc_blocks", [("doc_id", "FK"), ("page, block_no", "K"), ("kind", None),
                                           ("text", None)], C["doc"])
t_dcty = table(X2, 880, 245, "doc_countries", [("doc_id", "FK"), ("iso3", "K")], C["doc"])
t_tag = table(X2, 995, 245, "block_tags", [("doc_id, page, block_no", "FK"), ("tag_type", None), ("tag", None)],
              C["doc"])
v_evd = table(X3, 960, 235, "evidence", [("doc_id, page, block_no", "PK"), ("sectors, themes", None),
                                         ("countries_mentioned", None)], C["doc"], view=True,
              note="paragraphs with their tags")

# スクリプト → テーブル
for p_, t_ in ((p_proj, t_proj), (p_cty, t_cty), (p_risk, t_ind), (p_ebrd, t_eop), (p_pdf, t_docs)):
    line(p_["r"], p_["cy"], t_["x"], t_["cy"])
# テーブル・ビューの関係（実線＝キーでの関係、破線＝ビューの元）
line(t_proj["r"], t_proj["col"]["project_number"], t_inst["x"], t_inst["col"]["project_number"])
line(t_inst["r"], t_inst["y"] + 12, v_uniq["x"], v_uniq["y"] + 12, dash=True)
line(v_uniq["x"] + 115, v_uniq["y"] + v_uniq["h"], v_uniq["x"] + 115, v_enr["y"], dash=True)
line(t_cty["r"], t_cty["col"]["country"], v_enr["x"], v_enr["col"]["ifc_region"], dash=True)
line(t_ind["r"], t_ind["col"]["iso3"], v_risk["x"], v_risk["col"]["iso3"], dash=True)
line(t_eop["r"], t_eop["col"]["operation_key"], t_einst["x"], t_einst["col"]["operation_key"])
line(t_eop["r"], t_eop["col"]["project_id"], t_eproj["x"], t_eproj["col"]["project_id"])
line(t_eproj["r"], t_eproj["col"]["project_id"], t_ehist["x"], t_ehist["col"]["project_id"])
line(t_ehist["x"] + 115, t_ehist["y"] + t_ehist["h"], t_ehist["x"] + 115, v_echg["y"], dash=True)
line(t_docs["x"] + 117, t_docs["y"] + t_docs["h"], t_docs["x"] + 117, t_blk["y"])
text(t_docs["x"] + 124, t_blk["y"] - 6, "doc_id", 10, color=C["sub"], font=MONO)
line(t_docs["r"], t_docs["col"]["doc_id"], t_dcty["x"], t_dcty["col"]["doc_id"])
line(t_blk["r"], t_blk["col"]["page, block_no"], t_tag["x"], t_tag["col"]["doc_id, page, block_no"])
line(t_tag["r"], t_tag["cy"], v_evd["x"], v_evd["cy"] + 10, dash=True)
line(t_dcty["r"], t_dcty["cy"], v_evd["x"], v_evd["y"] + 14, dash=True)

# iso3 で結ぶ（ダッシュボードの中で結合する）
BUS = 1440
ys = [v_enr["col"]["iso3"], v_risk["col"]["iso3"], t_eproj["col"]["iso3"], t_dcty["col"]["iso3"]]
o.append(f'<line x1="{BUS}" y1="{min(ys)}" x2="{BUS}" y2="{max(ys)}" stroke="{C["key"]}" stroke-width="2" '
         'stroke-dasharray="2 4"/>')
for y_, x_ in zip(ys, (v_enr["r"], v_risk["r"], t_eproj["r"], t_dcty["r"])):
    o.append(f'<line x1="{x_}" y1="{y_}" x2="{BUS}" y2="{y_}" stroke="{C["key"]}" stroke-width="1.2" '
             'stroke-dasharray="2 4"/>')
o.append(f'<line x1="{t_eop["r"]}" y1="{t_eop["col"]["iso3"]}" x2="{t_einst["x"] - 4}" y2="{t_eop["col"]["iso3"]}" '
         f'stroke="{C["key"]}" stroke-width="1.2" stroke-dasharray="2 4"/>')
mid = (min(ys) + max(ys)) / 2
o.append(f'<text x="{BUS - 8}" y="{mid}" font-size="11" fill="{C["key"]}" font-family="{MONO}" text-anchor="middle" '
         f'transform="rotate(-90 {BUS - 8} {mid})">iso3 — joined in the app (pandas merge)</text>')

# ---------------------------------------------------------------- 4 使う側
a_app = card(1480, 120, 280, "app/app.py (Streamlit)",
             ["reads projects_enriched, project_instruments,", "country_risk, ebrd_operations (+ instruments),",
              "evidence, doc_countries, documents", "", "tab 1  IFC Overview", "tab 2  Portfolio Profile",
              "tab 3  Risk Profile", "tab 4  vs EBRD", "tab 5  Country Profile", "notes  definitions, this diagram"],
             C["app"])
a_sum = card(1480, 330, 280, "app/summaries.py", ["rule-based summary per tab", "fixed thresholds, no AI"], C["app"])
a_wl = card(1480, 400, 280, "app/watchlist.py (trial)", ["risk signals per active project"], C["app"])
a_ai = card(1480, 455, 280, "app/commentary.py",
            ["local only: summary + figures →", "Claude API → reviewed draft →", "content/commentary/<tab>.json",
             "dashboard shows the saved text"], C["app"])
a_pbi = card(1480, 640, 280, "src/export_powerbi.py",
             ["tables → data/curated/powerbi/*.parquet", "star schema for Power BI (optional)"], C["app"])
line(BUS, v_enr["cy"] - 20, a_app["x"], v_enr["cy"] - 20)
text(BUS + 6, v_enr["cy"] - 28, "read-only", 10, color=C["sub"])
line(a_app["x"] + 140, a_app["y"] + a_app["h"], a_app["x"] + 140, a_sum["y"])
line(BUS, a_pbi["cy"], a_pbi["x"], a_pbi["cy"], dash=True)

o.append("</svg>")
OUT.write_text("\n".join(o), encoding="utf-8")
print(f"保存: {OUT}")
