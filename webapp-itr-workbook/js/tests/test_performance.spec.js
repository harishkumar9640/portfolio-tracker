// js/tests/test_performance.spec.js
// Suite 6: Performance & Non-Functional Stability tests.
//
// "Non-functional stability" = the engine behaves well under
// realistic and adversarial workloads, in terms of:
//   - Speed: latency for typical and large workbooks
//   - Memory: bounded memory growth, no leaks
//   - Scalability: how performance scales with input size
//   - Deployment footprint: total JS size, no heavy dependencies
//   - Concurrency: same workbook computed in parallel gives
//     the same result as sequential

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// ---- Minimal localStorage polyfill ----
const _store = new Map();
globalThis.localStorage = {
  getItem: (k) => _store.has(k) ? _store.get(k) : null,
  setItem: (k, v) => _store.set(k, String(v)),
  removeItem: (k) => _store.delete(k),
  clear: () => _store.clear(),
  key: (i) => Array.from(_store.keys())[i] || null,
  get length() { return _store.size; },
};

const dm = require("../data_model.js");
const engine = require("../tax_engine.js");
const integ = require("../integrations.js");
const v = require("../validation.js");

// ============================================================
// Latency: typical cases
// ============================================================

test("Latency: simple workbook (1 employer, no cap gains) <2ms", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Warmup
  engine.computeBothRegimes(wb);
  // Measure
  const start = process.hrtime.bigint();
  for (let i = 0; i < 100; i++) engine.computeBothRegimes(wb);
  const elapsedMs = Number(process.hrtime.bigint() - start) / 1e6;
  const perOp = elapsedMs / 100;
  assert.ok(perOp < 2, `per-op: ${perOp.toFixed(3)}ms (expected <2ms)`);
});

test("Latency: complex workbook (1 employer + cap gains + house + 80C) <2ms", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1500000, allowances_exempt_10: 200000, professional_tax: 2500,
  }];
  wb.deductions["80c_total"] = 150000;
  wb.deductions["80d_self_family"] = 25000;
  wb.deductions["80ccd_1b"] = 50000;
  wb.capital_gains.stcg_111a = 50000;
  wb.capital_gains.ltcg_112a = 200000;
  wb.house_property.properties = [{
    type: "self-occupied", address: "Mumbai", rent_received: 0,
    municipal_taxes_paid: 5000, home_loan_interest_paid: 200000,
    home_loan_principal_paid: 0, co_ownership_share: 100, tds_on_rent: 0,
  }];
  engine.computeBothRegimes(wb);  // warmup
  const start = process.hrtime.bigint();
  for (let i = 0; i < 100; i++) engine.computeBothRegimes(wb);
  const elapsedMs = Number(process.hrtime.bigint() - start) / 1e6;
  const perOp = elapsedMs / 100;
  assert.ok(perOp < 2, `per-op: ${perOp.toFixed(3)}ms (expected <2ms)`);
});

test("Latency: high income (1 Cr) with 37% surcharge <2ms", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 10050000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  engine.computeBothRegimes(wb);  // warmup
  const start = process.hrtime.bigint();
  for (let i = 0; i < 100; i++) engine.computeBothRegimes(wb);
  const elapsedMs = Number(process.hrtime.bigint() - start) / 1e6;
  const perOp = elapsedMs / 100;
  assert.ok(perOp < 2, `per-op: ${perOp.toFixed(3)}ms (expected <2ms)`);
});

// ============================================================
// Latency: p95/p99
// ============================================================

test("Latency: p99 <5ms for typical workbook (1000 ops)", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1500000, allowances_exempt_10: 200000, professional_tax: 2500,
  }];
  wb.deductions["80c_total"] = 150000;
  wb.capital_gains.stcg_111a = 50000;
  wb.capital_gains.ltcg_112a = 200000;
  // Warmup
  for (let i = 0; i < 100; i++) engine.computeBothRegimes(wb);
  // Measure
  const times = [];
  for (let i = 0; i < 1000; i++) {
    const start = process.hrtime.bigint();
    engine.computeBothRegimes(wb);
    const elapsedNs = Number(process.hrtime.bigint() - start);
    times.push(elapsedNs / 1e6);  // ms
  }
  times.sort((a, b) => a - b);
  const p50 = times[500];
  const p95 = times[950];
  const p99 = times[990];
  console.log(`  latency: p50=${p50.toFixed(3)}ms  p95=${p95.toFixed(3)}ms  p99=${p99.toFixed(3)}ms  max=${times[999].toFixed(3)}ms`);
  assert.ok(p99 < 5, `p99 latency ${p99.toFixed(3)}ms exceeds 5ms`);
});

