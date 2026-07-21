// js/tests/test_integrations.spec.js
// Suite 3: API Integration & E-Filing tests.
//
// Tests the integration layer: Form 16 PDF parsing, Form 26AS JSON
// parsing, and ITR preview generation. v1 only supports structured
// (text-based) Form 16 PDFs; scanned/image PDFs need OCR (out of scope).

const test = require("node:test");
const assert = require("node:assert/strict");

const dm = require("../data_model.js");
const engine = require("../tax_engine.js");
const integ = require("../integrations.js");

// ============================================================
// Form 16: realistic sample text (TRACES format)
// ============================================================

// Sample Form 16 Part A + Part B text, mimicking a typical
// TRACES-issued PDF. Layouts vary by payroll software, but the
// keywords are consistent.
const SAMPLE_FORM16 = `
FORM NO. 16
[See rule 31(1) of the Income-tax Rules, 1962]
PART A
Certificate under Section 203 of the Income-tax Act, 1961 for
TDS deducted on salary paid to the employee.

Name of the Employer: ACME SOFTWARE PRIVATE LIMITED
TAN of the Employer: MUMA12345E
PAN of the Employee: ABCDE1234F
Name of the Employee: TEST USER
Period: 01/04/2024 to 31/03/2025

Quarterwise Details of TDS Deducted:
Q1 (Apr-Jun): Rs. 12,500
Q2 (Jul-Sep): Rs. 12,500
Q3 (Oct-Dec): Rs. 12,500
Q4 (Jan-Mar): Rs. 12,500
Total Tax Deducted: Rs. 50,000

PART B
Details of Salary Paid and Tax Deducted thereon

1. Salary as per provisions contained in section 17(1): Rs. 12,00,000
2. Less: Allowances to the extent exempt u/s 10:
   - HRA exempt: Rs. 1,80,000
   - LTA exempt: Rs. 20,000
   Total exempt u/s 10: Rs. 2,00,000
3. Standard Deduction u/s 16(ia): Rs. 50,000
4. Professional Tax: Rs. 2,500
5. Deductions under Chapter VI-A:
   a. Section 80C (PPF, ELSS, etc.): Rs. 1,50,000
   b. Section 80CCD(1B) (NPS): Rs. 50,000
   c. Section 80D (Health Insurance): Rs. 25,000
6. Tax on total income: Rs. 85,000
7. Less: Rebate u/s 87A: Rs. 0
8. Tax after rebate: Rs. 85,000
9. Surcharge: Rs. 0
10. Education Cess: Rs. 3,400
11. Total Tax Liability: Rs. 88,400
12. Tax payable / (Refund): Rs. 38,400
`;

// ============================================================
// Form 16 parsing
// ============================================================

test("Form 16: parses TAN correctly", () => {
  const r = integ.parseForm16Text(SAMPLE_FORM16);
  assert.equal(r.ok, true);
  assert.equal(r.fields.employer.tan, "MUMA12345E");
});

test("Form 16: parses employee PAN correctly", () => {
  const r = integ.parseForm16Text(SAMPLE_FORM16);
  assert.equal(r.fields.employee.pan, "ABCDE1234F");
});

test("Form 16: parses gross salary (Section 17(1))", () => {
  const r = integ.parseForm16Text(SAMPLE_FORM16);
  // 12,00,000 → 1200000
  assert.equal(r.fields.gross_salary, 1200000);
});

test("Form 16: parses allowances exempt u/s 10", () => {
  const r = integ.parseForm16Text(SAMPLE_FORM16);
  // 2,00,000
  assert.equal(r.fields.allowances_exempt_10, 200000);
});

test("Form 16: parses standard deduction", () => {
  const r = integ.parseForm16Text(SAMPLE_FORM16);
  // 50,000
  assert.equal(r.fields.standard_deduction, 50000);
});

test("Form 16: parses professional tax", () => {
  const r = integ.parseForm16Text(SAMPLE_FORM16);
  // 2,500
  assert.equal(r.fields.professional_tax, 2500);
});

