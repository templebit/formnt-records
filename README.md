# Form NT — published records

The record feed behind the Form NT iOS app, built from primary U.S. government
sources and served as static JSON.

**Base URL:** `https://templebit.github.io/formnt-records/v1/`

| Path | What |
|---|---|
| `v1/meta.json` | When this was last built, and from what |
| `v1/feed/sec/page-N.json` | SEC filings, paginated |
| `v1/feed/oge/page-N.json` | Congressional financial disclosures, paginated |
| `v1/signals/<id>.json` | One record |
| `v1/companies/<TICKER>.json` | One issuer |
| `v1/search-index.json` | Every record, for client-side filtering |

## Sources

All public, all free, all U.S. government:

- [SEC EDGAR full-text search](https://efts.sec.gov/LATEST/search-index)
- [SEC insider transactions data sets](https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets)
- [SEC EDGAR quarterly form index](https://www.sec.gov/Archives/edgar/full-index/)
- [Clerk of the U.S. House — financial disclosures](https://disclosures-clerk.house.gov/FinancialDisclosure)

Requests to SEC systems carry a descriptive User-Agent with a contact address
and stay well inside the published [10 requests/second fair-access limit](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data).

## How it is built

`Tools/build_site.py` matches a fixed rule library against those sources and
writes the tree above. It runs twice daily on GitHub Actions.

Rules are matched here rather than on the device. The app is a reader.

## What this data is not

- **No base rates.** No backtest has been run, so every record ships
  `baseRateStatus: "pending"`. No figure is invented to fill the field.
- **No forecasts, ratings, or scores** of any company's outlook.
- **No personal names.** Congressional disclosures name the filer; that name is
  discarded. Only the reporting role survives — self, spouse, or dependent
  child. A filing covering a family member is not an action by that person.
- **No converted values.** Disclosed value bands are copied verbatim, never
  averaged, summed, or reduced to a midpoint.
- **Nothing guessed.** Filings that cannot be read reliably are skipped and
  counted, never inferred.

A build that comes back sharply smaller than the last one is refused rather
than published, because an empty feed would read as "no filings" — a claim
about the world — rather than as a broken pipeline.

## Reuse

The underlying records are U.S. government works in the public domain. Note that
federal law restricts what anyone may do with a financial disclosure report
obtained from these sources — see [5 U.S.C. §13107(c)](https://uscode.house.gov/view.xhtml?req=granuleid%3AUSC-prelim-title5-section13107).

Not affiliated with, endorsed by, or acting on behalf of any government body.
