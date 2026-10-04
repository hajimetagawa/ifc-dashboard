"""
IFC Portfolio Explorer (Streamlit)

起動（プロジェクトのフォルダで実行）:
    uv run streamlit run app/app.py

データ: data/curated/ifc.duckdb（src/ 以下のスクリプトで作成）
画面の表示はすべて英語（閲覧者が海外のため）。

画面が答える問い:
    Who   … どの事業部門・部署が
    How   … どの投融資手段（融資・株式・保証・リスク管理商品）で
    Where … どの地域・国に
    What  … どの業種に
    How much … 現在いくら抱えているか（Active portfolio）、これまでの実績（Completed）、
               今後コミットする予定か（Pipeline）
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import watchlist  # watchlist: 試行的な注視リスト。削除するときはこの行と、render を呼ぶ行を消す

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("IFC_DB", ROOT / "data" / "curated" / "ifc.duckdb"))

ALL = "All"
DEFAULT_BUSINESS_LINE = "FIG"
DEFAULT_REGION = "Middle East and Central Asia"
STAGES = ["Active portfolio", "Completed", "Pipeline", "On hold"]
DEFAULT_STAGES = ["Active portfolio", "Pipeline"]
ACTIVE = ["Active", "Pending Disbursement"]

# 色はエンティティに固定する（絞り込みで色が変わらないように）
# 実行中を最も濃く、完了済みは控えめに、パイプラインは薄い青
STAGE_COLORS = {"Active portfolio": "#2a78d6", "Completed": "#9c9b96", "Pipeline": "#86b6ef", "On hold": "#d3d2cc"}
INSTRUMENT_ORDER = ["Loan", "Equity", "Guarantee", "Risk management", "Unallocated", "Not specified"]
INSTRUMENT_COLORS = {"Loan": "#eb6834", "Equity": "#1baf7a", "Guarantee": "#eda100",
                     "Risk management": "#e87ba4", "Unallocated": "#a8a7a2", "Not specified": "#c3c2b7"}
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
# レポートの記述を事業部門で絞るときに使う業種
BUSINESS_LINE_SECTORS = {
    "FIG": ["Financial Markets"],
    "MAS": ["Agribusiness and Forestry", "Manufacturing", "Tourism, Retail and Property",
            "Health, Education and Life Sciences"],
    "INF": ["Infrastructure", "Metals and Mining", "Telecommunications and Technology"],
    "Funds & Tech": ["Funds", "Telecommunications and Technology"],
}

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
    "**Active portfolio** = signed and ongoing (active, or signed and awaiting disbursement) — the current exposure. "
    "**Completed** = the track record. "
    "**Pipeline** = approved by the Board but not yet signed, or awaiting approval — not yet a legal commitment. "
    "Amounts are IFC's own investment as approved by the Board, in US\\$ millions."
)


def display_country(name: str) -> str:
    if name in COUNTRY_DISPLAY:
        return COUNTRY_DISPLAY[name]
    if name.endswith(" Subregion") or name.endswith(" Region"):
        return name.rsplit(" ", 1)[0] + " (multi-country)"
    return name


st.set_page_config(page_title="IFC Portfolio Explorer", layout="wide")


# ---------------------------------------------------------------- data
@st.cache_data(show_spinner=False)
def load_data(db: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    with duckdb.connect(db, read_only=True) as con:
        projects = con.sql("SELECT * FROM projects_enriched").df()
        instruments = con.sql("SELECT * FROM project_instruments").df()
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
def load_evidence_tables(db: str) -> dict[str, pd.DataFrame]:
    with duckdb.connect(db, read_only=True) as con:
        names = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        if "evidence" not in names:
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


if not DB_PATH.exists():
    st.error(f"Database not found: {DB_PATH}. Run the scripts in src/ first.")
    st.stop()

projects, facts_all = load_data(str(DB_PATH))
ev = load_evidence_tables(str(DB_PATH))

# ---------------------------------------------------------------- filters (sidebar)
st.sidebar.header("Filters")
st.sidebar.caption("Each filter answers one part of the question: who, how, where, what and when.")

fy_min, fy_max = int(projects["board_fy"].min()), int(projects["board_fy"].max())
fy = st.sidebar.slider("When — fiscal year", fy_min, fy_max, (max(fy_min, 2015), fy_max),
                       help="IFC fiscal years run July–June. Pipeline projects not yet approved are placed in "
                            "the fiscal year of their projected Board date.")
stages = st.sidebar.multiselect("Stage", STAGES, default=DEFAULT_STAGES,
                                help="Active portfolio: signed and ongoing (active, or awaiting disbursement). "
                                     "Completed: finished projects (track record). "
                                     "Pipeline: approved but not yet signed, or awaiting Board approval. "
                                     "On hold: put on hold by IFC.")

bl_options = [b for b in BUSINESS_LINE_ORDER if b in set(projects["business_line"])]
business_line = st.sidebar.selectbox(
    "Who — business line", [ALL] + bl_options,
    index=(bl_options.index(DEFAULT_BUSINESS_LINE) + 1) if DEFAULT_BUSINESS_LINE in bl_options else 0,
    format_func=lambda b: b if b == ALL else f"{b} — {BUSINESS_LINE_NAMES.get(b, b)}",
    help="Derived from the name of the IFC department responsible for the project.")
dept_pool = projects if business_line == ALL else projects[projects["business_line"] == business_line]
departments = sorted(dept_pool["dept_name"].unique())
department = st.sidebar.selectbox("Who — department", [ALL] + departments)

regions = sorted(projects["ifc_region"].unique())
region = st.sidebar.selectbox("Where — IFC region", [ALL] + regions,
                              index=(regions.index(DEFAULT_REGION) + 1) if DEFAULT_REGION in regions else 0)
country_pool = projects if region == ALL else projects[projects["ifc_region"] == region]
country_choice = st.sidebar.selectbox("Where — country", [ALL] + sorted(country_pool["country"].unique()))

industry = st.sidebar.selectbox("What — industry", [ALL] + sorted(projects["industry"].unique()))
instrument = st.sidebar.selectbox("How — instrument", [ALL] + [i for i in INSTRUMENT_ORDER
                                                              if i in set(facts_all["instrument"])])
metric = st.sidebar.radio("Measure", ["Amount", "Number of projects"], horizontal=True)
include_program = st.sidebar.checkbox(
    "Include program-level amounts", value=False,
    help="Some programs (e.g. GTFP) record the program-wide approval ceiling against each participating "
         "institution. Summing these overstates commitments substantially, so they are excluded by default.")

# ---------------------------------------------------------------- apply filters
def apply_filters(fy_range: tuple[int, int] | None, stage_list: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """画面左の絞り込みを適用する。fy_range=None なら年度で絞らない（実行中の案件を全件見るとき）。"""
    q = projects[projects["portfolio_stage"].isin(stage_list)]
    if fy_range is not None:
        q = q[q["board_fy"].between(*fy_range)]
    if business_line != ALL:
        q = q[q["business_line"] == business_line]
    if department != ALL:
        q = q[q["dept_name"] == department]
    if region != ALL:
        q = q[q["ifc_region"] == region]
    if country_choice != ALL:
        q = q[q["country"] == country_choice]
    if industry != ALL:
        q = q[q["industry"] == industry]
    fx = facts_all[facts_all["project_number"].isin(q["project_number"])]
    if instrument != ALL:
        fx = fx[fx["instrument"] == instrument]
        q = q[q["project_number"].isin(fx["project_number"])]
    fx = fx.assign(amount=fx["approved_usd_m"] if include_program else fx["approved_usd_m_for_sum"])
    q = q.merge(fx.groupby("project_number")["amount"].sum(min_count=1).rename("amount"),
                on="project_number", how="left")
    return q, fx


p, facts = apply_filters(fy, stages)

is_amount = metric == "Amount"
value_label = "US$ millions" if is_amount else "Projects"

# 「Who」「Where」は、1つに絞ると一段細かい区分を表示する
who_col, who_label = ("business_line", "business line") if business_line == ALL else ("dept_name", "department")
if department != ALL:
    who_col, who_label = "dept_name", "department"
where_col, where_label = ("ifc_region", "IFC region") if region == ALL else ("country", "country")
what_col, what_label = "industry", "industry"
how_col, how_label = "instrument", "instrument"


def scope_text() -> str:
    who = ("all IFC business lines" if business_line == ALL
           else (department if department != ALL else f"{business_line} ({BUSINESS_LINE_NAMES.get(business_line)})"))
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


def breakdown(df: pd.DataFrame, col: str, top: int = 10) -> pd.DataFrame:
    """区分 × 段階の集計。上位 top 件以外は "Other" にまとめる。"""
    if df.empty:
        return pd.DataFrame(columns=[col, "portfolio_stage", "value"])
    if is_amount:
        g = df.groupby([col, "portfolio_stage"])["amount"].sum()
    else:
        g = df.groupby([col, "portfolio_stage"])["project_number"].nunique()
    g = g.rename("value").reset_index()
    order = g.groupby(col)["value"].sum().sort_values(ascending=False)
    keep = list(order.index[:top])
    if len(order) > top:
        rest = df[~df[col].isin(keep)]
        if is_amount:
            o = rest.groupby("portfolio_stage")["amount"].sum()
        else:
            o = rest.groupby("portfolio_stage")["project_number"].nunique()
        other = o.rename("value").reset_index().assign(**{col: f"Other ({len(order) - top})"})
        g = pd.concat([g[g[col].isin(keep)], other], ignore_index=True)
    return g


def stacked_bar(g: pd.DataFrame, col: str, title: str):
    totals = g.groupby(col)["value"].sum().sort_values(ascending=False)
    # "Other (n)" は金額に関係なく最下段に置く
    order = [c for c in totals.index if not str(c).startswith("Other (")] + \
            [c for c in totals.index if str(c).startswith("Other (")]
    fig = px.bar(g, x="value", y=col, color="portfolio_stage", orientation="h", title=title,
                 category_orders={col: order, "portfolio_stage": STAGES},
                 color_discrete_map=STAGE_COLORS)
    fig.update_traces(hovertemplate="%{y}<br>%{fullData.name}: %{x:,.0f}<extra></extra>")
    # 棒の太さを区分の数によらず一定にする（区分が少ないときに棒が極端に太くならないように）
    fig = style_fig(fig, 30 * len(order) + 150)
    fig.update_layout(legend=dict(y=-60 / (30 * len(order) + 40)))
    fig.update_xaxes(title_text=value_label, tickformat=",")
    return fig


# ---------------------------------------------------------------- header
st.title("IFC Portfolio Explorer")
st.markdown("Who at IFC commits how much, **through which instruments**, **where** and **in which sectors** — "
            "and what is still in the pipeline.")

tab_active, tab_overview, tab_drill, tab_country, tab_list, tab_notes = st.tabs(
    ["Active portfolio", "Overview", "Drill down", "Country profile", "Project list", "Notes on the data"])

empty = p.empty

# ---------------------------------------------------------------- active portfolio
today = pd.Timestamp.today()
CURRENT_FY = today.year + (1 if today.month >= 7 else 0)   # IFCの会計年度は7月始まり

with tab_active:
    # 実行中の案件は、承認年度・段階の絞り込みによらず全件を対象にする
    pa, fa = apply_filters(None, ["Active portfolio"])
    if pa.empty:
        st.info("No active projects match the current filters.")
    else:
        pa = pa.assign(client=pa["company_name"].fillna("(client not specified)"))
        total = pa["amount"].sum()
        undisb = pa[pa["status"] == "Pending Disbursement"]
        st.markdown(f"{scope_text()} has **{md_usd(total)}** in its active portfolio today: "
                    f"**{n_projects(len(pa))}** with **{pa['client'].nunique():,} clients** in "
                    f"**{pa['country'].nunique():,} countries or regions**. "
                    f"**{md_usd(undisb['amount'].sum())}** of it is signed but not yet disbursed.")
        st.caption("All projects that are signed and still ongoing, whatever their approval year — the fiscal-year "
                   "and stage filters do not apply on this tab. Amounts are Board-approved commitments, not "
                   "outstanding balances: the source data has no disbursement, repayment or credit-quality figures.")
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Active portfolio", fmt_usd(total))
        k2.metric("Projects", f"{len(pa):,}")
        k3.metric("Clients", f"{pa['client'].nunique():,}")
        k4.metric("Awaiting disbursement", fmt_usd(undisb["amount"].sum()),
                  help=f"{n_projects(len(undisb))} signed but not yet disbursed")

        # 集中: 相手先と国
        st.subheader("Concentration")
        c1, c2 = st.columns(2)
        for col_ui, key, label in ((c1, "client", "client"), (c2, "country", "country or region")):
            g = pa.groupby(key)["amount"].sum().sort_values(ascending=False)
            top = g.head(10)
            share = top.sum() / total if total else 0
            df_top = top.reset_index().rename(columns={"amount": "value"})
            df_top["share"] = df_top["value"] / total * 100 if total else 0
            fig = px.bar(df_top, x="value", y=key, orientation="h", custom_data=["share"],
                         title=f"Top {len(top)} by {label} — {share:.0%} of the active portfolio",
                         category_orders={key: list(top.index)},
                         color_discrete_sequence=[STAGE_COLORS["Active portfolio"]])
            fig.update_traces(hovertemplate="%{y}<br>US$%{x:,.0f}m · %{customdata[0]:.1f}% of active<extra></extra>")
            fig = style_fig(fig, 30 * len(top) + 130)
            fig.update_xaxes(title_text="US$ millions", tickformat=",")
            col_ui.plotly_chart(fig, width="stretch", theme="streamlit")

        # リスクの性質
        st.subheader("Risk profile")
        eq_amt = fa[fa["instrument"] == "Equity"]["amount"].sum()
        env = pa["environmental_category"].fillna("")
        high_es = pa[env.str.startswith("A") | env.str.startswith("FI-1")]
        poor = pa[pa["lending_type"].eq("IDA").fillna(False) | pa["income_level"].eq("Low income").fillna(False)]
        old_ = pa[(CURRENT_FY - pa["board_fy"]) >= 10]
        prof = pd.DataFrame([
            ("Equity (no contractual repayment)", eq_amt, fa[fa["instrument"] == "Equity"]["project_number"].nunique()),
            ("High E&S category (A or FI-1)", high_es["amount"].sum(), len(high_es)),
            ("IDA or low-income countries", poor["amount"].sum(), len(poor)),
            ("Approved 10+ fiscal years ago", old_["amount"].sum(), len(old_)),
            ("Signed, awaiting disbursement", undisb["amount"].sum(), len(undisb)),
        ], columns=["Characteristic", "Amount", "Projects"])
        prof["Share"] = prof["Amount"] / total * 100 if total else 0
        left, right = st.columns([3, 2])
        fig = px.bar(prof, x="Share", y="Characteristic", orientation="h", custom_data=["Amount", "Projects"],
                     title="Share of the active portfolio with each characteristic",
                     category_orders={"Characteristic": list(prof["Characteristic"])},
                     color_discrete_sequence=[STAGE_COLORS["Active portfolio"]])
        fig.update_traces(hovertemplate="%{y}<br>%{x:.1f}% · US$%{customdata[0]:,.0f}m · "
                                        "%{customdata[1]} project(s)<extra></extra>")
        fig = style_fig(fig, 300)
        fig.update_xaxes(title_text="% of active portfolio amount", ticksuffix="%", range=[0, 100])
        left.plotly_chart(fig, width="stretch", theme="streamlit")

        # 経過年数
        age = (CURRENT_FY - pa["board_fy"]).clip(lower=0)
        buckets = pd.cut(age, bins=[-1, 2, 5, 9, 200], labels=["0–2 years", "3–5 years", "6–9 years", "10+ years"])
        ga = pa.assign(age=buckets).groupby("age", observed=False)["amount"].sum().reset_index()
        fig = px.bar(ga, x="age", y="amount", title=f"Years since approval (FY{CURRENT_FY})",
                     color_discrete_sequence=[STAGE_COLORS["Active portfolio"]])
        fig.update_traces(hovertemplate="%{x}<br>US$%{y:,.0f}m<extra></extra>")
        fig = style_fig(fig, 300)
        fig.update_yaxes(title_text="US$ millions", tickformat=",")
        right.plotly_chart(fig, width="stretch", theme="streamlit")

        watchlist.render(pa, fa, CURRENT_FY, fmt_usd)  # watchlist: 削除するときはこの行を消す

# ---------------------------------------------------------------- overview
with tab_overview:
    if empty:
        st.info("No projects match the current filters. Widen the fiscal years or change a filter on the left.")
    else:
        t = stage_totals(facts)
        n_proj = p["project_number"].nunique()
        n_unpriced = int((p["portfolio_stage"].eq("Pipeline") & p["amount"].isna()).sum())
        phrases = {
            "Active portfolio": "has **{amt}** in its active portfolio (**{n}**)",
            "Completed": "has completed **{amt}** (**{n}**)",
            "Pipeline": "has **{amt}** in the pipeline (**{n}**)",
            "On hold": "has **{amt}** on hold (**{n}**)",
        }
        parts = [phrases[sg].format(amt=md_usd(t[sg][0]), n=n_projects(t[sg][1]))
                 for sg in STAGES if sg in stages]
        st.markdown(f"Approved in FY{fy[0]}–FY{fy[1]}: {scope_text()} " + ", ".join(parts) + ".")
        st.caption(DEFINITIONS)

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Active portfolio", fmt_usd(t["Active portfolio"][0]) if "Active portfolio" in stages
                  else "not selected", help="Signed and ongoing: active, or signed and awaiting disbursement")
        k2.metric("Pipeline", fmt_usd(t["Pipeline"][0]) if "Pipeline" in stages else "not selected",
                  help="Approved but not yet signed, or awaiting Board approval")
        k3.metric("Completed", fmt_usd(t["Completed"][0]) if "Completed" in stages else "not selected",
                  help="Finished projects: the track record. Add it with the Stage filter.")
        k4.metric("Projects", f"{n_proj:,}")
        notes = []
        n_program = int(p["amount_basis"].str.startswith("Program-level").sum())
        if n_program and not include_program:
            notes.append(f"Amounts for {n_program} program-level record(s) are excluded from totals.")
        if n_unpriced:
            notes.append(f"{n_unpriced} pipeline project(s) have no amount disclosed yet.")
        if notes:
            st.caption(" ".join(notes) + " See *Notes on the data*.")

        st.subheader("Who, how, where and what")
        st.caption("Dark blue: active portfolio · grey: completed · light blue: pipeline. Choose one business line "
                   "or region on the left to see departments or countries.")
        c1, c2 = st.columns(2)
        c1.plotly_chart(stacked_bar(breakdown(facts, who_col), who_col, f"Who commits — by {who_label}"),
                        width="stretch", theme="streamlit")
        c2.plotly_chart(stacked_bar(breakdown(facts, how_col), how_col, "How — by instrument"),
                        width="stretch", theme="streamlit")
        c3, c4 = st.columns(2)
        c3.plotly_chart(stacked_bar(breakdown(facts, where_col), where_col, f"Where — by {where_label}"),
                        width="stretch", theme="streamlit")
        c4.plotly_chart(stacked_bar(breakdown(facts, what_col), what_col, "What — by industry"),
                        width="stretch", theme="streamlit")

        # 資金の流れ（サンキー図）: Who → How → Where → What
        st.subheader("How the money flows")
        st.caption(f"Amounts (US$ millions) from each {who_label}, through each instrument, to each {where_label} "
                   "and industry. Hover over a band to see its amount. Smaller items are grouped as \"Other\".")
        flow = facts.dropna(subset=["amount"])
        flow = flow[flow["amount"] > 0]
        if flow.empty:
            st.info("No amounts to show for the current filters.")
        else:
            levels = [who_col, how_col, where_col, what_col]
            flow = flow.copy()
            flow["_colour_key"] = flow["business_line"]   # 帯の色は事業部門で決める
            for col in levels:
                top = flow.groupby(col)["amount"].sum().sort_values(ascending=False).index[:8]
                flow[col] = flow[col].where(flow[col].isin(top), "Other")
            labels, index = [], {}
            for li, col in enumerate(levels):
                for v in flow.groupby(col)["amount"].sum().sort_values(ascending=False).index:
                    index[(li, v)] = len(labels)
                    labels.append(v)
            src, dst, val, colr = [], [], [], []
            for li in range(len(levels) - 1):
                g = flow.groupby([levels[li], levels[li + 1], "_colour_key"])["amount"].sum().reset_index()
                for r in g.itertuples(index=False):
                    src.append(index[(li, r[0])]); dst.append(index[(li + 1, r[1])]); val.append(r[3])
                    hexc = BUSINESS_LINE_COLORS.get(r[2], "#a8a7a2").lstrip("#")
                    colr.append(f"rgba({int(hexc[0:2], 16)},{int(hexc[2:4], 16)},{int(hexc[4:6], 16)},0.35)")
            fig = go.Figure(go.Sankey(
                arrangement="snap",
                node=dict(label=labels, pad=12, thickness=14, color="#8a8984",
                          hovertemplate="%{label}<br>US$%{value:,.0f}m<extra></extra>"),
                link=dict(source=src, target=dst, value=val, color=colr,
                          hovertemplate="%{source.label} → %{target.label}<br>US$%{value:,.0f}m<extra></extra>")))
            for x, txt in zip([0, 0.33, 0.67, 1.0], ["WHO", "HOW", "WHERE", "WHAT"]):
                fig.add_annotation(x=x, y=1.06, xref="paper", yref="paper", text=f"<b>{txt}</b>",
                                   showarrow=False, xanchor="center" if 0 < x < 1 else ("left" if x == 0 else "right"))
            fig.update_layout(height=520, margin=dict(l=10, r=10, t=40, b=10), font=dict(size=12))
            st.plotly_chart(fig, width="stretch", theme="streamlit")

        st.subheader("When")
        if is_amount:
            by_fy = facts.groupby(["board_fy", "portfolio_stage"])["amount"].sum().rename("value").reset_index()
        else:
            by_fy = facts.groupby(["board_fy", "portfolio_stage"])["project_number"].nunique().rename("value").reset_index()
        fig = px.bar(by_fy, x="board_fy", y="value", color="portfolio_stage",
                     title=f"{'Amount' if is_amount else 'Projects'} by fiscal year",
                     category_orders={"portfolio_stage": STAGES}, color_discrete_map=STAGE_COLORS)
        fig.update_traces(hovertemplate="FY%{x}<br>%{fullData.name}: %{y:,.0f}<extra></extra>")
        fig = style_fig(fig, 380)
        fig.update_xaxes(tickprefix="FY", dtick=1 if fy[1] - fy[0] <= 12 else 2, title_text="IFC fiscal year (July–June)")
        fig.update_yaxes(title_text=value_label, tickformat=",")
        st.plotly_chart(fig, width="stretch", theme="streamlit")

# ---------------------------------------------------------------- drill down
DIMENSIONS = {
    "Business line": "business_line", "Department": "dept_name", "IFC region": "ifc_region",
    "Country": "country", "Industry": "industry", "Instrument": "instrument",
}
PATHS = {
    "Who → Where → What → How": ["Business line", "Department", "IFC region", "Country", "Industry", "Instrument"],
    "Where → Who → What → How": ["IFC region", "Country", "Business line", "Department", "Industry", "Instrument"],
    "What → Who → Where → How": ["Industry", "Business line", "Department", "IFC region", "Country", "Instrument"],
    "How → Who → Where → What": ["Instrument", "Business line", "Department", "IFC region", "Country", "Industry"],
}

with tab_drill:
    if empty:
        st.info("No projects match the current filters.")
    else:
        st.markdown("Start from the whole portfolio within your filters and break it down step by step. "
                    "**Click a box to zoom into it; click the top bar to go back up.**")
        path_name = st.radio("Break down by", list(PATHS), horizontal=True)
        d = facts.dropna(subset=["amount"])
        d = d[d["amount"] > 0]
        # 絞り込みで値が1つに決まっている階層は飛ばす（その階層は分かれないため）
        fixed = [f"{lvl}: {facts[DIMENSIONS[lvl]].iloc[0]}" for lvl in PATHS[path_name]
                 if facts[DIMENSIONS[lvl]].nunique() == 1]
        path = [lvl for lvl in PATHS[path_name] if facts[DIMENSIONS[lvl]].nunique() > 1] or PATHS[path_name][:1]
        if fixed:
            st.caption("Fixed by your filters, so not broken down: " + " · ".join(fixed))
        if d.empty:
            st.info("No amounts to show for the current filters.")
        else:
            leaf = d.groupby([DIMENSIONS[x] for x in path])["amount"].sum().reset_index()
            first = DIMENSIONS[path[0]]
            if first == "business_line":
                cmap = BUSINESS_LINE_COLORS
            elif first == "instrument":
                cmap = INSTRUMENT_COLORS
            else:
                palette = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
                ranked = leaf.groupby(first)["amount"].sum().sort_values(ascending=False).index
                cmap = {v: (palette[i] if i < len(palette) else "#a8a7a2") for i, v in enumerate(ranked)}
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
            fig.update_layout(height=560, margin=dict(l=4, r=4, t=8, b=4))
            st.plotly_chart(fig, width="stretch", theme="streamlit")
            st.caption("Levels from left to right: " + " → ".join(path) + ". Box height shows the share of the "
                       "level to its left. Amounts in US$ millions for all selected stages combined "
                       "— use the Stage filter to look at one stage.")

        st.subheader("Projects behind the numbers")
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

# ---------------------------------------------------------------- country profile
with tab_country:
    country_list = (p.groupby("country")["amount"].sum().sort_values(ascending=False).index.tolist()
                    if not empty else [])
    if not country_list:
        st.info("No projects match the current filters.")
    else:
        country = st.selectbox("Country / region", country_list,
                               index=country_list.index(country_choice) if country_choice in country_list else 0)
        cp = p[p["country"] == country]
        cf = facts[facts["country"] == country]
        iso3 = cp["iso3"].dropna().iloc[0] if cp["iso3"].notna().any() else None
        info = cp.iloc[0]
        st.caption(f"World Bank region: {info.get('wb_region') or '—'} · "
                   f"Income group (current): {info.get('income_level') or '—'} · "
                   f"Lending category: {info.get('lending_type') or '—'} · "
                   f"IFC region: {info.get('ifc_region')} ({info.get('ifc_region_basis') or '—'})")
        tc_ = stage_totals(cf)
        k1, k2, k3 = st.columns(3)
        k1.metric("Active portfolio", fmt_usd(tc_["Active portfolio"][0]) if "Active portfolio" in stages
                  else "not selected")
        k2.metric("Pipeline", fmt_usd(tc_["Pipeline"][0]) if "Pipeline" in stages else "not selected")
        k3.metric("Projects", f"{cp['project_number'].nunique():,}")

        left, right = st.columns([1, 1])
        with left:
            if is_amount:
                g = cf.groupby(["board_fy", "instrument"])["amount"].sum().rename("value").reset_index()
            else:
                g = cf.groupby(["board_fy", "instrument"])["project_number"].nunique().rename("value").reset_index()
            fig = px.bar(g, x="board_fy", y="value", color="instrument", title="By fiscal year and instrument",
                         category_orders={"instrument": INSTRUMENT_ORDER}, color_discrete_map=INSTRUMENT_COLORS)
            fig.update_traces(hovertemplate="FY%{x}<br>%{fullData.name}: %{y:,.0f}<extra></extra>")
            fig = style_fig(fig, 360)
            fig.update_xaxes(tickprefix="FY", title_text="IFC fiscal year (July–June)")
            fig.update_yaxes(title_text=value_label, tickformat=",")
            st.plotly_chart(fig, width="stretch", theme="streamlit")

            st.markdown("**Largest projects**")
            show = (cp.sort_values("amount", ascending=False)
                    [["project_name", "company_name", "board_fy", "dept_name", "portfolio_stage", "amount",
                      "project_url"]]
                    .rename(columns={"project_name": "Project", "company_name": "Client", "board_fy": "FY",
                                     "dept_name": "Department", "portfolio_stage": "Stage",
                                     "amount": "Amount (US$m)", "project_url": "Disclosure"}))
            st.dataframe(show.head(20), hide_index=True, width="stretch",
                         column_config={"Disclosure": st.column_config.LinkColumn(display_text="Open"),
                                        "Amount (US$m)": st.column_config.NumberColumn(format="%,.1f")})

        with right:
            st.markdown("**What IFC reports say**")
            if not ev or iso3 is None:
                st.info("No IFC reports are available for this country / region yet.")
            else:
                e = ev["evidence"]
                doc_ids = set(ev["doc_countries"].loc[ev["doc_countries"]["iso3"] == iso3, "doc_id"])
                mentioned = e["countries_mentioned"].apply(lambda x: iso3 in x)
                ce = e[e["doc_id"].isin(doc_ids) | mentioned].copy()
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
                    st.caption("Reports: " + "; ".join(f"{t} ({p_})" for t, p_ in zip(docs["title"], docs["pub_date"])))
                    themes = ce.explode("themes").dropna(subset=["themes"])
                    tcount = themes.groupby("themes").size().sort_values(ascending=True).reset_index(name="Paragraphs")
                    fig = px.bar(tcount, x="Paragraphs", y="themes", orientation="h", title="Paragraphs by theme",
                                 color_discrete_sequence=[STAGE_COLORS["Active portfolio"]])
                    fig.update_traces(hovertemplate="%{y}: %{x} paragraph(s)<extra></extra>")
                    st.plotly_chart(style_fig(fig, max(260, 26 * len(tcount) + 80)), width="stretch",
                                    theme="streamlit")
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

# ---------------------------------------------------------------- project list
with tab_list:
    cols = {"project_number": "Project no.", "project_name": "Project", "company_name": "Client",
            "business_line": "Business line", "dept_name": "Department", "country": "Country / region",
            "ifc_region": "IFC region", "industry": "Industry", "board_fy": "FY", "board_fy_basis": "FY basis",
            "portfolio_stage": "Stage", "status": "Status", "amount": "Amount (US$m)",
            "total_approved_usd_m": "Approved as published (US$m)", "amount_basis": "Amount basis",
            "environmental_category": "E&S category", "income_level": "Income group (current)",
            "ifc_region_basis": "Basis for IFC region", "project_url": "Disclosure"}
    table = (p[list(cols)].sort_values(["board_fy", "project_number"], ascending=[False, True]).rename(columns=cols))
    st.caption(f"{len(table):,} project(s). *Amount* follows the filters (instrument, program-level setting); "
               "*Approved as published* is the total in the source data.")
    st.dataframe(table, hide_index=True, width="stretch",
                 column_config={"Disclosure": st.column_config.LinkColumn(display_text="Open"),
                                "Amount (US$m)": st.column_config.NumberColumn(format="%,.1f"),
                                "Approved as published (US$m)": st.column_config.NumberColumn(format="%,.1f")})
    st.download_button("Download CSV", table.to_csv(index=False).encode("utf-8-sig"),
                       file_name="ifc_projects_filtered.csv", mime="text/csv")

# ---------------------------------------------------------------- notes
with tab_notes:
    st.markdown("""