test("Form 16: parses 80C deduction claimed by employer", () => {
  const r = integ.parseForm16Text(SAMPLE_FORM16);
  assert.equal(r.fields.deductions_claimed_by_employer["80c"], 150000);
});

test("Form 16: parses 80CCD(1B) deduction (NPS)", () => {
  const r = integ.parseForm16Text(SAMPLE_FORM16);
  assert.equal(r.fields.deductions_claimed_by_employer["80ccd_1b"], 50000);
});

test("Form 16: parses 80D deduction (health insurance)", () => {
  const r = integ.parseForm16Text(SAMPLE_FORM16);
  assert.equal(r.fields.deductions_claimed_by_employer["80d"], 25000);
});

test("Form 16: parses total TDS", () => {
  const r = integ.parseForm16Text(SAMPLE_FORM16);
  // Total Tax Deducted: Rs. 50,000
  assert.equal(r.fields.tds_total, 50000);
});

test("Form 16: parses quarterly TDS", () => {
  const r = integ.parseForm16Text(SAMPLE_FORM16);
  // Each quarter: 12,500
  assert.equal(r.fields.tds_quarterly.Q1, 12500);
  assert.equal(r.fields.tds_quarterly.Q2, 12500);
  assert.equal(r.fields.tds_quarterly.Q3, 12500);
  assert.equal(r.fields.tds_quarterly.Q4, 12500);
});

// ============================================================
// Form 16: error handling
// ============================================================

test("Form 16: empty text returns error (v1 limitation)", () => {
  const r = integ.parseForm16Text("");
  assert.equal(r.ok, false);
  assert.ok(r.errors[0].includes("scanned") || r.errors[0].includes("empty"));
});

test("Form 16: null text returns error", () => {
  const r = integ.parseForm16Text(null);
  assert.equal(r.ok, false);
});

test("Form 16: text without expected keywords → fields 0 + warnings", () => {
  const r = integ.parseForm16Text("This is just random text, not a Form 16");
  assert.equal(r.ok, true);  // We don't fail, we just don't find anything
  assert.equal(r.fields.gross_salary, 0);
  // Multiple warnings
  assert.ok(r.warnings.length >= 3);
});

// ============================================================
// Form 16: apply to workbook
// ============================================================

test("Form 16 → workbook: empty workbook gets populated", () => {
  const wb = dm.emptyWorkbook("2025-26");
  const parsed = integ.parseForm16Text(SAMPLE_FORM16);
  const r = integ.applyForm16ToWorkbook(wb, parsed.fields);
  assert.equal(r.applied, true);
  assert.equal(wb.salary.employers.length, 1);
  assert.equal(wb.salary.employers[0].gross_salary, 1200000);
  assert.equal(wb.salary.employers[0].allowances_exempt_10, 200000);
  assert.equal(wb.salary.employers[0].professional_tax, 2500);
  assert.equal(wb.salary.employers[0].tan, "MUMA12345E");
  assert.equal(wb.salary.tds_total, 50000);
  assert.equal(wb.deductions["80c_total"], 150000);
  assert.equal(wb.deductions["80ccd_1b"], 50000);
});

test("Form 16 → workbook: existing salary with mismatch produces conflict", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 900000,  // different from Form 16's 12L
    allowances_exempt_10: 100000, professional_tax: 0,
  }];
  const parsed = integ.parseForm16Text(SAMPLE_FORM16);
  const r = integ.applyForm16ToWorkbook(wb, parsed.fields);
  // The new data overwrites (with a conflict notice)
  assert.equal(wb.salary.employers[0].gross_salary, 1200000);
  // Conflicts list is non-empty
  assert.ok(r.conflicts.length > 0);
});

test("Form 16 → workbook: matching salary produces no conflict", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.salary.employers = [{
    employer_name: "Acme", tan: "MUMA12345E",
    gross_salary: 1200000,  // matches
    allowances_exempt_10: 200000, professional_tax: 2500,
  }];
  wb.salary.tds_total = 50000;
  const parsed = integ.parseForm16Text(SAMPLE_FORM16);
  const r = integ.applyForm16ToWorkbook(wb, parsed.fields);
  assert.equal(r.conflicts.length, 0);
});

