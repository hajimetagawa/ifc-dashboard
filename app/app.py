"""
IFC Portfolio Explorer (Streamlit)

起動（プロジェクトのフォルダで実行）:
    uv run streamlit run app/app.py

データ: data/curated/ifc.duckdb（src/ 以下のスクリプトで作成）
画面の表示はすべて英語（閲覧者が海外のため）。
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("IFC_DB", ROOT / "data" / "curated" / "ifc.duckdb"))

DEFAULT_REGION = "Middle East and Central Asia"
DEFAULT_INDUSTRY = "Financial Markets"
ALL = "All"
ACTIVE = ["Active", "Pending Disbursement"]
STATUS_GROUPS = {
    "All": None,
    "Approved (active and completed)": ["Active", "Pending Disbursement", "Completed"],
    "Active only": ACTIVE,
    "Pipeline (pre-approval / pre-signing / on hold)": ["Pending Approval", "Pending Signing", "Hold"],
}
UNKNOWN_PRODUCT = "Not specified"
# 商品区分の色（エンティティに固定。フィルタで色が変わらないようにする）
PRODUCT_COLORS = {
    "Loan": "#2a78d6", "Guarantee": "#eb6834", "Equity": "#1baf7a",
    "Risk Management": "#eda100", UNKNOWN_PRODUCT: "#a8a7a2",
}
PRODUCT_ORDER = list(PRODUCT_COLORS)
BAR_COLOR = "#2a78d6"

st.set_page_config(page_title="IFC Portfolio Explorer", layout="wide")


# ---------------------------------------------------------------- data
@st.cache_data(show_spinner=False)
def load_projects(db: str) -> pd.DataFrame:
    with duckdb.connect(db, read_only=True) as con:
        df = con.sql("SELECT * FROM projects_enriched").df()
    df["product_line"] = df["product_line"].fillna(UNKNOWN_PRODUCT)
    df["ifc_region"] = df["ifc_region"].fillna("Unassigned")
    df["industry"] = df["industry"].fillna("Not specified")
    return df


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
    if pd.isna(m):
        return "—"
    return f"US${m / 1000:,.1f}bn" if m >= 1000 else f"US${m:,.0f}m"


def style_fig(fig, height: int = 380):
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", yanchor="top", y=-0.12, x=0, title=None),
        xaxis_title=None, yaxis_title=None, bargap=0.25,
    )
    fig.update_traces(marker_line_width=0)
    return fig


if not DB_PATH.exists():
    st.error(f"Database not found: {DB_PATH}. Run the scripts in src/ first.")
    st.stop()

projects = load_projects(str(DB_PATH))
ev = load_evidence_tables(str(DB_PATH))

# ---------------------------------------------------------------- filters (sidebar)
st.sidebar.header("Filters")
regions = sorted(projects["ifc_region"].unique())
region = st.sidebar.selectbox("IFC region", [ALL] + regions,
                              index=(regions.index(DEFAULT_REGION) + 1) if DEFAULT_REGION in regions else 0)
industries = sorted(projects["industry"].unique())
industry = st.sidebar.selectbox("Industry", [ALL] + industries,
                                index=(industries.index(DEFAULT_INDUSTRY) + 1) if DEFAULT_INDUSTRY in industries else 0)
fy_min, fy_max = int(projects["approval_fy"].min()), int(projects["approval_fy"].max())
fy = st.sidebar.slider("Board approval fiscal year", fy_min, fy_max, (max(fy_min, 2010), fy_max))
status_label = st.sidebar.selectbox("Project status", list(STATUS_GROUPS), index=1)
metric = st.sidebar.radio("Measure", ["Approved amount", "Number of projects"], horizontal=False)
include_program = st.sidebar.checkbox(
    "Include program-level amounts", value=False,
    help="Some programs (e.g. GTFP) record the program-wide approval ceiling against each participating "
         "institution. Summing these overstates commitments substantially, so they are excluded by default.")

f = projects[projects["approval_fy"].between(*fy)]
if region != ALL:
    f = f[f["ifc_region"] == region]
if industry != ALL:
    f = f[f["industry"] == industry]
if STATUS_GROUPS[status_label]:
    f = f[f["status"].isin(STATUS_GROUPS[status_label])]
f = f.assign(amount=f["total_approved_usd_m"] if include_program else f["approved_usd_m_for_sum"])
n_program = int(f["amount_basis"].str.startswith("Program-level").sum())

is_amount = metric == "Approved amount"
value_col, value_label = ("amount", "Approved (US$m)") if is_amount else ("project_number", "Projects")
agg = "sum" if is_amount else "count"
metric_short = "approved amount" if is_amount else "number of projects"

st.title("IFC Portfolio Explorer")
st.caption(f"{region} · {industry} · FY{fy[0]}–FY{fy[1]} · {status_label}"
           " | Sources: IFC Investment Services Projects (World Bank Group Finances One); IFC publications")

tab_overview, tab_country, tab_list, tab_notes = st.tabs(
    ["Overview", "Country profile", "Project list", "Notes on the data"])

# ---------------------------------------------------------------- overview
with tab_overview:
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Projects", f"{len(f):,}")
    k2.metric("Approved amount", fmt_usd(f["amount"].sum()))
    k3.metric("Active projects", f"{f['status'].isin(ACTIVE).sum():,}")
    k4.metric("Countries / regions", f"{f['country'].nunique():,}")
    if n_program and not include_program:
        st.caption(f"Amounts for {n_program} program-level record(s) are excluded from the total "
                   "(they are still counted as projects). See *Notes on the data*.")

    if f.empty:
        st.info("No projects match the current filters.")
    else:
        c1, c2 = st.columns(2)
        by_country = f.groupby("country")[value_col].agg(agg).sort_values(ascending=True).tail(15).reset_index()
        fig = px.bar(by_country, x=value_col, y="country", orientation="h",
                     title=f"Top 15 countries / regions by {metric_short}", color_discrete_sequence=[BAR_COLOR])
        fig.update_traces(hovertemplate="%{y}<br>" + value_label + ": %{x:,.0f}<extra></extra>")
        c1.plotly_chart(style_fig(fig, 460), width="stretch", theme="streamlit")

        by_fy = f.groupby("approval_fy")[value_col].agg(agg).reset_index()
        fig = px.bar(by_fy, x="approval_fy", y=value_col, title=f"{metric.capitalize()} by fiscal year",
                     color_discrete_sequence=[BAR_COLOR])
        fig.update_traces(hovertemplate="FY%{x}<br>" + value_label + ": %{y:,.0f}<extra></extra>")
        fig.update_xaxes(dtick=2)
        c2.plotly_chart(style_fig(fig, 460), width="stretch", theme="streamlit")

        top = by_country["country"].tolist()[::-1][:10]
        mix = f[f["country"].isin(top)].groupby(["country", "product_line"])[value_col].agg(agg).reset_index()
        mix["share"] = mix[value_col] / mix.groupby("country")[value_col].transform("sum") * 100
        fig = px.bar(mix, x="share", y="country", color="product_line", orientation="h",
                     title=f"Product mix, top 10 countries / regions (share of {metric_short})",
                     category_orders={"product_line": PRODUCT_ORDER, "country": top},
                     color_discrete_map=PRODUCT_COLORS, custom_data=[value_col])
        fig.update_traces(marker_line_width=1.5, marker_line_color="rgba(255,255,255,0.9)",
                          hovertemplate="%{y}<br>%{fullData.name}: %{x:.0f}% (%{customdata[0]:,.0f})<extra></extra>")
        fig.update_xaxes(ticksuffix="%", range=[0, 100])
        st.plotly_chart(style_fig(fig, 460), width="stretch", theme="streamlit")

        if ev:
            docs_by_iso = ev["doc_countries"].groupby("iso3")["doc_id"].nunique()
            cov = (f.groupby(["country", "iso3"], dropna=False)
                   .agg(Projects=("project_number", "count"), **{"Approved (US$m)": ("amount", "sum")},
                        Active=("status", lambda s: s.isin(ACTIVE).sum()))
                   .reset_index())
            cov["Reports"] = cov["iso3"].map(docs_by_iso).fillna(0).astype(int)
            st.subheader("Summary by country / region, with number of IFC reports available")
            st.dataframe(cov.drop(columns="iso3").rename(columns={"country": "Country / region"})
                         .sort_values("Approved (US$m)", ascending=False),
                         hide_index=True, width="stretch",
                         column_config={"Approved (US$m)": st.column_config.NumberColumn(format="%,.0f")})

# ---------------------------------------------------------------- country profile
with tab_country:
    countries = f.groupby("country")["amount"].sum().sort_values(ascending=False).index.tolist()
    if not countries:
        st.info("No projects match the current filters.")
    else:
        country = st.selectbox("Country / region", countries)
        cf = f[f["country"] == country]
        iso3 = cf["iso3"].dropna().iloc[0] if cf["iso3"].notna().any() else None
        info = cf.iloc[0]
        st.caption(f"World Bank region: {info.get('wb_region') or '—'} · "
                   f"Income group (current): {info.get('income_level') or '—'} · "
                   f"Lending category: {info.get('lending_type') or '—'} · "
                   f"Basis for IFC region: {info.get('ifc_region_basis') or '—'}")

        k1, k2, k3 = st.columns(3)
        k1.metric("Projects", f"{len(cf):,}")
        k2.metric("Approved amount", fmt_usd(cf["amount"].sum()))
        k3.metric("Active projects", f"{cf['status'].isin(ACTIVE).sum():,}")

        left, right = st.columns([1, 1])
        with left:
            by_fy = cf.groupby(["approval_fy", "product_line"])[value_col].agg(agg).reset_index()
            fig = px.bar(by_fy, x="approval_fy", y=value_col, color="product_line",
                         title=f"{metric.capitalize()} by fiscal year and product",
                         category_orders={"product_line": PRODUCT_ORDER}, color_discrete_map=PRODUCT_COLORS)
            fig.update_traces(marker_line_width=1.5, marker_line_color="rgba(255,255,255,0.9)",
                              hovertemplate="FY%{x}<br>%{fullData.name}: %{y:,.0f}<extra></extra>")
            st.plotly_chart(style_fig(fig, 360), width="stretch", theme="streamlit")

            st.markdown("**Largest projects** (by approved amount)")
            show = (cf.sort_values("amount", ascending=False)
                    [["project_name", "company_name", "approval_fy", "product_line", "amount", "status", "project_url"]]
                    .rename(columns={"project_name": "Project", "company_name": "Client", "approval_fy": "FY",
                                     "product_line": "Product", "amount": "Approved (US$m)", "status": "Status",
                                     "project_url": "Disclosure"}))
            st.dataframe(show.head(20), hide_index=True, width="stretch",
                         column_config={"Disclosure": st.column_config.LinkColumn(display_text="Open"),
                                        "Approved (US$m)": st.column_config.NumberColumn(format="%,.1f")})

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
                if industry != ALL:
                    if st.checkbox(f"Only paragraphs related to the selected industry ({industry})", value=True):
                        ce = ce[ce["sectors"].apply(lambda x: industry in x)]
                if ce.empty:
                    st.info("No matching paragraphs (no report for this country, "
                            "or no paragraph related to the selected industry).")
                else:
                    docs = ev["documents"][ev["documents"]["doc_id"].isin(ce["doc_id"].unique())]
                    st.caption("Reports: " + "; ".join(f"{t} ({p})" for t, p in zip(docs["title"], docs["pub_date"])))
                    themes = ce.explode("themes").dropna(subset=["themes"])
                    tc = themes.groupby("themes").size().sort_values(ascending=True).reset_index(name="Paragraphs")
                    fig = px.bar(tc, x="Paragraphs", y="themes", orientation="h", title="Paragraphs by theme",
                                 color_discrete_sequence=[BAR_COLOR])
                    fig.update_traces(hovertemplate="%{y}: %{x} paragraph(s)<extra></extra>")
                    st.plotly_chart(style_fig(fig, max(260, 26 * len(tc) + 80)), width="stretch", theme="streamlit")

                    theme = st.selectbox("Read paragraphs by theme", ["All themes"] + tc["themes"].tolist()[::-1])
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
            "country": "Country / region", "approval_fy": "FY", "industry": "Industry",
            "product_line": "Product", "status": "Status", "total_approved_usd_m": "Approved (US$m)",
            "amount_basis": "Amount basis", "environmental_category": "E&S category",
            "income_level": "Income group (current)", "ifc_region_basis": "Basis for IFC region",
            "project_url": "Disclosure"}
    table = (f[list(cols)].sort_values(["approval_fy", "project_number"], ascending=[False, True])
             .rename(columns=cols))
    st.caption(f"{len(table):,} project(s)")
    st.dataframe(table, hide_index=True, width="stretch",
                 column_config={"Disclosure": st.column_config.LinkColumn(display_text="Open"),
                                "Approved (US$m)": st.column_config.NumberColumn(format="%,.1f")})
    st.download_button("Download CSV", table.to_csv(index=False).encode("utf-8-sig"),
                       file_name="ifc_projects_filtered.csv", mime="text/csv")

# ---------------------------------------------------------------- notes
with tab_notes:
    st.markdown("""
