"""
国ごとの地域区分（世界銀行・IFCの2種類）と所得区分の対応表を作り、案件データに結合する。

使い方（プロジェクトのフォルダで実行。先に fetch_projects.py を実行しておく）:
    uv run python src/build_country_ref.py                   # 世界銀行APIから国情報を取得
    uv run python src/build_country_ref.py --input <raw.json> # 保存済みの国情報から再作成のみ

入力:
    世界銀行 国別API  https://api.worldbank.org/v2/country （無料・認証不要）
    sources/ifc_regions.csv       … IFCの地域区分（MCTはIFC公式ページの国一覧）
    sources/country_overrides.csv … 自動照合できない国名・地域案件の手作業の対応

出力（data/curated/ifc.duckdb）:
    countries          : 国名ごとの対応表
    projects_enriched  : projects_unique に地域・所得区分を結合したビュー

注意:
    所得区分・世界銀行の地域区分は「現在」の分類。過去の案件にも現在の分類が当たる。
    IFCの地域区分も現在の組織に基づく。MCT以外は世界銀行の地域からの暫定割り当て。
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import requests

WB_API = "https://api.worldbank.org/v2/country"
ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "structured"
DB_PATH = ROOT / "data" / "curated" / "ifc.duckdb"
IFC_REGIONS = ROOT / "sources" / "ifc_regions.csv"
OVERRIDES = ROOT / "sources" / "country_overrides.csv"

# MCT以外の国に、世界銀行の地域からIFCの地域を暫定的に割り当てる
WB_TO_IFC_PROVISIONAL = {
    "Sub-Saharan Africa": "Africa",
    "East Asia & Pacific": "East Asia and Pacific",
    "Europe & Central Asia": "Europe",
    "Latin America & Caribbean": "Latin America and the Caribbean",
    "South Asia": "South Asia",
}


def fetch_wb_countries() -> list[dict]:
    r = requests.get(WB_API, params={"format": "json", "per_page": 400}, timeout=60)
    r.raise_for_status()
    meta, records = r.json()
    if int(meta["total"]) > len(records):
        raise RuntimeError(f"国情報が一度で取得しきれていません（{len(records)} / {meta['total']}）")
    return records


def wb_table(records: list[dict]) -> pd.DataFrame:
    rows = []
    for x in records:
        region = (x.get("region") or {}).get("value", "").strip()
        if region == "Aggregates":  # 「世界」「高所得国」などの集計単位は除く
            continue
        rows.append({
            "iso3": x["id"],
            "iso2": x["iso2Code"],
            "wb_name": x["name"].strip(),
            "wb_region": region,
            "income_level": (x.get("incomeLevel") or {}).get("value", "").strip() or None,
            "lending_type": (x.get("lendingType") or {}).get("value", "").strip() or None,
        })
    return pd.DataFrame(rows)


def norm(name: str) -> str:
    """国名の表記ゆれを吸収するための簡易正規化（照合用）。"""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z ]", " ", s)
    drop = {"republic", "rep", "of", "the", "arab", "islamic", "federal", "democratic", "people", "s"}
    return " ".join(w for w in s.split() if w not in drop)


def build(wb: pd.DataFrame, con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    data = con.sql(
        "SELECT country, any_value(wb_country_code) AS wb_country_code, count(*) AS n_projects "
        "FROM projects_unique GROUP BY country"
    ).df()
    ov = pd.read_csv(OVERRIDES, dtype=str).set_index("data_country")
    ifc = pd.read_csv(IFC_REGIONS, dtype=str).set_index("iso3")

    by_iso2 = wb.set_index("iso2")
    by_iso3 = wb.set_index("iso3")
    by_name = {norm(n): i for i, n in zip(wb["iso3"], wb["wb_name"])}

    out = []
    for _, r in data.iterrows():
        c, code = r["country"], r["wb_country_code"]
        o = ov.loc[c] if c in ov.index else None
        iso3, match = None, "照合できず"
        if o is not None and pd.notna(o["iso3"]):
            iso3, match = o["iso3"], "手作業の対応表"
        elif pd.notna(code) and code in by_iso2.index:
            iso3, match = by_iso2.loc[code, "iso3"], "国コード"
        elif norm(c) in by_name:
            iso3, match = by_name[norm(c)], "国名"
        elif o is not None:
            match = "手作業の対応表（地域案件など）"

        w = by_iso3.loc[iso3] if iso3 is not None and iso3 in by_iso3.index else None
        wb_region = o["wb_region"] if o is not None and pd.notna(o["wb_region"]) else (w["wb_region"] if w is not None else None)

        if o is not None and pd.notna(o["ifc_region"]):
            ifc_region, basis = o["ifc_region"], "地域案件（案件の国名から判断）"
        elif iso3 is not None and iso3 in ifc.index:
            ifc_region, basis = ifc.loc[iso3, "ifc_region"], ifc.loc[iso3, "status"]
        elif wb_region in WB_TO_IFC_PROVISIONAL:
            ifc_region, basis = WB_TO_IFC_PROVISIONAL[wb_region], "暫定（世界銀行の地域から推定）"
        else:
            ifc_region, basis = None, "要確認"

        out.append({
            "country": c, "wb_country_code": code, "iso3": iso3, "match_method": match,
            "wb_region": wb_region,
            "income_level": w["income_level"] if w is not None else None,
            "lending_type": w["lending_type"] if w is not None else None,
            "ifc_region": ifc_region, "ifc_region_basis": basis,
            "n_projects": int(r["n_projects"]),
        })
    return pd.DataFrame(out).sort_values("country").reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="国ごとの地域・所得区分の対応表を作成")
    ap.add_argument("--input", type=Path, help="保存済みの世界銀行国情報(JSON)を使う")
    args = ap.parse_args()

    if args.input:
        records = json.loads(args.input.read_text(encoding="utf-8"))
    else:
        print("世界銀行APIから国情報を取得中...")
        records = fetch_wb_countries()
        raw = RAW_DIR / f"wb_countries_{date.today():%Y%m%d}.json"
        raw.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        print(f"元データを保存: {raw.relative_to(ROOT)}")

    wb = wb_table(records)
    with duckdb.connect(str(DB_PATH)) as con:
        countries = build(wb, con)
        # 既存の countries テーブルと名前が衝突しないよう、別名で登録してから書き込む
        con.register("countries_new", countries)
        con.execute("CREATE OR REPLACE TABLE countries AS SELECT * EXCLUDE (n_projects) FROM countries_new")
        con.unregister("countries_new")
        con.execute("""
            CREATE OR REPLACE VIEW projects_enriched AS
            SELECT p.*, c.iso3, c.wb_region, c.income_level, c.lending_type,
                   c.ifc_region, c.ifc_region_basis
            FROM projects_unique p LEFT JOIN countries c USING (country)
        """)
    countries.to_parquet(ROOT / "data" / "curated" / "countries.parquet", index=False)

    print(f"\n国名の数: {len(countries)}（世界銀行の国情報: {len(wb)} か国・地域）")
    print("\n照合方法ごとの国名数:")
    print(countries["match_method"].value_counts().to_string())
    print("\nIFCの地域 × 割り当ての根拠（案件数）:")
    print(countries.pivot_table(index="ifc_region", columns="ifc_region_basis", values="n_projects",
                                aggfunc="sum", fill_value=0, dropna=False).to_string())
    todo = countries[(countries["ifc_region_basis"] == "要確認") | (countries["match_method"] == "照合できず")]
    if len(todo):
        print("\n※確認が必要な国名（sources/country_overrides.csv に追記すると解消できます）:")
        print(todo[["country", "wb_country_code", "match_method", "wb_region", "ifc_region_basis", "n_projects"]].to_string(index=False))


if __name__ == "__main__":
    main()