// ============================================================
// Form 26AS / AIS JSON
// ============================================================

test("Form 26AS: parses TDS_on_Salary section", () => {
  const json = {
    "TDS_on_Salary": [
      { TAN: "MUMA12345E", PAN: "ABCDE1234F", TDS: 50000, Year: "2024-25" },
    ],
  };
  const r = integ.parseForm26ASJson(json);
  assert.equal(r.ok, true);
  assert.equal(r.by_section.TDS_on_Salary, 50000);
  assert.equal(r.total, 50000);
  assert.equal(r.count, 1);
});

test("Form 26AS: parses multiple sections (TDS + advance tax)", () => {
  const json = {
    "TDS_on_Salary": [
      { TAN: "MUMA12345E", TDS: 50000 },
    ],
    "TDS_on_Others": [
      { TAN: "MUMB45678F", TDS: 5000 },   // bank interest
      { TAN: "DELH12345G", TDS: 3000 },   // FD interest
    ],
    "Advance_Tax": [
      { Amount: 20000, BSRCode: "1234567" },
    ],
    "Self_Assessment_Tax": [
      { Amount: 10000 },
    ],
  };
  const r = integ.parseForm26ASJson(json);
  assert.equal(r.by_section.TDS_on_Salary, 50000);
  assert.equal(r.by_section.TDS_on_Others, 8000);  // 5K + 3K
  assert.equal(r.by_section.Advance_Tax, 20000);
  assert.equal(r.by_section.Self_Assessment_Tax, 10000);
  assert.equal(r.total, 88000);
  // 1 + 2 + 1 + 1 = 5 records with non-zero amounts
  assert.equal(r.count, 5);
});

test("Form 26AS: case-insensitive section matching", () => {
  const json = {
    "tds_on_salary": [{ TDS: 10000 }],  // lowercase
  };
  const r = integ.parseForm26ASJson(json);
  assert.equal(r.by_section.TDS_on_Salary, 10000);
});

test("Form 26AS: alternate field names (Amount, TaxDeducted)", () => {
  const json = {
    "TDS_on_Salary": [
      { Amount: 12000 },  // not TDS
    ],
    "TDS_on_Others": [
      { TaxDeducted: 5000 },  // not TDS
    ],
  };
  const r = integ.parseForm26ASJson(json);
  assert.equal(r.by_section.TDS_on_Salary, 12000);
  assert.equal(r.by_section.TDS_on_Others, 5000);
});

test("Form 26AS: missing sections don't cause errors", () => {
  const json = {
    "TDS_on_Salary": [{ TDS: 10000 }],
    // No other sections
  };
  const r = integ.parseForm26ASJson(json);
  assert.equal(r.ok, true);
  assert.equal(r.by_section.Advance_Tax, 0);
  assert.equal(r.by_section.Self_Assessment_Tax, 0);
});

test("Form 26AS: invalid JSON object → error", () => {
  assert.equal(integ.parseForm26ASJson(null).ok, false);
  assert.equal(integ.parseForm26ASJson("string").ok, false);
  assert.equal(integ.parseForm26ASJson(123).ok, false);
});

test("Form 26AS: TCS section", () => {
  const json = {
    "TCS": [{ Amount: 1500 }],  // tax collected at source on foreign remittance
  };
  const r = integ.parseForm26ASJson(json);
  assert.equal(r.by_section.TCS, 1500);
});

// ============================================================
// Form 26AS → workbook
// ============================================================

test("Form 26AS → workbook: empty workbook gets TDS from 26AS", () => {
  const wb = dm.emptyWorkbook("2025-26");
  const json = {
    "TDS_on_Salary": [{ TDS: 50000 }],
    "TDS_on_Others": [{ TDS: 5000 }],
    "Advance_Tax": [{ Amount: 10000 }],
  };
  const parsed = integ.parseForm26ASJson(json);
  const r = integ.applyForm26ASToWorkbook(wb, parsed);
  assert.equal(r.applied, true);
  assert.equal(wb.salary.tds_total, 50000);
  assert.equal(wb.taxes_paid.tds_other_than_salary, 5000);
  assert.equal(wb.taxes_paid.advance_tax, 10000);
});

