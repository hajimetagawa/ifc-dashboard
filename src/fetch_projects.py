"""
IFC Investment Services Projects（Finances One DS00499）を取得し、整形して保存する。

使い方（プロジェクトのフォルダで実行）:
    uv run python src/fetch_projects.py                     # APIから全件取得
    uv run python src/fetch_projects.py --input <raw.json>  # 保存済みの元データから再整形のみ

出力:
    data/raw/structured/ifc_projects_<取得日>.json  … APIの応答そのまま（加工なし）
    data/curated/ifc_projects.parquet               … 整形済みデータ
    data/curated/ifc.duckdb                         … SQLで集計するためのDB
        projects        : 開示文書単位（元データと同じ粒度）
        projects_unique : 案件単位（1案件1行。金額の集計はこちらを使う）
    金額の集計には approved_usd_m_for_sum を使う（プログラム全体の枠と判定した金額を除いたもの。
    判定の根拠は amount_basis 列）

API仕様: https://financesone.worldbank.org/api-explorer?id=DS00499
    認証不要 / 1回あたり最大1,000件（top と skip でページ分割）
"""

from __future__ import annotations

import argparse
import html
import json
import time
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import requests

API_URL = "https://datacatalogapi.worldbank.org/dexapps/fone/api/apiservice"
DATASET_ID = "DS00499"
RESOURCE_ID = "RS00448"
PAGE_SIZE = 1000

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "structured"
CURATED_DIR = ROOT / "data" / "curated"

# 金額列の名前を短くする（単位はすべて百万米ドル）
RENAME = {
    "ifc_investment_for_loanmillion__usd": "loan_usd_m",
    "ifc_investment_for_equitymillion__usd": "equity_usd_m",
    "ifc_investment_for_guaranteemillion__usd": "guarantee_usd_m",
    "ifc_investment_for_risk_managementmillion__usd": "risk_mgmt_usd_m",
    "total_ifc_investment_as_approved_by_boardmillion__usd": "total_approved_usd_m",
    # 理事会承認の年度（APIが公式の値を提供している）
    "board_approval___fiscal_year": "approval_fy",
    "board_approval___calendar_year": "approval_cy",
}
# プログラム全体の承認枠が参加先ごとに繰り返し記録されている案件の判定
# （例: GTFP は各参加銀行に70億ドルが記録されており、合計すると大幅な過大計上になる）
PROGRAM_MIN_GROUP = 4          # 同額・同日の案件がこの件数以上
PROGRAM_MIN_AMOUNT = 100.0     # かつ金額がこの値（百万ドル）以上
PROGRAM_NAME_PREFIXES = ("GTFP",)
AMOUNT_OVERRIDES = ROOT / "sources" / "amount_overrides.csv"

DATE_COLS = [
    "as_of_date", "date_disclosed", "ifc_approval_date", "ifc_invested_date",
    "ifc_signed_date", "projected_board_date",
]


def fetch_all(session: requests.Session) -> list[dict]:
    """ページを順に取得し、全レコードを返す。"""
    records: list[dict] = []
    total = None
    skip = 0
    while total is None or skip < total:
        params = {
            "datasetId": DATASET_ID, "resourceId": RESOURCE_ID,
            "type": "json", "top": PAGE_SIZE, "skip": skip,
        }
        for attempt in range(1, 4):  # 一時的な通信エラーに備えて3回まで再試行
            try:
                r = session.get(API_URL, params=params, timeout=60)
                r.raise_for_status()
                payload = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                if attempt == 3:
                    raise
                print(f"  再試行 {attempt}/3: {e}")
                time.sleep(3 * attempt)

        total = int(payload["count"])
        page = payload.get("data") or []
        if not page:
            break
        records.extend(page)
        skip += len(page)
        print(f"  取得 {len(records):,} / {total:,} 件")
        time.sleep(0.5)  # 公開APIに負荷をかけないよう間隔を空ける

    if total is not None and len(records) != total:
        print(f"※注意: 取得件数 {len(records):,} がAPIの総件数 {total:,} と一致しません")
    return records


def tidy(records: list[dict]) -> pd.DataFrame:
    """型を整える。未知の列が増えてもそのまま残す。"""
    df = pd.DataFrame.from_records(records).rename(columns=RENAME)

    # 文字列の整形: HTMLの特殊文字（&amp; など）を戻し、前後の空白を除き、空文字は欠損として扱う
    for col in df.columns:
        if df[col].dtype == object or pd.api.types.is_string_dtype(df[col]):
            s = df[col].astype("string").map(html.unescape, na_action="ignore").str.strip()
            df[col] = s.mask(s == "")

    for col in DATE_COLS:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], format="%d-%b-%Y", errors="coerce")

    for col in RENAME.values():
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "project_number" in df.columns:
        df["project_number"] = df["project_number"].astype("string").str.strip()

    # 承認年度はAPIの公式の値を使う。欠けている場合のみ承認日から計算する
    # （IFCの会計年度は7月1日〜6月30日。例: 2025年7月はFY2026）
    if "ifc_approval_date" in df.columns:
        d = df["ifc_approval_date"]
        computed = (d.dt.year + (d.dt.month >= 7).astype("Int64")).astype("Int64")
        if "approval_fy" in df.columns:
            df["approval_fy"] = pd.to_numeric(df["approval_fy"], errors="coerce").astype("Int64")
            df["approval_fy"] = df["approval_fy"].fillna(computed)
        else:
            df["approval_fy"] = computed
    if "approval_cy" in df.columns:
        df["approval_cy"] = pd.to_numeric(df["approval_cy"], errors="coerce").astype("Int64")
    return flag_program_amounts(df)