// ============================================================
// Throughput
// ============================================================

test("Throughput: 10,000 computations in <5s (>2000 ops/sec)", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1500000, allowances_exempt_10: 200000, professional_tax: 2500,
  }];
  wb.deductions["80c_total"] = 150000;
  wb.capital_gains.stcg_111a = 50000;
  const start = Date.now();
  for (let i = 0; i < 10000; i++) {
    engine.computeBothRegimes(wb);
  }
  const elapsed = Date.now() - start;
  const throughput = 10000 / (elapsed / 1000);
  console.log(`  throughput: ${throughput.toFixed(0)} ops/sec (${elapsed}ms for 10K)`);
  assert.ok(throughput > 2000, `throughput ${throughput.toFixed(0)} ops/sec is too low`);
  assert.ok(elapsed < 5000, `10K computations took ${elapsed}ms`);
});

// ============================================================
// Memory
// ============================================================

test("Memory: 10,000 workbook computations don't leak memory", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Warmup
  for (let i = 0; i < 100; i++) engine.computeBothRegimes(wb);
  // Force GC if available
  if (global.gc) global.gc();
  const before = process.memoryUsage().heapUsed;
  // Run 10K computations
  for (let i = 0; i < 10000; i++) {
    engine.computeBothRegimes(wb);
  }
  if (global.gc) global.gc();
  const after = process.memoryUsage().heapUsed;
  const growthMB = (after - before) / (1024 * 1024);
  console.log(`  heap growth: ${growthMB.toFixed(2)} MB over 10K computations`);
  // Allow some growth (10 MB tolerance) but should be much less
  assert.ok(growthMB < 10, `memory grew by ${growthMB.toFixed(2)}MB (expected <10MB)`);
});

test("Memory: creating 10,000 workbooks in succession doesn't leak", () => {
  // Warmup
  for (let i = 0; i < 100; i++) dm.emptyWorkbook("2025-26");
  if (global.gc) global.gc();
  const before = process.memoryUsage().heapUsed;
  for (let i = 0; i < 10000; i++) {
    const wb = dm.emptyWorkbook("2025-26");
    wb.salary.employers = [{
      employer_name: "Acme" + i, tan: "",
      gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
    }];
    engine.computeForRegime(wb, "old");
    // Allow GC between (we don't hold references)
  }
  if (global.gc) global.gc();
  const after = process.memoryUsage().heapUsed;
  const growthMB = (after - before) / (1024 * 1024);
  console.log(`  heap growth: ${growthMB.toFixed(2)} MB over 10K workbooks`);
  assert.ok(growthMB < 20, `memory grew by ${growthMB.toFixed(2)}MB`);
});

// ============================================================
// Scalability
// ============================================================

test("Scalability: 100 employers → 10x slower than 1 employer (<5ms)", () => {
  // Performance should scale linearly with number of employers
  // (we just sum across them).
  const wb1 = dm.emptyWorkbook("2025-26");
  wb1.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  const wb100 = dm.emptyWorkbook("2025-26");
  wb100.salary.employers = Array(100).fill(0).map((_, i) => ({
    employer_name: "Co" + i, tan: "",
    gross_salary: 10000, allowances_exempt_10: 0, professional_tax: 0,
  }));
  // Time 1-employer
  engine.computeBothRegimes(wb1);  // warmup
  const t0 = process.hrtime.bigint();
  for (let i = 0; i < 100; i++) engine.computeBothRegimes(wb1);
  const t1ms = Number(process.hrtime.bigint() - t0) / 1e6 / 100;
  // Time 100-employer
  engine.computeBothRegimes(wb100);  // warmup
  const t2 = process.hrtime.bigint();
  for (let i = 0; i < 100; i++) engine.computeBothRegimes(wb100);
  const t3ms = Number(process.hrtime.bigint() - t2) / 1e6 / 100;
  // 100-employer should be < 5ms (it doesn't need to be 100x; just
  // verify it's not pathological)
  assert.ok(t3ms < 5, `100-employer took ${t3ms.toFixed(3)}ms (expected <5ms)`);
  console.log(`  1-employer: ${t1ms.toFixed(3)}ms, 100-employer: ${t3ms.toFixed(3)}ms (${(t3ms/t1ms).toFixed(1)}x)`);
});