// ============================================================
// ITR preview
// ============================================================

test("ITR preview: full ITR-1 structure with all sections", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.personal.pan = "ABCDE1234F";
  wb.personal.name = "Test User";
  wb.salary.employers = [{
    employer_name: "Acme", tan: "MUMA12345E",
    gross_salary: 1000000, allowances_exempt_10: 100000, professional_tax: 0,
  }];
  wb.salary.tds_total = 50000;
  wb.deductions["80c_total"] = 100000;
  const tax = engine.computeBothRegimes(wb);
  const preview = integ.buildItrPreview(wb, tax);
  // Header
  assert.match(preview.FormName, /ITR-[12]/);
  assert.equal(preview.AssessmentYear, "2025");
  assert.equal(preview.PAN, "ABCDE1234F");
  assert.equal(preview.Name, "Test User");
  // Income
  // Net salary = 10L - 1L exempt - 50K std ded = 8,50,000
  assert.equal(preview.Salary.net_salary, 850000);
  assert.equal(preview.CapitalGains.stcg_after_cf, 0);
  // Computation: both regimes
  assert.ok(preview.Computation.old_regime);
  assert.ok(preview.Computation.new_regime);
  assert.ok(preview.Computation.recommendation);
  assert.equal(preview._source, "ITRready v1");
});

test("ITR preview: NRI user gets ITR-2 (not ITR-1)", () => {
  const wb = dm.emptyWorkbook("2025-26");
  wb.personal.filing_status = "nri";
  wb.salary.employers = [{
    employer_name: "Acme", tan: "",
    gross_salary: 1000000, allowances_exempt_10: 0, professional_tax: 0,
  }];
  const tax = engine.computeBothRegimes(wb);
  const preview = integ.buildItrPreview(wb, tax);
  // NRI must file ITR-2 (ITR-1 doesn't allow NRI status)
  assert.equal(preview.FormName, "ITR-2");
});

// ============================================================
// Round-trip: Form 16 → workbook → tax computation
// ============================================================

test("End-to-end: Form 16 → workbook → tax engine produces consistent numbers", () => {
  const wb = dm.emptyWorkbook("2025-26");
  const parsed = integ.parseForm16Text(SAMPLE_FORM16);
  integ.applyForm16ToWorkbook(wb, parsed.fields);
  // Now compute tax and verify
  const r = engine.computeForRegime(wb, "old");
  // Gross 12L, exempt 2L, std ded 50K, prof tax 2.5K = net 9,47,500
  // (Wait, that's not right. Per Form 16: 12L gross - 2L exempt = 10L. Then std ded and prof tax
  //  are deducted from the salary for net, not added separately. Actually the Form 16
  //  total is: 12L - 2L - 50K - 2.5K = 9,47,500)
  // Engine: gross - exempt_10 - std_ded - prof_tax = 12L - 2L - 50K - 2.5K = 9,47,500
  assert.equal(r.salary.net_salary, 947500);
  // TDS = 50K (top-level field, not nested under salary)
  assert.equal(r.tds_total, 50000);
  // 80C = 1.5L, 80CCD(1B) = 50K → total deductions 2L
  assert.equal(r.deductions.c80c, 150000);
  assert.equal(r.deductions.c80ccd1b, 50000);
  // Taxable = 9,47,500 - 2L = 7,47,500
  // Pre-rebate tax: 0-2.5: 0, 2.5-5: 12.5K, 5-7.475: 20%×2.475L=49,500 = 62K
  assert.equal(r.pre_rebate_tax, 62000);
  // 4% cess = 2,480
  assert.equal(r.cess, 2480);
  // Total = 64,480
  // TDS - tax = 50K - 64,480 = -14,480 (payable)
  // Actually the result should be tax_payable 14,480
  assert.equal(r.tax_payable_rounded, 14480);
});