def flag_program_amounts(df: pd.DataFrame) -> pd.DataFrame:
    """プログラム全体の枠と考えられる金額を判定し、集計用の金額列を作る。"""
    if "total_approved_usd_m" not in df.columns:
        return df
    uniq = df.drop_duplicates("project_number")[["project_number", "total_approved_usd_m", "ifc_approval_date"]]
    size = (uniq.dropna(subset=["total_approved_usd_m", "ifc_approval_date"])
                .groupby(["total_approved_usd_m", "ifc_approval_date"])["project_number"].transform("count"))
    uniq = uniq.assign(same_amount_group_size=size.reindex(uniq.index).fillna(1).astype("Int64"))
    df = df.merge(uniq[["project_number", "same_amount_group_size"]], on="project_number", how="left")

    by_name = df["project_name"].fillna("").str.upper().str.startswith(PROGRAM_NAME_PREFIXES)
    by_repeat = ((df["same_amount_group_size"] >= PROGRAM_MIN_GROUP)
                 & (df["total_approved_usd_m"] >= PROGRAM_MIN_AMOUNT))
    df["amount_basis"] = "Individual"
    df.loc[by_repeat, "amount_basis"] = "Program-level (same amount and date repeated)"
    df.loc[by_name, "amount_basis"] = "Program-level (identified by project name)"

    # 手作業での上書き（sources/amount_overrides.csv。amount_basis に Individual または Program-level を記入）
    if AMOUNT_OVERRIDES.exists():
        ov = pd.read_csv(AMOUNT_OVERRIDES, dtype=str).dropna(subset=["project_number"])
        for r in ov.itertuples():
            df.loc[df["project_number"] == r.project_number.strip(), "amount_basis"] = f"{r.amount_basis.strip()} (manual)"

    is_program = df["amount_basis"].str.startswith("Program-level")
    df["approved_usd_m_for_sum"] = df["total_approved_usd_m"].mask(is_program)
    return df


def summarize(df: pd.DataFrame) -> None:
    print(f"\n件数: {len(df):,} 件 / 列数: {df.shape[1]}")
    if "project_number" in df.columns:
        dup = df["project_number"].duplicated().sum()
        print(f"案件番号の重複: {dup:,} 件")
    if "approval_fy" in df.columns and df["approval_fy"].notna().any():
        print(f"承認年度の範囲: FY{df['approval_fy'].min()} 〜 FY{df['approval_fy'].max()}")
    print("\n列ごとの欠損率:")
    for col, rate in df.isna().mean().sort_values(ascending=False).items():
        print(f"  {col:<28} {rate:6.1%}")
    if "amount_basis" in df.columns:
        u = df.drop_duplicates("project_number")
        prog = u[u["amount_basis"].str.startswith("Program-level")]
        print(f"\nプログラム枠と判定した案件: {len(prog):,} 件"
              f"（承認額 {prog['total_approved_usd_m'].sum():,.0f} 百万ドルを集計から除外。"
              f"全体の {prog['total_approved_usd_m'].sum() / max(u['total_approved_usd_m'].sum(), 1):.0%}）")
        print(f"集計に使う承認額の合計: {u['approved_usd_m_for_sum'].sum():,.0f} 百万ドル")
    for col in ["status", "product_line", "industry"]:
        if col in df.columns:
            print(f"\n{col} の内訳（上位10）:")
            print(df[col].value_counts(dropna=False).head(10).to_string())


def main() -> None:
    ap = argparse.ArgumentParser(description="IFC Investment Services Projects を取得・整形")
    ap.add_argument("--input", type=Path, help="保存済みの元データ(JSON)から整形のみ行う")
    args = ap.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    CURATED_DIR.mkdir(parents=True, exist_ok=True)

    if args.input:
        records = json.loads(args.input.read_text(encoding="utf-8"))
        print(f"元データを読み込み: {args.input}（{len(records):,} 件）")
    else:
        print("APIから取得中...")
        with requests.Session() as s:
            records = fetch_all(s)
        raw_path = RAW_DIR / f"ifc_projects_{date.today():%Y%m%d}.json"
        raw_path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        print(f"元データを保存: {raw_path.relative_to(ROOT)}")

    df = tidy(records)

    parquet_path = CURATED_DIR / "ifc_projects.parquet"
    df.to_parquet(parquet_path, index=False)

    db_path = CURATED_DIR / "ifc.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute("CREATE OR REPLACE TABLE projects AS SELECT * FROM df")
        # 同じ案件に開示文書が2件ある場合（2006年方針と2012年方針）があるため、
        # 案件単位の集計用に1案件1行のビューを作る（2012年方針の文書を優先）
        con.execute("""
            CREATE OR REPLACE VIEW projects_unique AS
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY project_number
                    ORDER BY CASE WHEN document_type LIKE '%2012%' THEN 0 ELSE 1 END,
                             date_disclosed DESC
                ) AS rn
                FROM projects
            ) WHERE rn = 1
        """)

    print(f"整形済みデータを保存: {parquet_path.relative_to(ROOT)}、{db_path.relative_to(ROOT)}（projects）")
    summarize(df)


if __name__ == "__main__":
    main()