**Stages: active portfolio, completed and pipeline**
- *Active portfolio*: signed projects that are still ongoing — active, or signed and awaiting disbursement.
  This is IFC's current exposure and is shown first because it needs the closest monitoring.
- *Completed*: finished projects. They form the track record and are excluded by default; add them with the
  Stage filter.
- *Pipeline*: projects approved by the Board but not yet signed, or still awaiting Board approval. They are not
  yet legal commitments, their amounts may change, and some do not yet show an amount.
- *On hold*: projects IFC has put on hold. Excluded by default.

**Who: business lines and departments**
- Each project's business line is derived from the name of the IFC department recorded for it, using keywords
  (e.g. "FIG" or "Financial Markets" → FIG; "MAS", "Manufacturing" or "Agribusiness" → MAS; "INF" or
  "Infrastructure" → INF). About 1.4% of projects cannot be classified and appear as *Other*.
- The department recorded is the unit responsible at some point after approval, not necessarily the unit that
  originated the project. Department names change with reorganisations; business lines are treated as stable
  across them, so a project of the former *Global Industry, Financial Markets* department counts as FIG.
- Regions come from the project's country, not from the department name.

**How: instruments**
- Amounts are split by instrument using the loan, equity, guarantee and risk-management amounts in the source
  data, so a project combining a loan and equity contributes to both. The *Product* label in the source data is
  not used, because it often disagrees with these amounts.

