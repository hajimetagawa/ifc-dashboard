"""
PDF -> Markdown / JSONL 一括変換スクリプト（microsoft/markitdown 使用）

使い方（プロジェクトのフォルダで実行）:
    uv run python src/pdf_to_markdown.py data/raw/pdf data/extracted
    uv run python src/pdf_to_markdown.py data/raw/pdf data/extracted --no-normalize

出力:
    出力フォルダ/<PDF名>.md   … 1ファイルごとのMarkdown
    出力フォルダ/corpus.jsonl  … 全件を1行1レコードにまとめたデータ
    出力フォルダ/errors.log    … 変換に失敗したファイル（ある場合のみ）

注意:
    表の構造は保持されない（表は別途 pdfplumber で抽出する）。
    画像だけのPDFは本文が空になり、corpus.jsonl の is_empty が true になる。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from markitdown import MarkItDown


def normalize_text(text: str) -> str:
    """表記ゆれ・余分な空白を整える（数値・記号の全角半角も統一）。"""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t　]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def convert_folder(src: Path, dst: Path, normalize: bool = True) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    md = MarkItDown(enable_plugins=False)

    pdfs = sorted(p for p in src.rglob("*") if p.suffix.lower() == ".pdf")
    if not pdfs:
        print(f"PDFが見つかりません: {src}")
        return

    ok, failed = 0, []
    with (dst / "corpus.jsonl").open("w", encoding="utf-8") as jsonl:
        for pdf in pdfs:
            try:
                # ローカルファイルのみ扱うので convert_local を使う
                result = md.convert_local(str(pdf))
                text = result.markdown or ""
                if normalize:
                    text = normalize_text(text)

                rel = pdf.relative_to(src)
                out_md = (dst / rel).with_suffix(".md")
                out_md.parent.mkdir(parents=True, exist_ok=True)
                out_md.write_text(text, encoding="utf-8")

                record = {
                    "id": sha256_of(pdf)[:16],
                    "source_file": rel.as_posix(),
                    "title": result.title or pdf.stem,
                    "char_count": len(text),
                    "is_empty": len(text.strip()) == 0,
                    "converted_at": datetime.now(timezone.utc).isoformat(),
                    "content": text,
                }
                jsonl.write(json.dumps(record, ensure_ascii=False) + "\n")

                flag = "  ※テキストなし（画像PDFの可能性）" if record["is_empty"] else ""
                print(f"OK  {rel}  ({len(text):,}文字){flag}")
                ok += 1
            except Exception as e:  # noqa: BLE001
                failed.append(f"{pdf}\t{type(e).__name__}: {e}")
                print(f"NG  {pdf.name}  {type(e).__name__}: {e}")

    if failed:
        (dst / "errors.log").write_text("\n".join(failed) + "\n", encoding="utf-8")
    print(f"\n完了: 成功 {ok} 件 / 失敗 {len(failed)} 件 → {dst.resolve()}")


def main() -> None:
    ap = argparse.ArgumentParser(description="PDFをMarkdown/JSONLに一括変換")
    ap.add_argument("src", type=Path, help="PDFが入ったフォルダ（サブフォルダも対象）")
    ap.add_argument("dst", type=Path, help="出力先フォルダ")
    ap.add_argument("--no-normalize", action="store_true", help="テキスト正規化を行わない")
    args = ap.parse_args()
    convert_folder(args.src, args.dst, normalize=not args.no_normalize)


if __name__ == "__main__":
    main()
