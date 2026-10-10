"""
IFC Portfolio Explorer (Streamlit)

起動（プロジェクトのフォルダで実行）:
    uv run streamlit run app/app.py

データ: data/curated/ifc.duckdb（src/ 以下のスクリプトで作成）
画面の表示はすべて英語（閲覧者が海外のため）。

ダッシュボードの流れ（タブの順に読む）:
    1 IFC Overview      … IFC 全体は今どれだけの規模で、どこにあり、何が控えているか（絞り込みなし）
    2 Portfolio Profile … 中身は何で、新規承認の構成はどう変わってきたか（案件の一覧まで）
    3 Risk Profile      … 実行中の案件のどこにリスクが集中しているか
    4 vs EBRD           … 同じ国で、もう1つの開発金融機関（EBRD）と比べるとどうか
    5 Country Profile   … 1つの国で、数字・リスク・レポートの記述・EBRD はどうなっているか
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

import commentary  # AI commentary（自分の PC でだけ生成し、保存した文章を表示する）
import summaries  # 各タブの最後の Summary（ルールに基づく文章）
import watchlist  # watchlist: 試行的な注視リスト。削除するときはこの行と、render を呼ぶ行を消す

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("IFC_DB", ROOT / "data" / "curated" / "ifc.duckdb"))

ALL = "All"
DEFAULT_BUSINESS_LINE = "FIG"
DEFAULT_REGION = "Middle East and Central Asia"
STAGES = ["Active portfolio", "Completed", "Pipeline", "On hold"]
DEFAULT_STAGES = ["Active portfolio", "Pipeline"]
COMMITTED = ["Active portfolio", "Completed"]   # 署名済みの案件（EBRD との比較・Overview の推移に使う）
OVERVIEW_YEARS = 15                               # Overview の推移で表示する年度の数

# 色はエンティティに固定する（絞り込みで色が変わらないように）
# 実行中を最も濃く、完了済みは控えめに、パイプラインは薄い青
STAGE_COLORS = {"Active portfolio": "#2a78d6", "Completed": "#9c9b96", "Pipeline": "#86b6ef", "On hold": "#d3d2cc"}
INSTRUMENT_ORDER = ["Loan", "Equity", "Guarantee", "Risk management", "Unallocated", "Not specified"]
INSTRUMENT_COLORS = {"Loan": "#eb6834", "Equity": "#1baf7a", "Guarantee": "#eda100",
                     "Risk management": "#e87ba4", "Unallocated": "#a8a7a2", "Not specified": "#c3c2b7"}
# OECD国別リスク分類（0＝最も低い〜7＝最も高い）の色。リスクが高いほど濃い青
CRC_ORDER = [f"Category {i}" for i in range(8)] + ["Not classified", "No data"]
CRC_COLORS = {"Category 0": "#b7d3f6", "Category 1": "#9ec5f4", "Category 2": "#86b6ef", "Category 3": "#6da7ec",
              "Category 4": "#5598e7", "Category 5": "#2a78d6", "Category 6": "#1c5cab", "Category 7": "#0d366b",
              "Not classified": "#a8a7a2", "No data": "#d3d2cc"}
CRC_NOTE = ("OECD country risk classification (0 = lowest, 7 = highest), as of {as_of}. The OECD produces it to set "
            "minimum premium rates for officially supported export credits and does not endorse other uses; it is "
            "shown here as an indicator of country risk.")
BUSINESS_LINE_ORDER = ["FIG", "MAS", "INF", "Funds & Tech", "Equity Portfolio", "Other"]
BUSINESS_LINE_COLORS = {"FIG": "#2a78d6", "MAS": "#4a3aa7", "INF": "#008300", "Funds & Tech": "#e87ba4",
                        "Equity Portfolio": "#eda100", "Other": "#a8a7a2"}
BUSINESS_LINE_NAMES = {
    "FIG": "Financial Institutions Group",
    "MAS": "Manufacturing, Agribusiness and Services",
    "INF": "Infrastructure and Natural Resources",
    "Funds & Tech": "Funds, Venture Capital and Technology",
    "Equity Portfolio": "Equity Portfolio Management",
    "Other": "Other or unidentified departments",
}
NO_REGION = ["Unknown", "Not assigned", "Unassigned"]   # 地域が決まらない案件（灰色で最後に置く）
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#4a3aa7", "#008300", "#e34948"]
# レポートの記述を事業部門で絞るときに使う業種
BUSINESS_LINE_SECTORS = {
    "FIG": ["Financial Markets"],
    "MAS": ["Agribusiness and Forestry", "Manufacturing", "Tourism, Retail and Property",
            "Health, Education and Life Sciences"],
    "INF": ["Infrastructure", "Metals and Mining", "Telecommunications and Technology"],
    "Funds & Tech": ["Funds", "Telecommunications and Technology"],
}

# ---- EBRD との比較
IFC_COLOR, EBRD_COLOR = "#2a78d6", "#eb6834"
# EBRD の金額（百万ユーロ、EBRD の基準日の月末レート）を米ドルに換算するレート。EBRD のデータの基準日と同じ日の
# ECB の参照レートを使う。EBRD のデータを新しい基準日に更新したら、その日のレートをここに加える
EUR_USD = {"2025-12-31": (1.1750, "ECB euro reference rate, 31 December 2025")}
WHY_EBRD = ("The EBRD is the multilateral development bank whose business is closest to IFC's: its founding "
            "agreement directs at least 60% of its commitments to the private sector, it invests through loans, "
            "equity and guarantees, and its countries of operation overlap with IFC's in Central Asia, Türkiye, "
            "Central and Eastern Europe and the southern and eastern Mediterranean. Comparing the two in the same "
            "countries shows where IFC's presence is relatively large or small. The EBRD's private-sector "
            "operations are compared, since IFC invests only in the private sector.")
# IFC の事業部門 → 比較に使う EBRD の業種（列, 値, 表示名）。対応がない事業部門は全業種で比べる
EBRD_SECTOR_MATCH = {
    "FIG": ("sector_group", ["Financial Institutions"], "EBRD financial institutions"),
    "INF": ("sector_group", ["Sustainable Infrastructure"], "EBRD sustainable infrastructure"),
    "MAS": ("sector", ["Food and Agribusiness", "Manufacturing & Services", "Natural Resources", "Real Estate",
                       "Telecommunications, Media and Technology"], "EBRD corporate sectors (excluding equity funds)"),
    "Funds & Tech": ("sector", ["Equity Funds"], "EBRD equity funds"),
}
# IFC の案件がない国（IFC の地域が決まらない）で、EBRD が投資している国の地域
EBRD_REGION_FALLBACK = {"SVK": "Europe", "SVN": "Europe", "CYP": "Europe", "CZE": "Europe", "EST": "Europe",
                        "LVA": "Europe", "LTU": "Europe", "TKM": "Middle East and Central Asia"}
# 為替リスク: 直近5年の対米ドルの年平均下落率（World Bank の公定レート）の区分
FX_BUCKETS = [(0.15, "Very high (15%+ a year)"), (0.05, "High (5–15%)"), (0.02, "Moderate (2–5%)"),
              (-1.0, "Stable (under 2%)")]
FX_ORDER = [b for _, b in FX_BUCKETS][::-1] + ["No data"]
FX_COLORS = {"Stable (under 2%)": "#b7d3f6", "Moderate (2–5%)": "#6da7ec", "High (5–15%)": "#1c5cab",
             "Very high (15%+ a year)": "#0d366b", "No data": "#d3d2cc"}
# 国旗（flagcdn.com の画像）に使う2文字コード。世界銀行のコードが ISO と違う国だけ言い換える
FLAG_CODE = {"GZ": "ps", "JG": "je"}
# 金融手段の表示名を両者でそろえる
COMMON_INSTRUMENT = {"Loan": "Loan / debt", "Debt": "Loan / debt", "Equity": "Equity", "Guarantee": "Guarantee",
                     "Risk management": "Risk management"}
COMMON_INSTRUMENT_COLORS = {"Loan / debt": "#eb6834", "Equity": "#1baf7a", "Guarantee": "#eda100",
                            "Risk management": "#e87ba4", "Other": "#a8a7a2"}
EBRD_SECTOR_GROUP_COLORS = {"Financial Institutions": "#2a78d6", "Corporate": "#4a3aa7",
                            "Sustainable Infrastructure": "#008300", "Other": "#a8a7a2"}

# 元データの国名の表示用の言い換え（略記の展開、複数国案件の明示）
COUNTRY_DISPLAY = {
    "Turkiye": "Türkiye",
    "Middle East, Pakistan, and Afg Region": "Middle East, Pakistan and Afghanistan (multi-country)",
    "Cen Afr, and Anglophne W Africa Region": "Central and Anglophone West Africa (multi-country)",
    "Central Asia and Turkiye Region": "Central Asia and Türkiye (multi-country)",
    "World Region": "Global (multi-country)",
    "Country Name Pending": "Country not yet disclosed",
    "Unknown": "Country not specified",
}

DEFINITIONS = (
    "**Active portfolio** = signed and ongoing (active, or signed and awaiting disbursement). "
    "**Completed** = the track record. "
    "**Pipeline** = approved by the Board but not yet signed, or awaiting approval — not yet a legal commitment. "
    "Amounts are IFC's own investment as approved by the Board, in US\\$ millions, by approval fiscal year."
)


def display_country(name: str) -> str:
    if name in COUNTRY_DISPLAY:
        return COUNTRY_DISPLAY[name]
    if name.endswith(" Subregion") or name.endswith(" Region"):
        return name.rsplit(" ", 1)[0] + " (multi-country)"
    return name


st.set_page_config(page_title="IFC Portfolio Explorer", layout="wide")


# ---------------------------------------------------------------- data
def table_names(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}


def crc_group(df: pd.DataFrame) -> pd.Series:
    """表示用の区分: 分類あり → "Category n"、分類対象外 → "Not classified"、国が特定できない → "No data"。"""
    g = df["oecd_crc_label"].where(df["oecd_crc"].notna())
    g[df["oecd_crc"].isna() & df["oecd_crc_label"].notna()] = "Not classified"
    return g.fillna("No data")


def load_risk(con, names: set[str]) -> pd.DataFrame:
    # 国別のリスク指標（src/fetch_country_data.py で作成。まだ作っていなければ空欄）
    return (con.sql("SELECT * FROM country_risk").df() if "country_risk" in names
            else pd.DataFrame(columns=["iso3", "oecd_crc", "oecd_crc_label", "oecd_crc_as_of"]))


def fx_bucket(v) -> str:
    if pd.isna(v):
        return "No data"
    return next(label for cut, label in FX_BUCKETS if v >= cut)


@st.cache_data(show_spinner=False)
def load_data(db: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    with duckdb.connect(db, read_only=True) as con:
        projects = con.sql("SELECT * FROM projects_enriched").df()
        instruments = con.sql("SELECT * FROM project_instruments").df()
        risk = load_risk(con, table_names(con))
    projects = projects.merge(risk, on="iso3", how="left")
    projects["oecd_crc_group"] = crc_group(projects)
    if "fx_depr_5y" not in projects:              # 為替のデータをまだ取得していないとき
        projects["fx_depr_5y"] = np.nan
        projects["fx_depr_5y_label"] = None
    projects["fx_group"] = projects["fx_depr_5y"].map(fx_bucket)
    projects["country"] = projects["country"].fillna("Unknown").map(display_country)
    projects["ifc_region"] = projects["ifc_region"].fillna("Unassigned")
    projects["industry"] = projects["industry"].fillna("Not specified")
    projects["dept_name"] = projects["dept_name"].fillna("Not specified")
    projects["business_line"] = projects["business_line"].fillna("Other")
    attrs = ["project_number", "project_name", "company_name", "country", "iso3", "ifc_region", "industry",
             "business_line", "dept_name", "portfolio_stage", "status", "board_fy", "amount_basis"]
    facts = instruments.merge(projects[attrs], on="project_number", how="left")
    return projects, facts


@st.cache_data(show_spinner=False)
def load_ebrd(db: str) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """EBRD のデータ（src/build_ebrd.py で作成）。まだ作っていなければ None。"""
    with duckdb.connect(db, read_only=True) as con:
        names = table_names(con)
        if "ebrd_operations" not in names:
            return None
        ops = con.sql("SELECT * FROM ebrd_operations").df()
        inst = con.sql("SELECT * FROM ebrd_operation_instruments").df()
        risk = load_risk(con, names)
    ops = ops.merge(risk, on="iso3", how="left")
    ops["oecd_crc_group"] = crc_group(ops)
    if "fx_depr_5y" not in ops:
        ops["fx_depr_5y"] = np.nan
    return ops, inst


@st.cache_data(show_spinner=False)
def load_evidence_tables(db: str) -> dict[str, pd.DataFrame]:
    with duckdb.connect(db, read_only=True) as con:
        if "evidence" not in table_names(con):
            return {}
        evidence = con.sql("SELECT * FROM evidence").df()
        # タグの列（リスト）を、欠損は空のリストにそろえる
        for col in ("sectors", "themes", "countries_mentioned"):
            evidence[col] = evidence[col].apply(
                lambda x: list(x) if isinstance(x, (list, tuple)) or hasattr(x, "tolist") else [])
        return {
            "evidence": evidence,
            "doc_countries": con.sql("SELECT * FROM doc_countries").df(),
            "documents": con.sql("SELECT doc_id, title, doc_type, pub_date, countries, n_pages FROM documents").df(),
        }


def fmt_usd(m: float) -> str:
    if pd.isna(m) or m == 0:
        return "US$0"
    return f"US${m / 1000:,.1f}bn" if abs(m) >= 1000 else f"US${m:,.0f}m"


def fmt_eur(m: float) -> str:
    if pd.isna(m) or m == 0:
        return "€0"
    return f"€{m / 1000:,.1f}bn" if abs(m) >= 1000 else f"€{m:,.0f}m"


def md_usd(m: float) -> str:
    """st.markdown 用。「$」は数式の記号と解釈されるため、エスケープする。"""
    return fmt_usd(m).replace("$", "\\$")


def style_fig(fig, height: int = 380):
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=48, b=10),
        # 凡例はグラフの下、軸のタイトルと重ならない位置に置く
        legend=dict(orientation="h", yanchor="top", y=-0.2, x=0, title=None),
        xaxis_title=None, yaxis_title=None, bargap=0.3,
    )
    fig.update_traces(selector=dict(type="bar"), marker_line_width=0)
    return fig


def show(fig, where=None):
    (where or st).plotly_chart(fig, width="stretch", theme="streamlit")


if not DB_PATH.exists():
    st.error(f"Database not found: {DB_PATH}. Run the scripts in src/ first.")
    st.stop()

projects, facts_all = load_data(str(DB_PATH))
ebrd = load_ebrd(str(DB_PATH))
ev = load_evidence_tables(str(DB_PATH))
AS_OF = pd.Timestamp(projects["as_of_date"].max())
today = pd.Timestamp.today()
CURRENT_FY = today.year + (1 if today.month >= 7 else 0)   # IFCの会計年度は7月始まり

# ---------------------------------------------------------------- filters (sidebar)
# 左側は2つに分ける: 「対象範囲」（タブ2〜5）と「期間と段階」（一部のタブ）。タブ1は常に IFC 全体
st.sidebar.header("Scope")
st.sidebar.caption("Applies to tabs 2–5. Tab 1 always shows the whole of IFC.")

bl_options = [b for b in BUSINESS_LINE_ORDER if b in set(projects["business_line"])]
business_line = st.sidebar.selectbox(
    "Who — business line", [ALL] + bl_options,
    index=(bl_options.index(DEFAULT_BUSINESS_LINE) + 1) if DEFAULT_BUSINESS_LINE in bl_options else 0,
    format_func=lambda b: b if b == ALL else f"{b} — {BUSINESS_LINE_NAMES.get(b, b)}",
    help="Derived from the name of the IFC department responsible for the project. "
         "Departments within a business line are shown in Portfolio Profile.")

regions = sorted(projects["ifc_region"].unique())
region = st.sidebar.selectbox("Where — IFC region", [ALL] + regions,
                              index=(regions.index(DEFAULT_REGION) + 1) if DEFAULT_REGION in regions else 0)
country_pool = projects if region == ALL else projects[projects["ifc_region"] == region]
country_choice = st.sidebar.selectbox("Where — country", [ALL] + sorted(country_pool["country"].unique()))

with st.sidebar.expander("More filters"):
    industry = st.selectbox("What — industry", [ALL] + sorted(projects["industry"].unique()))
    instrument = st.selectbox("How — instrument", [ALL] + [i for i in INSTRUMENT_ORDER
                                                          if i in set(facts_all["instrument"])])
    include_program = st.checkbox(
        "Include program-level amounts", value=False,
        help="Some programs (e.g. GTFP) record the program-wide approval ceiling against each participating "
             "institution. Summing these overstates commitments substantially, so they are excluded by default. "
             "This setting applies to every tab.")

st.sidebar.header("Period and stage")
st.sidebar.caption("Fiscal year applies to tabs 2, 4 and 5; stage to tabs 2 and 5.")
fy_min, fy_max = int(projects["board_fy"].min()), int(projects["board_fy"].max())
fy = st.sidebar.slider("Fiscal year", fy_min, fy_max, (max(fy_min, 2015), fy_max),
                       help="IFC fiscal years run July–June. Pipeline projects not yet approved are placed in "
                            "the fiscal year of their projected Board date.")
stages = st.sidebar.multiselect("Stage", STAGES, default=DEFAULT_STAGES,
                                help="Active portfolio: signed and ongoing (active, or awaiting disbursement). "
                                     "Completed: finished projects (track record). "
                                     "Pipeline: approved but not yet signed, or awaiting Board approval. "
                                     "On hold: put on hold by IFC.")


# ---------------------------------------------------------------- apply filters
def apply_filters(fy_range: tuple[int, int] | None, stage_list: list[str], scoped: bool = True,
                  what_how: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    絞り込みを適用し、(案件1件1行, 案件×金融手段) を返す。
    fy_range=None なら年度で絞らない。scoped=False なら左側の対象範囲を使わない（IFC 全体）。
    what_how=False なら業種・金融手段の絞り込みを使わない（EBRD との比較で、構成をゆがめないため）。
    """
    q = projects[projects["portfolio_stage"].isin(stage_list)]
    if fy_range is not None:
        q = q[q["board_fy"].between(*fy_range)]
    if scoped:
        if business_line != ALL:
            q = q[q["business_line"] == business_line]
        if region != ALL:
            q = q[q["ifc_region"] == region]
        if country_choice != ALL:
            q = q[q["country"] == country_choice]
        if what_how and industry != ALL:
            q = q[q["industry"] == industry]
    fx = facts_all[facts_all["project_number"].isin(q["project_number"])]
    if scoped and what_how and instrument != ALL:
        fx = fx[fx["instrument"] == instrument]
        q = q[q["project_number"].isin(fx["project_number"])]
    fx = fx.assign(amount=fx["approved_usd_m"] if include_program else fx["approved_usd_m_for_sum"])
    q = q.merge(fx.groupby("project_number")["amount"].sum(min_count=1).rename("amount"),
                on="project_number", how="left")
    return q, fx