**What the amounts represent**
- IFC's own investment as approved by the Board, in US$ millions. Disbursements, outstanding balances,
  repayments and mobilised third-party funds are not included.
- Fiscal years are IFC fiscal years (July–June). Projects not yet approved are placed in the fiscal year of
  their projected Board date (shown as *Projected* in the *FY basis* column of the project list).

**Program-level amounts**
- For some programs, such as the Global Trade Finance Program (GTFP), the program-wide approval ceiling is
  recorded against each participating institution. Summing these records would overstate IFC's commitments
  substantially, so their amounts are excluded from totals by default (the records are still counted).
- A record is treated as program-level if its project name starts with "GTFP", or if the same amount
  (US$100m or more) was approved on the same date for four or more projects. A few records have been
  reviewed individually against IFC's disclosure pages and reclassified (marked "manual").

**Regions and income groups**
- IFC regions: countries in Middle East and Central Asia follow the country list on IFC's regional web page
  ("Official"). A few countries are confirmed from the IFC regional department responsible for their projects
  ("Confirmed (IFC department)"). Other countries are assigned provisionally from the World Bank regional
  classification.
- World Bank regions and income groups are the current classifications and are applied to all years.

**Text from IFC reports**
- Paragraphs from IFC reports are tagged with countries, industries and themes using keyword dictionaries.
  A tag indicates that a paragraph mentions a topic, not that the topic is its main subject.
- Tables of contents, footnotes, bibliographies, table rows, repeated paragraphs and standard boilerplate
  are excluded from the text.

**Sources**
- IFC Investment Services Projects, World Bank Group Finances One (dataset DS00499)
- World Bank country classification API
- IFC publications listed in each country profile
""")
