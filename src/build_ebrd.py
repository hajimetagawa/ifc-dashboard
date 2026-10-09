"""
EBRD（欧州復興開発銀行）が公表している2つのファイルから、分析用のデータセットを作る。

月次の更新手順:
    1. EBRD のサイトから2つのファイルをダウンロードする
         - 投資実績の Excel（Projects ページの "EBRD investments 1991–20xx"）
         - 案件一覧の CSV（Projects ページの "Download" ボタン。ファイル名は projectsData.csv）
    2. ダウンロードしたファイルを、名前を変えずに data/raw/ebrd/inbox/ に置く
    3. プロジェクトのフォルダで実行する:
         uv run python src/build_ebrd.py

    inbox のファイルは中身から種類を判定し、日付つきの名前で data/raw/ebrd/ に移して保管する
    （同じ日付のファイルがあれば上書き）。そのうえで、それぞれ最新のファイルからデータセットを作り直す。
        - Excel : ebrd_investments_asof_YYYYMMDD.xlsx（日付は Excel の表紙にある基準日）
        - CSV   : ebrd_projects_YYYYMMDD.csv        （日付はダウンロードした日＝ファイルの更新日）

    inbox に何もなければ、保管済みの最新ファイルから作り直すだけ。

出力（data/curated/ifc.duckdb。IFC のデータと同じデータベースに ebrd_ で始まる名前で入れる）:
    ebrd_operations             : 投資案件（Excel の List シート）。1行＝案件 × 国（複数国向けの案件は国ごとの配分）
    ebrd_operation_instruments  : 上の金額を、融資（Debt）・出資（Equity）・保証（Guarantee）に分けた縦長の表
    ebrd_projects               : 案件一覧（CSV）。1行＝案件。ステータスと案件ページへのリンクを持つ
    ebrd_project_status_history : 保管しているすべての CSV から作る、案件ステータスの月次の履歴
    ebrd_status_changes         : 直近2回の CSV の間でステータスが変わった案件（ビュー）

2つのファイルの関係:
    Excel には金額と金融手段の内訳があるが、案件番号とステータスがない。
    CSV には案件番号とステータスがあるが、金額がない。
    そこで、案件名（表記を正規化したもの）と国で照合し、Excel の案件に CSV の案件番号とステータスを付ける。
    照合できるのは、おもに案件ページのある案件。銀行経由の小口の融資（RSF など）は CSV に載らないため照合できない。

金額の注意（ダッシュボードと README にも書く）:
    - Excel の金額は「純累計投資額（NCBI）」の考え方で、取消・売却を差し引き、後から追加された分を含む。
      年は「最初の署名日」。IFC の「承認時点の承認額・承認年度」とは定義が違う。
    - 単位は百万ユーロ（月末の為替レート）。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import unicodedata
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "ebrd"
INBOX = RAW_DIR / "inbox"
DB_PATH = ROOT / "data" / "curated" / "ifc.duckdb"
WB_DIR = ROOT / "data" / "raw" / "structured"
ALIASES = ROOT / "sources" / "country_aliases.csv"

# EBRD の業種（Excel の13区分）→ 業種グループ（EBRD の Summary シートと同じ3区分）
SECTOR_GROUPS = {
    "Equity Funds": "Corporate",
    "Food and Agribusiness": "Corporate",
    "Manufacturing & Services": "Corporate",
    "Natural Resources": "Corporate",
    "Real Estate": "Corporate",
    "Telecommunications, Media and Technology": "Corporate",
    "Depository Credit (banks)": "Financial Institutions",
    "Insurance, Pension, Mutual Funds": "Financial Institutions",
    "Leasing Finance": "Financial Institutions",
    "Non-depository Credit (non-bank)": "Financial Institutions",
    "Energy": "Sustainable Infrastructure",
    "Municipal & Env Inf": "Sustainable Infrastructure",
    "Transport": "Sustainable Infrastructure",
}

# CSV のステータス → 段階（IFC の portfolio_stage と同じ名前にそろえる）。照合は小文字・前後の空白なしで行う
STATUS_STAGE = {
    "signed": "Active portfolio", "disbursing": "Active portfolio", "repaying": "Active portfolio",
    "active": "Active portfolio",
    "complete": "Completed", "completed": "Completed",
    "approved": "Pipeline", "passed final review": "Pipeline", "passed structure review": "Pipeline",
    "passed concept review": "Pipeline", "concept reviewed": "Pipeline", "exploratory": "Pipeline",
    "in exploratory": "Pipeline", "pending final review": "Pipeline",
    "cancelled": "Cancelled",
    "inactive": "Other",
}
PROJECT_TYPES = {"Investment project", "Technical Cooperation project"}
INSTRUMENTS = {"debt_eur_m": "Debt", "equity_eur_m": "Equity", "guarantee_eur_m": "Guarantee"}


# ---------------------------------------------------------------- 共通
def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def norm_name(s: str) -> str:
    """案件名の照合用の正規化（大文字小文字・記号・アクセントの違いを無視する）。"""
    s = unicodedata.normalize("NFKD", re.sub(r"[’\'`]", " ", str(s))).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def norm_country(s: str) -> str:
    s = unicodedata.normalize("NFKD", re.sub(r"[’\'`]", " ", str(s))).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]+", " ", s).strip()


def country_lookup() -> dict[str, str]:
    """国名 → ISO3。世界銀行の国名と sources/country_aliases.csv の別名を使う。"""
    files = sorted(WB_DIR.glob("wb_countries_*.json"))
    if not files:
        raise SystemExit("世界銀行の国情報がありません。先に src/build_country_ref.py を実行してください")
    lookup = {}
    for x in json.loads(files[-1].read_text(encoding="utf-8")):
        if (x.get("region") or {}).get("value", "").strip() == "Aggregates":
            continue
        lookup[norm_country(x["name"])] = x["id"]
    if ALIASES.exists():
        for r in pd.read_csv(ALIASES, dtype=str).itertuples():
            lookup[norm_country(r.alias)] = r.iso3
    return lookup


def to_iso3(names: pd.Series, lookup: dict[str, str], label: str) -> pd.Series:
    names = names.fillna("")
    out = names.map(lambda n: lookup.get(norm_country(n)))
    regional = names.str.strip().str.lower().isin(["regional", "global", ""])
    missing = sorted(set(names[out.isna() & ~regional]))
    if missing:
        print(f"  ! {label}: 国コードに変換できない国名 {missing} → sources/country_aliases.csv に追加してください")
    return out


# ---------------------------------------------------------------- 1. inbox の取り込み
def kind_of(path: Path) -> str | None:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            # with で開いてすぐ閉じる（開いたままだと、Windows ではこの後のファイルの移動ができない）
            with pd.ExcelFile(path) as x:
                if "List" in x.sheet_names:
                    return "investments"
        except Exception:  # noqa: BLE001
            return None
    if path.suffix.lower() == ".csv":
        head = path.read_text(encoding="utf-8-sig", errors="ignore").splitlines()[:1]
        if head and "Project ID" in head[0] and "Project status" in head[0]:
            return "projects"
    return None


def investments_as_of(path: Path) -> date:
    """Excel の表紙（Cover シート）の "As at 31 December 2025" から基準日を読む。"""
    cover = pd.read_excel(path, sheet_name=0, header=None)
    for v in cover.iloc[:, 0].dropna().astype(str):
        m = re.search(r"As at (\d{1,2} \w+ \d{4})", v)
        if m:
            return datetime.strptime(m.group(1), "%d %B %Y").date()
    raise SystemExit(f"{path.name}: 表紙に基準日（As at ...）が見つかりません")


def ingest_inbox() -> None:
    INBOX.mkdir(parents=True, exist_ok=True)
    files = [p for p in INBOX.iterdir() if p.is_file() and not p.name.startswith(".")]
    if not files:
        print("inbox に新しいファイルはありません。保管済みの最新ファイルから作り直します。")
        return
    for p in files:
        kind = kind_of(p)
        if kind == "investments":
            target = RAW_DIR / f"ebrd_investments_asof_{investments_as_of(p):%Y%m%d}.xlsx"
        elif kind == "projects":
            downloaded = datetime.fromtimestamp(p.stat().st_mtime).date()
            target = RAW_DIR / f"ebrd_projects_{downloaded:%Y%m%d}.csv"
        else:
            print(f"  ! {p.name}: EBRD のファイルとして認識できないため、inbox に残します")
            continue
        note = ""
        if target.exists():
            note = "（同じ内容のため置き換え）" if sha256(target) == sha256(p) else "（同じ日付のファイルを上書き）"
        os.replace(p, target)
        print(f"取り込み: {p.name} → {target.relative_to(ROOT)} {note}")


def latest(pattern: str) -> Path:
    files = sorted(RAW_DIR.glob(pattern))
    if not files:
        raise SystemExit(f"data/raw/ebrd/ に {pattern} がありません。inbox にファイルを置いてください")
    return files[-1]


def date_in_name(path: Path) -> date:
    return datetime.strptime(re.search(r"(\d{8})", path.stem).group(1), "%Y%m%d").date()


# ---------------------------------------------------------------- 2. Excel（投資実績）
def read_operations(path: Path, lookup: dict[str, str]) -> tuple[pd.DataFrame, float | None]:
    raw = pd.read_excel(path, sheet_name="List", header=None)
    # 見出しの行（"Country" で始まる行）を探す。上に説明の行があり、位置が変わることがあるため
    hdr = raw.index[raw.iloc[:, 0].astype(str).str.strip().eq("Country")][0]
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr]]
    total_row = df[df["Country"].astype(str).str.contains("Total", case=False, na=False)]
    published_total = float(total_row["EBRD Finance"].iloc[0]) / 1e6 if len(total_row) else None
    df = df[df["Operation Name"].notna() & ~df.index.isin(total_row.index)]

    eur_m = lambda c: pd.to_numeric(df[c], errors="coerce").fillna(0) / 1e6   # 元はユーロ単位
    ops = pd.DataFrame({
        "operation_name": df["Operation Name"].astype(str).str.strip(),
        "country_reported": df["Country"].astype(str).str.strip(),
        "allocation": df["Direct/Regional"].astype(str).str.strip(),          # Direct / Regional
        "portfolio_class": df["Portfolio Class"].astype(str).str.strip().str.title(),   # Private / State
        "sector": df["Sector"].astype(str).str.strip(),
        "signing_date": pd.to_datetime(df["Original Signing Date"], errors="coerce").dt.date,
        "ebrd_finance_eur_m": eur_m("EBRD Finance"),
        "debt_eur_m": eur_m("EBRD Finance - Debt"),
        "equity_eur_m": eur_m("EBRD Finance - Equity"),
        "guarantee_eur_m": eur_m("EBRD Finance - Guarantee"),
    })
    # 表示用の国名（Excel は大文字表記のため。"Bosnia and Herzegovina" のように前置詞などは小文字にする）
    ops["country"] = ops["country_reported"].str.title().str.replace(r"\b(And|Of|The)\b",
                                                                     lambda m: m.group(1).lower(), regex=True)
    ops["iso3"] = to_iso3(ops["country_reported"], lookup, "Excel")
    ops["signing_year"] = pd.to_datetime(ops["signing_date"]).dt.year.astype("Int64")
    ops["sector_group"] = ops["sector"].map(SECTOR_GROUPS).fillna("Other")
    unknown = sorted(set(ops.loc[ops["sector_group"].eq("Other"), "sector"]))
    if unknown:
        print(f"  ! Excel: 業種グループが未定義の業種 {unknown} → SECTOR_GROUPS に追加してください")
    ops["name_key"] = ops["operation_name"].map(norm_name)
    # 案件番号がないため、案件名・国・署名日から行のキーを作る（同じ組み合わせが重なれば連番を付ける）
    base = pd.Series([f"{n}|{c}|{d}" for n, c, d in zip(ops["name_key"], ops["country_reported"], ops["signing_date"])],
                     index=ops.index)
    seq = base.groupby(base).cumcount().astype(str)
    ops["operation_key"] = (base + "|" + seq).map(lambda s: hashlib.sha1(s.encode()).hexdigest()[:12])
    ops["as_of"] = investments_as_of(path)
    ops["source_file"] = path.name
    return ops.reset_index(drop=True), published_total


# ---------------------------------------------------------------- 3. CSV（案件一覧）
def read_projects_csv(path: Path, lookup: dict[str, str] | None = None) -> pd.DataFrame:
    """
    CSV はステータスにカンマが入っても引用符で囲まれていない行がある
    （例: In Exploratory, Pending Final Review）。そのため、両端の列の位置から読み取る。
    """
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header, body = rows[0], rows[1:]
    if header[:3] != ["Publication date", "Project ID", "Country"] or header[-1] != "URL link to project":
        raise SystemExit(f"{path.name}: 列の並びが想定と違います: {header}")
    recs = []
    for r in body:
        if len(r) < 10:
            continue
        url = r[-1] if r[-1].startswith("http") else None
        mid = r[3:-1] if url else r[3:]
        # mid = [Title..., Sector, Project type, Notice Type, ESIA available, Project status...]
        # 種類（Investment project / Technical Cooperation project）の列を目印にする。見つからなければ列の位置で読む
        k = next((i for i, v in enumerate(mid) if v.strip() in PROJECT_TYPES), 2)
        recs.append({
            "publication_date": r[0], "project_id": r[1].strip(), "country": r[2].strip(),
            "title": ",".join(mid[:k - 1]).strip(), "sector": mid[k - 1].strip(),
            "project_type": mid[k].strip(), "notice_type": mid[k + 1].strip(),
            "esia_available": mid[k + 2].strip(), "status": ",".join(mid[k + 3:]).strip(),
            "psd_url": url,
        })
    p = pd.DataFrame(recs).replace({"": None})
    p["publication_date"] = pd.to_datetime(p["publication_date"], format="%d %b %Y", errors="coerce").dt.date
    p["status"] = p["status"].str.replace(r"\s+", " ", regex=True)
    p["stage"] = p["status"].map(stage_of)
    if lookup is not None:
        p["iso3"] = to_iso3(p["country"], lookup, "CSV")
    p["name_key"] = p["title"].fillna("").map(norm_name)
    p["as_of"] = date_in_name(path)
    p["source_file"] = path.name
    return p


def stage_of(status: str | None) -> str | None:
    if not status:
        return None
    s = status.strip().lower()
    if s in STATUS_STAGE:
        return STATUS_STAGE[s]
    if "com approved" in s or "tc approved" in s:          # 技術協力（TC）・特別基金の承認
        return "Other"
    first = s.split(",")[0].strip()                           # "In Exploratory, Pending Final Review" など
    return STATUS_STAGE.get(first, "Other")


# ---------------------------------------------------------------- 4. 照合
def link(ops: pd.DataFrame, proj: pd.DataFrame) -> pd.DataFrame:
    """Excel の案件に、CSV の案件番号・ステータスを付ける（案件名＋国で照合）。"""
    inv = proj[proj["project_type"].eq("Investment project") & proj["name_key"].ne("")]
    cand = ops[["operation_key", "name_key", "iso3", "allocation"]].merge(
        inv[["name_key", "project_id", "iso3", "country"]].rename(columns={"iso3": "p_iso3"}), on="name_key")
    same_country = (cand["iso3"].notna() & cand["iso3"].eq(cand["p_iso3"])) | \
                   (cand["allocation"].eq("Regional") & cand["country"].eq("Regional"))
    # 1) 名前と国が一致し、候補が1件
    a = cand[same_country].groupby("operation_key").filter(lambda g: g["project_id"].nunique() == 1)
    a = a.drop_duplicates("operation_key").assign(link_method="Name and country")
    # 2) 名前だけが一致し、候補が1件（国の表記が違う・配分先の国になっている場合など）
    rest = cand[~cand["operation_key"].isin(a["operation_key"])]
    b = rest.groupby("operation_key").filter(lambda g: g["project_id"].nunique() == 1)
    b = b.drop_duplicates("operation_key").assign(link_method="Name only")
    m = pd.concat([a, b])[["operation_key", "project_id", "link_method"]]
    out = ops.merge(m, on="operation_key", how="left")
    out = out.merge(proj.dropna(subset=["project_id"])[["project_id", "status", "stage", "psd_url"]]
                    .drop_duplicates("project_id"),
                    on="project_id", how="left")
    out["link_method"] = out["link_method"].fillna("Not linked")
    out["stage"] = out["stage"].fillna("Unknown (not linked)")
    return out


# ---------------------------------------------------------------- 5. 保存
def store(con: duckdb.DuckDBPyConnection, name: str, df: pd.DataFrame) -> None:
    con.register("_df", df)
    con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _df")
    con.unregister("_df")


def main() -> None:
    ap = argparse.ArgumentParser(description="EBRD の公表データから分析用データセットを作る")
    ap.add_argument("--no-ingest", action="store_true", help="inbox を見ずに、保管済みの最新ファイルから作る")
    args = ap.parse_args()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if not args.no_ingest:
        ingest_inbox()
    inv_file, csv_file = latest("ebrd_investments_asof_*.xlsx"), latest("ebrd_projects_*.csv")
    print(f"\n使用するファイル: {inv_file.name} / {csv_file.name}")

    lookup = country_lookup()
    ops, published_total = read_operations(inv_file, lookup)
    proj = read_projects_csv(csv_file, lookup)
    ops = link(ops, proj)

    cols = ["operation_key", "operation_name", "country", "iso3", "country_reported", "allocation",
            "portfolio_class", "sector_group", "sector", "signing_date", "signing_year",
            "ebrd_finance_eur_m", "debt_eur_m", "equity_eur_m", "guarantee_eur_m",
            "project_id", "status", "stage", "link_method", "psd_url", "as_of", "source_file"]
    ops = ops[cols]
    inst = ops.melt(id_vars=["operation_key"], value_vars=list(INSTRUMENTS),
                    var_name="instrument", value_name="amount_eur_m")
    inst["instrument"] = inst["instrument"].map(INSTRUMENTS)
    inst = inst[inst["amount_eur_m"] != 0].reset_index(drop=True)

    # すべての CSV からステータスの履歴を作る（何度実行しても同じ結果になる）
    hist = pd.concat([read_projects_csv(f)[["as_of", "project_id", "status", "stage"]]
                      for f in sorted(RAW_DIR.glob("ebrd_projects_*.csv"))], ignore_index=True)
    hist = hist.rename(columns={"as_of": "snapshot_date"}).drop_duplicates(["snapshot_date", "project_id"])

    proj_cols = ["project_id", "title", "country", "iso3", "sector", "project_type", "notice_type", "status",
                 "stage", "esia_available", "publication_date", "psd_url", "as_of", "source_file"]
    with duckdb.connect(str(DB_PATH)) as con:
        store(con, "ebrd_operations", ops)
        store(con, "ebrd_operation_instruments", inst)
        store(con, "ebrd_projects", proj[proj_cols])
        store(con, "ebrd_project_status_history", hist)
        # 日付の列は日付型にそろえる（pandas から渡すと日時型になることがあるため）
        for tbl, cols_ in {"ebrd_operations": ["signing_date", "as_of"],
                           "ebrd_projects": ["publication_date", "as_of"],
                           "ebrd_project_status_history": ["snapshot_date"]}.items():
            for c in cols_:
                con.execute(f"ALTER TABLE {tbl} ALTER {c} TYPE DATE")
        con.execute("""
            CREATE OR REPLACE VIEW ebrd_status_changes AS
            WITH d AS (SELECT DISTINCT snapshot_date FROM ebrd_project_status_history
                       ORDER BY snapshot_date DESC LIMIT 2),
                 cur AS (SELECT * FROM ebrd_project_status_history WHERE snapshot_date = (SELECT max(snapshot_date) FROM d)),
                 prev AS (SELECT * FROM ebrd_project_status_history WHERE snapshot_date = (SELECT min(snapshot_date) FROM d))
            SELECT cur.project_id, prev.snapshot_date AS previous_date, prev.status AS previous_status,
                   cur.snapshot_date AS current_date, cur.status AS current_status, cur.stage AS current_stage
            FROM cur LEFT JOIN prev USING (project_id)
            WHERE (SELECT count(*) FROM d) = 2 AND prev.status IS DISTINCT FROM cur.status
        """)

    # ------------------------------------------------------------ 確認用の表示
    total = ops["ebrd_finance_eur_m"].sum()
    parts = ops[list(INSTRUMENTS)].sum().sum()
    print(f"\nebrd_operations: {len(ops):,} 行（案件 {ops['operation_name'].nunique():,} 件）"
          f"・合計 €{total:,.0f}m・署名 {ops['signing_year'].min()}〜{ops['signing_year'].max()}年・基準日 {ops['as_of'].iloc[0]}")
    if published_total is not None:
        ok = abs(total - published_total) < 1
        print(f"  Excel の合計行との照合: €{published_total:,.0f}m → {'一致' if ok else '不一致（要確認）'}")
    print(f"  金融手段の内訳の合計との照合: {'一致' if abs(total - parts) < 1 else f'差 €{total - parts:,.1f}m（要確認）'}")
    print(f"  官民の別: " + ", ".join(f"{k} €{v:,.0f}m" for k, v in
                                    ops.groupby("portfolio_class")["ebrd_finance_eur_m"].sum().items()))
    print(f"  国コードなし: {ops['iso3'].isna().sum()} 行（"
          + ", ".join(sorted(set(ops.loc[ops['iso3'].isna(), 'country_reported']))) + "）")
    linked = ops["link_method"].ne("Not linked")
    print(f"  CSV との照合: {linked.mean():.0%} の行・金額の {ops.loc[linked, 'ebrd_finance_eur_m'].sum() / total:.0%}"
          f"（名前と国 {ops['link_method'].eq('Name and country').sum():,}・名前のみ {ops['link_method'].eq('Name only').sum():,}）")
    recent = ops[ops["signing_year"] >= ops["signing_year"].max() - 4]
    print(f"    直近5年の署名分: {recent['link_method'].ne('Not linked').mean():.0%} の行")
    print("  段階（CSV のステータスによる。金額）: " + ", ".join(
        f"{k} €{v:,.0f}m" for k, v in ops.groupby("stage")["ebrd_finance_eur_m"].sum().sort_values(ascending=False).items()))
    print(f"\nebrd_projects: {len(proj):,} 件（基準日 {proj['as_of'].iloc[0]}）")
    print("  種類: " + ", ".join(f"{k} {v:,}" for k, v in proj["project_type"].value_counts().items()))
    print("  段階: " + ", ".join(f"{k} {v:,}" for k, v in proj["stage"].fillna("(no status)").value_counts().items()))
    other = proj.loc[proj["stage"].eq("Other"), "status"].value_counts()
    if len(other):
        print("  段階 Other のステータス: " + ", ".join(f"{k} ({v})" for k, v in other.items()))
    print(f"\nebrd_project_status_history: {hist['snapshot_date'].nunique()} 回分の CSV・{len(hist):,} 行")


if __name__ == "__main__":
    main()
