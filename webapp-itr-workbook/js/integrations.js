// js/integrations.js
// External-data integration: Form 16 PDF, Form 26AS JSON, ITR export.
//
// What this module does in v1:
//   - Parses Form 16 PDF text (extracted by PDF.js) into structured
//     fields: gross salary, allowances exempt, TDS, etc.
//   - Parses Form 26AS JSON into TDS records grouped by section
//     (TDS on Salary, TDS on Other than Salary, etc.)
//   - Exports the workbook as an ITR-1 / ITR-2 preview JSON
//     in the schema the IT e-filing portal accepts (best-effort,
//     not a full implementation of the ITR JSON schema).
//
// Important: v1 only supports structured (text-based) Form 16 PDFs.
// Scanned/image PDFs need OCR, which is out of scope for v1.

(function (root, factory) {
  if (typeof window !== "undefined") {
    const api = factory();
    Object.assign(window, api);
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  // ============================================================
  // Form 16 parser
  // ============================================================
  //
  // Form 16 is a 2-page document issued by employers:
  //   Part A: TDS details (from TRACES, includes TAN, TDS amount)
  //   Part B: Salary breakdown (gross, exemptions, deductions)
  //
  // After PDF.js extracts the text, we look for:
  //   - "Gross Salary" / "Salary as per provisions contained in section 17(1)"
  //   - "Less: Allowances to the extent exempt u/s 10" / "Exempt u/s 10"
  //   - "Standard Deduction" / "Standard deduction u/s 16(ia)"
  //   - "Tax on total income" / "Tax"
  //   - "TDS" / "Total Tax Deducted"
  //   - TAN / PAN of employer / employee
  //   - Quarter-wise TDS breakdown
  //
  // Different employers / payroll software produce slightly
  // different layouts. v1 covers the most common format (TRACES
  // PDF) and degrades gracefully when fields aren't found.

  /**
   * Extract the FIRST number from text like "Rs. 12,34,567" or
   * "₹1234567". Returns null if no number found.
   *
   * Strategy: prefer a number that follows a currency marker
   * (Rs. / ₹ / INR / $ / £). If none, return the first number
   * in the string. Critically: extracts only ONE standalone number,
   * does NOT concatenate all digits.
   */
  function parseIndianNumber(text) {
    if (text === null || text === undefined) return null;
    const s = String(text);
    // Pattern: number with optional commas and decimal
    //   12,00,000  (Indian: groups of 2/3)
    //   1,234.50
    //   50000
    //   2,500
    // Number itself: one or more digit groups separated by commas,
    // optional decimal. We allow any grouping (loose) because the
    // input text is messy.
    const NUMBER = String.raw`\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?`;
    // Pattern 1: number after currency marker
    const currencyMatch = s.match(new RegExp(`(?:Rs\\.?|₹|INR|\\$|£)\\s*(${NUMBER})`));
    if (currencyMatch) {
      const n = parseFloat(currencyMatch[1].replace(/,/g, ""));
      if (Number.isFinite(n)) return n;
    }
    // Pattern 2: any standalone number
    const standaloneMatch = s.match(new RegExp(`(${NUMBER})`));
    if (standaloneMatch) {
      const n = parseFloat(standaloneMatch[1].replace(/,/g, ""));
      if (Number.isFinite(n)) return n;
    }
    return null;
  }

  /**
   * Look for a label followed by a number on the same or next
   * line. Returns the first number found after the label, or null.
   */
  function findAmountAfter(text, label, lookAhead = 3) {
    const lines = text.split(/\r?\n/);
    // Pass 1: find lines that START with the label (most specific)
    // e.g., "Total exempt u/s 10:" — this is the summary line
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].toLowerCase().trim();
      if (line.startsWith(label.toLowerCase())) {
        for (let j = i; j < Math.min(i + 2, lines.length); j++) {
          const num = parseIndianNumber(lines[j]);
          if (num !== null && num >= 100) return num;
        }
      }
    }
    // Pass 2: find lines that contain the label as a phrase
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].toLowerCase();
      if (line.includes(label.toLowerCase())) {
        for (let j = i; j < Math.min(i + lookAhead, lines.length); j++) {
          const num = parseIndianNumber(lines[j]);
          if (num !== null && num >= 100) return num;
        }
      }
    }
    return null;
  }

  /**
   * Parse Form 16 PDF text into structured fields.
   * @param {string} pdfText  Plain text extracted by PDF.js
   * @returns {Object} { ok, fields, warnings, errors }
   */
  function parseForm16Text(pdfText) {
    if (typeof pdfText !== "string" || pdfText.trim() === "") {
      return {
        ok: false,
        fields: null,
        warnings: [],
        errors: ["PDF text is empty — was the PDF scanned? v1 supports text-based PDFs only."],
      };
    }

    const warnings = [];
    const fields = {
      employer: { tan: "", name: "" },
      employee: { pan: "", name: "" },
      gross_salary: 0,
      allowances_exempt_10: 0,
      deductions_claimed_by_employer: {
        "80c": 0, "80ccd_1b": 0, "80d": 0, "80e": 0, "80g": 0, "80tte": 0,
      },
      standard_deduction: 0,
      professional_tax: 0,
      tds_total: 0,
      tds_quarterly: { Q1: 0, Q2: 0, Q3: 0, Q4: 0 },
    };

    // --- Extract employer TAN and PAN ---
    // Look for the TAN format (4 letters + 5 digits + 1 letter)
    // anywhere in the text. After "TAN:" or similar prefix.
    const tanMatch = pdfText.match(/TAN[^\n]*?([A-Z]{4}\d{5}[A-Z])/i);
    if (tanMatch) fields.employer.tan = tanMatch[1].toUpperCase();
    else warnings.push("TAN not found in Form 16");

    // --- Extract employee PAN ---
    const panMatch = pdfText.match(/PAN of the Employee[^\n]*?([A-Z]{5}\d{4}[A-Z])/i)
                  || pdfText.match(/\b([A-Z]{5}\d{4}[A-Z])\b/);
    if (panMatch) fields.employee.pan = panMatch[1].toUpperCase();
    else warnings.push("Employee PAN not found in Form 16");

    // --- Gross salary (Section 17(1)) ---
    fields.gross_salary = findAmountAfter(pdfText, "Salary as per provisions")
                       || findAmountAfter(pdfText, "Gross Salary")
                       || findAmountAfter(pdfText, "Total Salary")
                       || 0;
    if (fields.gross_salary === 0) warnings.push("Gross salary not found");

    // --- Allowances exempt u/s 10 ---
    // The label "exempt u/s 10" may appear on one line, with the
    // actual amount on a continuation line ("Total exempt u/s 10: Rs. 2,00,000").
    // We search for the SUMMARY line specifically (not the sub-items
    // like "HRA exempt: Rs. 1,80,000") to get the TOTAL exemption.
    fields.allowances_exempt_10 = findAmountAfter(pdfText, "Total exempt u/s 10", 3)
                               || findAmountAfter(pdfText, "exempt u/s 10", 10)
                               || findAmountAfter(pdfText, "Allowances to the extent exempt", 10)
                               || 0;

    // --- Standard deduction (Section 16(ia)) ---
    fields.standard_deduction = findAmountAfter(pdfText, "Standard Deduction")
                             || findAmountAfter(pdfText, "Standard deduction")
                             || 0;

    // --- Professional tax (Section 16(iii)) ---
    fields.professional_tax = findAmountAfter(pdfText, "Professional Tax")
                            || findAmountAfter(pdfText, "Tax on Profession")
                            || 0;

    // --- TDS total ---
    fields.tds_total = findAmountAfter(pdfText, "Total Tax Deducted")
                    || findAmountAfter(pdfText, "Total TDS")
                    || findAmountAfter(pdfText, "Tax Deducted at Source")
                    || 0;
    if (fields.tds_total === 0) warnings.push("TDS total not found");

    // --- 80C deduction claimed by employer ---
    // Form 16 Part B section 14: "Deductions under section 80C ..."
    // Look for individual sub-items: PPF, ELSS, LIC, etc.
    fields.deductions_claimed_by_employer["80c"] =
      findAmountAfter(pdfText, "80C")
      || findAmountAfter(pdfText, "section 80C")
      || 0;
    fields.deductions_claimed_by_employer["80ccd_1b"] =
      findAmountAfter(pdfText, "80CCD (1B)")
      || findAmountAfter(pdfText, "80CCD(1B)")
      || 0;
    fields.deductions_claimed_by_employer["80d"] =
      findAmountAfter(pdfText, "80D")
      || 0;
    fields.deductions_claimed_by_employer["80e"] =
      findAmountAfter(pdfText, "80E")
      || 0;

    // --- Quarterly TDS (Q1: Apr-Jun, Q2: Jul-Sep, Q3: Oct-Dec, Q4: Jan-Mar) ---
    // Form 16 Annexure shows quarter-wise TDS deducted.
    const qMatch = pdfText.match(/Q[1-4][^0-9]*([\d,]+(?:\.\d+)?)/gi);
    if (qMatch) {
      for (let i = 0; i < Math.min(4, qMatch.length); i++) {
        const num = parseIndianNumber(qMatch[i]);
        if (num !== null) fields.tds_quarterly[`Q${i + 1}`] = num;
      }
    }

    // If the gross salary is unreasonably large, flag it
    if (fields.gross_salary > 100000000) {
      warnings.push("Gross salary seems unusually large (>10 Cr); please verify");
    }
    if (fields.gross_salary < 0) {
      warnings.push("Negative gross salary detected; check parser");
    }

    return {
      ok: true,
      fields,
      warnings,
      errors: [],
    };
  }

  /**
   * Apply Form 16 fields to a workbook's salary section.
   * @param {Object} wb The workbook to update
   * @param {Object} form16 The result of parseForm16Text().fields
   * @returns {Object} { applied, conflicts }
   */
  function applyForm16ToWorkbook(wb, form16) {
    const conflicts = [];
    // If workbook already has salary data, check for conflicts
    const existing = wb.salary;
    if (existing.employers.length > 0 && existing.employers[0].gross_salary > 0) {
      const oldGross = existing.employers[0].gross_salary;
      const newGross = form16.gross_salary;
      if (Math.abs(oldGross - newGross) > 1) {
        conflicts.push({
          field: "salary.employers[0].gross_salary",
          old: oldGross, new: newGross,
          message: "Existing data will be overwritten",
        });
      }
    }
    // Replace the first employer entry with Form 16 data
    wb.salary.employers = [{
      employer_name: form16.employer.name || "Imported from Form 16",
      tan: form16.employer.tan || "",
      gross_salary: form16.gross_salary,
      allowances_exempt_10: form16.allowances_exempt_10,
      professional_tax: form16.professional_tax,
    }];
    wb.salary.tds_total = form16.tds_total;
    // Apply employer-claimed deductions (with user override later)
    if (form16.deductions_claimed_by_employer["80c"] > 0) {
      wb.deductions["80c_total"] = form16.deductions_claimed_by_employer["80c"];
    }
    if (form16.deductions_claimed_by_employer["80ccd_1b"] > 0) {
      wb.deductions["80ccd_1b"] = form16.deductions_claimed_by_employer["80ccd_1b"];
    }
    return { applied: true, conflicts };
  }

  // ============================================================
  // Form 26AS / AIS parser
  // ============================================================
  //
  // Form 26AS is the annual TDS statement. The IT department now
  // also exposes this as JSON via the e-filing portal API. The
  // JSON shape (post-2023) is roughly:
  //   {
  //     "TDS_on_Salary": [{...}],
  //     "TDS_on_Others": [{...}],
  //     "TDS_on_Sale_of_Assets": [...],
  //     ...
  //   }
  //
  // Each record has deductor info, amount, year. We aggregate by
  // section.

  /**
   * Parse Form 26AS JSON into TDS aggregates.
   * @param {Object} json
   * @returns {Object} { ok, by_section: {TDS_on_Salary: total, ...}, total, count, errors }
   */
  function parseForm26ASJson(json) {
    if (!json || typeof json !== "object") {
      return { ok: false, by_section: {}, total: 0, count: 0,
               errors: ["Not a JSON object"] };
    }
    const bySection = {
      "TDS_on_Salary": 0,
      "TDS_on_Others": 0,
      "TDS_on_Sale_of_Assets": 0,
      "TDS_on_Rent": 0,
      "TDS_on_Interest": 0,
      "TDS_on_Dividend": 0,
      "TDS_on_Other_Income": 0,
      "Advance_Tax": 0,
      "Self_Assessment_Tax": 0,
      "TCS": 0,
    };
    let count = 0;

    // The IT department's JSON has variable casing. Match common
    // variants.
    for (const [section, records] of Object.entries(json)) {
      if (!Array.isArray(records)) continue;
      // Find the section key (case-insensitive match)
      const sectionKey = Object.keys(bySection).find((k) =>
        k.toLowerCase() === section.toLowerCase() ||
        k.replace(/_/g, " ").toLowerCase() === section.toLowerCase()
      ) || (section.includes("Salary") ? "TDS_on_Salary"
            : section.includes("Others") ? "TDS_on_Others"
            : section.includes("Rent") ? "TDS_on_Rent"
            : section.includes("Interest") ? "TDS_on_Interest"
            : section.includes("Dividend") ? "TDS_on_Dividend"
            : section.includes("Sale") ? "TDS_on_Sale_of_Assets"
            : section.includes("Advance") ? "Advance_Tax"
            : section.includes("Self") ? "Self_Assessment_Tax"
            : section.toUpperCase().startsWith("TCS") ? "TCS"
            : "TDS_on_Other_Income");

      for (const r of records) {
        // Different fields in different records: "TDS", "Amount",
        // "TaxDeducted", "tax_amount"
        const amt = +r.TDS || +r.Amount || +r.TaxDeducted
                 || +r.tax_amount || +r.tds_amount || 0;
        if (amt > 0) {
          bySection[sectionKey] = (bySection[sectionKey] || 0) + amt;
          count++;
        }
      }
    }

    const total = Object.values(bySection).reduce((s, v) => s + v, 0);
    return { ok: true, by_section: bySection, total, count, errors: [] };
  }

  /**
   * Apply 26AS data to the workbook's taxes_paid section.
   */
  function applyForm26ASToWorkbook(wb, form26as) {
    const b = form26as.by_section;
    // TDS on Salary: already in salary.tds_total; cross-check
    if (b.TDS_on_Salary > 0) {
      const existing = wb.salary.tds_total || 0;
      if (existing > 0 && Math.abs(existing - b.TDS_on_Salary) > 1) {
        // Discrepancy: prefer Form 26AS (authoritative)
        wb.salary.tds_total = b.TDS_on_Salary;
      } else if (existing === 0) {
        wb.salary.tds_total = b.TDS_on_Salary;
      }
    }
    // Other TDS
    const otherTds = (b.TDS_on_Others || 0) + (b.TDS_on_Rent || 0)
                    + (b.TDS_on_Interest || 0) + (b.TDS_on_Dividend || 0)
                    + (b.TDS_on_Other_Income || 0);
    wb.taxes_paid.tds_other_than_salary = otherTds;
    // Advance tax & self-assessment tax
    if (b.Advance_Tax) wb.taxes_paid.advance_tax = b.Advance_Tax;
    if (b.Self_Assessment_Tax) wb.taxes_paid.self_assessment_tax = b.Self_Assessment_Tax;
    if (b.TCS) wb.taxes_paid.tcs = b.TCS;
    return { applied: true, by_section: b, total: form26as.total };
  }

  // ============================================================
  // ITR export
  // ============================================================
  //
  // Export the workbook as a JSON structure that mirrors (a
  // subset of) the ITR-1 / ITR-2 schemas. v1 is best-effort:
  //   - Personal info
  //   - Income heads (salary, house property, other sources, cap gains)
  //   - Deductions (Chapter VI-A)
  //   - Taxes paid
  //   - Computation (regime, GTI, slab tax, rebate, cess, payable)
  //
  // Not in v1:
  //   - The full ITR JSON schema (which is enormous: ~50 KB of
  //     rules, conditional fields, schedules, etc.)
  //   - Direct e-filing integration (requires DSC/Aadhaar OTP)
  //
  // v1's export is for "paste into the ITR utility" workflows
  // and for review by a CA before manual filing.

  /**
   * Convert a workbook to an ITR preview JSON.
   * @param {Object} wb
   * @param {Object} taxResult  Result of computeBothRegimes(wb)
   * @returns {Object}
   */
  function buildItrPreview(wb, taxResult) {
    if (!taxResult || !taxResult.old || !taxResult.new) {
      throw new Error("taxResult is required (use computeBothRegimes)");
    }
    const p = wb.personal;
    const ayInfo = { "2025-26": "2025", "2024-25": "2024" }[wb.ay] || "2025";
    return {
      // ITR-1 / ITR-2 header
      FormName: `ITR-${p.filing_status === "nri" ? "2" : "1"}`,
      AssessmentYear: ayInfo,
      PAN: p.pan,
      Name: p.name,
      DOB: p.dob,
      Address: p.address,
      FilingStatus: p.filing_status,
      BankAccount: p.bank_for_refund,
      // Regime
      NewRegime: p.new_regime || false,
      // Income
      Salary: taxResult.old.salary,
      HouseProperty: taxResult.old.house,
      OtherSources: taxResult.old.other,
      CapitalGains: taxResult.old.cg,
      // Deductions
      Deductions: taxResult.old.deductions,
      // Computation: both regimes, user picks one
      Computation: {
        old_regime: taxResult.old,
        new_regime: taxResult.new,
        recommendation: taxResult.recommendation,
        savings: taxResult.savings,
      },
      // Source tracking
      _source: "ITRready v1",
      _generated_at: new Date().toISOString(),
    };
  }

  return {
    parseForm16Text,
    applyForm16ToWorkbook,
    parseForm26ASJson,
    applyForm26ASToWorkbook,
    buildItrPreview,
    // Exposed for tests
    _internal: { parseIndianNumber, findAmountAfter },
  };
});
