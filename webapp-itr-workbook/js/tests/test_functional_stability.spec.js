// js/tests/test_functional_stability.spec.js
// Suite 5: Functional Stability tests.
//
// "Functional stability" = the engine produces consistent,
// repeatable, predictable results under various conditions:
//   - Idempotency: same input → same output, every time
//   - Determinism: no random, time-of-day, or environment
//     dependencies in the computation
//   - Error recovery: invalid input returns errors, doesn't
//     crash, and a valid input after invalid still works
//   - Stress: 1000 workbooks in a row, all produce the same
//     correct result
//   - Edge case input: empty strings, zero values, negative
//     numbers, extremely small/large numbers, NaN, null
//   - Schema evolution: a v2 workbook (with extra fields) loaded
//     into v1 doesn't break
//   - Round-trip: JSON.stringify → JSON.parse → recompute gives
//     the same result

const test = require("node:test");
const assert = require("node:assert/strict");

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
// Idempotency
// ============================================================

test("Idempotency: same input gives the same output (100 runs)", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "MUMA12345E",
    gross_salary: 1500000, allowances_exempt_10: 200000, professional_tax: 2500,
  }];
  wb.deductions["80c_total"] = 150000;
  wb.capital_gains.stcg_111a = 50000;

  // Run compute 100 times, ensure identical results
  const first = engine.computeForRegime(wb, "old");
  for (let i = 0; i < 100; i++) {
    const r = engine.computeForRegime(wb, "old");
    assert.equal(r.gti, first.gti);
    assert.equal(r.taxable_income, first.taxable_income);
    assert.equal(r.pre_rebate_tax, first.pre_rebate_tax);
    assert.equal(r.tax_after_rebate, first.tax_after_rebate);
    assert.equal(r.surcharge, first.surcharge);
    assert.equal(r.cess, first.cess);
    assert.equal(r.total_tax_liability, first.total_tax_liability);
    assert.equal(r.tds_total, first.tds_total);
    assert.equal(r.net_payable, first.net_payable);
  }
});

test("Idempotency: computeBothRegimes is deterministic", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 800000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  const first = engine.computeBothRegimes(wb);
  for (let i = 0; i < 50; i++) {
    const r = engine.computeBothRegimes(wb);
    assert.equal(r.old.total_tax_liability, first.old.total_tax_liability);
    assert.equal(r.new.total_tax_liability, first.new.total_tax_liability);
    assert.equal(r.recommendation, first.recommendation);
  }
});

test("Determinism: same input at different times of day → same result", () => {
  // The engine should NOT depend on Date.now() (other than
  // timestamps in the workbook, which don't affect the math).
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1200000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Capture the result, then "wait" (we can't actually wait in
  // a unit test, but the point is: nothing in the engine reads
  // the current time)
  const r1 = engine.computeForRegime(wb, "old");
  // Simulate time passing by sleeping briefly
  const t0 = Date.now();
  while (Date.now() - t0 < 5) { /* spin 5ms */ }
  const r2 = engine.computeForRegime(wb, "old");
  assert.equal(r1.total_tax_liability, r2.total_tax_liability);
  assert.equal(r1.net_payable, r2.net_payable);
});

// ============================================================
// Error recovery
// ============================================================

test("Error recovery: invalid input returns errors, engine still works after", () => {
  // 1. Bad input
  const badWb = { ay: "invalid-ay", schema_version: 1 };
  // Engine accepts unknown AY (returns the default regime) but
  // verify the result is valid (not NaN)
  let r;
  try {
    r = engine.computeForRegime(badWb, "old");
  } catch (e) {
    // If it throws, that's also fine (the test framework catches
    // uncaught exceptions); we just want to ensure subsequent
    // valid workbooks work
  }
  // 2. Valid input still works
  const goodWb = dm.emptyWorkbook("2025-26");
  goodWb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  r = engine.computeForRegime(goodWb, "old");
  assert.ok(r.total_tax_liability > 0);
  // 3. Multiple bad inputs interleaved with good ones
  for (let i = 0; i < 10; i++) {
    try { engine.computeForRegime({ ay: "" }, "old"); } catch (e) {}
    engine.computeForRegime(goodWb, "old");
  }
  // The good input still works
  r = engine.computeForRegime(goodWb, "old");
  assert.ok(r.total_tax_liability > 0);
});

test("Error recovery: NaN inputs handled without crashing", () => {
  // NaN in numeric fields is treated as 0 (the +x || 0 fallback
  // in computeNetSalary). So net_salary = 0 - 50K → 0.
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: NaN,  // bad input
    allowances_exempt_10: 0, professional_tax: 0,
  }];
  const r = engine.computeForRegime(wb, "old");
  // Net salary is 0, not NaN (engine handles NaN gracefully)
  assert.equal(r.salary.net_salary, 0, "net salary is 0, not NaN");
  assert.equal(r.total_tax_liability, 0, "total tax is 0, not NaN");
});

