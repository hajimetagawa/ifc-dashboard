"""
国に紐づく公開データを取得し、国別指標のテーブル（country_indicators）にまとめる。

使い方（プロジェクトのフォルダで実行）:
    uv run python src/fetch_country_data.py                   # 最新の元資料を取得し、DuckDBに格納する
    uv run python src/fetch_country_data.py --no-download     # 保存済みの最新の元資料から格納し直すだけ
    uv run python src/fetch_country_data.py --download-only   # 元資料を取得して保存するだけ

出力（data/curated/ifc.duckdb）:
    country_indicators : 国 × 指標 × 基準日の値（縦長の形式。データ源が増えても同じ形で追加する）
        iso3, indicator, indicator_name, value, value_label, as_of, source, source_url, note
    country_risk       : 国ごとの最新の値を横に並べたビュー（ダッシュボード用）

データ源は SOURCES に1つずつ登録する。新しいデータ源を加えるときは、
取得（download）と読み取り（parse）の関数を書き、SOURCES に1行追加する。

現在のデータ源:
    oecd_crc : OECD Country Risk Classification（国別リスク分類。0＝最も低い 〜 7＝最も高い）
               https://www.oecd.org/en/topics/sub-issues/country-risk-classification.html
               注意: OECDは「輸出信用の最低保険料率を決めるための分類であり、それ以外の用途を推奨しない」と
               明記している。ダッシュボードでは出典とこの注意を併記する。
"""

from __future__ import annotations

import argparse
import re
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd
import pdfplumber
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "country"
DB_PATH = ROOT / "data" / "curated" / "ifc.duckdb"

OECD_CRC_PAGE = "https://www.oecd.org/en/topics/sub-issues/country-risk-classification.html"
OECD_CRC_URLS = {
    "current": "https://www.oecd.org/content/dam/oecd/en/topics/policy-sub-issues/country-risk-classification/"
               "cre-crc-current-english.pdf",
    "historical": "https://www.oecd.org/content/dam/oecd/en/topics/policy-sub-issues/country-risk-classification/"
                  "cre-crc-historical-internet-english.pdf",
}
# OECDの国コードが世界銀行（このプロジェクトの基準）と異なる国
OECD_TO_WB_ISO3 = {"XKV": "XKX"}   # コソボ
# 分類されていない国の注記番号 → 表示用の理由
OECD_CRC_NOTES = {
    "5": "Not classified (not currently reviewed)",
    "6": "Not classified (high-income OECD country)",
    "7": "Not classified (high-income euro area country)",
    "8": "Not classified (euro-using micro-state)",
}


# ---------------------------------------------------------------- 共通
def download(url: str, name: str) -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0 (ifc-dashboard research)"})
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"PDFではない応答が返りました: {url}")
    path = RAW_DIR / f"{name}_{date.today():%Y%m%d}.pdf"
    path.write_bytes(r.content)
    print(f"保存: {path.relative_to(ROOT)}（{len(r.content) / 1024:,.0f} KB）")
    return path


def latest(name: str) -> Path:
    files = sorted(RAW_DIR.glob(f"{name}_*.pdf"))
    if not files:
        raise SystemExit(f"{name} の元資料がありません。--no-download を外して実行してください")
    return files[-1]


# ---------------------------------------------------------------- OECD 国別リスク分類（現行版）
def oecd_crc_download() -> None:
    download(OECD_CRC_URLS["current"], "oecd_crc_current")
    download(OECD_CRC_URLS["historical"], "oecd_crc_historical")


