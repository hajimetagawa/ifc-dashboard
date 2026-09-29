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

- **Amounts are Board-approved amounts** (US$ millions). They are not disbursements or outstanding balances.
  The dataset also includes pipeline projects pending approval or signing.
- **Program-level amounts are excluded from totals.** For some programs, such as the Global Trade Finance
  Program (GTFP), the program-wide approval ceiling (e.g. US$7bn) is recorded against each participating bank.
  Summed naively, these records account for about 59% of all approved amounts in the dataset.
  A record is treated as program-level if its name starts with "GTFP", or if the same amount (US$100m or more)
  was approved on the same date for four or more projects. The column `approved_usd_m_for_sum` holds the
  amount used for totals, and `amount_basis` records the reason. Individual cases can be overridden in
  `sources/amount_overrides.csv`.
- **Disclosure documents vs. projects.** The source data has one row per disclosure document; 17 projects have
  two documents. Use the `projects_unique` view (one row per project) for any aggregation.
- **IFC regions.** Only Middle East and Central Asia follows the country list published on IFC's website.
  Other countries are assigned provisionally from the World Bank region (`ifc_region_basis` shows which).
- **Current classifications.** World Bank regions and income groups are today's classifications, applied to all years.
- **Keyword tagging** shows that a paragraph mentions a topic, not that the topic is its main subject.

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
| `ifc_regions.csv` | IFC region by country, with status (Official / Provisional / To be confirmed) and source |
| `country_overrides.csv` | Manual mapping for country names that cannot be matched automatically, including regional projects |
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
| `countries` | table | World Bank region, income group, lending category and IFC region by country name |
| `projects_enriched` | view | `projects_unique` joined with `countries` (used by the dashboard) |
| `documents` | table | One row per PDF report, with page count and file hash |
| `doc_blocks` | table | Text blocks with page, position, language and kind (paragraph, caption, short, boilerplate) |
| `doc_text` | view | English body paragraphs only |
| `doc_countries` | table | Countries covered by each report |
| `block_tags` | table | Country, industry and theme tags per paragraph |
| `evidence` | view | Paragraph text with its tags in one row (used by the dashboard) |
| `country_sector_evidence` | view | Paragraphs and reports per country and industry; joins to `projects_enriched` on `iso3` and `industry` |
| `country_theme_evidence` | view | Paragraphs and reports per country and theme |

## Dashboard

- **Overview** – headline figures, amounts or project counts by country and fiscal year,
  product mix by country, and the number of IFC reports available per country.
- **Country profile** – projects by fiscal year and product, the largest projects with links to IFC's
  disclosure pages, and paragraphs from IFC reports filtered by theme and by the selected industry,
  with report title and page number.
- **Project list** – all projects matching the filters, downloadable as CSV.
- **Notes on the data** – the caveats above, for readers of the dashboard.

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
