// js/tests/test_tax_engine.spec.js
// Tests for the tax computation engine. Each test uses a known
// scenario with hand-computed expected values. Any failure here
// means the user would get a wrong tax computation in the ITR
// preview — which is unacceptable.

const test = require("node:test");
const assert = require("node:assert/strict");

const dm = require("../data_model.js");
const engine = require("../tax_engine.js");

// ============================================================
// Salary head
// ============================================================

test("salary head: single employer, no exemptions", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme",
    tan: "",
    gross_salary: 1200000,
    allowances_exempt_10: 0,
    professional_tax: 0,
  }];
  const r = engine.computeForRegime(wb, "old");
  // Net salary = 12,00,000 - 50,000 (std ded) = 11,50,000
  assert.equal(r.salary.net_salary, 1150000);
});

test("salary head: HRA + standard deduction reduces net", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme",
    tan: "",
    gross_salary: 1000000,
    allowances_exempt_10: 200000,  // HRA exempt
    professional_tax: 2500,
  }];
  const r = engine.computeForRegime(wb, "old");
  // Net = 10,00,000 - 2,00,000 - 50,000 - 2,500 = 7,47,500
  assert.equal(r.salary.net_salary, 747500);
});

test("salary head: multiple employers summed", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [
    { employer_name: "A", tan: "", gross_salary: 600000, allowances_exempt_10: 0, professional_tax: 0 },
    { employer_name: "B", tan: "", gross_salary: 400000, allowances_exempt_10: 0, professional_tax: 0 },
  ];
  const r = engine.computeForRegime(wb, "old");
  // Net = 10,00,000 - 50,000 = 9,50,000
  assert.equal(r.salary.gross_salary, 1000000);
  assert.equal(r.salary.net_salary, 950000);
});

// ============================================================
// Capital gains: ₹1L exemption, set-off
// ============================================================

test("CG: ₹1L LTCG exemption fully applied", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.capital_gains.ltcg_112a = 50000;  // below threshold
  const r = engine.computeForRegime(wb, "old");
  // 50K < 1L exemption, so net CG = 0
  assert.equal(r.cg.ltcg_exemption_applied, 50000);
  assert.equal(r.cg.ltcg_after_cf, 0);
});

test("CG: ₹1L LTCG exemption partially applied", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.capital_gains.ltcg_112a = 150000;
  const r = engine.computeForRegime(wb, "old");
  assert.equal(r.cg.ltcg_exemption_applied, 100000);
  assert.equal(r.cg.ltcg_after_cf, 50000);
});

test("CG: STCL brought forward set off against STCG first", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 500000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.capital_gains.stcg_111a = 200000;
  wb.capital_gains.ltcg_112a = 0;
  wb.capital_gains.stcl_brought_forward = 50000;
  const r = engine.computeForRegime(wb, "old");
  // STCL should fully absorb against STCG 111A
  assert.equal(r.cg.stcl_used, 50000);
  assert.equal(r.cg.stcg_after_cf, 150000);
  // Total GTI: net salary 4,50,000 + STCG 1,50,000 = 6,00,000
  // (Standard deduction already applied)
  assert.equal(r.gti, 600000);
});

// ============================================================
// Old regime: 5L slab example
// ============================================================

test("old regime: ₹5,00,000 income → 0 tax (87A rebate)", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 550000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Net salary = 5,00,000
  const r = engine.computeForRegime(wb, "old");
  assert.equal(r.gti, 500000);
  // 0 - 2.5L = 0, then 2.5L to 5L = 5% × 2.5L = 12,500
  assert.equal(r.pre_rebate_tax, 12500);
  // Rebate 87A: total income ≤ 5L → tax nil
  assert.equal(r.rebate_87a, 12500);
  assert.equal(r.tax_after_rebate, 0);
  assert.equal(r.total_tax_liability, 0);
});