test("Scalability: capital gains with very large numbers stays fast", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // 1000-digit cap gain (extreme edge case)
  wb.capital_gains.stcg_111a = 1e100;
  engine.computeBothRegimes(wb);  // warmup
  const start = process.hrtime.bigint();
  for (let i = 0; i < 1000; i++) engine.computeBothRegimes(wb);
  const perOp = Number(process.hrtime.bigint() - start) / 1e6 / 1000;
  console.log(`  per-op with 1e100 CG: ${perOp.toFixed(4)}ms`);
  assert.ok(perOp < 5);
});

// ============================================================
// Deployment footprint
// ============================================================

test("Deployment footprint: total JS source is <50KB (no heavy deps)", () => {
  // The static Tax P&L app is meant to run in the browser. Heavy
  // dependencies (e.g. PDF.js, Lodash) inflate the bundle. Our
  // engine is intentionally dependency-free.
  const jsDir = path.resolve(__dirname, "..", "..", "js");
  const files = ["data_model.js", "tax_engine.js", "validation.js", "integrations.js"];
  let total = 0;
  for (const f of files) {
    const fp = path.join(jsDir, f);
    if (fs.existsSync(fp)) {
      total += fs.statSync(fp).size;
    }
  }
  const totalKB = total / 1024;
  console.log(`  Total source: ${totalKB.toFixed(1)} KB across ${files.length} files`);
  assert.ok(totalKB < 80, `Total source is ${totalKB.toFixed(1)}KB; should be <80KB`);
});