**What the amounts represent**
- Amounts are IFC investment as approved by the Board (US$ millions). They do not reflect disbursements,
  outstanding balances or repayments.
- The dataset also contains pipeline projects that are pending approval or signing; use the status filter
  to include or exclude them.

**Program-level amounts**
- For some programs, such as the Global Trade Finance Program (GTFP), the program-wide approval ceiling is
  recorded against each participating institution. Summing these records would overstate IFC's commitments
  substantially, so their amounts are excluded from totals by default (the records are still counted).
- A record is treated as program-level if its project name starts with "GTFP", or if the same amount
  (US$100m or more) was approved on the same date for four or more projects. The basis is shown in the
  *Amount basis* column of the project list.

**Regions and income groups**
- IFC regions: countries in Middle East and Central Asia follow the country list on IFC's regional web page.
  Other countries are assigned provisionally from the World Bank regional classification. The basis is
  shown in the *Basis for IFC region* column.
- World Bank regions and income groups are the current classifications and are applied to all years.

**Text from IFC reports**
- Paragraphs from IFC reports are tagged with countries, industries and themes using keyword dictionaries.
  A tag indicates that a paragraph mentions a topic, not that the topic is its main subject.

**Sources**
- IFC Investment Services Projects, World Bank Group Finances One (dataset DS00499)
- World Bank country classification API
- IFC publications listed in each country profile
""")
