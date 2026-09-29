"""
PDFから抽出した本文の段落に、国・セクター・テーマのタグを付け、案件データと結び付ける。

使い方（プロジェクトのフォルダで実行。先に extract_pdfs.py と build_country_ref.py を実行しておく）:
    uv run python src/tag_blocks.py

タグの付け方（すべて辞書による判定。有料のAIサービスは使わない）:
    国       : 資料の対象国（台帳の countries 列）＋本文中に出てくる国名
               国名の辞書 = 案件データの国名＋世界銀行の国名＋ sources/country_aliases.csv
    セクター : sources/tag_dictionary.csv（名称は案件データの industry と同じ）
    テーマ   : sources/tag_dictionary.csv（MSME金融、ジェンダー、気候など）
    辞書のCSVを編集して再実行すれば、タグはすべて作り直される。

出力（data/curated/ifc.duckdb）:
    doc_countries           : 資料ごとの対象国
    block_tags              : 段落ごとのタグ（1段落に複数のタグが付く）
    evidence                : 段落の本文とタグを1行にまとめたビュー（ダッシュボード表示用）
    country_sector_evidence : 国×セクターごとの段落数・資料数（案件データとの結合用）
    country_theme_evidence  : 国×テーマごとの段落数・資料数
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "curated" / "ifc.duckdb"
DICT_PATH = ROOT / "sources" / "tag_dictionary.csv"
ALIAS_PATH = ROOT / "sources" / "country_aliases.csv"
RAW_DIR = ROOT / "data" / "raw" / "structured"


def country_patterns(con: duckdb.DuckDBPyConnection) -> tuple[re.Pattern, dict[str, str]]:
    """国名の辞書を作り、長い名前から優先して照合する正規表現を返す。"""
    names: dict[str, str] = {}
    for country, iso3 in con.execute(
            "SELECT country, iso3 FROM countries WHERE iso3 IS NOT NULL").fetchall():
        names[country] = iso3
    wb_files = sorted(RAW_DIR.glob("wb_countries_*.json"))
    if wb_files:
        for x in json.loads(wb_files[-1].read_text(encoding="utf-8")):
            if (x.get("region") or {}).get("value", "").strip() != "Aggregates":
                names[x["name"].strip()] = x["id"]
    for _, a in pd.read_csv(ALIAS_PATH, dtype=str).iterrows():
        names[a["alias"].strip()] = a["iso3"].strip()
    # 国名は大文字で始まる固有名詞なので、大文字・小文字を区別して照合する（誤検出を減らすため）
    ordered = sorted(names, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w-])(" + "|".join(re.escape(n) for n in ordered) + r")(?![\w-])")
    return pattern, names


def main() -> None:
    with duckdb.connect(str(DB_PATH)) as con:
        for t in ("countries", "doc_blocks", "documents"):
            if not con.execute(f"SELECT count(*) FROM information_schema.tables WHERE table_name = '{t}'").fetchone()[0]:
                raise SystemExit(f"テーブル {t} がありません。先に前段のスクリプトを実行してください")

        # 対象: 英語の本文段落と図表の見出し
        blocks = con.execute("""
            SELECT doc_id, page, block_no, text FROM doc_blocks
            WHERE lang = 'en' AND kind IN ('paragraph', 'caption')
        """).df()

        dictionary = pd.read_csv(DICT_PATH, dtype=str)
        compiled = [(r.tag_type, r.tag, re.compile(r.pattern, re.IGNORECASE))
                    for r in dictionary.itertuples()]
        c_pat, c_names = country_patterns(con)

        rows = []
        for b in blocks.itertuples():
            for tag_type, tag, pat in compiled:
                hits = len(pat.findall(b.text))
                if hits:
                    rows.append((b.doc_id, b.page, b.block_no, tag_type, tag, "text", hits))
            found: dict[str, int] = {}
            for m in c_pat.finditer(b.text):
                iso3 = c_names[m.group(1)]
                found[iso3] = found.get(iso3, 0) + 1
            for iso3, hits in found.items():
                rows.append((b.doc_id, b.page, b.block_no, "country", iso3, "text", hits))
        tags = pd.DataFrame(rows, columns=["doc_id", "page", "block_no", "tag_type", "tag", "source", "hits"])

        # 資料の対象国（台帳の countries 列。複数国は ; 区切り）
        docs = con.execute("SELECT doc_id, countries FROM documents").df()
        doc_countries = (docs.assign(iso3=docs["countries"].fillna("").str.split(";"))
                         .explode("iso3").assign(iso3=lambda d: d["iso3"].str.strip())
                         .query("iso3 != ''")[["doc_id", "iso3"]])

        # 既存のテーブルと名前が衝突しないよう、別名で登録してから書き込む
        con.register("doc_countries_new", doc_countries)
        con.register("block_tags_new", tags)
        con.execute("CREATE OR REPLACE TABLE doc_countries AS SELECT * FROM doc_countries_new")
        con.execute("CREATE OR REPLACE TABLE block_tags AS SELECT * FROM block_tags_new")
        con.unregister("doc_countries_new")
        con.unregister("block_tags_new")

        con.execute("""
            CREATE OR REPLACE VIEW evidence AS
            SELECT b.doc_id, d.title, d.doc_type, d.pub_date, d.countries AS doc_countries,
                   b.page, b.block_no, b.kind, b.text,
                   list(DISTINCT t.tag ORDER BY t.tag) FILTER (WHERE t.tag_type = 'sector')  AS sectors,
                   list(DISTINCT t.tag ORDER BY t.tag) FILTER (WHERE t.tag_type = 'theme')   AS themes,
                   list(DISTINCT t.tag ORDER BY t.tag) FILTER (WHERE t.tag_type = 'country') AS countries_mentioned
            FROM doc_blocks b
            JOIN documents d USING (doc_id)
            LEFT JOIN block_tags t USING (doc_id, page, block_no)
            WHERE b.lang = 'en' AND b.kind IN ('paragraph', 'caption')
            GROUP BY ALL
        """)

        # 国×セクター／テーマ: 資料の対象国の段落、または本文でその国に言及した段落を数える
        for kind, name in (("sector", "country_sector_evidence"), ("theme", "country_theme_evidence")):
            con.execute(f"""
                CREATE OR REPLACE VIEW {name} AS
                WITH block_country AS (
                    SELECT DISTINCT b.doc_id, b.page, b.block_no, dc.iso3, 'document' AS basis
                    FROM doc_blocks b JOIN doc_countries dc USING (doc_id)
                    WHERE b.lang = 'en' AND b.kind IN ('paragraph', 'caption')
                    UNION
                    SELECT DISTINCT doc_id, page, block_no, tag AS iso3, 'mention' AS basis
                    FROM block_tags WHERE tag_type = 'country'
                )
                SELECT bc.iso3, t.tag AS {kind},
                       count(DISTINCT (t.doc_id, t.page, t.block_no)) AS n_paragraphs,
                       count(DISTINCT t.doc_id) AS n_documents,
                       count(DISTINCT (t.doc_id, t.page, t.block_no)) FILTER (WHERE bc.basis = 'document')
                           AS n_paragraphs_in_country_docs
                FROM block_tags t JOIN block_country bc USING (doc_id, page, block_no)
                WHERE t.tag_type = '{kind}'
                GROUP BY ALL
            """)

        n_blocks = len(blocks)
        tagged = tags.groupby(["doc_id", "page", "block_no"]).ngroups if len(tags) else 0
        print(f"対象の段落: {n_blocks:,} / タグが1つ以上付いた段落: {tagged:,}（{tagged / max(n_blocks, 1):.0%}）")
        for tt in ("sector", "theme"):
            print(f"\n{tt} ごとの段落数:")
            print(tags[tags.tag_type == tt].groupby("tag").size().sort_values(ascending=False).to_string())
        print("\n本文中で言及の多い国（上位15、段落数）:")
        print(tags[tags.tag_type == "country"].groupby("tag").size().sort_values(ascending=False).head(15).to_string())


if __name__ == "__main__":
    main()