p, facts = apply_filters(fy, stages)
empty = p.empty
value_label = "US$ millions"

# 「Who」「Where」は、1つに絞ると一段細かい区分を表示する
who_col, who_label = ("business_line", "business line") if business_line == ALL else ("dept_name", "department")
where_col, where_label = ("ifc_region", "IFC region") if region == ALL else ("country", "country")


# AI commentary の対象（IFC 全体＝左側の絞り込みがすべて All）
WHOLE_IFC = all(v == ALL for v in (business_line, region, country_choice, industry, instrument))


def ai_commentary(tab: str, eligible: bool, signature: dict) -> None:
    facts_, paras_ = summaries.LAST[tab]
    commentary.section(tab, facts_, paras_, signature | {"include_program": include_program},
                       "the whole of IFC", f"{AS_OF:%d %B %Y}", eligible)


def scope_text() -> str:
    who = ("all IFC business lines" if business_line == ALL
           else f"{business_line} ({BUSINESS_LINE_NAMES.get(business_line)})")
    where = country_choice if country_choice != ALL else (region if region != ALL else "all regions")
    parts = [f"**{who}**", f"in **{where}**"]
    if industry != ALL:
        parts.append(f"in **{industry}**")
    if instrument != ALL:
        parts.append(f"through **{instrument.lower()}s**")
    return " ".join(parts)


def n_projects(n: int, word: str = "project") -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")


def stage_totals(df: pd.DataFrame) -> dict[str, tuple[float, int]]:
    out = {}
    for s in STAGES:
        d = df[df["portfolio_stage"] == s]
        out[s] = (d["amount"].sum(), d["project_number"].nunique())
    return out


def breakdown(df: pd.DataFrame, col: str, top: int = 10, other: bool = True) -> pd.DataFrame:
    """区分 × 段階の集計。上位 top 件以外は "Other" にまとめる（other=False なら上位だけ）。"""
    if df.empty:
        return pd.DataFrame(columns=[col, "portfolio_stage", "value"])
    g = df.groupby([col, "portfolio_stage"])["amount"].sum().rename("value").reset_index()
    order = g.groupby(col)["value"].sum().sort_values(ascending=False)
    keep = list(order.index[:top])
    g = g[g[col].isin(keep)]
    if other and len(order) > top:
        rest = df[~df[col].isin(keep)].groupby("portfolio_stage")["amount"].sum()
        g = pd.concat([g, rest.rename("value").reset_index().assign(**{col: f"Other ({len(order) - top})"})],
                      ignore_index=True)
    return g


def stacked_bar(g: pd.DataFrame, col: str, title: str, color: str = "portfolio_stage",
                colors: dict | None = None, color_order: list | None = None):
    totals = g.groupby(col)["value"].sum().sort_values(ascending=False)
    # "Other (n)" は金額に関係なく最下段に置く
    order = [c for c in totals.index if not str(c).startswith("Other (")] + \
            [c for c in totals.index if str(c).startswith("Other (")]
    fig = px.bar(g, x="value", y=col, color=color, orientation="h", title=title,
                 category_orders={col: order, color: color_order or STAGES},
                 color_discrete_map=colors or STAGE_COLORS)
    fig.update_traces(hovertemplate="%{y}<br>%{fullData.name}: US$%{x:,.0f}m<extra></extra>")
    # 棒の太さを区分の数によらず一定にする（区分が少ないときに棒が極端に太くならないように）
    fig = style_fig(fig, 30 * len(order) + 150)
    fig.update_layout(legend=dict(y=-60 / (30 * len(order) + 40)))
    fig.update_xaxes(title_text=value_label, tickformat=",")
    return fig


def share_by_year(df: pd.DataFrame, col: str, title: str, colors: dict | None = None, top: int = 6,
                  order: list | None = None):
    """年度ごとの構成比（100%積み上げ）。上位 top 区分以外は Other にまとめる。"""
    d = df.dropna(subset=["amount"])
    d = d[d["amount"] > 0]
    keep = order or list(d.groupby(col)["amount"].sum().sort_values(ascending=False).index[:top])
    d = d.assign(**{col: d[col].where(d[col].isin(keep), "Other")})
    g = d.groupby(["board_fy", col])["amount"].sum().reset_index()
    g["share"] = g["amount"] / g.groupby("board_fy")["amount"].transform("sum") * 100
    cmap = dict(colors) if colors else {k: PALETTE[i % len(PALETTE)] for i, k in enumerate(keep)}
    cmap.setdefault("Other", "#a8a7a2")
    fig = px.bar(g, x="board_fy", y="share", color=col, title=title, custom_data=["amount"],
                 category_orders={col: keep + ["Other"]}, color_discrete_map=cmap)
    fig.update_traces(hovertemplate="FY%{x} · %{fullData.name}<br>%{y:.0f}% · US$%{customdata[0]:,.0f}m"
                                    "<extra></extra>")
    fig = style_fig(fig, 400)
    fig.update_layout(legend=dict(y=-0.15))
    fig.update_xaxes(tickprefix="FY", dtick=1 if g["board_fy"].nunique() <= 12 else 2)
    fig.update_yaxes(ticksuffix="%", range=[0, 100])
    return fig


