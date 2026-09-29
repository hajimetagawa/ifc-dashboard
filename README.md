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
# PDFを一括でMarkdown/JSONLに変換
uv run python src/pdf_to_markdown.py data/raw/pdf data/extracted

# ライブラリの追加
uv add <ライブラリ名>
```
