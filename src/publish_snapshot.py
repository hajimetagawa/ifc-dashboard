"""
公開用のデータのスナップショット（data/published/ifc.duckdb）を作る。

公開版のダッシュボード（Streamlit Community Cloud）は data/curated/ を持たないため（Git で管理していない）、
このスナップショットを Git に記録して公開版に渡す。データを更新して公開版にも反映したいときに実行する。

使い方（プロジェクトのフォルダで、ダッシュボードを止めてから実行）:
    uv run python src/publish_snapshot.py                 # すべてのデータを含める
    uv run python src/publish_snapshot.py --without-ebrd  # EBRD のデータを含めない

    その後: git add data/published && git commit -m "Update published data" && git push

注意:
    - EBRD のサイトの利用規約は、内容の利用を「個人的・非商業的な利用」に限り、再配布や二次的著作物の作成を
      認めていない。公開版に EBRD のデータを含めるかどうかは、規約を確認したうえで判断する。
    - IFC の案件データ（World Bank Finances One、DS00499）は CC BY 4.0。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "curated" / "ifc.duckdb"
OUT_DIR = ROOT / "data" / "published"
OUT = OUT_DIR / "ifc.duckdb"


def main() -> None:
    ap = argparse.ArgumentParser(description="公開用のデータのスナップショットを作る")
    ap.add_argument("--without-ebrd", action="store_true", help="EBRD のテーブルとビューを含めない")
    args = ap.parse_args()
    if not SRC.exists():
        raise SystemExit(f"{SRC} がありません。先にデータを作ってください")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp.duckdb")
    tmp.unlink(missing_ok=True)

    con = duckdb.connect(str(tmp))
    con.execute(f"ATTACH '{SRC.as_posix()}' AS src (READ_ONLY)")
    con.execute("COPY FROM DATABASE src TO " + con.execute("SELECT current_database()").fetchone()[0])
    con.execute("DETACH src")
    if args.without_ebrd:
        for (name, kind) in con.execute(
                "SELECT table_name, table_type FROM information_schema.tables WHERE table_name LIKE 'ebrd_%' "
                "ORDER BY table_type DESC").fetchall():      # ビューを先に消す
            con.execute(f"DROP {'VIEW' if kind == 'VIEW' else 'TABLE'} {name}")
    con.execute("CREATE OR REPLACE TABLE snapshot_info AS SELECT ? AS published_at, ? AS includes_ebrd",
                [datetime.now(timezone.utc).isoformat(timespec="seconds"), not args.without_ebrd])
    tables = con.execute("SELECT count(*) FROM information_schema.tables").fetchone()[0]
    con.execute("CHECKPOINT")
    con.close()
    tmp.replace(OUT)
    print(f"保存: {OUT.relative_to(ROOT)}（{OUT.stat().st_size / 1e6:.1f} MB、テーブルとビュー {tables} 個、"
          f"EBRD {'なし' if args.without_ebrd else 'あり'}）")
    print("次に: git add data/published && git commit -m \"Update published data\" && git push")


if __name__ == "__main__":
    main()
