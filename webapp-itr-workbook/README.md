# ITR Workbook (Personal ITR-1 / ITR-2 checker)

A privacy-first, **client-side** ITR workbook for Indian taxpayers.
Files never leave the browser. Same architecture as the static
Tax P&L app — no backend, no npm install required for the app, no
analytics, no third-party calls. Deployable to Vercel/Netlify
as a static site.

**Project path on this machine:** `webapp-itr-workbook/` (under the
portfolio-tracker repo).

## What it does (v1 scope)

- **Two Assessment Years supported:** AY 2025-26 (FY 2024-25) and
  AY 2024-25 (FY 2023-24) for prior-year comparison.
- **Inputs:** salary (multi-employer), house property (self-occupied
  + let-out), other sources (interest, dividends, lottery),
  capital gains (STCG 111A / LTCG 112A / STCL / LTCL), deductions
  (80C, 80CCD, 80D, 80E, 80G, 80TTA, 80TTB), TDS / advance tax.
- **Computes:** both old regime (default 1961 IT Act slabs) and
  new regime (Section 115BAC, FY 2024-25 with 75K std ded + 7L
  rebate). Side-by-side comparison, recommends the cheaper one.
- **Outputs:** printable summary, ITR preview JSON (for pasting
  into the official ITR utility), and a side-by-side tax preview.
- **Form 16 PDF parser** (text-based PDFs only in v1; scanned
  PDFs need OCR, out of scope).
- **Form 26AS JSON parser** for TDS reconciliation.

## Out of scope (v1)

- Business income (ITR-3 / ITR-4)
- Foreign income (Schedule FSI / FA)
- Crypto / VDA taxation
- Direct e-filing integration (DSC / Aadhaar OTP)
- 234A/B/C interest, 271/273 penalties
- Per-year STCL/LTCL buckets with 8-year expiry
- Scanned Form 16 PDFs (need OCR)

## Project layout

```
webapp-itr-workbook/
  package.json                 # test runner + npm dependencies
  js/
    data_model.js              # Workbook shape + localStorage
    tax_engine.js              # Indian tax computation (both regimes)
    validation.js              # JSON-schema-style validator
    integrations.js            # Form 16, Form 26AS, ITR preview
    tests/
      test_tax_engine.spec.js            # 26 tests
      test_statutory_compliance.spec.js  # 48 tests (IT Act sections)
      test_schema_validation.spec.js     # 38 tests
      test_integrations.spec.js          # 28 tests (Form 16, 26AS, ITR)
      test_security.spec.js              # 27 tests
      test_functional_stability.spec.js  # 25 tests
      test_performance.spec.js           # 17 tests
      test_industry_benchmarking.spec.js # 24 tests
```

## Running the tests

```bash
cd webapp-itr-workbook
npm install   # one-time
npm test      # 233 tests, ~160ms
```

## Browser usage (planned UI)

The v1 build focuses on the JS engine + tests. A full HTML/CSS
UI is slated for v2. The engine is fully usable via Node REPL
or as a library:

```js
const dm = require("./js/data_model.js");
const engine = require("./js/tax_engine.js");

const wb = dm.emptyWorkbook("2025-26");
wb.salary.employers = [{
  employer_name: "Acme",
  gross_salary: 1500000,
  allowances_exempt_10: 200000,
  professional_tax: 0,
}];
wb.deductions["80c_total"] = 150000;
wb.capital_gains.stcg_111a = 50000;

const result = engine.computeBothRegimes(wb);
console.log("Old:", result.old.total_tax_rounded);
console.log("New:", result.new.total_tax_rounded);
console.log("Recommendation:", result.recommendation);
```

## Architecture decisions

- **No backend, no build step.** Same pattern as the static Tax
  P&L app. The HTML/JS load directly from a CDN or static host.
- **localStorage only.** No IndexedDB, no service worker, no
  background sync. The data stays in the browser; clearing
  browser data clears the workbook.
- **Aadhaar stored as last 4 digits only** (privacy by design).
- **PAN, bank account, full Aadhaar numbers** are NEVER stored
  on any server (because there's no server). They're only in
  the user's localStorage.
- **No npm runtime dependencies** for the engine itself. The
  test runner uses `xlsx` (a Node-only test dep) for parsing
  fixtures; the production app loads nothing from npm.

## License

Same as the parent portfolio-tracker repo.
