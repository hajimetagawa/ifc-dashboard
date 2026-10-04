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
        project_instruments : 案件 × 投融資手段（融資・株式・保証・リスク管理商品）ごとの承認額
    金額の集計には approved_usd_m_for_sum を使う（プログラム全体の枠と判定した金額を除いたもの。
    判定の根拠は amount_basis 列）
    年度の絞り込みには board_fy を使う（承認前の案件は理事会の予定日の年度。根拠は board_fy_basis 列）

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
BUSINESS_LINE_RULES = ROOT / "sources" / "business_line_rules.csv"

# コミットの段階（ステータスから判定）
#   Committed: 署名済み（実行中・完了・実行待ち）
#   Pipeline : 理事会承認済みで署名待ち、または理事会の承認待ち
#   On hold  : 保留中
COMMITMENT_STAGE = {
    "Active": "Committed", "Completed": "Committed", "Pending Disbursement": "Committed",
    "Pending Signing": "Pipeline", "Pending Approval": "Pipeline",
    "Hold": "On hold",
}

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

    # 分析用の年度: 承認済みは承認年度、承認前（承認日がない案件）は理事会の予定日の年度
    if "approval_fy" in df.columns and "projected_board_date" in df.columns:
        p = df["projected_board_date"]
        projected_fy = (p.dt.year + (p.dt.month >= 7).astype("Int64")).astype("Int64")
        df["board_fy"] = df["approval_fy"].fillna(projected_fy)
        df["board_fy_basis"] = pd.Series(pd.NA, index=df.index, dtype="string")
        df.loc[df["approval_fy"].notna(), "board_fy_basis"] = "Approved"
        df.loc[df["approval_fy"].isna() & projected_fy.notna(), "board_fy_basis"] = "Projected"

    if "status" in df.columns:
        df["commitment_stage"] = df["status"].map(COMMITMENT_STAGE).fillna("Other").astype("string")
    if "department" in df.columns:
        df = classify_department(df)
    return flag_program_amounts(df)


def classify_department(df: pd.DataFrame) -> pd.DataFrame:
    """部署名から部署（コードと下位の課を除いた名称）と事業部門を取り出す。

    仮定: 事業部門は部署名に含まれるキーワードで決まり、組織再編で名称が変わっても同じとみなす。
    規則は sources/business_line_rules.csv（priority の小さい順に照合し、最初に当てはまったもの）。
    """
    dept = df["department"].astype("string")
    # 例: "CF5CT - Regional Industry FIG MCT/FIG Central Asia & Turkey"
    #   → 部署 "Regional Industry FIG MCT"、下位の課 "FIG Central Asia & Turkey"
    no_code = dept.str.replace(r"^[A-Z0-9]{5} - ", "", regex=True)
    has_code = dept.str.match(r"^[A-Z0-9]{5} - ").fillna(False)
    df["dept_name"] = no_code.where(~has_code, no_code.str.split("/", n=1).str[0]).str.strip()
    df["dept_subunit"] = no_code.str.split("/", n=1).str[1].where(has_code).str.strip()

    df["business_line"] = pd.Series("Other", index=df.index, dtype="string")
    if BUSINESS_LINE_RULES.exists():
        rules = pd.read_csv(BUSINESS_LINE_RULES, dtype=str).sort_values("priority", key=lambda s: s.astype(int))
        assigned = pd.Series(False, index=df.index)
        for r in rules.itertuples():
            hit = df["dept_name"].str.contains(r.pattern, regex=True).fillna(False) & ~assigned
            df.loc[hit, "business_line"] = r.business_line
            assigned |= hit
    return df


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
        known = set(df["project_number"].dropna())
        for r in ov.itertuples():
            pn, basis = r.project_number.strip(), str(r.amount_basis).strip()
            if basis not in ("Individual", "Program-level"):
                print(f"※amount_overrides.csv: 案件 {pn} の amount_basis「{basis}」は無効です"
                      "（Individual または Program-level）。この行は無視します")
                continue
            if pn not in known:
                print(f"※amount_overrides.csv: 案件番号 {pn} がデータにありません。この行は無視します")
                continue
            df.loc[df["project_number"] == pn, "amount_basis"] = f"{basis} (manual)"

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
    if "board_fy_basis" in df.columns:
        u = df.drop_duplicates("project_number")
        print(f"年度の根拠: 承認年度 {(u['board_fy_basis'] == 'Approved').sum():,} 件"
              f" / 理事会の予定日 {(u['board_fy_basis'] == 'Projected').sum():,} 件"
              f" / 年度なし {u['board_fy_basis'].isna().sum():,} 件")
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
    for col in ["commitment_stage", "business_line"]:
        if col in df.columns:
            u = df.drop_duplicates("project_number")
            print(f"\n{col} の内訳（案件数・集計用の承認額 百万ドル）:")
            print(u.groupby(col, dropna=False).agg(n=("project_number", "count"),
                                                   usd_m=("approved_usd_m_for_sum", "sum")).round(0).to_string())
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
        # 承認額を投融資手段ごとに縦長に展開（1案件に複数の手段がある場合があるため）
        #   内訳の合計が承認額の合計に満たない分は "Unallocated"、金額が何もない案件は "Not specified"（金額なし）
        #   プログラム枠と判定した案件は、集計用の金額（approved_usd_m_for_sum）を空欄にする
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
        con.execute("""
            CREATE OR REPLACE TABLE project_instruments AS
            WITH u AS (
                SELECT project_number, amount_basis, total_approved_usd_m AS total,
                       loan_usd_m, equity_usd_m, guarantee_usd_m, risk_mgmt_usd_m,
                       coalesce(loan_usd_m, 0) + coalesce(equity_usd_m, 0)
                         + coalesce(guarantee_usd_m, 0) + coalesce(risk_mgmt_usd_m, 0) AS parts
                FROM projects_unique
            ), long AS (
                SELECT project_number, 'Loan' AS instrument, loan_usd_m AS amt FROM u WHERE loan_usd_m > 0
                UNION ALL SELECT project_number, 'Equity', equity_usd_m FROM u WHERE equity_usd_m > 0
                UNION ALL SELECT project_number, 'Guarantee', guarantee_usd_m FROM u WHERE guarantee_usd_m > 0
                UNION ALL SELECT project_number, 'Risk management', risk_mgmt_usd_m FROM u WHERE risk_mgmt_usd_m > 0
                UNION ALL SELECT project_number, 'Unallocated', total - parts FROM u WHERE total - parts > 0.5
                UNION ALL SELECT project_number, 'Not specified', NULL FROM u
                    WHERE parts = 0 AND coalesce(total, 0) = 0
            )
            SELECT l.project_number, l.instrument, l.amt AS approved_usd_m,
                   CASE WHEN starts_with(u.amount_basis, 'Program-level') THEN NULL ELSE l.amt END
                       AS approved_usd_m_for_sum
            FROM long l JOIN u USING (project_number)
        """)

    print(f"整形済みデータを保存: {parquet_path.relative_to(ROOT)}、{db_path.relative_to(ROOT)}（projects）")
    summarize(df)


if __name__ == "__main__":
    main()
