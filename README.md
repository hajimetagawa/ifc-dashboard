# IFC Dashboard

IFCが公表する構造化データ（Investment Services Projects等）とPDFレポートを統合し、
数値と定性情報をあわせて見られるインタラクティブ・ダッシュボードを構築するプロジェクト。

## フォルダ構成

| フォルダ | 内容 | Git管理 |
|---|---|---|
| `data/raw/pdf/` | ダウンロードしたPDF（加工しない） | しない |
| `data/raw/structured/` | ダウンロードしたCSV・JSON（加工しない） | しない |
| `data/extracted/` | PDFから抽出したMarkdown・表 | しない |
| `data/curated/` | 整形済みデータ（Parquet / DuckDB） | しない |
| `sources/` | 取得元の台帳（`pdf_manifest.csv`） | する |
| `src/` | 取得・抽出・整形のスクリプト | する |
| `notebooks/` | 探索・検証用ノートブック | する |
| `app/` | ダッシュボード（Streamlit） | する |

データはGitで管理しない代わりに、取得元URLと取得日を `sources/pdf_manifest.csv` に記録し、
スクリプトを再実行すればいつでも同じデータを再現できる状態を保つ。

## セットアップ（Windows / PowerShell）

```powershell
cd $HOME\Documents\Projects\ifc-dashboard
uv sync          # uv.lock どおりに仮想環境(.venv)を作成
```

## 主なコマンド

```powershell
# 1. IFC Investment Services Projects を取得・整形（APIから全件）
uv run python src/fetch_projects.py
# 2. 国ごとの地域区分（世界銀行・IFC）と所得区分を作成し、案件データに結合
uv run python src/build_country_ref.py

# PDFを一括でMarkdown/JSONLに変換
uv run python src/pdf_to_markdown.py data/raw/pdf data/extracted

# ライブラリの追加
uv add <ライブラリ名>
```

## データベース（data/curated/ifc.duckdb）

| 名前 | 種類 | 内容 |
|---|---|---|
| `projects` | テーブル | 開示文書単位（APIの元データと同じ粒度） |
| `projects_unique` | ビュー | 案件単位（1案件1行）。金額の集計はこちらを使う |
| `countries` | テーブル | 国名ごとの世界銀行の地域・所得区分、IFCの地域区分 |
| `projects_enriched` | ビュー | `projects_unique` に `countries` を結合したもの |

IFCの地域区分は、MCT（Middle East and Central Asia）のみIFC公式ページの国一覧に基づく。
それ以外は世界銀行の地域からの暫定割り当てで、根拠は `ifc_region_basis` 列に記録している。
対応表の元は `sources/ifc_regions.csv` と `sources/country_overrides.csv`。
