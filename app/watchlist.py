"""
注視リスト（Watchlist）— 試行的なモジュール

実行中の案件（Active portfolio）に、透明な規則で「リスクの兆候」を付け、該当数の多い順に並べる。
格付けや信用評価ではなく、注意を向ける先を絞るための目安。

削除するとき:
    1. このファイル（app/watchlist.py）を削除する
    2. app/app.py の「watchlist」と書かれた2か所（import 文と、render を呼ぶ行）を削除する

規則のしきい値は、下の定数で調整する。
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

LARGE_AMOUNT_USD_M = 100.0     # この金額（百万米ドル）以上は「大口」
LARGE_TOP_SHARE = 0.10         # または、表示中の実行中案件のうち金額の上位10%
LONG_RUNNING_YEARS = 10        # 承認からこの年数以上たって、まだ実行中
CLIENT_SHARE = 0.05            # 1つの相手先への実行中の承認額が、表示中のポートフォリオのこの割合以上
DEFAULT_MIN_SIGNALS = 2        # 既定で表示する、兆候の最小数

SIGNALS = {
    "Large exposure": f"US${LARGE_AMOUNT_USD_M:,.0f}m or more, or in the top {LARGE_TOP_SHARE:.0%} by amount",
    "Equity": "Includes an equity investment (no contractual repayment; exposed to valuation changes)",
    "High E&S category": "Environmental and social category A or FI-1 (significant potential impact)",
    "IDA / low-income country": "Country eligible for IDA or classified as low income (current classification)",
    "Long-running": f"Approved {LONG_RUNNING_YEARS} or more fiscal years ago and still active",
    "Client concentration": f"The client's active projects together make up {CLIENT_SHARE:.0%} or more of the "
                            "active portfolio in the current selection",
}


def flag(active: pd.DataFrame, facts: pd.DataFrame, current_fy: int) -> pd.DataFrame:
    """1案件1行の実行中案件に、兆候の列（True/False）と該当数を付けて返す。"""
    a = active.copy()
    equity = facts[(facts["instrument"] == "Equity") & (facts["approved_usd_m"] > 0)]["project_number"].unique()
    amount = a["amount"].fillna(0)
    top_cut = amount.quantile(1 - LARGE_TOP_SHARE) if len(a) >= 10 else float("inf")
    client = a["company_name"].fillna("(not specified)")
    env = a["environmental_category"].fillna("")

    a["Large exposure"] = (amount >= LARGE_AMOUNT_USD_M) | ((amount >= top_cut) & (amount > 0))
    a["Equity"] = a["project_number"].isin(equity)
    a["High E&S category"] = env.str.startswith("A") | env.str.startswith("FI-1")
    a["IDA / low-income country"] = a["lending_type"].eq("IDA").fillna(False) | \
        a["income_level"].eq("Low income").fillna(False)
    a["Long-running"] = (current_fy - a["board_fy"]).ge(LONG_RUNNING_YEARS).fillna(False)
    client_amt = amount.groupby(client).transform("sum")
    a["Client concentration"] = (client_amt >= CLIENT_SHARE * amount.sum()) & (amount.sum() > 0) & \
        client.ne("(not specified)")

    a["signals"] = a[list(SIGNALS)].sum(axis=1).astype(int)
    a["signal_list"] = a[list(SIGNALS)].apply(lambda r: ", ".join(k for k in SIGNALS if r[k]), axis=1)
    return a


def render(active: pd.DataFrame, facts: pd.DataFrame, current_fy: int, fmt_usd) -> None:
    st.subheader("Watchlist (trial)")
    st.caption("Active projects carrying one or more risk signals, ranked by the number of signals and then by "
               "amount. These are screening signals that point attention, not credit ratings or assessments.")
    if active.empty:
        st.info("No active projects in the current selection.")
        return
    a = flag(active, facts, current_fy)
    min_signals = st.slider("Show projects with at least this many signals", 1, len(SIGNALS),
                            DEFAULT_MIN_SIGNALS, key="watchlist-min")
    hit = a[a["signals"] >= min_signals]

    counts = pd.DataFrame({
        "Signal": list(SIGNALS),
        "Projects": [int(a[k].sum()) for k in SIGNALS],
        "Amount": [a.loc[a[k], "amount"].sum() for k in SIGNALS],
    })
    left, right = st.columns([1, 1])
    with left:
        fig = px.bar(counts.sort_values("Projects"), x="Projects", y="Signal", orientation="h",
                     title="Active projects by signal", color_discrete_sequence=["#e34948"],
                     custom_data=["Amount"])
        fig.update_traces(hovertemplate="%{y}<br>%{x} project(s), US$%{customdata[0]:,.0f}m<extra></extra>")
        fig.update_layout(height=300, margin=dict(l=10, r=10, t=48, b=10), xaxis_title=None, yaxis_title=None)
        st.plotly_chart(fig, width="stretch", theme="streamlit")
    with right:
        st.markdown("**Signals used**")
        for k, v in SIGNALS.items():
            st.markdown(f"- **{k}**: {v.replace('$', chr(92) + '$')}")
        # st.caption は「$」を数式の記号と解釈するため、エスケープする
        usd = lambda m: fmt_usd(m).replace("$", "\\$")
        st.caption(f"{len(hit):,} of {len(a):,} active projects ({usd(hit['amount'].sum())} of "
                   f"{usd(a['amount'].sum())}) carry {min_signals} or more signals.")

    table = (hit.sort_values(["signals", "amount"], ascending=[False, False])
             [["signals", "signal_list", "project_name", "company_name", "country", "dept_name", "board_fy",
               "status", "amount", "project_url"]]
             .rename(columns={"signals": "Signals", "signal_list": "Which signals", "project_name": "Project",
                              "company_name": "Client", "country": "Country / region", "dept_name": "Department",
                              "board_fy": "Approved FY", "status": "Status", "amount": "Amount (US$m)",
                              "project_url": "Disclosure"}))
    st.dataframe(table, hide_index=True, width="stretch", height=420,
                 column_config={"Disclosure": st.column_config.LinkColumn(display_text="Open"),
                                "Amount (US$m)": st.column_config.NumberColumn(format="%,.1f")})
