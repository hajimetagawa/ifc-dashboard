"""
台帳（sources/pdf_manifest.csv）に登録されたPDFから、本文をページ・ブロック単位で抽出してDuckDBに格納する。

使い方（プロジェクトのフォルダで実行）:
    uv run python src/extract_pdfs.py            # 新規・差し替えられたPDFだけを処理
    uv run python src/extract_pdfs.py --force    # 全件を処理し直す

新しいPDFを追加するとき:
    1. PDFを data/raw/pdf/ に置く
    2. sources/pdf_manifest.csv に1行追記する（doc_id は一意な名前にする）
    3. このスクリプトを実行する

出力（data/curated/ifc.duckdb）:
    documents  : 資料ごとの情報（台帳の内容＋ページ数・ハッシュ値・処理日時）
    doc_blocks : 本文のブロック（段落のまとまり）。資料・ページ・位置・言語・種類付き
    doc_text   : 本文として使うブロックだけに絞ったビュー
                 （英語・段落・ヘッダー/フッター以外）

ブロックの種類（kind）:
    paragraph   … 12語以上の文章
    short       … 見出し、図表のラベルなど
    caption     … 図表の見出し・出典・注記（Figure / Table / Source / Note などで始まるもの）
    boilerplate … 多くのページに繰り返し出てくる文字列（ヘッダー、フッター、ページ番号など）や、
                  複数の資料に同じ文章で出てくる定型文（About IFC など）
"""

from __future__ import annotations

import argparse
import hashlib
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd
import pymupdf

ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "data" / "raw" / "pdf"
MANIFEST = ROOT / "sources" / "pdf_manifest.csv"
BOILERPLATE_PHRASES = ROOT / "sources" / "boilerplate_phrases.csv"
DB_PATH = ROOT / "data" / "curated" / "ifc.duckdb"

PARAGRAPH_MIN_WORDS = 12
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
LATIN = re.compile(r"[A-Za-z]")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_block(text: str) -> str:
    """ブロック内の改行・ハイフネーション・重複行を整える。"""
    lines = [ln.strip() for ln in text.replace("­", "").splitlines()]
    lines = [ln for ln in lines if ln]
    # 同じ行が連続する場合（太字表現のための重ね書き）は1行にする
    dedup: list[str] = []
    for ln in lines:
        if not dedup or ln != dedup[-1]:
            dedup.append(ln)
    out = ""
    for ln in dedup:
        if out.endswith("-") and ln[:1].islower():
            out = out[:-1] + ln          # 行末ハイフンで分割された単語をつなぐ
        else:
            out = f"{out} {ln}" if out else ln
    return re.sub(r"\s+", " ", out).strip()


def detect_lang(text: str) -> str:
    cyr, lat = len(CYRILLIC.findall(text)), len(LATIN.findall(text))
    if cyr == 0 and lat == 0:
        return "none"
    return "ru" if cyr > lat else "en"


def extract_document(path: Path, doc_id: str) -> tuple[int, pd.DataFrame]:
    rows = []
    with pymupdf.open(path) as doc:
        n_pages = doc.page_count
        for pno, page in enumerate(doc, start=1):
            # sort=True: 上から下・左から右の読み順に並べる（段組みを崩さない）
            for b in page.get_text("blocks", sort=True):
                x0, y0, x1, y1, text, block_no, block_type = b[:7]
                if block_type != 0:      # 0=テキスト、1=画像
                    continue
                t = clean_block(text)
                if not t:
                    continue
                rows.append({
                    "doc_id": doc_id, "page": pno, "block_no": int(block_no),
                    "x0": round(x0, 1), "y0": round(y0, 1), "x1": round(x1, 1), "y1": round(y1, 1),
                    "text": t, "n_words": len(t.split()), "lang": detect_lang(t),
                })
    df = pd.DataFrame(rows)
    if df.empty:
        return n_pages, df

    # ヘッダー・フッターの判定: 数字を伏せた同じ短い文字列が、全ページの20%以上かつ3ページ以上に出てくるもの
    key = df["text"].str.replace(r"\d+", "#", regex=True)
    pages_per_key = df.assign(key=key).groupby("key")["page"].nunique()
    threshold = max(3, int(n_pages * 0.2))
    repeated = set(pages_per_key[pages_per_key >= threshold].index)
    is_boiler = key.isin(repeated) & (df["n_words"] < PARAGRAPH_MIN_WORDS)
    # ページ番号だけのブロック
    is_boiler |= df["text"].str.fullmatch(r"[\divxlcIVXLC\s\-–|/.]+")

    # 単語の大半が数字のブロック（図表の数値など）は段落として扱わない
    alpha_ratio = df["text"].str.split().apply(
        lambda ws: sum(any(ch.isalpha() for ch in w) for w in ws) / max(len(ws), 1))

    df["kind"] = "short"
    df.loc[(df["n_words"] >= PARAGRAPH_MIN_WORDS) & (alpha_ratio >= 0.5), "kind"] = "paragraph"
    # 図表の見出し・出典・注記（本文とは分けて扱う）
    is_caption = df["text"].str.match(r"(?i)^(figure|fig\.|table|box|chart|source|sources|note|notes)\b")
    df.loc[is_caption, "kind"] = "caption"
    df.loc[is_boiler, "kind"] = "boilerplate"
    return n_pages, df


def ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            doc_id VARCHAR PRIMARY KEY, file_name VARCHAR, title VARCHAR, publisher VARCHAR,
            doc_type VARCHAR, pub_date VARCHAR, countries VARCHAR, language VARCHAR, layout VARCHAR,
            source_url VARCHAR, downloaded_on VARCHAR, notes VARCHAR,
            n_pages INTEGER, sha256 VARCHAR, processed_at TIMESTAMP
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS doc_blocks (
            doc_id VARCHAR, page INTEGER, block_no INTEGER,
            x0 DOUBLE, y0 DOUBLE, x1 DOUBLE, y1 DOUBLE,
            text VARCHAR, n_words INTEGER, lang VARCHAR, kind VARCHAR
        )""")
    con.execute("""
        CREATE OR REPLACE VIEW doc_text AS
        SELECT b.*, d.title, d.doc_type, d.pub_date, d.countries
        FROM doc_blocks b JOIN documents d USING (doc_id)
        WHERE b.kind = 'paragraph' AND b.lang = 'en'
    """)


def main() -> None:
    ap = argparse.ArgumentParser(description="PDFの本文をブロック単位で抽出してDuckDBに格納")
    ap.add_argument("--force", action="store_true", help="処理済みのPDFも処理し直す")
    ap.add_argument("--db", type=Path, default=DB_PATH, help="出力先のDuckDBファイル")
    args = ap.parse_args()

    manifest = pd.read_csv(MANIFEST, dtype=str).fillna("")
    dups = manifest["doc_id"][manifest["doc_id"].duplicated()].tolist()
    if dups:
        raise SystemExit(f"台帳の doc_id が重複しています: {dups}")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(args.db)) as con:
        ensure_tables(con)
        done = dict(con.execute("SELECT doc_id, sha256 FROM documents").fetchall())

        # 台帳から削除された資料や include=no にした資料は、DBからも外す
        active = set(manifest.loc[manifest["include"].str.lower() != "no", "doc_id"])
        for gone in set(done) - active:
            con.execute("DELETE FROM doc_blocks WHERE doc_id = ?", [gone])
            con.execute("DELETE FROM documents WHERE doc_id = ?", [gone])
            print(f"除外  {gone}（台帳にない、または include=no）")

        # 台帳に登録されていないPDFを知らせる
        registered = set(manifest["file_name"])
        for f in sorted(PDF_DIR.glob("*.pdf")):
            if f.name not in registered:
                print(f"※台帳に未登録のPDF: {f.name}")

        for _, m in manifest.iterrows():
            if m["include"].lower() == "no":
                continue
            path = PDF_DIR / m["file_name"]
            if not path.exists():
                print(f"※ファイルが見つかりません: {m['doc_id']}（{m['file_name']}）")
                continue
            digest = sha256_of(path)
            if not args.force and done.get(m["doc_id"]) == digest:
                print(f"済み  {m['doc_id']}")
                continue

            try:
                n_pages, blocks = extract_document(path, m["doc_id"])
            except Exception as e:  # noqa: BLE001
                print(f"NG    {m['doc_id']}  {type(e).__name__}: {e}")
                continue

            con.execute("BEGIN")
            con.execute("DELETE FROM doc_blocks WHERE doc_id = ?", [m["doc_id"]])
            con.execute("DELETE FROM documents WHERE doc_id = ?", [m["doc_id"]])
            if not blocks.empty:
                con.execute("INSERT INTO doc_blocks SELECT doc_id, page, block_no, x0, y0, x1, y1, "
                            "text, n_words, lang, kind FROM blocks")
            con.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [m["doc_id"], m["file_name"], m["title"], m["publisher"], m["doc_type"], m["pub_date"],
                 m["countries"], m["language"], m["layout"], m["source_url"], m["downloaded_on"],
                 m["notes"], n_pages, digest, datetime.now(timezone.utc)],
            )
            con.execute("COMMIT")

            c = blocks["kind"].value_counts() if not blocks.empty else {}
            en_par = int(((blocks["kind"] == "paragraph") & (blocks["lang"] == "en")).sum()) if not blocks.empty else 0
            print(f"OK    {m['doc_id']}  {n_pages}ページ / ブロック {len(blocks):,}"
                  f"（段落 {c.get('paragraph', 0):,}・図表見出し {c.get('caption', 0):,}"
                  f"・短文 {c.get('short', 0):,}・ヘッダー等 {c.get('boilerplate', 0):,}）"
                  f" / 本文として使う英語の段落 {en_par:,}")
            if en_par == 0:
                print("      ※英語の段落が抽出できていません。スキャンPDFなどの可能性があります")

        # 資料をまたいで同じ文章が出てくる段落（「About IFC」などの定型文）を本文から外す
        # 判定: 先頭100文字が同じ段落が2つ以上の資料にある
        con.execute("""
            UPDATE doc_blocks SET kind = 'boilerplate'
            WHERE kind = 'paragraph' AND left(lower(text), 100) IN (
                SELECT left(lower(text), 100) FROM doc_blocks
                WHERE kind = 'paragraph' AND n_words >= 12
                GROUP BY 1 HAVING count(DISTINCT doc_id) >= 2
            )
        """)
        # 定型文の言い回し（sources/boilerplate_phrases.csv。. は任意の1文字に一致）
        if BOILERPLATE_PHRASES.exists():
            for phrase in pd.read_csv(BOILERPLATE_PHRASES, dtype=str)["phrase"].dropna():
                con.execute("UPDATE doc_blocks SET kind = 'boilerplate' "
                            "WHERE kind = 'paragraph' AND regexp_matches(lower(text), ?)",
                            [phrase.strip().lower()])
        n_shared = con.execute(
            "SELECT count(*) FROM doc_blocks WHERE kind = 'boilerplate' AND n_words >= 12").fetchone()[0]
        if n_shared:
            print(f"定型文と判定した段落（資料間で共通・定型の言い回し）: {n_shared:,}")

        total = con.execute("SELECT count(DISTINCT doc_id), count(*) FROM doc_text").fetchone()
        print(f"\n本文データ（doc_text）: {total[0]} 資料 / {total[1]:,} 段落")


if __name__ == "__main__":
    main()