def pie(df: pd.DataFrame, col: str, title: str, colors: dict | None = None, top: int = 7,
        other_label: str = "Other", height: int = 380, legend_below: bool = False):
    """金額の構成比のドーナツグラフ。上位 top 区分以外は「Other (n)」にまとめる。"""
    g = df.groupby(col)["amount"].sum()
    g = g[g > 0].sort_values(ascending=False)
    if len(g) > top:
        g = pd.concat([g.head(top), pd.Series({f"{other_label} ({len(g) - top})": g.iloc[top:].sum()})])
    d = g.rename("amount").rename_axis("label").reset_index()
    cmap = dict(colors or {})
    for i, k in enumerate(d["label"]):
        if str(k).startswith(other_label + " ("):
            cmap[k] = "#c3c2b7"
        cmap.setdefault(k, PALETTE[i % len(PALETTE)])
    fig = px.pie(d, names="label", values="amount", title=title, hole=0.45, color="label",
                 color_discrete_map=cmap, category_orders={"label": list(d["label"])})
    fig.update_traces(sort=False, direction="clockwise", textinfo="percent", textposition="inside",
                      insidetextorientation="horizontal",
                      hovertemplate="%{label}<br>US$%{value:,.0f}m · %{percent}<extra></extra>")
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=48, b=10), uniformtext_minsize=10,
                      uniformtext_mode="hide", legend=dict(title=None, font=dict(size=11)))
    if legend_below:      # 区分が多いときは凡例を下に置き、円を大きくする
        fig.update_layout(legend=dict(orientation="h", yanchor="top", y=-0.02, x=0))
    return fig


def share_bars(parts: dict[str, pd.Series], color: str, title: str, order: list, colors: dict):
    """機関ごとの構成比を横に並べた100%積み上げ棒（IFC と EBRD の比較用）。"""
    frames = []
    for inst, s in parts.items():
        s = s[s > 0]
        if s.sum() > 0:
            frames.append((s / s.sum() * 100).rename("share").rename_axis(color).reset_index()
                          .assign(Institution=inst))
    d = pd.concat(frames) if frames else pd.DataFrame(columns=[color, "share", "Institution"])
    fig = px.bar(d, x="share", y="Institution", color=color, orientation="h", title=title,
                 category_orders={"Institution": list(parts), color: order}, color_discrete_map=colors)
    fig.update_traces(hovertemplate="%{y} · %{fullData.name}<br>%{x:.0f}%<extra></extra>")
    fig = style_fig(fig, 260)
    fig.update_layout(legend=dict(y=-0.3))
    fig.update_xaxes(ticksuffix="%", range=[0, 100])
    return fig


def presence_chart(d: pd.DataFrame, title: str, height: int | None = None):
    """IFC と EBRD の合計に占める比率（100%積み上げ）。d は index が区分、列に IFC・EBRD を持つ。"""
    d = d[["IFC", "EBRD"]]
    tot = d.sum(axis=1)
    long = (d.div(tot, axis=0) * 100).rename_axis("label").reset_index().melt(
        id_vars="label", var_name="Institution", value_name="share")
    long["amount"] = [d.loc[l, i] for l, i in zip(long["label"], long["Institution"])]
    fig = px.bar(long, x="share", y="label", color="Institution", orientation="h", title=title,
                 custom_data=["amount"], text=long["share"].map(lambda v: f"{v:.0f}%" if v >= 8 else ""),
                 category_orders={"label": list(d.index), "Institution": ["IFC", "EBRD"]},
                 color_discrete_map={"IFC": IFC_COLOR, "EBRD": EBRD_COLOR})
    fig.update_traces(hovertemplate="%{y} · %{fullData.name}<br>%{x:.0f}% · US$%{customdata[0]:,.0f}m"
                                    "<extra></extra>", textposition="inside", insidetextanchor="middle")
    h = height or 36 * len(d) + 150
    fig = style_fig(fig, h)
    fig.update_layout(legend=dict(y=-60 / (h - 110)))
    fig.update_xaxes(ticksuffix="%", range=[0, 100])
    return fig


INSTRUMENT_WORDS = {"Loan": "loans", "Guarantee": "guarantees", "Equity": "equity",
                    "Risk management": "risk-management products"}


def mix_shift(df: pd.DataFrame, col: str, n_years: int = 3) -> tuple[list[str], tuple[str, str] | None]:
    """期間の最初と最後の n_years 年度で構成比を比べ、SHIFT_PP 以上動いた区分（最大2つ）を文章にする。"""
    d = df.dropna(subset=["amount"])
    d = d[d["amount"] > 0]
    years = sorted(int(y) for y in d["board_fy"].dropna().unique())
    if len(years) < 2 * n_years:
        return [], None
    a, b = d[d["board_fy"].isin(years[:n_years])], d[d["board_fy"].isin(years[-n_years:])]
    sa, sb = a.groupby(col)["amount"].sum() / a["amount"].sum(), b.groupby(col)["amount"].sum() / b["amount"].sum()
    both = pd.concat([sa.rename("a"), sb.rename("b")], axis=1).fillna(0)
    both["d"] = both["b"] - both["a"]
    both = both.reindex(both["d"].abs().sort_values(ascending=False).index).head(2)
    out = [f"the share of {INSTRUMENT_WORDS.get(k, k) if col == 'instrument' else k} "
           f"{'rose' if r.d > 0 else 'fell'} from {r.a:.0%} to {r.b:.0%}"
           for k, r in both.iterrows() if abs(r.d) * 100 >= summaries.SHIFT_PP]
    return out, (f"FY{years[0]}–{str(years[n_years - 1])[-2:]}", f"FY{years[-n_years]}–{str(years[-1])[-2:]}")


# ---------------------------------------------------------------- header
st.title("IFC Portfolio Explorer")
st.markdown("What IFC invests in, where and how — and where the risks sit. Read the tabs from left to right: "
            "**1** the whole of IFC → **2** what the portfolio in your scope is made of → **3** where its risks "
            "concentrate → **4** how it compares with the EBRD → **5** one country in detail.")

tab_overview, tab_profile, tab_risk, tab_ebrd, tab_country, tab_notes = st.tabs(
    ["1 IFC Overview", "2 Portfolio Profile", "3 Risk Profile", "4 vs EBRD", "5 Country Profile",
     "Notes on the data"])

# ================================================================ 1 IFC Overview（IFC 全体）
with tab_overview:
    oa, _ = apply_filters(None, ["Active portfolio"], scoped=False)
    op, _ = apply_filters(None, ["Pipeline"], scoped=False)
    total = oa["amount"].sum()
    n_cty = oa.loc[oa["iso3"].notna(), "iso3"].nunique()
    n_noamt = int(op["amount"].isna().sum())
    st.markdown(f"As of **{AS_OF:%d %B %Y}**, IFC has **{md_usd(total)}** in its active portfolio: "
                f"**{n_projects(len(oa))}** in **{n_cty} countries** plus multi-country projects. "
                f"A further **{md_usd(op['amount'].sum())}** is in the pipeline — **{n_projects(len(op))}**, "
                f"of which {n_noamt} do not show an amount yet.")
    st.caption("This tab always shows the whole of IFC; the filters on the left apply from tab 2 onwards. "
               "Amounts are Board-approved commitments, not outstanding balances.")
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Active portfolio", fmt_usd(total))
    k2.metric("Active projects", f"{len(oa):,}")
    k3.metric("Countries", f"{n_cty:,}", help="Countries with at least one active single-country project")
    k4.metric("Pipeline", fmt_usd(op["amount"].sum()),
              help=f"{n_projects(len(op))}; {n_noamt} without an amount yet")

    st.subheader("Where the active portfolio is, and what kind")
    left, right = st.columns([3, 2])
    by_cty = oa.dropna(subset=["iso3"]).groupby(["iso3", "country"])["amount"].sum().reset_index()
    by_cty = by_cty[by_cty["amount"] > 0]
    # 少数の大きい国に色が引っ張られないよう、色は金額の対数で決める（目盛りは金額で表示する）
    by_cty["log_amount"] = np.log10(by_cty["amount"].clip(lower=1))
    lo_c = np.log10(max(by_cty["amount"].quantile(0.05), 1))        # ごく小さい国で色の幅を使わない
    hi_c = by_cty["log_amount"].max()
    ticks = [v for v in (1, 10, 100, 1000, 10000) if 10 ** lo_c <= v <= 10 ** hi_c]
    fig = px.choropleth(by_cty, locations="iso3", color="log_amount", hover_name="country", custom_data=["amount"],
                        range_color=[lo_c, hi_c],
                        color_continuous_scale=["#e3eefb", "#86b6ef", "#2a78d6", "#0d366b"],
                        title="Active portfolio by country (single-country projects)")
    fig.update_traces(hovertemplate="%{hovertext}<br>US$%{customdata[0]:,.0f}m<extra></extra>")
    fig.update_layout(height=420, margin=dict(l=0, r=0, t=48, b=0),
                      coloraxis_colorbar=dict(title="US$", tickvals=[np.log10(v) for v in ticks],
                                              ticktext=[fmt_usd(v).replace("US$", "") for v in ticks]),
                      geo=dict(showframe=False, showcoastlines=False, projection_type="natural earth",
                               bgcolor="rgba(0,0,0,0)"))
    show(fig, left)
    left.caption("Colours follow a logarithmic scale (each step is ten times larger), so that countries with "
                 "smaller portfolios remain distinguishable from one another.")
    g = oa[~oa["ifc_region"].isin(NO_REGION)].groupby(["ifc_region", "business_line"])["amount"].sum() \
        .rename("value").reset_index()
    show(stacked_bar(g[g["value"] > 0], "ifc_region", "By IFC region and business line", color="business_line",
                     colors=BUSINESS_LINE_COLORS, color_order=BUSINESS_LINE_ORDER), right)

    st.subheader("How it built up")
    first_fy = CURRENT_FY - OVERVIEW_YEARS
    oc, _ = apply_filters((first_fy, CURRENT_FY), COMMITTED, scoped=False)
    colour_by = st.radio("Colour by", ["IFC region", "Business line"], horizontal=True, key="ov-colour")
    col = "ifc_region" if colour_by == "IFC region" else "business_line"
    if col == "business_line":
        cmap, corder = BUSINESS_LINE_COLORS, BUSINESS_LINE_ORDER
    else:
        corder = list(oc.groupby(col)["amount"].sum().sort_values(ascending=False).index)
        named = [r for r in corder if r not in NO_REGION]
        cmap = {r: PALETTE[i % len(PALETTE)] for i, r in enumerate(named)} | {r: "#c3c2b7" for r in NO_REGION}
        corder = named + [r for r in corder if r in NO_REGION]
    g = oc.groupby(["board_fy", col])["amount"].sum().reset_index()
    fig = px.bar(g, x="board_fy", y="amount", color=col, color_discrete_map=cmap, category_orders={col: corder},
                 title=f"New commitments by approval fiscal year, FY{first_fy}–FY{CURRENT_FY}")
    fig.update_traces(hovertemplate="FY%{x} · %{fullData.name}<br>US$%{y:,.0f}m<extra></extra>")
    fig = style_fig(fig, 420)
    fig.update_layout(legend=dict(y=-0.15))
    fig.update_xaxes(tickprefix="FY", dtick=1)
    fig.update_yaxes(title_text=value_label, tickformat=",")
    show(fig)
    st.caption("Signed projects only (active or completed), by the IFC fiscal year (July–June) of Board approval. "
               "Recent years look lower partly because recently approved projects may not be signed yet — they "
               "are in the pipeline below.")

    st.subheader("What is coming: the pipeline")
    left, right = st.columns([2, 3])
    gp = op.assign(status=op["status"].replace({"Pending Approval": "Awaiting Board approval",
                                                "Pending Signing": "Approved, awaiting signing"}))
    g = gp.groupby(["ifc_region", "status"]).agg(value=("amount", "sum"), n=("project_number", "count")).reset_index()
    fig = px.bar(g, x="value", y="ifc_region", color="status", orientation="h", custom_data=["n"],
                 title="Pipeline by IFC region",
                 category_orders={"ifc_region": list(g.groupby("ifc_region")["value"].sum()
                                                     .sort_values(ascending=False).index),
                                  "status": ["Approved, awaiting signing", "Awaiting Board approval"]},
                 color_discrete_map={"Awaiting Board approval": "#b7d3f6", "Approved, awaiting signing": "#5598e7"})
    fig.update_traces(hovertemplate="%{y} · %{fullData.name}<br>US$%{x:,.0f}m · %{customdata[0]} project(s)"
                                    "<extra></extra>")
    fig = style_fig(fig, 380)
    fig.update_xaxes(title_text=value_label, tickformat=",")
    show(fig, left)
    top = (op.sort_values("amount", ascending=False).head(10)
           [["project_name", "company_name", "country", "business_line", "status", "board_fy", "amount"]]
           .rename(columns={"project_name": "Project", "company_name": "Client", "country": "Country / region",
                            "business_line": "Business line", "status": "Status", "board_fy": "FY",
                            "amount": "Amount (US$m)"}))
    right.markdown("**Largest pipeline projects**")
    right.dataframe(top, hide_index=True, width="stretch",
                    column_config={"Amount (US$m)": st.column_config.NumberColumn(format="%,.1f")})
    st.caption("Pipeline projects are not yet legal commitments and their amounts may change. Most projects still "
               "awaiting Board approval do not show an amount yet; hover over a bar to see the number of projects.")

    # ---- Summary（直近3年度と、その前の3年度の平均を比べる。今年度は途中なので含めない）
    last = CURRENT_FY - 1
    by_fy_reg = oc.groupby(["board_fy", "ifc_region"])["amount"].sum().unstack(fill_value=0)
    rec_y, base_y = list(range(last - 2, last + 1)), list(range(last - 5, last - 2))
    rec_r, base_r = by_fy_reg.reindex(rec_y).fillna(0).mean(), by_fy_reg.reindex(base_y).fillna(0).mean()
    diff = (rec_r - base_r).drop(labels=[r for r in NO_REGION if r in rec_r.index], errors="ignore")
    big = diff.abs().idxmax() if len(diff) else None
    summaries.render(summaries.overview({
        "as_of": f"{AS_OF:%d %B %Y}", "active": total, "n_active": len(oa), "n_countries": n_cty,
        "by_region": oa.groupby("ifc_region")["amount"].sum(), "by_bl": oa.groupby("business_line")["amount"].sum(),
        "recent": rec_r.sum(), "base": base_r.sum(), "recent_years": (rec_y[0], rec_y[-1]),
        "base_years": (base_y[0], base_y[-1]),
        "top_growth": (f"{big} ({summaries.usd(diff[big])} a year)".replace("US$-", "−US$") if big else None),
        "pipeline": op["amount"].sum(), "n_pipeline": len(op), "n_noamt": n_noamt,
        "pipe_by_region": op.groupby("ifc_region")["amount"].sum(),
    }), "overview", "Generated from the figures on this tab using fixed rules (see Notes on the data). Amounts are "
                    "Board-approved commitments, not outstanding balances.")
    ai_commentary("overview", True, {"tab": "overview"})

