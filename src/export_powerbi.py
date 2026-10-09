"""
DuckDB のデータを、Power BI で読み込むための Parquet ファイルに書き出す。

使い方（プロジェクトのフォルダで実行。データ更新の最後に実行する）:
    uv run python src/export_powerbi.py

出力（data/curated/powerbi/）:
    projects.parquet             … 案件（1案件1行）。金額の集計には approved_usd_m_for_sum を使う
                                   事業部門（business_line）・部署（dept_name）・案件の段階（portfolio_stage: Active portfolio / Completed / Pipeline / On hold）を含む
    project_instruments.parquet  … 案件 × 投融資手段（融資・株式・保証・リスク管理商品）の承認額
    countries.parquet            … 国（1国1行、キー: iso3）。案件のない国も含む（has_ifc_projects で区別）
    industries.parquet           … 業種（1業種1行、キー: industry）
    documents.parquet            … レポート（1資料1行、キー: doc_id）
    paragraphs.parquet           … レポートの段落（1段落1行、キー: paragraph_id）
    paragraph_countries.parquet  … 段落と国の対応（レポートの対象国、または本文中の言及）
    paragraph_industries.parquet … 段落と業種の対応
    paragraph_themes.parquet     … 段落とテーマの対応

Power BI でのリレーションシップ（すべて多対一。→ の先が「一」側）:
    projects[iso3]                      → countries[iso3]
    project_instruments[project_number] → projects[project_number]
    country_indicators[iso3]            → countries[iso3]   （指標・基準日ごとに複数行。indicator で絞って使う）
    projects[industry]                  → industries[industry]
    paragraphs[doc_id]                  → documents[doc_id]
    paragraph_countries[paragraph_id]   → paragraphs[paragraph_id]   （クロスフィルター: 双方向）
    paragraph_countries[iso3]           → countries[iso3]
    paragraph_industries[paragraph_id]  → paragraphs[paragraph_id]   （クロスフィルター: 双方向）
    paragraph_industries[industry]      → industries[industry]
    paragraph_themes[paragraph_id]      → paragraphs[paragraph_id]   （クロスフィルター: 双方向）
"""

from __future__ import annotations

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "curated" / "ifc.duckdb"
OUT_DIR = ROOT / "data" / "curated" / "powerbi"

# 段落のキー: 資料ID・ページ・ブロック番号をつないだ文字列
PID = "doc_id || '|' || page::VARCHAR || '|' || block_no::VARCHAR"