test("old regime: ₹5,50,000 income → marginal relief", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 600000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Net salary = 5,50,000
  const r = engine.computeForRegime(wb, "old");
  // Old regime slabs: 0-2.5L: 0, 2.5-5L: 12,500, 5-5.5L: 20%×50K = 10,000 = 22,500
  // (Income above ₹5L starts getting taxed at 20% immediately)
  assert.equal(r.pre_rebate_tax, 22500);
  // Rebate 87A: total income (5.5L) > 5L, so no rebate
  // Marginal relief: tax (22,500) > excess (5.5L - 5L = 50K)? No (22.5K < 50K)
  // So tax = 22,500
  assert.equal(r.tax_after_rebate, 22500);
});

test("old regime: ₹7L income, ₹1.5L 80C → tax 32,500", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 750000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.deductions["80c_total"] = 150000;
  const r = engine.computeForRegime(wb, "old");
  // Net salary = 7,50,000 - 50,000 = 7,00,000
  // 80C = 1,50,000
  // Taxable = 5,50,000
  // 0-2.5L: 0, 2.5-5L: 12,500, 5-5.5L: 20%×50K = 10,000 = 22,500
  assert.equal(r.taxable_income, 550000);
  assert.equal(r.pre_rebate_tax, 22500);
  assert.equal(r.tax_after_rebate, 22500);
  // Surcharge: 5.5L < 50L, no surcharge
  // Cess: 4% × 22,500 = 900
  assert.equal(r.surcharge, 0);
  assert.equal(r.cess, 900);
  assert.equal(r.total_tax_rounded, 23400);
});

// ============================================================
// New regime: 7L threshold
// ============================================================

test("new regime: ₹7L income → 0 tax (87A rebate)", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 775000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Net salary (new regime std ded = 75K) = 7,00,000
  const r = engine.computeForRegime(wb, "new");
  assert.equal(r.gti, 700000);
  // Slab: 0-3L: 0, 3-7L: 5% × 4L = 20,000
  assert.equal(r.pre_rebate_tax, 20000);
  // Rebate 87A: total income ≤ 7L → tax nil (capped at 25K)
  assert.equal(r.rebate_87a, 20000);
  assert.equal(r.tax_after_rebate, 0);
});

test("new regime: ₹10L income → 30,000 tax + rebate", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 1075000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Net salary (std ded 75K) = 10,00,000
  const r = engine.computeForRegime(wb, "new");
  // Slab: 0-3L: 0, 3-7L: 20K, 7-10L: 10% × 3L = 30K
  assert.equal(r.pre_rebate_tax, 50000);
  // Rebate: 87A doesn't apply at 10L (above 7L threshold)
  assert.equal(r.tax_after_rebate, 50000);
  // 4% cess = 2,000
  assert.equal(r.cess, 2000);
  assert.equal(r.total_tax_rounded, 52000);
});

// ============================================================
// Surcharge and cess
// ============================================================

test("surcharge: ₹60L income, 10% surcharge kicks in", () => {
  const wb = dm.emptyWorkbook("2025-26");
  // Massive capital gains to hit 60L. Use LTCG so the ₹1L exemption
  // applies (giving GTI of 59L, still well into the surcharge zone).
  wb.capital_gains.ltcg_112a = 6000000;  // 60L → 59L after ₹1L exemption
  const r = engine.computeForRegime(wb, "old");
  // GTI = 59L (60L - 1L exemption)
  assert.equal(r.gti, 5900000);
  // Pre-rebate tax: 0-2.5L: 0, 2.5-5L: 12.5K, 5-10L: 100K, 10-59L: 30%×49L=14,70,000
  // = 1,582,500
  assert.equal(r.pre_rebate_tax, 1582500);
  // Surcharge: 50-100L bracket = 10% × 1,582,500 = 158,250
  assert.equal(r.surcharge_rate, 0.10);
  assert.equal(r.surcharge, 158250);
  // 4% cess on (tax + surcharge) = 1,582,500 + 158,250 = 1,740,750
  // 1,740,750 × 1.04 = 1,810,380
  assert.equal(r.total_tax_rounded, 1810380);
});