# ================================================================ 2 Portfolio Profile
with tab_profile:
    if empty:
        st.info("No projects match the current filters. Widen the fiscal years or change a filter on the left.")
    else:
        t = stage_totals(facts)
        n_unpriced = int((p["portfolio_stage"].eq("Pipeline") & p["amount"].isna()).sum())
        phrases = {
            "Active portfolio": "**{amt}** is in the active portfolio (**{n}**)",
            "Completed": "**{amt}** is completed (**{n}**)",
            "Pipeline": "**{amt}** is in the pipeline (**{n}**)",
            "On hold": "**{amt}** is on hold (**{n}**)",
        }
        parts = [phrases[sg].format(amt=md_usd(t[sg][0]), n=n_projects(t[sg][1])) for sg in STAGES if sg in stages]
        st.markdown(f"Of the commitments approved in FY{fy[0]}–FY{fy[1]} for {scope_text()}, "
                    + ", ".join(parts) + ".")
        st.caption(DEFINITIONS)
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Active portfolio", fmt_usd(t["Active portfolio"][0]) if "Active portfolio" in stages
                  else "not selected")
        k2.metric("Pipeline", fmt_usd(t["Pipeline"][0]) if "Pipeline" in stages else "not selected")
        k3.metric("Completed", fmt_usd(t["Completed"][0]) if "Completed" in stages else "not selected",
                  help="The track record. Add it with the Stage filter.")
        k4.metric("Projects", f"{p['project_number'].nunique():,}")
        notes = []
        n_program = int(p["amount_basis"].str.startswith("Program-level").sum())
        if n_program and not include_program:
            notes.append(f"Amounts for {n_program} program-level record(s) are excluded from totals.")
        if n_unpriced:
            notes.append(f"{n_unpriced} pipeline project(s) have no amount disclosed yet.")
        if notes:
            st.caption(" ".join(notes) + " See *Notes on the data*.")

        st.subheader("What it is made of")
        st.caption("Share of the approved amount for the stages selected on the left. Choose one business line or "
                   "region on the left to see departments or countries.")
        c1, c2 = st.columns(2)
        show(pie(facts, "industry", "What — by industry"), c1)
        show(pie(facts, "instrument", "How — by instrument", colors=INSTRUMENT_COLORS), c2)
        c3, c4 = st.columns(2)
        show(pie(facts, who_col, f"Who commits — by {who_label}",
                 colors=BUSINESS_LINE_COLORS if who_col == "business_line" else None), c3)
        show(pie(facts, where_col, f"Where — by {where_label}"), c4)

        st.subheader("Largest clients")
        client_of = p.set_index("project_number")["company_name"].fillna("(client not specified)")
        cl = facts.assign(client=facts["project_number"].map(client_of))
        g = breakdown(cl, "client", other=False)
        share = g["value"].sum() / facts["amount"].sum() if facts["amount"].sum() else 0
        show(stacked_bar(g, "client", f"Top 10 clients — {share:.0%} of the selection"))

        st.subheader("How new commitments have shifted")
        st.caption("Share of each fiscal year's approved amount, for the stages selected on the left. These are new "
                   "commitments per year, not the portfolio outstanding at the time — the source data has no "
                   "historical balances. Hover to see amounts.")
        c1, c2 = st.columns(2)
        show(share_by_year(facts, "instrument", "Instrument mix by fiscal year", colors=INSTRUMENT_COLORS,
                           order=[i for i in INSTRUMENT_ORDER if i in set(facts["instrument"])]), c1)
        # 業種が1つに決まるとき（例: FIG はほぼ Financial Markets のみ）は、国（または地域）の構成を出す
        if facts.dropna(subset=["amount"])["industry"].nunique() > 1:
            show(share_by_year(facts, "industry", "Industry mix by fiscal year (top 6)"), c2)
        else:
            show(share_by_year(facts, where_col, f"{where_label.capitalize()} mix by fiscal year (top 6)"), c2)

        # ---- 案件までの絞り込み
        st.subheader("Projects behind the numbers")
        DIMENSIONS = {"Business line": "business_line", "Department": "dept_name", "IFC region": "ifc_region",
                      "Country": "country", "Industry": "industry", "Instrument": "instrument"}
        PATHS = {
            "Who → Where → What → How": ["Business line", "Department", "IFC region", "Country", "Industry",
                                         "Instrument"],
            "Where → Who → What → How": ["IFC region", "Country", "Business line", "Department", "Industry",
                                         "Instrument"],
            "What → Who → Where → How": ["Industry", "Business line", "Department", "IFC region", "Country",
                                         "Instrument"],
        }
        st.caption("Break the selection down step by step. Click a box to zoom in; click the top bar to go back.")
        path_name = st.radio("Break down by", list(PATHS), horizontal=True)
        d = facts.dropna(subset=["amount"])
        d = d[d["amount"] > 0]
        # 絞り込みで値が1つに決まっている階層は飛ばす（その階層は分かれないため）
        fixed = [f"{lvl}: {facts[DIMENSIONS[lvl]].iloc[0]}" for lvl in PATHS[path_name]
                 if facts[DIMENSIONS[lvl]].nunique() == 1]
        path = [lvl for lvl in PATHS[path_name] if facts[DIMENSIONS[lvl]].nunique() > 1] or PATHS[path_name][:1]
        if fixed:
            st.caption("Fixed by your filters, so not broken down: " + " · ".join(fixed))
        if not d.empty:
            leaf = d.groupby([DIMENSIONS[x] for x in path])["amount"].sum().reset_index()
            first = DIMENSIONS[path[0]]
            if first == "business_line":
                cmap = BUSINESS_LINE_COLORS
            elif first == "instrument":
                cmap = INSTRUMENT_COLORS
            else:
                ranked = leaf.groupby(first)["amount"].sum().sort_values(ascending=False).index
                cmap = {v: (PALETTE[i] if i < len(PALETTE) else "#a8a7a2") for i, v in enumerate(ranked)}
            fig = px.icicle(leaf, path=[px.Constant("All selected projects")] + [DIMENSIONS[x] for x in path],
                            values="amount", color=first, color_discrete_map=cmap, maxdepth=3)
            fig.update_traces(texttemplate="%{label}<br>US$%{value:,.0f}m · %{percentRoot:.0%} of total",
                              hovertemplate="%{label}<br>US$%{value:,.0f}m<br>%{percentRoot:.1%} of total · "
                                            "%{percentParent:.1%} of the level above<extra></extra>",
                              root_color="rgba(0,0,0,0)")
            # 全体を表す左端の箱は、区分の色と紛れないように灰色にする
            tr = fig.data[0]
            if tr.marker.colors is not None:
                tr.marker.colors = ["#8a8984" if i == "All selected projects" else c
                                    for i, c in zip(tr.ids, tr.marker.colors)]
            fig.update_layout(height=520, margin=dict(l=4, r=4, t=8, b=4))
            show(fig)

        st.caption("Pick a value at each level to narrow down to individual projects.")
        sub = facts.copy()
        cols = st.columns(len(path))
        for i, (col_ui, lvl) in enumerate(zip(cols, path)):
            c = DIMENSIONS[lvl]
            opts = sub.groupby(c)["amount"].sum().sort_values(ascending=False).index.tolist()
            choice = col_ui.selectbox(lvl, [ALL] + opts, key=f"drill-{path_name}-{i}")
            if choice != ALL:
                sub = sub[sub[c] == choice]
        tt = stage_totals(sub)
        st.markdown(f"**{n_projects(sub['project_number'].nunique())}**"
                    + "".join(f" · {sg.lower()} **{md_usd(tt[sg][0])}**" for sg in STAGES if sg in stages))
        proj_tbl = (sub.groupby("project_number")
                    .agg(amount=("amount", lambda s: s.sum(min_count=1)),
                         instruments=("instrument", lambda s: ", ".join(sorted(set(s)))))
                    .reset_index()
                    .merge(projects[["project_number", "project_name", "company_name", "country", "board_fy",
                                     "dept_name", "portfolio_stage", "status", "project_url"]],
                           on="project_number", how="left")
                    .sort_values("amount", ascending=False))
        proj_tbl = proj_tbl[["project_name", "company_name", "country", "board_fy", "dept_name", "instruments",
                             "portfolio_stage", "status", "amount", "project_url"]].rename(columns={
            "project_name": "Project", "company_name": "Client", "country": "Country / region", "board_fy": "FY",
            "dept_name": "Department", "instruments": "Instruments", "portfolio_stage": "Stage",
            "status": "Status", "amount": "Amount (US$m)", "project_url": "Disclosure"})
        st.dataframe(proj_tbl, hide_index=True, width="stretch", height=420,
                     column_config={"Disclosure": st.column_config.LinkColumn(display_text="Open"),
                                    "Amount (US$m)": st.column_config.NumberColumn(format="%,.1f")})
        # CSV には、表に出していない属性（E&S分類、所得水準、国のリスクなど）も含める
        csv_cols = {"project_number": "Project no.", "project_name": "Project", "company_name": "Client",
                    "business_line": "Business line", "dept_name": "Department", "country": "Country / region",
                    "ifc_region": "IFC region", "industry": "Industry", "board_fy": "FY",
                    "board_fy_basis": "FY basis", "portfolio_stage": "Stage", "status": "Status",
                    "amount": "Amount (US$m)", "total_approved_usd_m": "Approved as published (US$m)",
                    "amount_basis": "Amount basis", "environmental_category": "E&S category",
                    "income_level": "Income group (current)", "oecd_crc_group": "OECD country risk",
                    "ifc_region_basis": "Basis for IFC region", "project_url": "Disclosure"}
        csv = (p[p["project_number"].isin(sub["project_number"])][list(csv_cols)]
               .sort_values(["board_fy", "project_number"], ascending=[False, True]).rename(columns=csv_cols))
        st.download_button(f"Download these {len(csv):,} projects as CSV (all attributes)",
                           csv.to_csv(index=False).encode("utf-8-sig"),
                           file_name="ifc_projects_selected.csv", mime="text/csv")

        # ---- Summary
        by_client = cl.groupby("client")["amount"].sum().sort_values(ascending=False)
        ftot = facts["amount"].sum()
        by_ind = facts.groupby("industry")["amount"].sum()
        mix_col = "industry" if (by_ind > 0).sum() > 1 else where_col
        s1, yrs = mix_shift(facts, "instrument")
        s2, _ = mix_shift(facts, mix_col)
        summaries.render(summaries.portfolio({
            "scope": scope_text(), "fy": fy, "total": ftot, "n_projects": p["project_number"].nunique(),
            "stages": {sg: t[sg][0] for sg in STAGES if sg in stages},
            "by_industry": by_ind[by_ind > 0].sort_values(ascending=False), "n_industries": int((by_ind > 0).sum()),
            "by_instrument": facts.groupby("instrument")["amount"].sum(),
            "by_where": facts.groupby(where_col)["amount"].sum(), "where_label": where_label,
            "top10_clients": by_client.head(10).sum() / ftot if ftot else 0,
            "top_client": by_client.index[0] if len(by_client) else None,
            "top_client_share": by_client.iloc[0] / ftot if ftot and len(by_client) else 0,
            "shift": s1 + s2, "shift_span": yrs,
            "mix_label": "industry" if mix_col == "industry" else where_label,
        }), "portfolio", "Generated from the figures on this tab using fixed rules (see Notes on the data). Amounts are "
                         "Board-approved commitments by approval fiscal year for the stages selected; shares of new "
                         "commitments are compared between the first and last three fiscal years with amounts.")
        ai_commentary("portfolio", WHOLE_IFC, {"tab": "portfolio", "fy": list(fy), "stages": stages})