QUERIES = {
    "projects": """
        SELECT project_number, project_name, company_name, country, iso3,
               business_line, dept_name, dept_subunit, portfolio_stage,
               wb_region, income_level, lending_type, ifc_region, ifc_region_basis,
               industry, coalesce(product_line, 'Not specified') AS product_line, department,
               status, status IN ('Active', 'Pending Disbursement') AS is_active,
               environmental_category, document_type,
               board_fy, board_fy_basis, approval_fy, approval_cy, ifc_approval_date, ifc_signed_date, ifc_invested_date,
               projected_board_date, date_disclosed,
               total_approved_usd_m, approved_usd_m_for_sum, amount_basis,
               starts_with(amount_basis, 'Program-level') AS is_program_level,
               loan_usd_m, equity_usd_m, guarantee_usd_m, risk_mgmt_usd_m,
               project_url, as_of_date
        FROM projects_enriched
    """,
    # 案件のある国に加え、世界銀行の国情報にある国をすべて含める（レポートでの言及とつなぐため）
    "countries": """
        SELECT iso3, country AS country_name, wb_country_code, wb_region, income_level,
               lending_type, ifc_region, ifc_region_basis, TRUE AS has_ifc_projects
        FROM countries WHERE iso3 IS NOT NULL
        UNION ALL
        SELECT w.id, trim(w.name), NULL, trim(w.region.value), trim(w.incomeLevel.value),
               trim(w.lendingType.value), r.ifc_region,
               CASE WHEN r.ifc_region IS NOT NULL THEN r.status END, FALSE
        FROM read_json_auto('{wb_json}') w
        LEFT JOIN read_csv_auto('{ifc_regions}') r ON r.iso3 = w.id
        WHERE trim(w.region.value) <> 'Aggregates'
          AND w.id NOT IN (SELECT iso3 FROM countries WHERE iso3 IS NOT NULL)
    """,
    # 案件 × 投融資手段の承認額（金額の集計はこちらの approved_usd_m_for_sum を使うと手段別に分けられる）
    "project_instruments": """
        SELECT project_number, instrument, approved_usd_m, approved_usd_m_for_sum
        FROM project_instruments
    """,
    # 国別のリスク指標（縦長の形式。src/fetch_country_data.py で作成）
    "country_indicators": """
        SELECT iso3, indicator, indicator_name, value, value_label, as_of, source, source_url
        FROM country_indicators
    """,
    "industries": """
        SELECT DISTINCT industry FROM projects_enriched WHERE industry IS NOT NULL
        UNION
        SELECT DISTINCT tag FROM block_tags WHERE tag_type = 'sector'
    """,
    "documents": """
        SELECT doc_id, title, publisher, doc_type, pub_date, countries, language, n_pages, file_name
        FROM documents
    """,
    "paragraphs": f"""
        SELECT {PID} AS paragraph_id, doc_id, page, block_no, kind, n_words, text
        FROM doc_blocks
        WHERE lang = 'en' AND kind IN ('paragraph', 'caption')
    """,
    "paragraph_countries": f"""
        SELECT DISTINCT {PID} AS paragraph_id, dc.iso3, 'Report country' AS basis
        FROM doc_blocks b JOIN doc_countries dc USING (doc_id)
        WHERE b.lang = 'en' AND b.kind IN ('paragraph', 'caption')
        UNION
        SELECT DISTINCT {PID} AS paragraph_id, tag AS iso3, 'Mentioned in text' AS basis
        FROM block_tags WHERE tag_type = 'country'
    """,
    "paragraph_industries": f"""
        SELECT {PID} AS paragraph_id, tag AS industry, hits
        FROM block_tags WHERE tag_type = 'sector'
    """,
    "paragraph_themes": f"""
        SELECT {PID} AS paragraph_id, tag AS theme, hits
        FROM block_tags WHERE tag_type = 'theme'
    """,
}

# 「一」側のテーブルのキー（重複があると Power BI でリレーションシップを作れない）
UNIQUE_KEYS = {
    "projects": "project_number", "countries": "iso3", "industries": "industry",
    "documents": "doc_id", "paragraphs": "paragraph_id",
}


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wb_files = sorted((ROOT / "data" / "raw" / "structured").glob("wb_countries_*.json"))
    if not wb_files:
        raise SystemExit("世界銀行の国情報がありません。先に build_country_ref.py を実行してください")
    QUERIES["countries"] = QUERIES["countries"].format(
        wb_json=wb_files[-1].as_posix(), ifc_regions=(ROOT / "sources" / "ifc_regions.csv").as_posix())
    # 読み取り専用で開く（他の処理の書き込みを妨げない）
    with duckdb.connect(str(DB_PATH), read_only=True) as con:
        tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
        for name, sql in QUERIES.items():
            if name == "country_indicators" and "country_indicators" not in tables:
                print("省略  country_indicators（src/fetch_country_data.py をまだ実行していないため）")
                continue
            con.execute(f"CREATE OR REPLACE TEMP VIEW _export AS {sql}")
            n = con.execute("SELECT count(*) FROM _export").fetchone()[0]
            key = UNIQUE_KEYS.get(name)
            if key:
                dup = con.execute(
                    f"SELECT count(*) - count(DISTINCT {key}) + count(*) FILTER (WHERE {key} IS NULL) FROM _export"
                ).fetchone()[0]
                if dup:
                    raise SystemExit(f"{name}: キー {key} に重複または空欄が {dup} 件あります。書き出しを中止します")
            path = OUT_DIR / f"{name}.parquet"
            con.execute(f"COPY _export TO '{path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            print(f"書き出し  {path.relative_to(ROOT)}  {n:,} 行")

        # 案件・段落とつながらない国コードを確認（Power BI で空白の行として現れるため）
        orphan = con.execute(f"""
            SELECT count(*) FROM ({QUERIES['paragraph_countries']}) p
            WHERE iso3 NOT IN (SELECT iso3 FROM ({QUERIES['countries']}))
        """).fetchone()[0]
        if orphan:
            print(f"※注意: 段落の国コードのうち {orphan:,} 件が countries にありません。"
                  "Power BI では国名が空白として表示されます")

    print(f"\n完了: {OUT_DIR}")


if __name__ == "__main__":
    main()