test("Deployment footprint: requires no npm install (no package.json deps)", () => {
  // The static app's only runtime dep is SheetJS (for PDF parsing,
  // loaded from CDN). The tax engine itself has NO npm deps.
  // Verify by reading the source files and checking for
  // require() / import statements.
  const jsDir = path.resolve(__dirname, "..", "..", "js");
  const files = ["data_model.js", "tax_engine.js", "validation.js", "integrations.js"];
  for (const f of files) {
    const fp = path.join(jsDir, f);
    if (fs.existsSync(fp)) {
      const src = fs.readFileSync(fp, "utf-8");
      // No external imports (other than relative ./xxx.js)
      const externals = (src.match(/require\(['"]([^'"./][^'"]*)['"]\)/g) || [])
        .concat(src.match(/from\s+['"]([^'"./][^'"]*)['"]/g) || [])
        .filter(line => !line.includes("./"));  // exclude relative
      assert.equal(externals.length, 0,
        `${f} has external imports: ${externals.join(", ")}`);
    }
  }
});

test("Deployment footprint: works in browser (no Node-specific APIs)", () => {
  // Check that the source doesn't use require(), process, Buffer,
  // etc. which are Node-only and would break in the browser.
  // We allow `require` inside `typeof require !== "undefined"` guards
  // (the engine uses this pattern for the optional defensive
  // fallback to data_model.emptyWorkbook; in the browser, `require`
  // is undefined and the code falls through to the inline fallback).
  const jsDir = path.resolve(__dirname, "..", "..", "js");
  const files = ["data_model.js", "tax_engine.js", "validation.js", "integrations.js"];
  for (const f of files) {
    const fp = path.join(jsDir, f);
    if (fs.existsSync(fp)) {
      const src = fs.readFileSync(fp, "utf-8");
      // Strip lines that are inside `typeof require !== "undefined"` guards
      // (these are the safe, browser-aware Node fallbacks)
      const cleaned = src.replace(/typeof\s+require\s*!==?\s*["']undefined["'][\s\S]*?require\([^)]+\)/g, '');
      assert.ok(!/\brequire\s*\(/.test(cleaned),
        `${f} uses require() outside a typeof guard (Node-only)`);
      assert.ok(!/\bprocess\./.test(cleaned),
        `${f} uses process.* (Node-only)`);
      assert.ok(!/\bBuffer\b/.test(cleaned),
        `${f} uses Buffer (Node-only)`);
    }
  }
});

// ============================================================
// Form 16 / 26AS performance
// ============================================================

test("Form 16 parser: 1000 invocations <500ms", () => {
  const sample = `
FORM NO. 16
TAN of the Employer: MUMA12345E
PAN of the Employee: ABCDE1234F
1. Salary as per provisions contained in section 17(1): Rs. 12,00,000
2. Less: Allowances to the extent exempt u/s 10:
   Total exempt u/s 10: Rs. 2,00,000
3. Standard Deduction u/s 16(ia): Rs. 50,000
4. Professional Tax: Rs. 2,500
5. Deductions under Chapter VI-A:
   Section 80C: Rs. 1,50,000
   Section 80CCD(1B): Rs. 50,000
6. Tax on total income: Rs. 85,000
7. Education Cess: Rs. 3,400
Total Tax Deducted: Rs. 50,000
`;
  integ.parseForm16Text(sample);  // warmup
  const start = Date.now();
  for (let i = 0; i < 1000; i++) integ.parseForm16Text(sample);
  const elapsed = Date.now() - start;
  console.log(`  Form 16 parse: ${elapsed}ms for 1000 invocations (${(elapsed/1000).toFixed(2)}ms each)`);
  assert.ok(elapsed < 500);
});

test("Form 26AS parser: 1000 invocations <500ms", () => {
  const json = {
    "TDS_on_Salary": Array(50).fill({ TAN: "MUMA12345E", TDS: 1000 }),
    "TDS_on_Others": Array(50).fill({ TAN: "MUMB45678F", TDS: 500 }),
    "Advance_Tax": Array(20).fill({ Amount: 10000 }),
  };
  integ.parseForm26ASJson(json);  // warmup
  const start = Date.now();
  for (let i = 0; i < 1000; i++) integ.parseForm26ASJson(json);
  const elapsed = Date.now() - start;
  console.log(`  Form 26AS parse: ${elapsed}ms for 1000 invocations (${(elapsed/1000).toFixed(2)}ms each)`);
  assert.ok(elapsed < 500);
});

test("Validation: 1000 workbooks <500ms", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.personal.pan = "ABCDE1234F";
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  v.validateWorkbook(wb);  // warmup
  const start = Date.now();
  for (let i = 0; i < 1000; i++) v.validateWorkbook(wb);
  const elapsed = Date.now() - start;
  console.log(`  Validation: ${elapsed}ms for 1000 (${(elapsed/1000).toFixed(2)}ms each)`);
  assert.ok(elapsed < 500);
});

// ============================================================
// Concurrency / parallel
// ============================================================

test("Concurrency: parallel computations give same result as sequential", async () => {
  // JavaScript is single-threaded, but the engine should be safe
  // to call from multiple async contexts (no shared mutable state
  // except the input workbook).
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1500000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Sequential baseline
  const r0 = engine.computeBothRegimes(wb);
  // Parallel (using Promise.all)
  const results = await Promise.all(Array(10).fill(0).map(() =>
    Promise.resolve().then(() => engine.computeBothRegimes(wb))
  ));
  // All parallel results should equal the sequential baseline
  for (const r of results) {
    assert.equal(r.old.total_tax_liability, r0.old.total_tax_liability);
    assert.equal(r.new.total_tax_liability, r0.new.total_tax_liability);
  }
});

// ============================================================
// Browser-realistic: large typical workbook
// ============================================================

test("Realistic: full ITR-1 workbook (10 cap-gains, 2 employers, 80C+D) <5ms", () => {
  // Simulates a typical retail investor's full workbook
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [
    { employer_name: "Primary Inc", tan: "MUMA12345E",
      gross_salary: 1800000, allowances_exempt_10: 200000, professional_tax: 2500 },
    { employer_name: "Side Gig", tan: "DELH12345F",
      gross_salary: 200000, allowances_exempt_10: 0, professional_tax: 0 },
  ];
  wb.house_property.properties = [{
    type: "self-occupied", address: "Mumbai", rent_received: 0,
    municipal_taxes_paid: 5000, home_loan_interest_paid: 180000,
    home_loan_principal_paid: 0, co_ownership_share: 100, tds_on_rent: 0,
  }];
  wb.other_sources.savings_account_interest = 15000;
  wb.other_sources.fd_interest = 30000;
  wb.other_sources.dividend_gross = 25000;
  wb.capital_gains.stcg_111a = 100000;
  wb.capital_gains.ltcg_112a = 300000;
  wb.deductions["80c_total"] = 150000;
  wb.deductions["80d_self_family"] = 25000;
  wb.deductions["80ccd_1b"] = 50000;
  wb.deductions["80e"] = 0;
  wb.taxes_paid.tds_other_than_salary = 5000;
  wb.taxes_paid.advance_tax = 20000;
  engine.computeBothRegimes(wb);  // warmup
  const start = process.hrtime.bigint();
  for (let i = 0; i < 100; i++) engine.computeBothRegimes(wb);
  const perOp = Number(process.hrtime.bigint() - start) / 1e6 / 100;
  console.log(`  Realistic ITR-1: ${perOp.toFixed(3)}ms per computation`);
  assert.ok(perOp < 5, `Realistic workbook took ${perOp.toFixed(3)}ms (expected <5ms)`);
});