# ================================================================ 3 Risk Profile
with tab_risk:
    # 実行中の案件は、承認年度・段階の絞り込みによらず全件を対象にする
    pa, fa = apply_filters(None, ["Active portfolio"])
    total = pa["amount"].sum() if not pa.empty else 0
    if pa.empty or not total:
        st.info("No active projects match the current filters.")
    else:
        pa = pa.assign(client=pa["company_name"].fillna("(client not specified)"))
        undisb = pa[pa["status"] == "Pending Disbursement"]
        top10 = pa.groupby("client")["amount"].sum().nlargest(10).sum() / total
        eq_amt = fa[fa["instrument"] == "Equity"]["amount"].sum()
        high_crc = pa[pa["oecd_crc"] >= 6]
        st.markdown(f"{scope_text()} has **{md_usd(total)}** in its active portfolio: "
                    f"**{n_projects(len(pa))}** with **{pa['client'].nunique():,} clients**. "
                    f"The ten largest clients account for **{top10:.0%}** of it, equity for **{eq_amt / total:.0%}**, "
                    f"and countries in OECD risk categories 6–7 for **{high_crc['amount'].sum() / total:.0%}**.")
        st.caption("All signed, ongoing projects, whatever their approval year (the period and stage filters do not "
                   "apply). This is a structural view based on approved amounts: the source data has no outstanding "
                   "balances, repayments or credit-quality information, so it shows where risk could concentrate, "
                   "not how the assets are performing.")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Active portfolio", fmt_usd(total), help=n_projects(len(pa)))
        k2.metric("Top 10 clients", f"{top10:.0%}", help="Share of the active portfolio")
        k3.metric("Equity", f"{eq_amt / total:.0%}", help="Share of the active portfolio")
        k4.metric("OECD risk 6–7", fmt_usd(high_crc["amount"].sum()))

        st.subheader("Concentration")
        c1, c2 = st.columns(2)
        for col_ui, key, label in ((c1, "client", "clients"), (c2, "country", "countries")):
            top = pa.groupby(key)["amount"].sum().sort_values(ascending=False).head(10)
            show(pie(pa, key, f"Top 10 {label} — {top.sum() / total:.0%} of the active portfolio", top=10,
                     other_label=f"Other {label}", height=560, legend_below=True), col_ui)

        st.subheader("Risk characteristics")
        env = pa["environmental_category"].fillna("")
        high_es = pa[env.str.startswith("A") | env.str.startswith("FI-1")]
        poor = pa[pa["lending_type"].eq("IDA").fillna(False) | pa["income_level"].eq("Low income").fillna(False)]
        old_ = pa[(CURRENT_FY - pa["board_fy"]) >= 10]
        fx_high = pa[pa["fx_depr_5y"] >= 0.05]
        prof = pd.DataFrame([
            ("OECD country risk category 6–7", high_crc["amount"].sum(), len(high_crc)),
            ("Equity (no contractual repayment)", eq_amt, fa[fa["instrument"] == "Equity"]["project_number"].nunique()),
            ("High E&S category (A or FI-1)", high_es["amount"].sum(), len(high_es)),
            ("IDA or low-income countries", poor["amount"].sum(), len(poor)),
            ("Approved 10+ fiscal years ago", old_["amount"].sum(), len(old_)),
            ("Currency down 5%+ a year against US$ (5 years)", fx_high["amount"].sum(), len(fx_high)),
            ("Signed, awaiting disbursement", undisb["amount"].sum(), len(undisb)),
        ], columns=["Characteristic", "Amount", "Projects"])
        prof["Share"] = prof["Amount"] / total * 100
        fig = px.bar(prof, x="Share", y="Characteristic", orientation="h", custom_data=["Amount", "Projects"],
                     title="Share of the active portfolio with each characteristic",
                     category_orders={"Characteristic": list(prof["Characteristic"])},
                     color_discrete_sequence=[STAGE_COLORS["Active portfolio"]])
        fig.update_traces(hovertemplate="%{y}<br>%{x:.1f}% · US$%{customdata[0]:,.0f}m · "
                                        "%{customdata[1]} project(s)<extra></extra>")
        fig = style_fig(fig, 330)
        fig.update_xaxes(title_text="% of active portfolio amount", ticksuffix="%", range=[0, 100])
        show(fig)
        st.caption("The characteristics overlap: one project can have several.")

        st.subheader("Country risk")
        # 0〜7の全段階を常に同じ並びで表示する（該当がない段階は0）。分類対象外・国なしは該当があるときだけ
        crc = pa.groupby("oecd_crc_group")["amount"].sum().reindex(CRC_ORDER, fill_value=0).reset_index()
        crc = crc[crc["oecd_crc_group"].str.startswith("Category") | (crc["amount"] > 0)]
        crc["axis_label"] = crc["oecd_crc_group"].str.replace("Category ", "", regex=False)
        rated = pa.dropna(subset=["oecd_crc", "amount"])
        w_avg = (rated["oecd_crc"] * rated["amount"]).sum() / rated["amount"].sum() if rated["amount"].sum() else None
        as_of = pa["oecd_crc_as_of"].dropna()
        if crc["amount"].sum() == 0 or as_of.empty:
            st.info("Country risk data is not loaded yet (run src/fetch_country_data.py).")
        else:
            left, right = st.columns([3, 1])
            fig = px.bar(crc, x="axis_label", y="amount", color="oecd_crc_group",
                         title="Active portfolio by OECD country risk category",
                         category_orders={"axis_label": list(crc["axis_label"]), "oecd_crc_group": CRC_ORDER},
                         color_discrete_map=CRC_COLORS)
            fig.update_traces(hovertemplate="%{fullData.name}<br>US$%{y:,.0f}m<extra></extra>")
            fig = style_fig(fig, 320)
            fig.update_layout(showlegend=False)
            # 「0」〜「7」が数値と解釈されないよう、区分の軸として扱う
            fig.update_xaxes(type="category", title_text="OECD country risk category (0 = lowest, 7 = highest)")
            fig.update_yaxes(title_text="US$ millions", tickformat=",")
            show(fig, left)
            right.metric("Amount-weighted average category", f"{w_avg:.1f}" if w_avg is not None else "—",
                         help="Average OECD category of the countries, weighted by active amount. "
                              "Multi-country projects and unclassified countries are left out.")
            st.caption(CRC_NOTE.format(as_of=pd.Timestamp(as_of.max()).strftime("%d %B %Y"))
                       + " Multi-country projects have no single country and show as *No data*.")

        # ---- 為替リスク
        st.subheader("Currency risk")
        if pa["fx_depr_5y"].notna().sum() == 0:
            st.info("Exchange-rate data is not loaded yet (run src/fetch_country_data.py --source wb_fx).")
        else:
            left, right = st.columns([3, 1])
            fxg = pa.groupby("fx_group")["amount"].sum().reindex(FX_ORDER, fill_value=0).reset_index()
            fig = px.bar(fxg, x="fx_group", y="amount", color="fx_group", color_discrete_map=FX_COLORS,
                         category_orders={"fx_group": FX_ORDER},
                         title="Active portfolio by how fast the country's currency fell against the US$")
            fig.update_traces(hovertemplate="%{x}<br>US$%{y:,.0f}m<extra></extra>")
            fig = style_fig(fig, 320)
            fig.update_layout(showlegend=False)
            fig.update_xaxes(title_text="Average annual depreciation against the US$ over the last 5 years")
            fig.update_yaxes(title_text="US$ millions", tickformat=",")
            show(fig, left)
            fx_c = fx_high.groupby("country")["amount"].sum().sort_values(ascending=False)
            right.metric("Depreciating 5%+ a year", f"{fx_high['amount'].sum() / total:.0%}",
                         help=fmt_usd(fx_high["amount"].sum()) + " of the active portfolio")
            right.caption("Largest: " + (", ".join(fx_c.index[:3]) if len(fx_c) else "none"))
            fx_year = pd.to_datetime(pa["fx_as_of"].dropna()).dt.year.max() if "fx_as_of" in pa else None
            st.caption("The source data does not say in which currency each investment is made. Most IFC loans are "
                       "in US dollars: a falling local currency then weighs on clients whose income is in that "
                       "currency and returns to IFC as credit risk, while local-currency loans and equity carry it "
                       "directly. Rates are World Bank official exchange rates (annual averages"
                       + (f", latest {fx_year}" if fx_year else "") + "); in some countries the official rate "
                       "differs from the market rate.")

            # ---- 国のリスクの散布図（国リスク × 為替 × 金額）
            st.subheader("Country risk map")
            cm = (pa.dropna(subset=["iso3"]).groupby(["country", "ifc_region"])
                  .agg(amount=("amount", "sum"), projects=("project_number", "count"),
                       crc=("oecd_crc", "first"), fx=("fx_depr_5y", "first"),
                       crc_label=("oecd_crc_label", "first"), fx_label=("fx_depr_5y_label", "first"))
                  .reset_index())
            cm = cm[(cm["amount"] > 0) & cm["crc"].notna() & cm["fx"].notna()]
            if cm.empty:
                st.info("No country in the selection has both an OECD category and exchange-rate data.")
            else:
                cm["fx_pct"] = cm["fx"] * 100
                fig = px.scatter(cm, x="fx_pct", y="crc", size="amount", color="ifc_region", size_max=55,
                                 hover_name="country", custom_data=["amount", "projects", "fx_label"],
                                 title="Each bubble is a country: country risk (OECD) against currency weakness, "
                                       "sized by active amount",
                                 color_discrete_sequence=PALETTE)
                fig.update_traces(marker=dict(opacity=0.7, line=dict(width=0.5, color="#ffffff")),
                                  hovertemplate="%{hovertext}<br>US$%{customdata[0]:,.0f}m · %{customdata[1]} "
                                                "project(s)<br>OECD category %{y:.0f} · currency %{customdata[2]}"
                                                "<extra></extra>")
                big = cm.nlargest(8, "amount")
                for r in big.itertuples():
                    fig.add_annotation(x=r.fx_pct, y=r.crc, text=r.country, showarrow=False, yshift=14,
                                       font=dict(size=10, color="#5f5e5a"))
                fig.add_vline(x=5, line_dash="dot", line_color="#a8a7a2")
                fig.add_hline(y=5.5, line_dash="dot", line_color="#a8a7a2")
                fig = style_fig(fig, 480)
                fig.update_xaxes(title_text="Average annual depreciation against the US$, last 5 years (%)",
                                 ticksuffix="%", zeroline=True)
                fig.update_yaxes(title_text="OECD country risk category (0 = lowest, 7 = highest)",
                                 range=[-0.5, 7.8], dtick=1)
                fig.update_layout(legend=dict(y=-0.2))
                show(fig)
                st.caption("Countries in the upper right combine high country risk with a quickly weakening "
                           "currency. Dotted lines mark OECD category 6 and 5% annual depreciation. Labels show the "
                           "eight largest countries. Unclassified countries and multi-country projects are not "
                           "shown.")

        watchlist.render(pa, fa, CURRENT_FY, fmt_usd)  # watchlist: 削除するときはこの行を消す

        # ---- Summary
        by_client = pa.groupby("client")["amount"].sum().sort_values(ascending=False)
        by_cty = pa.groupby("country")["amount"].sum().sort_values(ascending=False)
        crc67_c = high_crc.groupby("country")["amount"].sum().sort_values(ascending=False)
        try:   # 注視リストのモジュールを削除しても、Summary は動くようにする
            watch = int((watchlist.flag(pa, fa, CURRENT_FY)["signals"] >= watchlist.DEFAULT_MIN_SIGNALS).sum())
        except NameError:
            watch = None
        summaries.render(summaries.risk({
            "scope": scope_text(), "total": total, "n": len(pa), "n_clients": pa["client"].nunique(),
            "top10": top10, "top_client": by_client.index[0], "top_client_share": by_client.iloc[0] / total,
            "top_country": by_cty.index[0], "top_country_share": by_cty.iloc[0] / total,
            "w_avg": w_avg, "crc67": high_crc["amount"].sum() / total, "crc67_amt": high_crc["amount"].sum(),
            "crc67_countries": list(crc67_c.index[:3]),
            "equity": eq_amt / total, "high_es": high_es["amount"].sum() / total, "old": old_["amount"].sum() / total,
            "undisb": undisb["amount"].sum(), "watch": watch,
            "fx_high": fx_high["amount"].sum() / total if pa["fx_depr_5y"].notna().any() else None,
            "fx_countries": list(fx_high.groupby("country")["amount"].sum().sort_values(ascending=False).index[:3]),
        }), "risk", "Generated from the figures on this tab using fixed rules (see Notes on the data). Based on "
                    "Board-approved amounts; outstanding balances, repayments and credit quality are not in the "
                    "source data.")
        ai_commentary("risk", WHOLE_IFC, {"tab": "risk"})

