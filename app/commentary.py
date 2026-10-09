"""
AI commentary（生成 AI による解説）— 自分の PC でだけ生成し、保存した文章をダッシュボードに表示する。

仕組み:
    - 生成は、次の2つがそろったときだけ有効になる（公開版では有効にしないため、両方とも必要）
        環境変数 IFC_ENABLE_AI_COMMENTARY=1
        環境変数 ANTHROPIC_API_KEY（Claude API のキー）
    - 生成するときは、画面の Summary（ルールに基づく文章）と、その元になった数字だけを Claude に渡し、
      「この数字以外の事実を書かない」と指示する。
    - 生成した文章の数字が、渡した数字に含まれるかを照合し、含まれない数字を一覧にする。
      本人が読んで「Save」を押したときだけ content/commentary/<tab>.json に保存する（Git で管理）。
    - ダッシュボードは保存済みの文章を表示するだけ。閲覧者の操作では更新されない。
    - 対象は IFC 全体（左側の絞り込みをすべて All にした状態）のみ。保存時の条件と今の条件が同じときだけ表示する。

PowerShell での使い方:
    $env:ANTHROPIC_API_KEY = "（自分のキー）"
    $env:IFC_ENABLE_AI_COMMENTARY = "1"
    uv run streamlit run app/app.py
"""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / "content" / "commentary"
MODEL = os.environ.get("IFC_COMMENTARY_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = 1200

TAB_PURPOSE = {
    "overview": "Tab 1 (IFC Overview): the size of IFC's whole active portfolio, where it is, how new commitments "
                "have built up, and the pipeline.",
    "portfolio": "Tab 2 (Portfolio Profile): what the portfolio is made of — industries, instruments, regions, "
                 "clients — and how the mix of new commitments has shifted.",
    "risk": "Tab 3 (Risk Profile): where risk could concentrate in the active portfolio — clients, countries, "
            "OECD country risk and other risk characteristics. Based on approved amounts only.",
    "ebrd": "Tab 4 (vs EBRD): IFC's presence compared with the EBRD in the countries where both invest.",
}

SYSTEM = (
    "You write short analytical commentary for a public dashboard about the International Finance Corporation "
    "(IFC). Readers are professionals in development finance.\n"
    "Rules:\n"
    "- Use ONLY the figures and facts given to you. Do not add outside facts, dates, events or figures.\n"
    "- Do not state causes as facts. If you suggest a possible reason, say clearly that it is a possible "
    "explanation the data cannot confirm.\n"
    "- Keep a neutral, factual tone. No praise, no recommendations to IFC.\n"
    "- Write figures exactly as given (same rounding and units).\n"
    "- Mention the main data limitation that matters for the reading.\n"
    "- English, 150–250 words, two or three paragraphs, no headings, no bullet points."
)


# ---------------------------------------------------------------- 有効かどうか
def enabled() -> bool:
    return os.environ.get("IFC_ENABLE_AI_COMMENTARY") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------- 数字の受け渡し
def jsonable(x, top: int = 8):
    """Summary に使った数字（pandas の Series を含む）を、AI に渡せる形にする。"""
    if isinstance(x, pd.Series):
        s = x[x > 0].sort_values(ascending=False) if x.dtype.kind in "fi" else x
        tot = s.sum() if s.dtype.kind in "fi" else None
        return {str(k): {"amount_usd_m": round(float(v), 1), "share": round(float(v / tot), 3) if tot else None}
                for k, v in s.head(top).items()}
    if isinstance(x, dict):
        return {k: jsonable(v, top) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v, top) for v in x]
    if isinstance(x, float):
        return round(x, 4)
    if hasattr(x, "item"):           # numpy の数値
        return jsonable(x.item(), top)
    return x


NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUM.findall(text)}