test("cess: 4% on tax + surcharge", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.capital_gains.ltcg_112a = 1000000;  // 10L → 9L after ₹1L exemption
  const r = engine.computeForRegime(wb, "old");
  // GTI = 9L (10L - 1L exemption)
  // Pre-rebate tax: 0-2.5L: 0, 2.5-5L: 12.5K, 5-9L: 20%×4L = 80K = 92,500
  // Surcharge: 9L < 50L, none
  // 4% cess = 3,700
  assert.equal(r.surcharge, 0);
  assert.equal(r.cess, 3700);
  assert.equal(r.total_tax_rounded, 96200);
});

// ============================================================
// TDS adjustment
// ============================================================

test("TDS: tax payable reduced by TDS", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 1500000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.salary.tds_total = 200000;
  const r = engine.computeForRegime(wb, "old");
  // Net salary = 14,50,000
  // Pre-rebate tax: 0-2.5L: 0, 2.5-5L: 12.5K, 5-10L: 100K, 10-14.5L: 30%×4.5L = 135K
  // Total: 247,500
  // Surcharge: 14.5L < 50L, none
  // Cess: 4% × 247,500 = 9,900
  // Total tax = 257,400
  // TDS paid = 200,000
  // Net payable = 57,400
  assert.equal(r.total_tax_liability, 257400);
  assert.equal(r.tds_total, 200000);
  assert.equal(r.tax_payable_rounded, 57400);
});

test("TDS: refund when TDS > tax", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 600000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.salary.tds_total = 50000;  // TDS > tax
  const r = engine.computeForRegime(wb, "old");
  // Net salary = 5,50,000
  // Pre-rebate tax: 0-2.5L: 0, 2.5-5L: 12.5K, 5-5.5L: 20%×50K = 10K = 22,500
  // 4% cess = 900
  // Total tax = 23,400
  // TDS = 50,000
  // Net = 50,000 - 23,400 = 26,600 → refund
  assert.equal(r.result, "refund");
  assert.equal(r.refund_due_rounded, 26600);
});

// ============================================================
// Regime comparison
// ============================================================

test("regime comparison: lower-income benefits from new regime", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 1075000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  // Without 80C, new regime wins because of higher std ded (75K vs 50K)
  // and lower slabs.
  const both = engine.computeBothRegimes(wb);
  // New regime: 10L → 50K tax → + 4% cess = 52,000
  // Old regime: 10L → 0-2.5L: 0, 2.5-5L: 12.5K, 5-10L: 100K = 112,500
  //   + 4% cess = 117,000
  assert.ok(both.new.total_tax_liability < both.old.total_tax_liability,
            "new regime should be cheaper for plain 10L salary");
  assert.equal(both.recommendation, "new");
});

test("regime comparison: with 80C, old regime may win", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 1500000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.deductions["80c_total"] = 150000;
  // New regime: 15L, 75K std ded, no 80C → 14,25,000 taxable
  //   Slab: 0-3: 0, 3-7: 20K, 7-10: 30K, 10-12: 15%×2L=30K, 12-15: 20%×3L=60K = 140K
  // Old regime: 15L - 50K std - 150K 80C = 13,00,000 taxable
  //   Slab: 0-2.5: 0, 2.5-5: 12.5K, 5-10: 100K, 10-13: 30%×3L=90K = 202,500
  // So old regime with 80C: 202,500
  // New regime without 80C: 140,000
  // New is still cheaper here
  const both = engine.computeBothRegimes(wb);
  assert.ok(both.new.total_tax_liability < both.old.total_tax_liability);
});

// ============================================================
// AY 2024-25 (FY 2023-24) sanity
// ============================================================

test("AY 2024-25: new regime uses 50K std ded, not 75K", () => {
  const wb = dm.emptyWorkbook("2024-25");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 550000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  const r = engine.computeForRegime(wb, "new");
  // Net salary (new regime, AY 2024-25) = 5,00,000
  assert.equal(r.salary.net_salary, 500000);
  // Slab: 0-3L: 0, 3-5L: 5%×2L = 10,000
  assert.equal(r.pre_rebate_tax, 10000);
  // 87A: ≤ 7L → tax nil
  assert.equal(r.tax_after_rebate, 0);
});

// ============================================================
// Schedule CG
// ============================================================