# ================================================================ 4 vs EBRD
with tab_ebrd:
    if ebrd is None:
        st.info("EBRD data is not loaded yet. Put the EBRD files in data/raw/ebrd/inbox/ and run src/build_ebrd.py.")
    else:
        e_ops, e_inst = ebrd
        e_last = int(e_ops["signing_year"].max())
        y0, y1 = fy[0], min(fy[1], e_last)
        st.markdown(WHY_EBRD)
        private_only = True        # IFC は民間向けだけなので、EBRD も民間向けで比べる（政府向けは「役割」の図で示す）
        e_as_of = str(pd.Timestamp(e_ops["as_of"].max()).date())
        rate, rate_src = EUR_USD.get(e_as_of, EUR_USD[max(EUR_USD)])
        if e_as_of not in EUR_USD:
            rate_src += f" (no rate set for the EBRD data date {e_as_of}; add it to EUR_USD in app.py)"

        # IFC: 署名済みの案件（実行中＋完了）。業種・金融手段の絞り込みは使わない（構成をゆがめないため）
        ic, ifx = apply_filters((y0, y1), COMMITTED, what_how=False)
        ic_all = ic                                  # 複数国向けも含む（地域単位の比較に使う）
        ic = ic.dropna(subset=["iso3"])
        # EBRD: 署名年・国・業種（事業部門に対応するもの）で絞る
        e = e_ops[e_ops["signing_year"].between(y0, y1) & e_ops["iso3"].notna()]
        if business_line in EBRD_SECTOR_MATCH:
            m_col, m_values, e_label = EBRD_SECTOR_MATCH[business_line]
            e = e[e[m_col].isin(m_values)]
        else:
            e_label = "all EBRD sectors"
        iso_region = projects.dropna(subset=["iso3"]).groupby("iso3")["ifc_region"].first()
        if region != ALL:
            e = e[e["iso3"].map(iso_region).eq(region)]
        if country_choice != ALL:
            e = e[e["iso3"].isin(projects.loc[projects["country"] == country_choice, "iso3"])]
        e_all = e                                   # 官民の両方（政府向けの比率に使う）
        if private_only:
            e = e[e["portfolio_class"] == "Private"]
        e = e.assign(amount_usd=e["ebrd_finance_eur_m"] * rate)

        ifc_cty = ic.groupby("iso3")["amount"].sum()
        ebrd_cty = e.groupby("iso3")["amount_usd"].sum()
        both = sorted(set(ifc_cty[ifc_cty > 0].index) & set(ebrd_cty[ebrd_cty > 0].index))
        names = projects.dropna(subset=["iso3"]).groupby("iso3")["country"].first()

        bl_text = (f"{business_line} ({BUSINESS_LINE_NAMES.get(business_line)})" if business_line != ALL
                   else "all IFC business lines")
        st.markdown(f"**IFC {bl_text}** compared with **{e_label}** (private sector)"
                    + f", in the countries where both invested in {y0}–{y1}"
                    + (f" within **{region}**" if region != ALL else "") + ".")
        st.caption(f"IFC: signed projects (active or completed) by approval fiscal year, in US\\$. EBRD: net "
                   f"cumulative investment by year of first signing, in euros converted at {rate:.4f} US\\$ per euro ({rate_src}). "
                   f"EBRD data ends in {e_last}. The industry and instrument filters on the left do not apply here. "
                   "The definitions differ, so read the comparison as orders of magnitude and shares rather than "
                   "exact figures — see *Notes on the data*.")
        # ---- 地域ごとのプレゼンス（地域・国を絞っていないとき）
        # 比べる国は、EBRD が投資している国（期間中に1件でもある国）に限る。IFC の地域には EBRD の対象外の国が
        # 多く含まれるため（例: アフリカの大半）、地域全体の IFC と比べると、IFC の比率が実態より大きく見える
        if region == ALL and country_choice == ALL:
            st.subheader("Presence by region")
            region_of = lambda iso: iso_region.get(iso, EBRD_REGION_FALLBACK.get(iso))
            ebrd_iso = set(e["iso3"])
            r_ifc = ic[ic["iso3"].isin(ebrd_iso)].groupby(ic["iso3"].map(region_of))["amount"].sum()
            r_ebrd = e.groupby(e["iso3"].map(region_of))["amount_usd"].sum()
            reg = pd.DataFrame({"IFC": r_ifc, "EBRD": r_ebrd}).fillna(0)
            reg = reg.assign(total=reg["IFC"] + reg["EBRD"]).sort_values("total", ascending=False)
            reg.index = [f"{r} ({sum(region_of(x) == r for x in ebrd_iso)} countries)" for r in reg.index]
            show(presence_chart(reg, "IFC share of the two, by IFC region (countries where the EBRD invests)"))
            st.caption("Only countries where the EBRD invested in the period are counted, grouped by IFC's "
                       "regional classification — e.g. Africa here means Egypt, Morocco and Tunisia (and any other "
                       "EBRD country in Africa). IFC multi-country projects are not included because they cannot be "
                       "assigned to a country; EBRD regional operations count through the EBRD's own allocation to "
                       "countries.")

        if not both:
            st.info("No country in the current scope has both IFC and EBRD investment in this period. "
                    "Try a wider region or period.")
        else:
            ic = ic[ic["iso3"].isin(both)]
            ifx = ifx[ifx["project_number"].isin(ic["project_number"])]
            e, e_all = e[e["iso3"].isin(both)], e_all[e_all["iso3"].isin(both)]
            i_tot, e_tot = ic["amount"].sum(), e["amount_usd"].sum()
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("IFC", fmt_usd(i_tot), help=n_projects(ic["project_number"].nunique()))
            k2.metric("EBRD", fmt_usd(e_tot), help=f"{fmt_eur(e['ebrd_finance_eur_m'].sum())}; "
                                                   f"{e['operation_name'].nunique():,} operations")
            k3.metric("IFC share of the two", f"{i_tot / (i_tot + e_tot):.0%}")
            k4.metric("Countries compared", f"{len(both)}")

            # ---- 国ごとの規模
            st.subheader("Where each invests")
            cmp_ = pd.DataFrame({"IFC": ifc_cty.reindex(both), "EBRD": ebrd_cty.reindex(both)}).fillna(0)
            cmp_["country"] = [names.get(i, i) for i in cmp_.index]
            cmp_ = cmp_.assign(total=cmp_["IFC"] + cmp_["EBRD"]).sort_values("total", ascending=False)
            top = cmp_.head(15)
            sc = cmp_[(cmp_["IFC"] > 0) & (cmp_["EBRD"] > 0)].copy()
            sc["region"] = [iso_region.get(i, EBRD_REGION_FALLBACK.get(i, "Other")) for i in sc.index]
            sc["share"] = sc["IFC"] / sc["total"] * 100
            fig = px.scatter(sc, x="IFC", y="EBRD", size="total", color="region", hover_name="country", log_x=True,
                             log_y=True, size_max=45, custom_data=["share"], color_discrete_sequence=PALETTE,
                             title="Each bubble is a country: IFC against EBRD amount (log scales)")
            fig.update_traces(marker=dict(opacity=0.75, line=dict(width=0.5, color="#ffffff")),
                              hovertemplate="%{hovertext}<br>IFC US$%{x:,.0f}m · EBRD US$%{y:,.0f}m<br>"
                                            "IFC share %{customdata[0]:.0f}%<extra></extra>")
            lo, hi = sc[["IFC", "EBRD"]].min().min() / 1.5, sc[["IFC", "EBRD"]].max().max() * 1.5
            fig.add_shape(type="line", x0=lo, y0=lo, x1=hi, y1=hi, line=dict(dash="dot", color="#a8a7a2"))
            fig.add_annotation(x=np.log10(hi / 1.6), y=np.log10(hi / 1.6), text="same amount", showarrow=False,
                               xshift=-10, yshift=-12, font=dict(size=10, color="#8a8984"))
            for r in sc.nlargest(10, "total").itertuples():
                fig.add_annotation(x=np.log10(r.IFC), y=np.log10(r.EBRD), text=r.country, showarrow=False,
                                   yshift=13, font=dict(size=10, color="#5f5e5a"))
            fig = style_fig(fig, 34 * len(top) + 150)
            fig.update_xaxes(title_text="IFC, US$ millions")
            fig.update_yaxes(title_text="EBRD, US$ millions (converted)")
            c1, c2 = st.columns([3, 2])
            show(fig, c1)
            c1.caption("Countries above the dotted line receive more from the EBRD than from IFC; countries below it, "
                       "more from IFC. Labels show the ten largest countries.")
            show(presence_chart(top.set_index("country"), "IFC share of the two, by country",
                                height=34 * len(top) + 150), c2)
            multi = ic_all[ic_all["iso3"].isna()]
            if multi["amount"].sum() > 0:
                st.caption(f"IFC also has {md_usd(multi['amount'].sum())} in "
                           f"{n_projects(multi['project_number'].nunique(), 'multi-country project')} in this scope "
                           "that cannot be assigned to a country and are not in the country comparison. EBRD regional "
                           "operations are included through the EBRD's allocation to countries.")

            # ---- 役割（政府向けの比率）と金融セクターへの比重
            st.subheader("Role and focus")
            c1, c2 = st.columns(2)
            state = (e_all[e_all["portfolio_class"] == "State"].groupby("iso3")["ebrd_finance_eur_m"].sum()
                     / e_all.groupby("iso3")["ebrd_finance_eur_m"].sum()).reindex(top.index).fillna(0) * 100
            g = pd.DataFrame({"country": top["country"], "share": state.values})
            if g["share"].max() < 1:
                c1.markdown("**EBRD: share lent to the public sector**")
                c1.caption(f"Almost none: in these countries, EBRD lending in this sector ({e_label}) goes to "
                           "the private sector, as IFC's does. The difference in role shows in other sectors — "
                           "choose *All* or INF on the left.")
            else:
                fig = px.bar(g, x="share", y="country", orientation="h",
                             title="EBRD: share lent to the public sector",
                             category_orders={"country": list(top["country"])}, color_discrete_sequence=[EBRD_COLOR])
                fig.update_traces(hovertemplate="%{y}<br>%{x:.0f}% public sector<extra></extra>")
                fig = style_fig(fig, 30 * len(top) + 120)
                fig.update_xaxes(ticksuffix="%", range=[0, 100])
                show(fig, c1)
                c1.caption("IFC lends only to the private sector (0% by definition). This chart always includes "
                           "EBRD public-sector lending, whatever the toggle above.")
            if business_line == ALL:
                fi_i = (ic[ic["business_line"] == "FIG"].groupby("iso3")["amount"].sum()
                        / ifc_cty).reindex(top.index).fillna(0) * 100
                fi_e = (e[e["sector_group"] == "Financial Institutions"].groupby("iso3")["amount_usd"].sum()
                        / ebrd_cty).reindex(top.index).fillna(0) * 100
                g = pd.DataFrame({"country": top["country"], "IFC": fi_i.values, "EBRD": fi_e.values}).melt(
                    id_vars="country", var_name="Institution", value_name="share")
                fig = px.bar(g, x="share", y="country", color="Institution", orientation="h", barmode="group",
                             title="Share going to financial institutions",
                             category_orders={"country": list(top["country"]), "Institution": ["IFC", "EBRD"]},
                             color_discrete_map={"IFC": IFC_COLOR, "EBRD": EBRD_COLOR})
                fig.update_traces(hovertemplate="%{y} · %{fullData.name}<br>%{x:.0f}%<extra></extra>")
                fig = style_fig(fig, 30 * len(top) + 150)
                fig.update_xaxes(ticksuffix="%", range=[0, 100])
                show(fig, c2)
                c2.caption("IFC: projects of its Financial Institutions Group (FIG). EBRD: its financial "
                           "institutions sectors (banks, non-bank lenders, leasing, insurance and pensions).")
            else:
                c2.markdown("**Sectors compared**")
                c2.caption(f"IFC {business_line} is compared with {e_label}. Choose *All* business lines on the "
                           "left to compare how much of each institution's investment goes to financial "
                           "institutions.")

            # ---- 金融手段と国リスク（構成比）
            c1, c2 = st.columns(2)
            ii = ifx.assign(inst=ifx["instrument"].map(COMMON_INSTRUMENT).fillna("Other")).groupby("inst")["amount"].sum()
            ee = e_inst[e_inst["operation_key"].isin(e["operation_key"])]
            ee = ee.assign(inst=ee["instrument"].map(COMMON_INSTRUMENT).fillna("Other")).groupby("inst")["amount_eur_m"].sum()
            show(share_bars({"IFC": ii, "EBRD": ee}, "inst", "Instrument mix",
                            list(COMMON_INSTRUMENT_COLORS), COMMON_INSTRUMENT_COLORS), c1)
            show(share_bars({"IFC": ic.groupby("oecd_crc_group")["amount"].sum(),
                             "EBRD": e.groupby("oecd_crc_group")["amount_usd"].sum()},
                            "oecd_crc_group", "Country risk taken (OECD category of the country)",
                            CRC_ORDER, CRC_COLORS), c2)

            # ---- 推移
            st.subheader("Over time")
            ty = pd.concat([ic.groupby("board_fy")["amount"].sum().rename("value").rename_axis("year")
                            .reset_index().assign(Institution="IFC"),
                            e.groupby("signing_year")["amount_usd"].sum().rename("value").rename_axis("year")
                            .reset_index().assign(Institution="EBRD")])
            fig = px.line(ty, x="year", y="value", color="Institution", markers=True,
                          title="Amount per year in the countries compared",
                          color_discrete_map={"IFC": IFC_COLOR, "EBRD": EBRD_COLOR})
            fig.update_traces(hovertemplate="%{x} · %{fullData.name}<br>US$%{y:,.0f}m<extra></extra>")
            fig = style_fig(fig, 340)
            fig.update_xaxes(dtick=1, title_text="IFC: approval fiscal year · EBRD: calendar year of first signing")
            fig.update_yaxes(title_text="US$ millions", tickformat=",", rangemode="tozero")
            show(fig)

            # ---- Summary
            cs = cmp_.assign(share=cmp_["IFC"] / cmp_["total"])
            yy = ty.pivot_table(index="year", columns="Institution", values="value", aggfunc="sum").fillna(0)
            yy = yy.reindex(range(y0, y1 + 1)).fillna(0)
            share_of = lambda d: d["IFC"].sum() / (d["IFC"].sum() + d["EBRD"].sum()) if d.values.sum() else None
            dshare = lambda s_, k: s_.get(k, 0) / s_.sum() if s_.sum() else 0
            st_all = (e_all[e_all["portfolio_class"] == "State"].groupby("iso3")["ebrd_finance_eur_m"].sum()
                      / e_all.groupby("iso3")["ebrd_finance_eur_m"].sum()).fillna(0)
            summaries.render(summaries.ebrd({
                "n_countries": len(both), "years": (y0, y1), "ifc": i_tot, "ebrd": e_tot, "rate": rate,
                "ifc_label": "IFC (all business lines)" if business_line == ALL else f"IFC {bl_text}", "ebrd_label": e_label + (" (private sector)" if private_only else ""),
                "ifc_larger": list(cs[cs["share"] > 0.55]["country"].head(5)),
                "ebrd_larger": list(cs[cs["share"] < 0.45]["country"].head(5)),
                "ifc_debt": dshare(ii, "Loan / debt"), "ebrd_debt": dshare(ee, "Loan / debt"),
                "ifc_guar": dshare(ii, "Guarantee"), "ebrd_guar": dshare(ee, "Guarantee"),
                "ifc_eq": dshare(ii, "Equity"), "ebrd_eq": dshare(ee, "Equity"),
                "ifc_crc67": ic.loc[ic["oecd_crc"] >= 6, "amount"].sum() / i_tot if i_tot else 0,
                "ebrd_crc67": e.loc[e["oecd_crc"] >= 6, "amount_usd"].sum() / e_tot if e_tot else 0,
                "state_max": st_all.max() if len(st_all) else None,
                "state_country": names.get(st_all.idxmax(), st_all.idxmax()) if len(st_all) else None,
                "recent_share": share_of(yy.tail(3)) if len(yy) >= 6 else None,
                "early_share": share_of(yy.head(3)) if len(yy) >= 6 else None,
                "recent_years": (y1 - 2, y1),
            }), "ebrd", "Generated from the figures on this tab using fixed rules (see Notes on the data). IFC and EBRD "
                        "amounts are defined differently (approved amounts by fiscal year vs net cumulative investment "
                        "by year of first signing); read them as orders of magnitude and shares.")
            ai_commentary("ebrd", WHOLE_IFC, {"tab": "ebrd", "years": [y0, y1], "private_only": private_only,
                                              "rate": rate})

