# IFC Portfolio Explorer

An interactive dashboard that combines IFC's structured project data with text from IFC publications,
so that investment figures and the qualitative context behind them can be read side by side.

The default view focuses on **Financial Markets (FIG) projects in the Middle East and Central Asia (MCT)**,
but every filter can be changed to cover other regions and industries.

## What it does

1. **Project data** – downloads all records from the *IFC Investment Services Projects* dataset
   (World Bank Group Finances One, dataset DS00499) through its public API, cleans them and stores them in DuckDB.
2. **Country reference** – attaches World Bank regions, income groups and lending categories
   (World Bank country API) and IFC regions to each project.
3. **Report text** – extracts the body text of IFC reports (PDF) paragraph by paragraph, with page numbers,
   handling multi-column layouts and removing headers, footers and boilerplate.
4. **Tagging** – tags each paragraph with countries, industries and themes using editable keyword dictionaries,
   so that paragraphs can be joined to projects by country and industry.
5. **Dashboard** – a Streamlit app with an overview, country profiles (figures next to report text),
   a filterable project list and notes on the data.

All tools and data sources are free. No paid AI or cloud services are used.

## Important notes on the data

- **Portfolio stages.** `portfolio_stage` groups project status: *Active portfolio* = signed and ongoing
  (Active, Pending Disbursement) — the current exposure that needs the closest monitoring; *Completed* = the track
  record; *Pipeline* = Pending Signing or Pending Approval (not yet a legal commitment; amounts may change or be
  missing); *On hold* = Hold.
- **Business lines (who).** `business_line` (FIG, MAS, INF, Funds & Tech, Equity Portfolio, Other) is derived from
  the department name with keyword rules in `sources/business_line_rules.csv`. Assumptions: the recorded department
  is the unit responsible at some point after approval (not necessarily the originating unit), and business lines
  stay stable across reorganisations. About 1.4% of projects are *Other*. `dept_name` is the department without
  its unit code and sub-unit (`dept_subunit`).
- **Instruments (how).** `project_instruments` splits each project's approved amount into loan, equity, guarantee
  and risk-management amounts from the source data (a project can use several). The source `product_line` label
  is not used for amounts, because it often disagrees with these figures.

- **Amounts are Board-approved amounts** (US$ millions). They are not disbursements or outstanding balances.
  The dataset also includes pipeline projects pending approval or signing.
- **Program-level amounts are excluded from totals.** For some programs, such as the Global Trade Finance
  Program (GTFP), the program-wide approval ceiling (e.g. US$7bn) is recorded against each participating bank.
  Summed naively, these records account for about 59% of all approved amounts in the dataset.
  A record is treated as program-level if its name starts with "GTFP", or if the same amount (US$100m or more)
  was approved on the same date for four or more projects. Borderline cases were checked against IFC's
  disclosure pages and overridden in `sources/amount_overrides.csv` (e.g. four unrelated US$100m loans
  approved on the same day are kept as individual projects). The column `approved_usd_m_for_sum` holds the
  amount used for totals, and `amount_basis` records the reason.
- **Disclosure documents vs. projects.** The source data has one row per disclosure document; 17 projects have
  two documents. Use the `projects_unique` view (one row per project) for any aggregation.
- **Fiscal years.** IFC fiscal years run July–June. `board_fy` is the fiscal year of Board approval, or of the
  projected Board date for pipeline projects not yet approved (`board_fy_basis` = Approved / Projected).
  Use `board_fy` for time filters; otherwise all projects pending approval drop out.
- **IFC regions.** Middle East and Central Asia follows the country list published on IFC's website ("Official").
  Tajikistan and North African countries are confirmed from the IFC regional department that handles their
  projects ("Confirmed (IFC department)"). Other countries are assigned provisionally from the World Bank region
  (`ifc_region_basis` shows which).