def oecd_crc_parse() -> pd.DataFrame:
    path = latest("oecd_crc_current")
    rows, as_of = [], None
    with pdfplumber.open(path) as pdf:
        m = re.search(r"Valid as of:\s*(\d{1,2} \w+ \d{4})", pdf.pages[0].extract_text() or "")
        if m:
            as_of = datetime.strptime(m.group(1), "%d %B %Y").date()
        for page in pdf.pages:
            for table in page.extract_tables():
                for r in table:
                    # 行の形: [番号, ISO3, 国名, 前回, 現在, 注記]
                    if not r or len(r) < 6 or not (r[0] or "").strip().isdigit():
                        continue
                    nb, iso3, name, prev, cur, notes = (str(x or "").strip() for x in r[:6])
                    if not re.fullmatch(r"[A-Z]{3}", iso3):
                        continue
                    iso3 = OECD_TO_WB_ISO3.get(iso3, iso3)
                    note_nums = re.findall(r"\((\d)\)", notes)
                    if cur.isdigit():
                        value, label = int(cur), f"Category {cur}"
                    else:
                        value = None
                        label = next((OECD_CRC_NOTES[n] for n in note_nums if n in OECD_CRC_NOTES), "Not classified")
                    rows.append({
                        "iso3": iso3, "indicator": "oecd_crc",
                        "indicator_name": "OECD country risk classification (0 = lowest, 7 = highest)",
                        "value": value, "value_label": label, "as_of": as_of,
                        "source": "OECD Country Risk Classifications of the Participants to the Arrangement on "
                                  "Officially Supported Export Credits",
                        "source_url": OECD_CRC_PAGE,
                        "note": f"name in source: {name}; previous: {prev}"
                                + (f"; notes: {notes}" if notes else ""),
                    })
    if as_of is None:
        raise RuntimeError(f"基準日（Valid as of）を読み取れませんでした: {path.name}")
    df = pd.DataFrame(rows)
    if df.empty or df["iso3"].duplicated().any():
        raise RuntimeError(f"表の読み取りに失敗しました（{len(df)} 行、重複あり）: {path.name}")
    print(f"OECD国別リスク分類: {len(df)} か国・地域（基準日 {as_of}、分類あり {df['value'].notna().sum()}）")
    return df


SOURCES = {
    "oecd_crc": {"download": oecd_crc_download, "parse": oecd_crc_parse},
}


# ---------------------------------------------------------------- 格納
def store(frames: list[pd.DataFrame]) -> None:
    new = pd.concat(frames, ignore_index=True)
    with duckdb.connect(str(DB_PATH)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS country_indicators (
                iso3 VARCHAR, indicator VARCHAR, indicator_name VARCHAR, value DOUBLE, value_label VARCHAR,
                as_of DATE, source VARCHAR, source_url VARCHAR, note VARCHAR
            )""")
        con.register("new_rows", new)
        # 同じ指標・同じ基準日の行は入れ替える（再実行しても重複しない）。過去の基準日の行は残す
        con.execute("""
            DELETE FROM country_indicators
            WHERE (indicator, as_of) IN (SELECT DISTINCT indicator, as_of FROM new_rows)
        """)
        con.execute("INSERT INTO country_indicators SELECT iso3, indicator, indicator_name, value, value_label, "
                    "as_of, source, source_url, note FROM new_rows")
        con.unregister("new_rows")
        # 国ごとの最新の値（ダッシュボード用）。指標が増えたら列を追加する
        con.execute("""
            CREATE OR REPLACE VIEW country_risk AS
            WITH latest AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY iso3, indicator ORDER BY as_of DESC) AS rn
                FROM country_indicators
            )
            SELECT iso3,
                   max(value)       FILTER (WHERE indicator = 'oecd_crc') AS oecd_crc,
                   max(value_label) FILTER (WHERE indicator = 'oecd_crc') AS oecd_crc_label,
                   max(as_of)       FILTER (WHERE indicator = 'oecd_crc') AS oecd_crc_as_of
            FROM latest WHERE rn = 1
            GROUP BY iso3
        """)
        n = con.execute("SELECT count(*) FROM country_indicators").fetchone()[0]
        print(f"country_indicators: {n:,} 行 / country_risk ビューを更新")


def main() -> None:
    ap = argparse.ArgumentParser(description="国に紐づく公開データを取得")
    ap.add_argument("--download-only", action="store_true", help="元資料の取得と保存だけを行う")
    ap.add_argument("--no-download", action="store_true", help="取得せず、保存済みの最新の元資料を使う")
    args = ap.parse_args()

    if not args.no_download:
        for name, src in SOURCES.items():
            src["download"]()
    if args.download_only:
        return
    store([src["parse"]() for src in SOURCES.values()])


if __name__ == "__main__":
    main()