# ================================================================ 5 Country Profile
with tab_country:
    country_list = (p.groupby("country")["amount"].sum().sort_values(ascending=False).index.tolist()
                    if not empty else [])
    if not country_list:
        st.info("No projects match the current filters.")
    else:
        doc_iso = set(ev["doc_countries"]["iso3"]) if ev else set()
        cty_iso = p.dropna(subset=["iso3"]).groupby("country")["iso3"].first()
        country = st.selectbox(
            "Country / region", country_list,
            index=country_list.index(country_choice) if country_choice in country_list else 0,
            format_func=lambda c: c + ("  ·  IFC reports available" if cty_iso.get(c) in doc_iso else ""))
        cp = p[p["country"] == country]
        cf = facts[facts["country"] == country]
        iso3 = cp["iso3"].dropna().iloc[0] if cp["iso3"].notna().any() else None
        info = cp.iloc[0]
        # 国旗（flagcdn.com の画像）と国名。複数国向けの区分には国旗を出さない
        code = info.get("wb_country_code")
        flag = (f'<img src="https://flagcdn.com/h40/{FLAG_CODE.get(code, code).lower()}.png" alt="" '
                f'style="height:36px;border:1px solid #d3d2cc;border-radius:3px;margin-right:12px;'
                f'vertical-align:middle">' if iso3 and isinstance(code, str) and len(code) == 2 else "")
        st.markdown(f'<div style="display:flex;align-items:center;margin:4px 0 8px 0">{flag}'
                    f'<span style="font-size:1.8rem;font-weight:700">{country}</span></div>',
                    unsafe_allow_html=True)
        st.caption(f"World Bank region: {info.get('wb_region') or '—'} · "
                   f"Income group (current): {info.get('income_level') or '—'} · "
                   f"Lending category: {info.get('lending_type') or '—'} · "
                   f"IFC region: {info.get('ifc_region')} ({info.get('ifc_region_basis') or '—'}) · "
                   f"OECD country risk: {info.get('oecd_crc_label') if pd.notna(info.get('oecd_crc_label')) else '—'} · "
                   f"Currency against US\\$: {info.get('fx_depr_5y_label') if pd.notna(info.get('fx_depr_5y_label')) else '—'}")
        tc_ = stage_totals(cf)
        k1, k2, k3 = st.columns(3)
        k1.metric("Active portfolio", fmt_usd(tc_["Active portfolio"][0]) if "Active portfolio" in stages
                  else "not selected")
        k2.metric("Pipeline", fmt_usd(tc_["Pipeline"][0]) if "Pipeline" in stages else "not selected")
        k3.metric("Projects", f"{cp['project_number'].nunique():,}")

        ebrd_c, report_f = None, {}
        # ---- 数字（IFC と同じ国の EBRD を並べる）→ 大きい案件 → レポートの記述 の順に表示する
        st.subheader(f"Investment in {country}")
        left, right = st.columns(2)
        with left:
            g = cf.groupby(["board_fy", "instrument"])["amount"].sum().rename("value").reset_index()
            fig = px.bar(g, x="board_fy", y="value", color="instrument", title="IFC by fiscal year and instrument",
                         category_orders={"instrument": INSTRUMENT_ORDER}, color_discrete_map=INSTRUMENT_COLORS)
            fig.update_traces(hovertemplate="FY%{x}<br>%{fullData.name}: US$%{y:,.0f}m<extra></extra>")
            fig = style_fig(fig, 340)
            fig.update_xaxes(tickprefix="FY", title_text="IFC fiscal year (July–June)")
            fig.update_yaxes(title_text=value_label, tickformat=",")
            show(fig)

        with right:
            # 同じ国の EBRD（全業種・官民の両方）
            if ebrd is None or iso3 is None:
                st.caption("EBRD data is not available for this selection.")
            else:
                ec = ebrd[0][(ebrd[0]["iso3"] == iso3) & ebrd[0]["signing_year"].between(*fy)]
                ebrd_c = ec
                if ec.empty:
                    st.caption(f"No EBRD investment in {country} in {fy[0]}–{fy[1]} "
                               "(the EBRD may not operate there).")
                else:
                    g = ec.groupby(["signing_year", "sector_group"])["ebrd_finance_eur_m"].sum().reset_index()
                    fig = px.bar(g, x="signing_year", y="ebrd_finance_eur_m", color="sector_group",
                                 title=f"EBRD in {country}, by year of first signing (€ millions)",
                                 color_discrete_map=EBRD_SECTOR_GROUP_COLORS)
                    fig.update_traces(hovertemplate="%{x} · %{fullData.name}<br>€%{y:,.0f}m<extra></extra>")
                    fig = style_fig(fig, 340)
                    fig.update_yaxes(tickformat=",")
                    show(fig)
                    e_sum = ec["ebrd_finance_eur_m"].sum()
                    st.caption(f"EBRD total {fmt_eur(e_sum)}, of which "
                               f"{ec.loc[ec['portfolio_class'] == 'State', 'ebrd_finance_eur_m'].sum() / e_sum:.0%} "
                               "to the public sector. All EBRD sectors; see tab 4 for the comparison with IFC.")

        st.subheader("Largest IFC projects")
        table_ = (cp.sort_values("amount", ascending=False)
                  [["project_name", "company_name", "board_fy", "dept_name", "portfolio_stage", "amount",
                    "project_url"]]
                  .rename(columns={"project_name": "Project", "company_name": "Client", "board_fy": "FY",
                                   "dept_name": "Department", "portfolio_stage": "Stage",
                                   "amount": "Amount (US$m)", "project_url": "Disclosure"}))
        st.dataframe(table_.head(20), hide_index=True, width="stretch",
                     column_config={"Disclosure": st.column_config.LinkColumn(display_text="Open"),
                                    "Amount (US$m)": st.column_config.NumberColumn(format="%,.1f")})

        # ---- IFC レポートの記述（定性情報）
        st.subheader("What IFC reports say")
        if not ev or iso3 is None:
            st.info("No IFC reports are available for this country / region yet.")
        else:
            e_ = ev["evidence"]
            doc_ids = set(ev["doc_countries"].loc[ev["doc_countries"]["iso3"] == iso3, "doc_id"])
            mentioned = e_["countries_mentioned"].apply(lambda x: iso3 in x)
            ce = e_[e_["doc_id"].isin(doc_ids) | mentioned].copy()
            # 対象国の資料の段落を先に、他国の資料での言及を後に並べる
            ce["basis"] = ce["doc_id"].isin(doc_ids).map({True: 0, False: 1})
            sectors = ([industry] if industry != ALL else BUSINESS_LINE_SECTORS.get(business_line, []))
            if sectors:
                label = ", ".join(sectors)
                if st.checkbox(f"Only paragraphs related to {label}", value=True):
                    ce = ce[ce["sectors"].apply(lambda x: any(s in x for s in sectors))]
            if ce.empty:
                st.info("No matching paragraphs (no report for this country, "
                        "or no paragraph related to the selected sector).")
            else:
                docs = ev["documents"][ev["documents"]["doc_id"].isin(ce["doc_id"].unique())]
                themes = ce.explode("themes").dropna(subset=["themes"])
                tcount = themes.groupby("themes").size().sort_values(ascending=True).reset_index(name="Paragraphs")
                fig = px.bar(tcount, x="Paragraphs", y="themes", orientation="h", title="Paragraphs by theme",
                             color_discrete_sequence=[STAGE_COLORS["Active portfolio"]])
                fig.update_traces(hovertemplate="%{y}: %{x} paragraph(s)<extra></extra>")
                c1, c2 = st.columns([3, 2])
                show(style_fig(fig, max(260, 26 * len(tcount) + 80)), c1)
                c2.markdown("**Reports used**")
                for t_, d_ in zip(docs["title"], docs["pub_date"]):
                    c2.caption(f"{t_} ({d_})")
                report_f = {"n_paragraphs": int((ce["kind"] == "paragraph").sum()),
                            "top_themes": tcount.sort_values("Paragraphs", ascending=False)["themes"].head(3).tolist()}
                theme = st.selectbox("Read paragraphs by theme", ["All themes"] + tcount["themes"].tolist()[::-1])
                sel = ce if theme == "All themes" else ce[ce["themes"].apply(lambda x: theme in x)]
                sel = sel[sel["kind"] == "paragraph"].sort_values(["basis", "doc_id", "page", "block_no"])
                st.caption(f"{len(sel):,} paragraph(s) (showing the first 30)")
                for r in sel.head(30).itertuples():
                    with st.container(border=True):
                        st.caption(("[Mentioned in a report on another country] " if r.basis else "")
                                   + f"{r.title} ({r.pub_date}), p.{r.page}"
                                   + (f" | {', '.join(r.themes)}" if r.themes else ""))
                        st.write(r.text)

        # ---- Summary
        by_cl = cp.assign(c=cp["company_name"].fillna("(client not specified)")).groupby("c")["amount"].sum()
        ctot = cp["amount"].sum()
        cf_ = {"country": country, "income": info.get("income_level"),
               "crc": info.get("oecd_crc_label") if pd.notna(info.get("oecd_crc_label")) else None,
               "scope": scope_text(), "fy": fy, "total": ctot, "n": cp["project_number"].nunique(),
               "stages": {sg: tc_[sg][0] for sg in STAGES if sg in stages},
               "by_instrument": cf.groupby("instrument")["amount"].sum(),
               "top_client": by_cl.idxmax() if len(by_cl) and by_cl.max() > 0 else "—",
               "top_client_share": by_cl.max() / ctot if ctot else 0} | report_f
        if ebrd_c is not None:
            cf_ |= {"ebrd": ebrd_c["ebrd_finance_eur_m"].sum(), "ebrd_eur": fmt_eur(ebrd_c["ebrd_finance_eur_m"].sum()),
                    "ebrd_state": (ebrd_c.loc[ebrd_c["portfolio_class"] == "State", "ebrd_finance_eur_m"].sum()
                                   / ebrd_c["ebrd_finance_eur_m"].sum()) if len(ebrd_c) else 0,
                    "ebrd_by_group": ebrd_c.groupby("sector_group")["ebrd_finance_eur_m"].sum()}
        summaries.render(summaries.country(cf_), "country",
                         "Generated from the figures on this tab using fixed rules (see Notes on the data). IFC figures "
                         "follow the filters on the left; EBRD figures cover all sectors in the same years.")