- **Current classifications.** World Bank regions and income groups are today's classifications, applied to all years.
- **Keyword tagging** shows that a paragraph mentions a topic, not that the topic is its main subject.
  Tables of contents, footnotes, bibliographies, table rows, repeated paragraphs and boilerplate are
  excluded before tagging.

## Project structure

| Folder | Contents | In Git |
|---|---|---|
| `data/raw/pdf/` | Downloaded PDF reports (never modified) | No |
| `data/raw/structured/` | Raw API responses (JSON) | No |
| `data/extracted/` | Markdown conversions of PDFs (optional) | No |
| `data/curated/` | Cleaned data (Parquet, DuckDB) | No |
| `sources/` | Report register, reference tables and dictionaries (CSV) | Yes |
| `src/` | Data pipeline scripts | Yes |
| `notebooks/` | Exploration notebooks | Yes |
| `app/` | Streamlit dashboard | Yes |

Data files are not committed. Everything in `data/` can be rebuilt by re-running the scripts;
the origin of each report is recorded in `sources/pdf_manifest.csv`.

## Setup

Requirements: Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/hajimetagawa/ifc-dashboard.git
cd ifc-dashboard
uv sync            # creates .venv with the exact versions in uv.lock
```

## Running the pipeline

```bash
uv run python src/fetch_projects.py      # 1. project data from the Finances One API
uv run python src/build_country_ref.py   # 2. regions, income groups, IFC regions
uv run python src/extract_pdfs.py        # 3. text from PDF reports in data/raw/pdf/
uv run python src/tag_blocks.py          # 4. country / industry / theme tags
uv run python src/export_powerbi.py      # 5. (optional) Parquet files for Power BI
uv run streamlit run app/app.py          # dashboard at http://localhost:8501
```

Useful options:

- `fetch_projects.py --input <raw.json>` re-processes a saved API response without calling the API.
- `build_country_ref.py --input <raw.json>` does the same for the World Bank country data.
- `extract_pdfs.py` only processes new or changed PDFs; `--force` re-processes all of them.

After rebuilding the data, restart the dashboard (it caches data while running).

## Adding PDF reports

1. Put the PDF in `data/raw/pdf/`.
2. Add one row to `sources/pdf_manifest.csv` (`doc_id` must be unique; set `include` to `no` to exclude a report).
   The `countries` column takes ISO3 codes separated by `;`.
3. Run `extract_pdfs.py` and then `tag_blocks.py`.

Files in `data/raw/pdf/` that are not in the register are reported when `extract_pdfs.py` runs.

## Reference tables and dictionaries (`sources/`)

| File | Purpose |
|---|---|
| `pdf_manifest.csv` | Register of PDF reports: title, publisher, type, date, countries, language, layout |
| `ifc_regions.csv` | IFC region by country, with status (Official / Confirmed (IFC department) / Provisional / Not assigned) and evidence |
| `country_overrides.csv` | Manual mapping for country names that cannot be matched automatically, including regional projects |
| `business_line_rules.csv` | Keyword rules mapping department names to business lines (first match wins, by `priority`) |
| `amount_overrides.csv` | Manual override of the program-level amount classification (`Individual` or `Program-level`) |
| `tag_dictionary.csv` | Regular expressions for industry tags (named as in the project data) and theme tags |
| `country_aliases.csv` | Alternative country names used in report text (e.g. Turkey / Türkiye) |
| `boilerplate_phrases.csv` | Phrases that mark boilerplate paragraphs (About IFC, copyright notices, disclaimers) |

Edit these files and re-run the relevant script to apply changes.

## Database (`data/curated/ifc.duckdb`)

| Name | Type | Contents |
|---|---|---|
| `projects` | table | One row per disclosure document (same granularity as the API) |
| `projects_unique` | view | One row per project; use this for aggregation |
| `project_instruments` | table | Approved amount per project and instrument (loan, equity, guarantee, risk management) |
| `countries` | table | World Bank region, income group, lending category and IFC region by country name |
| `projects_enriched` | view | `projects_unique` joined with `countries` (used by the dashboard) |
| `documents` | table | One row per PDF report, with page count and file hash |
| `doc_blocks` | table | Text blocks with page, position, language and kind (paragraph, caption, short, boilerplate, toc, reference, note, table, duplicate) |
| `doc_text` | view | English body paragraphs only |
| `doc_countries` | table | Countries covered by each report |
| `block_tags` | table | Country, industry and theme tags per paragraph |
| `evidence` | view | Paragraph text with its tags in one row (used by the dashboard) |
| `country_sector_evidence` | view | Paragraphs and reports per country and industry; joins to `projects_enriched` on `iso3` and `industry` |
| `country_theme_evidence` | view | Paragraphs and reports per country and theme |

## Dashboard

- **Overview** – a one-sentence answer to "who commits how much, through which instruments, where and in which
  sectors", committed and pipeline totals, four breakdowns (who / how / where / what), a flow diagram from
  business line or department through instrument to region or country and industry, and a fiscal-year trend.
- **Drill down** – an icicle chart to click from the whole portfolio down to department, country, industry and
  instrument, and step-by-step selectors that lead to the individual projects behind each figure.
- **Country profile** – projects by fiscal year and product, the largest projects with links to IFC's
  disclosure pages, and paragraphs from IFC reports filtered by theme and by the selected industry,
  with report title and page number.
- **Project list** – all projects matching the filters, downloadable as CSV.
- **Notes on the data** – the caveats above, for readers of the dashboard.

## Using the data in Power BI

`src/export_powerbi.py` writes a star schema of Parquet files to `data/curated/powerbi/`,
so the same data can be analysed in Power BI Desktop without connecting to DuckDB directly
(the export opens the database read-only).

| File | Grain | Key |
|---|---|---|
| `projects.parquet` | one row per project | `project_number` |
| `project_instruments.parquet` | project × instrument | – |
| `countries.parquet` | one row per country, including countries without IFC projects | `iso3` |
| `industries.parquet` | one row per industry | `industry` |
| `documents.parquet` | one row per report | `doc_id` |
| `paragraphs.parquet` | one row per paragraph | `paragraph_id` |
| `paragraph_countries.parquet` | paragraph × country (report country or mentioned in text) | – |
| `paragraph_industries.parquet` | paragraph × industry tag | – |
| `paragraph_themes.parquet` | paragraph × theme tag | – |

Relationships (many-to-one):

- `projects[iso3]` → `countries[iso3]`; `projects[industry]` → `industries[industry]`
- `project_instruments[project_number]` → `projects[project_number]`
- `paragraphs[doc_id]` → `documents[doc_id]`
- `paragraph_countries`, `paragraph_industries`, `paragraph_themes` → `paragraphs[paragraph_id]`
  (set cross-filter direction to *Both*)
- `paragraph_countries[iso3]` → `countries[iso3]`; `paragraph_industries[industry]` → `industries[industry]`

Use `approved_usd_m_for_sum` for amount measures, e.g. `Approved (US$m) = SUM(projects[approved_usd_m_for_sum])`.
Summing `total_approved_usd_m` includes program-level ceilings and overstates totals.

## Tech stack

Python 3.14 · uv · DuckDB · pandas · PyArrow · PyMuPDF · pdfplumber · MarkItDown · Streamlit · Plotly

PyMuPDF is licensed under AGPL-3.0; check its terms before distributing this code in a closed-source form.

## Data sources

- IFC Investment Services Projects – World Bank Group Finances One (DS00499):
  https://financesone.worldbank.org/api-explorer?id=DS00499
- World Bank country API: https://api.worldbank.org/v2/country
- IFC regional page (Middle East and Central Asia): https://www.ifc.org/en/where-we-work/middle-east
- IFC publications as listed in `sources/pdf_manifest.csv`

Use of the data is subject to the terms of the respective sources.
