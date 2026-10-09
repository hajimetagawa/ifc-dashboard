"""
各タブの最後に表示する「Summary」（文章による分析のまとめ）を作る。

方針:
    - 文章に使う数字は、app.py が画面の図と同じ計算から渡す（図と文章が食い違わないように）。
    - 「集中している」「高い」などの評価の言葉は、下の基準（しきい値）で機械的に選ぶ。
      基準を変えるときはここの定数を直す。注記タブにも同じ基準を載せている（THRESHOLDS_NOTE）。
    - 生成 AI は使わない。同じ絞り込みなら、いつでも同じ文章になる。
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------- 評価の基準
CLIENT_CONC = [(0.50, "highly concentrated"), (0.30, "moderately concentrated"), (0.0, "diversified")]
COUNTRY_CONC = [(0.50, "dominated by one country"), (0.30, "led by one country"), (0.0, "spread across countries")]
CRC_LEVEL = [(5.0, "elevated"), (3.0, "moderate"), (0.0, "low")]          # 金額で加重した OECD の平均分類
SHARE_LEVEL = [(0.40, "a large share"), (0.15, "a meaningful share"), (0.0, "a small share")]
PRESENCE = [(0.75, "IFC is clearly the larger of the two"), (0.55, "IFC is larger"),
            (0.45, "the two are of similar size"), (0.25, "the EBRD is larger"),
            (0.0, "the EBRD is clearly the larger of the two")]
SHIFT_PP = 10   # 構成比の変化を「変わった」と書く最小の幅（パーセントポイント）

THRESHOLDS_NOTE = """
**How the summaries are written**
- Each tab ends with a summary generated from the figures on that tab, using fixed rules — no AI is involved,
  so the same selection always produces the same text.
- Words of assessment follow these thresholds: client concentration — *highly concentrated* if the ten largest
  clients hold 50% or more, *moderately concentrated* from 30%; country concentration — *dominated by one
  country* from 50%, *led by one country* from 30%; country risk — *elevated* if the amount-weighted OECD category
  is 5 or more, *moderate* from 3; shares — *large* from 40%, *meaningful* from 15%; IFC vs EBRD — *similar
  size* if IFC has 45–55% of the two, *larger* beyond that, *clearly larger* beyond 75% (or below 25%);
  a change in mix is mentioned when it is 10 percentage points or more.