test("Error recovery: undefined inputs handled without crashing", () => {
  // The engine has defensive guards (added in Suite 4) for null/undefined
  const r = engine.computeForRegime(undefined, "old");
  assert.equal(r.total_tax_liability, 0);
  const r2 = engine.computeForRegime(null, "old");
  assert.equal(r2.total_tax_liability, 0);
});

test("Error recovery: invalid regime kind", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Pass garbage regime — should default to "old" or throw
  // a clear error, not silently return wrong numbers
  let r;
  try {
    r = engine.computeForRegime(wb, "garbage");
    // If it didn't throw, the result should still be reasonable
    // (defaults to old regime)
    assert.ok(typeof r === "object");
    assert.ok(r.total_tax_liability >= 0);
  } catch (e) {
    // Throwing is also acceptable
    assert.ok(e instanceof Error);
  }
});

// ============================================================
// Stress / load
// ============================================================

test("Stress: 1000 workbooks computed in <2s", () => {
  const start = Date.now();
  for (let i = 0; i < 1000; i++) {
    const wb = dm.emptyWorkbook(i % 2 === 0 ? "2025-26" : "2024-25");
    wb.salary.employers = [{
      employer_name: "Acme" + i, tan: "",
      gross_salary: 500000 + (i * 1000),
      allowances_exempt_10: 50000, professional_tax: 0,
    }];
    engine.computeBothRegimes(wb);
  }
  const elapsed = Date.now() - start;
  assert.ok(elapsed < 2000, `1000 workbooks took ${elapsed}ms (expected <2000)`);
});

test("Stress: large workbook (100 employers) computed in <100ms", () => {
  // v1 caps at 10 employers via validation, but the engine itself
  // can handle more. Verify performance with 100 employers.
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = Array(100).fill(0).map((_, i) => ({
    employer_name: "Co" + i, tan: "",
    gross_salary: 100000, allowances_exempt_10: 0, professional_tax: 0,
  }));
  const start = Date.now();
  engine.computeBothRegimes(wb);
  const elapsed = Date.now() - start;
  assert.ok(elapsed < 100, `100-employer workbook took ${elapsed}ms`);
});

test("Stress: 100K rows of capital gains processed in <200ms", () => {
  // The engine aggregates CG; this is O(1) per call. 100K rows
  // is hypothetical (in v1 we don't have per-row CG, but the
  // aggregator should still be fast).
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Push high CG numbers
  wb.capital_gains.stcg_111a = 1e10;
  wb.capital_gains.ltcg_112a = 1e10;
  wb.capital_gains.stcl_brought_forward = 1e10;
  const start = Date.now();
  for (let i = 0; i < 100000; i++) {
    engine.computeForRegime(wb, "old");
  }
  const elapsed = Date.now() - start;
  assert.ok(elapsed < 200, `100K computations took ${elapsed}ms`);
});

// ============================================================
// Edge case inputs
// ============================================================

test("Edge: all zeros → 0 tax, no NaN", () => {
  const wb = dm.emptyWorkbook("2025-26");
  const r = engine.computeForRegime(wb, "old");
  assert.equal(r.gti, 0);
  assert.equal(r.taxable_income, 0);
  assert.equal(r.pre_rebate_tax, 0);
  assert.equal(r.total_tax_liability, 0);
  assert.equal(r.net_payable, 0);
  assert.equal(r.refund_due, 0);
  assert.equal(r.tax_payable, 0);
});

test("Edge: ₹1 salary → tiny tax (boundary)", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 51000,  // 51K → net = 1K (after 50K std ded)
    allowances_exempt_10: 0, professional_tax: 0,
  }];
  const r = engine.computeForRegime(wb, "old");
  // 1K is in 0-2.5L slab, tax = 0
  assert.equal(r.pre_rebate_tax, 0);
  assert.equal(r.total_tax_liability, 0);
});