test("Schedule CG: builds one row per gain type", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.capital_gains.stcg_111a = 50000;
  wb.capital_gains.ltcg_112a = 200000;  // 1L exempt, 1L taxable
  wb.capital_gains.stcg_other = 0;
  wb.capital_gains.ltcg_other = 0;
  // buildScheduleCG works on the raw cap-gains fields, not the
  // computed `computeCapitalGains` output. So it sees the gross
  // amount (200K), not the post-exemption amount (100K). The
  // exemption is documented in the description.
  const r = engine.buildScheduleCG(wb.capital_gains);
  // Should have 2 rows: STCG 111A and LTCG 112A
  assert.equal(r.length, 2);
  const ltcgRow = r.find((x) => x.section === "Bii");
  assert.equal(ltcgRow.amount, 200000);
  // The description should mention the ₹1L exemption
  assert.match(ltcgRow.description, /₹1L|1,00,000|100000/);
});

test("Schedule CG: empty when no gains", () => {
  const wb = dm.emptyWorkbook("2025-26");
  const r = engine.buildScheduleCG(wb.capital_gains);
  assert.equal(r.length, 0);
});

// ============================================================
// Other sources
// ============================================================

test("other sources: interest + dividends summed", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.other_sources.savings_account_interest = 10000;
  wb.other_sources.fd_interest = 25000;
  wb.other_sources.dividend_gross = 15000;
  const r = engine.computeForRegime(wb, "old");
  assert.equal(r.other.net_other_sources, 50000);
});

test("lottery taxed at 30% flat", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 0, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.other_sources.lottery_winnings = 100000;
  const r = engine.computeForRegime(wb, "old");
  assert.equal(r.lottery_tax, 30000);
});

// ============================================================
// House property
// ============================================================

test("house property: self-occupied, only interest deductible", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.house_property.properties = [{
    type: "self-occupied",
    address: "Mumbai",
    rent_received: 0,
    municipal_taxes_paid: 5000,
    home_loan_interest_paid: 250000,  // capped at 2L
    home_loan_principal_paid: 100000,  // goes to 80C
    co_ownership_share: 100,
    tds_on_rent: 0,
  }];
  const r = engine.computeForRegime(wb, "old");
  // House property net = 0 - 5000 - 200000 = -2,05,000 → 0 (can't be negative)
  assert.equal(r.house.net_house_property, 0);
  // But interest paid is captured for reference
  assert.equal(r.house.total_interest, 250000);
});

test("house property: let-out, 30% std deduction", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 500000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.house_property.properties = [{
    type: "let-out",
    address: "Bangalore",
    rent_received: 240000,
    municipal_taxes_paid: 10000,
    home_loan_interest_paid: 100000,
    home_loan_principal_paid: 0,
    co_ownership_share: 100,
    tds_on_rent: 0,
  }];
  const r = engine.computeForRegime(wb, "old");
  // NAV = 2,40,000
  // 30% std = 72,000
  // Municipal = 10,000
  // Interest = 100,000
  // Net = 2,40,000 - 72,000 - 10,000 - 100,000 = 58,000
  assert.equal(r.house.net_house_property, 58000);
});

// ============================================================
// Deductions: caps
// ============================================================

test("deductions: 80C capped at 1.5L", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 500000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.deductions["80c_total"] = 500000;  // user claims 5L, capped at 1.5L
  const r = engine.computeForRegime(wb, "old");
  assert.equal(r.deductions.c80c, 150000);
});

test("deductions: 80TTA only in old regime", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "", gross_salary: 500000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  wb.deductions["80tta"] = 15000;
  wb.deductions["80ttb"] = 60000;
  const old = engine.computeForRegime(wb, "old");
  const newR = engine.computeForRegime(wb, "new");
  // Old: 80TTA capped at 10K, 80TTB capped at 50K
  assert.equal(old.deductions.c80tta, 10000);
  assert.equal(old.deductions.c80ttb, 50000);
  // New: both 0
  assert.equal(newR.deductions.c80tta, 0);
  assert.equal(newR.deductions.c80ttb, 0);
});