# ================================================================ notes
# データの流れの図（docs/make_data_flow.py で作る SVG。README も同じ図を表示する）
DATA_FLOW_SVG = ROOT / "docs" / "data_flow.svg"

# タブごとに使うデータ（注記タブの表と README に載せる）
DATA_BY_TAB = [
    ("1 IFC Overview", "projects_enriched, project_instruments",
     "Whole of IFC. Active portfolio and pipeline now; new commitments by approval fiscal year (signed projects)."),
    ("2 Portfolio Profile", "projects_enriched, project_instruments",
     "Scope, fiscal years and stages from the sidebar. Approved amounts (US$m) by approval fiscal year."),
    ("3 Risk Profile", "projects_enriched, project_instruments, country_risk",
     "Scope from the sidebar; all active projects. Approved amounts, OECD category of the project's country."),
    ("4 vs EBRD", "projects_enriched, project_instruments, ebrd_operations, ebrd_operation_instruments, "
     "country_risk",
     "IFC signed projects by approval fiscal year (US$) vs EBRD net cumulative investment by year of first "
     "signing (€, converted). Countries where both invested; IFC multi-country projects left out."),
    ("5 Country Profile", "projects_enriched, project_instruments, country_risk, ebrd_operations, evidence",
     "One country; IFC figures follow the sidebar. EBRD investment in the same country; report paragraphs by "
     "theme."),
]

with tab_notes:
    st.subheader("How the data flows")
    if DATA_FLOW_SVG.exists():
        st.image(str(DATA_FLOW_SVG), width="stretch")
    st.caption("Each source is processed by a script in src/ into one database, which the dashboard reads. "
               "The README in the project repository describes each step in detail.")
    st.markdown("**What each tab uses**")
    st.table(pd.DataFrame(DATA_BY_TAB, columns=["Tab", "Data (database tables)", "Selection and amounts"])
             .set_index("Tab"))
    st.markdown("""
**How to read the tabs**
- *1 IFC Overview* always shows the whole of IFC. The filters on the left apply from tab 2 onwards.
- *2 Portfolio Profile* and *5 Country Profile* use the fiscal-year and stage filters.
- *3 Risk Profile* covers all active projects, whatever their approval year.
- *4 vs EBRD* covers signed projects (active or completed) within the fiscal-year range.

**Stages: active portfolio, completed and pipeline**
- *Active portfolio*: signed projects that are still ongoing — active, or signed and awaiting disbursement.
- *Completed*: finished projects. They form the track record; add them in tab 2 with the Stage filter.
- *Pipeline*: projects approved by the Board but not yet signed, or still awaiting Board approval. They are not
  yet legal commitments, their amounts may change, and many do not show an amount yet.
- *On hold*: projects IFC has put on hold. Excluded by default.

**What the amounts represent**
- IFC's own investment as approved by the Board, in US\\$ millions. Disbursements, outstanding balances,
  repayments and mobilised third-party funds are not included. There is therefore no history of the portfolio
  outstanding: charts over time show new commitments by approval year.
- Fiscal years are IFC fiscal years (July–June). Projects not yet approved are placed in the fiscal year of
  their projected Board date (shown as *Projected* in the *FY basis* column of the CSV download).

**Who: business lines and departments**
- Each project's business line is derived from the name of the IFC department recorded for it, using keywords
  (e.g. "FIG" or "Financial Markets" → FIG; "MAS", "Manufacturing" or "Agribusiness" → MAS; "INF" or
  "Infrastructure" → INF). About 1.4% of projects cannot be classified and appear as *Other*.
- The department recorded is the unit responsible at some point after approval, not necessarily the unit that
  originated the project. Business lines are treated as stable across reorganisations.

**How: instruments**
- Amounts are split by instrument using the loan, equity, guarantee and risk-management amounts in the source
  data, so a project combining a loan and equity contributes to both.

**Program-level amounts**
- For some programs, such as the Global Trade Finance Program (GTFP), the program-wide approval ceiling is
  recorded against each participating institution. Their amounts are excluded from totals by default (the
  records are still counted). A record is treated as program-level if its project name starts with "GTFP", or
  if the same amount (US\\$100m or more) was approved on the same date for four or more projects. A few records
  have been reviewed individually and reclassified (marked "manual").

**Regions and income groups**
- IFC regions: countries in Middle East and Central Asia follow IFC's regional web page ("Official"); a few
  countries are confirmed from the IFC regional department responsible for their projects. Other countries are
  assigned provisionally from the World Bank regional classification.
- World Bank regions and income groups are the current classifications and are applied to all years.

**Country risk**
- The OECD Country Risk Classification (0 = lowest, 7 = highest) covers transfer and convertibility risk and
  force majeure. The latest published list is used for all projects.
- The OECD produces it to set minimum premium rates for officially supported export credits and does not endorse
  its use for other purposes. High-income OECD and euro-area countries are not classified. Multi-country projects
  appear as *No data*.

**Currency risk**
- The source data does not record the currency of each investment. Currency risk is therefore shown at country
  level: the average annual change of the country's currency against the US dollar over the last five years, from
  the World Bank's official exchange rates (annual averages, indicator PA.NUS.FCRF). Positive values mean the local
  currency weakened. Euro-area and dollarised economies show little or no change against the dollar.
- Most IFC loans are in US dollars, so currency weakness mainly reaches IFC through its clients' ability to repay;
  local-currency loans and equity are exposed directly. Official rates can differ from market rates where
  exchange controls apply.

**Comparison with the EBRD**
- EBRD figures come from the EBRD's list of investments since 1991 and its project list (ebrd.com). They are
  *net cumulative investment*: commitments after cancellations and sales, including later increases, dated by
  the year the operation was first signed, in euros at month-end reported rates.
- IFC figures are Board-approved amounts by approval fiscal year (July–June), in US dollars. One EUR/USD rate,
  converts EBRD amounts: the ECB euro reference rate on the EBRD data's as-of date (1.1750 US\\$ per euro on
  31 December 2025), consistent with the month-end rates the EBRD uses. Read the comparison as orders of
  magnitude and shares.
- IFC invests only in the private sector; the EBRD also lends to the public sector. The tab compares EBRD
  private-sector operations; EBRD public-sector lending is shown separately as the EBRD's public-sector share.
- Business lines are matched to EBRD sectors approximately: FIG → financial institutions; INF → sustainable
  infrastructure; MAS → corporate sectors excluding equity funds; Funds & Tech → equity funds. Other business
  lines are compared with all EBRD sectors.
- Multi-country projects: IFC records them without a country, so they are left out of the comparison. The EBRD
  allocates its regional operations to countries (by intended, later actual, distribution), so their country
  shares are included. In most selections this is a small part of the EBRD amounts.

**Text from IFC reports**
- Paragraphs from IFC reports are tagged with countries, industries and themes using keyword dictionaries.
  A tag indicates that a paragraph mentions a topic, not that the topic is its main subject. The reports
  currently cover a few countries in Middle East and Central Asia.

""" + summaries.THRESHOLDS_NOTE + """
**Sources**
- IFC Investment Services Projects, World Bank Group Finances One (dataset DS00499)
- World Bank country classification API
- OECD Country Risk Classifications of the Participants to the Arrangement on Officially Supported Export Credits
- EBRD investments since 1991 and EBRD project list (ebrd.com)
- IFC publications listed in each country profile
""")