def unmatched_numbers(output: str, sources: str) -> list[str]:
    """生成文の数字のうち、渡した Summary・数字のどこにも現れないもの（確認用）。"""
    allowed = numbers(sources)
    # 比率（0.123）から % 表記（12%）も許す
    for n in list(allowed):
        try:
            v = float(n)
            if 0 < v < 1:
                allowed.add(str(round(v * 100)))
        except ValueError:
            pass
    return sorted(n for n in numbers(output) - allowed if n not in {"1", "2", "3", "0", "7"})


# ---------------------------------------------------------------- 生成と保存
def generate(tab: str, facts: dict, summary: list[str], scope: str) -> str:
    import anthropic   # 公開版では使わないため、ここで読み込む
    client = anthropic.Anthropic()
    prompt = (f"Dashboard section: {TAB_PURPOSE[tab]}\nScope: {scope}\n\n"
              "Rule-based summary already shown on the dashboard (do not repeat it sentence by sentence; "
              "add interpretation of how the figures relate to each other):\n"
              + "\n".join(summary)
              + "\n\nFigures behind it (JSON; amounts in US$ millions unless stated; shares as fractions):\n"
              + json.dumps(jsonable(facts), ensure_ascii=False, default=str, indent=1))
    msg = client.messages.create(model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
                                 messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def path(tab: str) -> Path:
    return DIR / f"{tab}.json"


def load(tab: str) -> dict | None:
    try:
        return json.loads(path(tab).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save(tab: str, text: str, signature: dict, scope: str, data_as_of: str) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    path(tab).write_text(json.dumps({
        "tab": tab, "text": text, "model": MODEL, "generated_on": date.today().isoformat(),
        "data_as_of": data_as_of, "scope": scope, "signature": signature,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 表示
def section(tab: str, facts: dict, summary: list[str], signature: dict, scope: str, data_as_of: str,
            eligible: bool) -> None:
    """
    保存済みの AI commentary を表示する（今の条件が保存時と同じとき）。
    自分の PC で生成が有効なら、生成・確認・保存の操作も表示する。
    """
    saved = load(tab)
    sig = json.loads(json.dumps(signature, default=str))
    if saved and saved.get("signature") == sig:
        st.markdown("**AI commentary**")
        with st.container(border=True):
            st.markdown(saved["text"].replace("$", "\\$"))
            st.caption(f"Written by {saved['model']} (Claude, Anthropic) on {saved['generated_on']} from the figures "
                       f"in the summary above, for {saved['scope']}, data as of {saved['data_as_of']}. It was "
                       "reviewed before publication and is not updated when the data or filters change.")
    elif saved and eligible is False:
        st.caption(f"An AI commentary is available for {saved['scope']}: set the filters on the left to All to "
                   "read it.")

    if not (enabled() and eligible):
        return
    key = f"ai-draft-{tab}"
    with st.expander("Generate AI commentary (local only)", expanded=key in st.session_state):
        st.caption(f"Sends the summary and its figures to the Claude API ({MODEL}). Nothing is published until "
                   "you save it. Saved commentary goes to content/commentary/ and should be committed to Git.")
        if st.button("Generate", key=f"ai-gen-{tab}"):
            with st.spinner("Asking Claude…"):
                try:
                    st.session_state[key] = generate(tab, facts, summary, scope)
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Generation failed: {exc}")
        draft = st.session_state.get(key)
        if draft:
            st.markdown(draft.replace("$", "\\$"))
            sources = "\n".join(summary) + json.dumps(jsonable(facts), default=str)
            odd = unmatched_numbers(draft, sources)
            if odd:
                st.warning("These numbers do not appear in the figures given to the model — check them before "
                           "saving: " + ", ".join(odd))
            else:
                st.success("Every number in the draft appears in the figures given to the model.")
            c1, c2 = st.columns(2)
            if c1.button("Save and show on the dashboard", key=f"ai-save-{tab}"):
                save(tab, draft, sig, scope, data_as_of)
                del st.session_state[key]
                st.rerun()
            if c2.button("Discard", key=f"ai-discard-{tab}"):
                del st.session_state[key]
                st.rerun()