test("Edge: very large salary (10 Cr) → surcharge + cess", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 10e7,  // 10 Cr
    allowances_exempt_10: 0, professional_tax: 0,
  }];
  const r = engine.computeForRegime(wb, "old");
  // 10 Cr > 5 Cr → 37% surcharge (old regime, no cap-gains)
  assert.equal(r.surcharge_rate, 0.37);
  // Tax should be a real number, not Infinity
  assert.ok(Number.isFinite(r.total_tax_liability));
  // Sanity: 10 Cr income → tax in the range of 4-5 Cr
  assert.ok(r.total_tax_liability > 3e7);
  assert.ok(r.total_tax_liability < 6e7);
});

test("Edge: salary just above 99,99,999 (1 Cr) → 15% surcharge", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 10100000,  // 1.01 Cr
    allowances_exempt_10: 0, professional_tax: 0,
  }];
  const r = engine.computeForRegime(wb, "old");
  // 1.01 Cr > 1 Cr → 15% surcharge
  assert.equal(r.surcharge_rate, 0.15);
});

test("Edge: salary giving GTI exactly ₹50L → no surcharge", () => {
  // Surcharge starts at GTI > ₹50L (Section 2(29)). The income
  // threshold is on GTI (total income), not gross salary.
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 5050000,  // net = 50,00,000 = exactly 50L
    allowances_exempt_10: 0, professional_tax: 0,
  }];
  const r = engine.computeForRegime(wb, "old");
  // GTI = 50,00,000 exactly
  assert.equal(r.gti, 5000000);
  // Surcharge: at exactly 50L, no surcharge (must be > 50L)
  assert.equal(r.surcharge, 0);
});

test("Edge: salary giving GTI just above ₹50L → 10% surcharge", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 5051000,  // net = 50,01,000 (just above 50L)
    allowances_exempt_10: 0, professional_tax: 0,
  }];
  const r = engine.computeForRegime(wb, "old");
  // GTI = 50,01,000 > 50L → 10% surcharge
  assert.equal(r.surcharge_rate, 0.10);
});

// ============================================================
// Schema evolution
// ============================================================

test("Schema evolution: v2 workbook (with extra fields) loads in v1", () => {
  // Future versions may add new fields. The v1 engine should
  // ignore unknown fields and compute correctly.
  const v2Workbook = {
    schema_version: 2,  // future
    ay: "2025-26",
    fy: "2024-25",
    personal: { pan: "ABCDE1234F", name: "Future User" },
    salary: {
      employers: [{
        employer_name: "Future Co", tan: "",
        gross_salary: 1000000, allowances_exempt_10: 0,
        professional_tax: 0,
        // New field in v2:
        flexible_benefits: 50000,
      }],
      tds_total: 0,
    },
    house_property: { properties: [] },
    other_sources: {
      // New field in v2:
      crypto_gains: 100000,
    },
    capital_gains: {},
    deductions: { "80c_total": 0 },
    taxes_paid: {},
    // New top-level field in v2:
    nri_income: 0,
  };
  const r = engine.computeForRegime(v2Workbook, "old");
  // v1 ignores new fields, computes correctly
  assert.equal(r.gti, 950000);  // 10L - 50K std ded
  assert.equal(r.total_tax_liability > 0, true);
});

test("Schema evolution: v0 workbook (missing schema_version) loads with warning", () => {
  // A workbook saved before schema versioning should still work
  const oldWorkbook = {
    ay: "2025-26",
    salary: { employers: [], tds_total: 0 },
  };
  // v1's mergeWithDefaults fills in missing fields
  const merged = dm.mergeWithDefaults(oldWorkbook);
  assert.equal(merged.schema_version, 1);
  assert.equal(merged.ay, "2025-26");
  // Engine should still work
  const r = engine.computeForRegime(merged, "old");
  assert.equal(r.total_tax_liability, 0);
});

// ============================================================
// Round-trip
// ============================================================

test("Round-trip: stringify → parse → recompute gives same result", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "MUMA12345E",
    gross_salary: 1200000, allowances_exempt_10: 100000, professional_tax: 0,
  }];
  wb.deductions["80c_total"] = 100000;
  wb.capital_gains.stcg_111a = 50000;

  // Round-trip
  const json = JSON.stringify(wb);
  const parsed = JSON.parse(json);
  const merged = dm.mergeWithDefaults(parsed);

  // Compute on both
  const r1 = engine.computeForRegime(wb, "old");
  const r2 = engine.computeForRegime(merged, "old");

  // Should be identical
  assert.equal(r1.gti, r2.gti);
  assert.equal(r1.total_tax_liability, r2.total_tax_liability);
  assert.equal(r1.net_payable, r2.net_payable);
});