"""


LAST: dict[str, tuple[dict, list[str]]] = {}   # タブごとの直近の (数字, 文章)。AI commentary に渡す


def keep(name: str):
    """文章を作る関数の結果を LAST に残す（AI commentary が同じ数字・文章を使えるように）。"""
    def deco(fn):
        def wrapper(f: dict) -> list[str]:
            paras = fn(f)
            LAST[name] = (f, [x for x in paras if x])
            return paras
        return wrapper
    return deco


def level(value: float, scale: list[tuple[float, str]]) -> str:
    for cut, word in scale:
        if value >= cut:
            return word
    return scale[-1][1]


def usd(m: float) -> str:
    if pd.isna(m) or m == 0:
        return "US$0"
    return f"US${m / 1000:,.1f}bn" if abs(m) >= 1000 else f"US${m:,.0f}m"


def pct(x: float) -> str:
    return f"{x:.0%}"


def listing(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def top_shares(s: pd.Series, n: int = 3) -> str:
    """金額の Series（index＝区分）から「A (40%), B (20%) and C (10%)」を作る。"""
    s = s[s > 0].sort_values(ascending=False)
    tot = s.sum()
    s = s[s / tot >= 0.005] if tot else s          # 0% と表示される小さい区分は書かない
    return listing([f"{k} ({v / tot:.0%})" for k, v in s.head(n).items()]) if tot else ""


def plural(n: int, word: str) -> str:
    if n == 1:
        return f"1 {word}"
    return f"{n:,} " + (word[:-1] + "ies" if word.endswith("y") and word[-2] not in "aeiou" else word + "s")


def render(paragraphs: list[str], key: str, caveat: str) -> None:
    """Summary の枠を表示する。文章はテキストファイルとしてダウンロードもできる。"""
    paragraphs = [p for p in paragraphs if p]
    st.subheader("Summary")
    with st.container(border=True):
        for p in paragraphs:
            st.markdown(p.replace("$", "\\$"))
        st.caption(caveat)
    text = "\n\n".join(paragraphs + [caveat])
    st.download_button("Download this summary (text)", text.replace("**", "").encode("utf-8"),
                       file_name=f"summary_{key}.txt", mime="text/plain", key=f"summary-dl-{key}")


# ---------------------------------------------------------------- 1 IFC Overview
@keep("overview")
def overview(f: dict) -> list[str]:
    p1 = (f"As of {f['as_of']}, IFC's active portfolio stands at **{usd(f['active'])}** across "
          f"{plural(f['n_active'], 'project')} in {f['n_countries']} countries, plus multi-country projects. "
          f"By region, it is spread across {top_shares(f['by_region'])}. By business line, the largest are "
          f"{top_shares(f['by_bl'])}.")
    p2 = ""
    if f["recent"] is not None and f["base"]:
        change = f["recent"] / f["base"] - 1
        direction = "above" if change > 0.05 else ("below" if change < -0.05 else "close to")
        p2 = (f"New commitments by approval year averaged **{usd(f['recent'])}** a year in "
              f"FY{f['recent_years'][0]}–FY{f['recent_years'][1]}, {direction} the average of "
              f"{usd(f['base'])} in FY{f['base_years'][0]}–FY{f['base_years'][1]}"
              + (f" ({change:+.0%})." if direction != "close to" else "."))
        if f.get("top_growth"):
            p2 += f" The largest change by region was in {f['top_growth']}."
        p2 += (" The most recent years may be understated, because recently approved projects that are not yet "
               "signed are counted in the pipeline.")
    p3 = (f"The pipeline holds **{usd(f['pipeline'])}** in {plural(f['n_pipeline'], 'project')}, "
          f"{f['n_noamt']} of which do not show an amount yet; by region it is led by {top_shares(f['pipe_by_region'], 2)}. "
          "These projects are not yet legal commitments.")
    return [p1, p2, p3]


# ---------------------------------------------------------------- 2 Portfolio Profile
@keep("portfolio")
def portfolio(f: dict) -> list[str]:
    stage_txt = listing([f"{usd(v)} {k.lower()}" for k, v in f["stages"].items()])
    p1 = (f"For {f['scope']}, commitments approved in FY{f['fy'][0]}–FY{f['fy'][1]} total **{usd(f['total'])}** "
          f"across {plural(f['n_projects'], 'project')} ({stage_txt}).")
    parts = []
    if f["n_industries"] > 1:
        parts.append(f"By industry, the largest are {top_shares(f['by_industry'])}")
    else:
        parts.append(f"All of it is in one industry ({f['by_industry'].index[0]})")
    parts.append(f"by instrument, {top_shares(f['by_instrument'])}")
    parts.append(f"by {f['where_label']}, {top_shares(f['by_where'])}")
    p2 = "; ".join(parts) + ". " + (
        f"The ten largest clients account for **{pct(f['top10_clients'])}**, so the selection is "
        f"{level(f['top10_clients'], CLIENT_CONC)} by client"
        + (f", led by {f['top_client']} ({pct(f['top_client_share'])})." if f.get("top_client") else "."))
    p3 = ""
    if f.get("shift"):
        p3 = (f"Between {f['shift_span'][0]} and {f['shift_span'][1]}, " + listing(f["shift"])
              + " (shares of new commitments).")
    elif f.get("shift_span"):
        p3 = (f"The instrument and {f['mix_label']} mix of new commitments was broadly stable between "
              f"{f['shift_span'][0]} and {f['shift_span'][1]} (no change of {SHIFT_PP} percentage points or more).")
    return [p1, p2, p3]


# ---------------------------------------------------------------- 3 Risk Profile
@keep("risk")
def risk(f: dict) -> list[str]:
    p1 = (f"{f['scope'][0].upper() + f['scope'][1:]} has **{usd(f['total'])}** in its active portfolio, across "
          f"{plural(f['n'], 'project')} and {plural(f['n_clients'], 'client')}.")
    p2 = (f"The portfolio is **{level(f['top10'], CLIENT_CONC)}** by client: the ten largest clients account for "
          f"{pct(f['top10'])}, led by {f['top_client']} ({pct(f['top_client_share'])}). By country it is "
          f"**{level(f['top_country_share'], COUNTRY_CONC)}** — {f['top_country']} accounts for "
          f"{pct(f['top_country_share'])}.")
    if f["w_avg"] is not None:
        p3 = (f"Country risk is **{level(f['w_avg'], CRC_LEVEL)}**: the amount-weighted OECD category is "
              f"{f['w_avg']:.1f} on a 0–7 scale, and {pct(f['crc67'])} ({usd(f['crc67_amt'])}) is in categories 6–7"
              + (f", mainly {listing(f['crc67_countries'])}." if f["crc67_countries"] else "."))
    else:
        p3 = "Country risk data is not loaded."
    chars = [f"equity is {level(f['equity'], SHARE_LEVEL)} ({pct(f['equity'])})",
             f"projects with a high E&S category account for {pct(f['high_es'])}",
             f"projects approved ten or more fiscal years ago for {pct(f['old'])}",
             f"{usd(f['undisb'])} is signed but not yet disbursed"]
    p4 = "Among other characteristics, " + listing(chars) + "."
    if f.get("watch") is not None:
        p4 += (f" On the trial watchlist, {f['watch']} of {f['n']} projects carry two or more risk signals.")
    return [p1, p2, p3, p4]


# ---------------------------------------------------------------- 4 vs EBRD
@keep("ebrd")
def ebrd(f: dict) -> list[str]:
    share = f["ifc"] / (f["ifc"] + f["ebrd"]) if f["ifc"] + f["ebrd"] else 0
    p1 = (f"In the {plural(f['n_countries'], 'country')} where both invested in {f['years'][0]}–{f['years'][1]}, "
          f"{f['ifc_label']} committed **{usd(f['ifc'])}** and {f['ebrd_label']} **{usd(f['ebrd'])}** "
          f"(converted at {f['rate']:.2f} US$ per euro). IFC accounts for **{pct(share)}** of the two: "
          f"{level(share, PRESENCE)}.")
    p2 = ""
    if f["ifc_larger"] or f["ebrd_larger"]:
        bits = []
        if f["ifc_larger"]:
            bits.append(f"IFC is the larger investor in {listing(f['ifc_larger'])}")
        if f["ebrd_larger"]:
            bits.append(f"the EBRD is larger in {listing(f['ebrd_larger'])}")
        p2 = "By country, " + "; ".join(bits) + "."
    p3 = (f"Loans and other debt make up {pct(f['ifc_debt'])} of IFC's amount and {pct(f['ebrd_debt'])} of the "
          f"EBRD's; guarantees {pct(f['ifc_guar'])} and {pct(f['ebrd_guar'])}; equity {pct(f['ifc_eq'])} and "
          f"{pct(f['ebrd_eq'])}. Countries in OECD risk categories 6–7 take {pct(f['ifc_crc67'])} of IFC's amount "
          f"and {pct(f['ebrd_crc67'])} of the EBRD's.")
    p4 = ""
    if f.get("state_max") is not None:
        p4 = (f"The EBRD also lends to the public sector — up to {pct(f['state_max'])} of its lending in "
              f"{f['state_country']} — which IFC does not do."
              if f["state_max"] >= 0.01 else
              "In these countries the EBRD's lending in the sectors compared is almost entirely private, as IFC's is.")
    p5 = ""
    if f.get("recent_share") is not None:
        p5 = (f"In the last three years ({f['recent_years'][0]}–{f['recent_years'][1]}) IFC's share was "
              f"{pct(f['recent_share'])}, against {pct(f['early_share'])} in the first three years of the period.")
    return [p1, p2, p3, p4, p5]


# ---------------------------------------------------------------- 5 Country Profile
@keep("country")
def country(f: dict) -> list[str]:
    income = f["income"] if isinstance(f["income"], str) else "income group not available"
    p1 = (f"**{f['country']}** ({income}; OECD country risk "
          f"{f['crc'] or 'not available'}). For {f['scope']}, IFC commitments approved in "
          f"FY{f['fy'][0]}–FY{f['fy'][1]} total **{usd(f['total'])}** across {plural(f['n'], 'project')} "
          f"({listing([f'{usd(v)} {k.lower()}' for k, v in f['stages'].items()])}).")
    p2 = (f"By instrument, {top_shares(f['by_instrument'])}. The largest client is {f['top_client']} "
          f"({pct(f['top_client_share'])} of the amount)." if f["total"] else "")
    p3 = ""
    if f.get("ebrd") is not None:
        p3 = (f"The EBRD invested {f['ebrd_eur']} in {f['country']} in the same years (all sectors, "
              f"{pct(f['ebrd_state'])} to the public sector), led by {top_shares(f['ebrd_by_group'], 2)}."
              if f["ebrd"] else f"The EBRD has no investment in {f['country']} in these years.")
    p4 = ""
    if f.get("n_paragraphs"):
        p4 = (f"IFC reports provide {plural(f['n_paragraphs'], 'paragraph')} on this country"
              + (f", most often on {listing(f['top_themes'])}." if f["top_themes"] else "."))
    return [p1, p2, p3, p4]