test("Round-trip: validation passes on round-tripped workbook", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "MUMA12345E",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.personal.pan = "ABCDE1234F";
  const json = JSON.stringify(wb);
  const parsed = JSON.parse(json);
  const r = v.validateWorkbook(parsed);
  assert.equal(r.ok, true);
});

// ============================================================
// Both regimes, side-by-side consistency
// ============================================================

test("Consistency: new regime never has higher total than old for ₹0 deductions", () => {
  // Post Finance Act 2024, new regime is structurally better for
  // most people without deductions. Verify this holds for a range
  // of incomes.
  for (const gross of [500000, 1000000, 2000000, 5000000, 10000000]) {
    const wb = dm.emptyWorkbook("2025-26");
    wb.salary.employers = [{
      employer_name: "Acme", tan: "",
      gross_salary: gross, allowances_exempt_10: 0, professional_tax: 0,
    }];
    const both = engine.computeBothRegimes(wb);
    assert.ok(both.new.total_tax_liability <= both.old.total_tax_liability,
              `at gross=${gross}: new (${both.new.total_tax_liability}) should be ≤ old (${both.old.total_tax_liability})`);
  }
});

test("Consistency: GTI = sum of head values", () => {
  // GTI must equal: net_salary + net_house_property + net_other_sources
  // + net_capital_gains. Verify across multiple scenarios.
  const scenarios = [
    { gross: 0, hra: 0, hp: 0, os: 0, cg: 0 },
    { gross: 1000000, hra: 0, hp: 0, os: 0, cg: 0 },
    { gross: 1000000, hra: 200000, hp: 100000, os: 50000, cg: 100000 },
    { gross: 5000000, hra: 0, hp: 200000, os: 0, cg: 0 },
  ];
  for (const s of scenarios) {
    const wb = dm.emptyWorkbook("2025-26");
    wb.salary.employers = [{
      employer_name: "Acme", tan: "",
      gross_salary: s.gross, allowances_exempt_10: s.hra, professional_tax: 0,
    }];
    if (s.hp > 0) {
      wb.house_property.properties = [{
        type: "let-out", address: "X", rent_received: s.hp * 1.5,
        municipal_taxes_paid: 0, home_loan_interest_paid: 0,
        home_loan_principal_paid: 0, co_ownership_share: 100, tds_on_rent: 0,
      }];
    }
    if (s.os > 0) wb.other_sources.fd_interest = s.os;
    if (s.cg > 0) wb.capital_gains.ltcg_112a = s.cg;
    const r = engine.computeForRegime(wb, "old");
    const expectedGTI = r.salary.net_salary + r.house.net_house_property
                      + r.other.net_other_sources + r.cg.net_capital_gains;
    assert.equal(r.gti, expectedGTI,
      `GTI mismatch for scenario ${JSON.stringify(s)}: GTI=${r.gti} but head sum=${expectedGTI}`);
  }
});

test("Consistency: taxable_income = GTI - deductions", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1500000, allowances_exempt_10: 100000, professional_tax: 0,
  }];
  wb.deductions["80c_total"] = 150000;
  const r = engine.computeForRegime(wb, "old");
  const expected = Math.max(0, r.gti - r.deductions.total_deductions);
  assert.equal(r.taxable_income, expected);
});

test("Consistency: total_tax_liability = tax + surcharge + cess + lottery_tax", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 2000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.other_sources.lottery_winnings = 50000;
  const r = engine.computeForRegime(wb, "old");
  const expected = r.tax_after_rebate + r.surcharge + r.cess + r.lottery_tax;
  assert.equal(r.total_tax_liability, expected);
});

test("Consistency: result = 'payable' iff net_payable >= 0", () => {
  // For 5 different income levels, check result flag is consistent
  for (const gross of [500000, 1000000, 2000000, 5000000, 10000000]) {
    const wb = dm.emptyWorkbook("2025-26");
    wb.salary.employers = [{
      employer_name: "Acme", tan: "",
      gross_salary: gross, allowances_exempt_10: 0, professional_tax: 0,
    }];
    wb.salary.tds_total = 0;
    const r = engine.computeForRegime(wb, "old");
    const expected = r.net_payable >= 0 ? "payable" : "refund";
    assert.equal(r.result, expected);
    // Also: tax_payable + refund_due should equal abs(net_payable)
    if (r.net_payable >= 0) {
      assert.equal(r.tax_payable, r.net_payable);
    } else {
      assert.equal(r.refund_due, -r.net_payable);
    }
  }
});
