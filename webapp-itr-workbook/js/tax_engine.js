// js/tax_engine.js
// Indian income-tax computation engine for AY 2025-26 (FY 2024-25)
// and AY 2024-25 (FY 2023-24).
//
// What this module does:
//   - Takes a workbook (see data_model.js) and returns the full tax
//     computation: gross total income, deductions, taxable income,
//     slab-wise tax, rebate u/s 87A, surcharge, 4% H&E cess, and the
//     final tax payable / refund due.
//   - Computes BOTH the old regime (default 1961 IT Act slabs) and
//     the new regime (Section 115BAC, post-Finance-Act-2020).
//   - Applies the standard deduction (auto for new regime, opt-in
//     for old).
//   - Applies the ₹1,00,000 LTCG exemption (Section 112A) before
//     computing the 10% LTCG tax.
//   - Sets off STCL against STCG, then STCL against LTCG (per
//     Section 70 read with the proviso to Section 10(38)).
//   - Applies rebate u/s 87A: full rebate up to ₹5L (old) / ₹7L
//     (new) of total income.
//   - Applies marginal relief: if tax > income - threshold, tax is
//     capped at the excess.
//
// What this module does NOT do (yet):
//   - Business income (ITR-3/4) — not in v1
//   - Foreign income (Schedule FSI / FA) — not in v1
//   - Crypto / VDA — not in v1
//   - Tax on accumulated balance in recognised provident fund
//     (Section 111A proviso) — not in v1
//   - Schedule CG line-by-line for each scrip (uses aggregate
//     numbers from the workbook; the user imports from the
//     static app or types the totals in).

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
  // Constants — Indian tax slabs by AY
  //
  // IMPORTANT: tax law changes every year. When Finance Act
  // amendments land, ONLY this section needs to change. The
  // rest of the engine reads these constants.
  // ============================================================

  // AY 2025-26 (FY 2024-25) — old regime
  // Rebate u/s 87A makes tax nil for income up to ₹5,00,000.
  const OLD_REGIME_2024_25 = {
    label: "Old regime (default)",
    slabs: [
      { upto: 250000,  rate: 0.00 },
      { upto: 500000,  rate: 0.05 },
      { upto: 1000000, rate: 0.20 },
      { upto: Infinity, rate: 0.30 },
    ],
    standard_deduction: 50000,        // Section 16(ia)
    rebate_87a_max_income: 500000,    // tax = 0 if total income ≤ this
    rebate_87a_max_tax: 12500,        // upper bound on rebate (max tax below threshold)
    surcharge: {
      // Slabs apply to total income exceeding the lower bound
      brackets: [
        { lower: 0,         upper: 50e5,   rate: 0.00 },
        { lower: 50e5,      upper: 1e7,    rate: 0.10 },
        { lower: 1e7,       upper: 2e7,    rate: 0.15 },
        { lower: 2e7,       upper: 5e7,    rate: 0.25 },
        { lower: 5e7,       upper: Infinity, rate: 0.37, note: "37% if income > ₹10 Cr (less than ₹2 Cr capital gains)" },
      ],
      // Marginal relief u/s 87A extends to surcharge for income
      // just above ₹50L — see computeOldRegime for the implementation
    },
    cess_rate: 0.04,                  // 4% Health & Education Cess
  };

  // AY 2025-26 (FY 2024-25) — new regime (Section 115BAC)
  // 8-slab structure post Finance Act 2024 (effective FY 2024-25).
  const NEW_REGIME_2024_25 = {
    label: "New regime (Section 115BAC)",
    slabs: [
      { upto: 300000,  rate: 0.00 },
      { upto: 700000,  rate: 0.05 },
      { upto: 1000000, rate: 0.10 },
      { upto: 1200000, rate: 0.15 },
      { upto: 1500000, rate: 0.20 },
      { upto: Infinity, rate: 0.30 },
    ],
    standard_deduction: 75000,        // raised to 75K in FY 2024-25 new regime
    rebate_87a_max_income: 700000,    // tax = 0 if total income ≤ ₹7L
    rebate_87a_max_tax: 25000,        // upper bound
    surcharge: {
      brackets: [
        { lower: 0,         upper: 50e5,   rate: 0.00 },
        { lower: 50e5,      upper: 1e7,    rate: 0.10 },
        { lower: 1e7,       upper: 2e7,    rate: 0.15 },
        { lower: 2e7,       upper: 5e7,    rate: 0.25 },
        { lower: 5e7,       upper: Infinity, rate: 0.25, note: "Capped at 25% in new regime" },
      ],
    },
    cess_rate: 0.04,
  };

  // AY 2024-25 (FY 2023-24) — same slab structure as AY 2025-26 old
  // regime (Finance Act 2023 set this up). The new regime was the
  // default from FY 2023-24 onwards for salaried individuals.
  // Standard deduction in new regime was ₹50,000 (raised to 75K in
  // FY 2024-25 only).
  const NEW_REGIME_2023_24 = {
    label: "New regime (Section 115BAC)",
    slabs: NEW_REGIME_2024_25.slabs,   // same slab structure
    standard_deduction: 50000,        // 50K in FY 2023-24
    rebate_87a_max_income: 700000,
    rebate_87a_max_tax: 25000,
    surcharge: NEW_REGIME_2024_25.surcharge,
    cess_rate: 0.04,
  };

  /**
   * Return the regime configs that apply for a given AY.
   */
  function getRegimeConfigs(ay) {
    if (ay === "2025-26") {
      return { old: OLD_REGIME_2024_25, new: NEW_REGIME_2024_25 };
    }
    if (ay === "2024-25") {
      // For AY 2024-25, the new regime was the default. We still
      // let the user see the old regime comparison.
      return { old: OLD_REGIME_2024_25, new: NEW_REGIME_2023_24 };
    }
    throw new Error(`No tax slabs defined for AY ${ay}`);
  }

  // ============================================================
  // Formatters
  // ============================================================

  function fmtRs(n) {
    if (n === null || n === undefined || !Number.isFinite(n)) return "₹0";
    const sign = n < 0 ? "-" : "";
    return sign + "₹" + Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  }

  // ============================================================
  // Step 1: Compute gross total income by head
  // ============================================================

  /**
   * Salary head (Section 15-17).
   * Net salary = Gross - exempt u/s 10 - Standard Deduction
   *            - Professional Tax (deductible u/s 16(iii))
   * Note: HRA exemption is computed separately below because it
   * depends on rent paid + city type, which the workbook doesn't
   * currently collect. For v1 we trust the user to enter the
   * "allowances exempt u/s 10" total from their Form 16 Part B.
   */
  function computeNetSalary(salary, regime) {
    if (!salary.employers || salary.employers.length === 0) {
      return { net_salary: 0, gross_salary: 0, exempt_10: 0, std_deduction: 0, prof_tax: 0 };
    }
    let gross = 0, exempt = 0, profTax = 0;
    for (const e of salary.employers) {
      gross += +e.gross_salary || 0;
      exempt += +e.allowances_exempt_10 || 0;
      profTax += +e.professional_tax || 0;
    }
    // Standard deduction is the same for all employers combined
    const stdDed = regime.standard_deduction;
    const net = Math.max(0, gross - exempt - stdDed - profTax);
    return {
      gross_salary: gross,
      exempt_10: exempt,
      standard_deduction: stdDed,
      professional_tax: profTax,
      net_salary: net,
    };
  }

  /**
   * House Property head (Section 22-25).
   * For self-occupied: net annual value (NAV) = 0, only home loan
   *   interest under Section 24(b) is deductible, capped at ₹2L.
   * For let-out / deemed-let-out: NAV = rent received (or higher
   *   of municipal valuation, but for simplicity v1 uses rent
   *   received directly). Deductions: 30% standard deduction,
   *   municipal taxes paid, home loan interest.
   */
  function computeNetHouseProperty(hp) {
    if (!hp.properties || hp.properties.length === 0) {
      return { net_house_property: 0, total_rent: 0, total_municipal_taxes: 0, total_interest: 0 };
    }
    let totalIncome = 0, totalRent = 0, totalMunicipal = 0, totalInterest = 0;
    for (const p of hp.properties) {
      const share = (+p.co_ownership_share || 100) / 100;
      const rent = (+p.rent_received || 0) * share;
      const municipal = (+p.municipal_taxes_paid || 0) * share;
      const interest = (+p.home_loan_interest_paid || 0) * share;
      totalRent += rent;
      totalMunicipal += municipal;
      totalInterest += interest;
      if (p.type === "self-occupied") {
        // NAV = 0, deduct interest up to ₹2L
        const deductibleInterest = Math.min(interest, 200000);
        totalIncome += (0 - municipal - deductibleInterest);
      } else {
        // Let-out: NAV = rent, deduct 30% standard + municipal + interest
        const std = rent * 0.30;
        totalIncome += (rent - std - municipal - interest);
      }
    }
    return {
      net_house_property: Math.max(0, totalIncome),
      total_rent: totalRent,
      total_municipal_taxes: totalMunicipal,
      total_interest: totalInterest,
    };
  }

  /**
   * Other Sources head (Section 56-59).
   * Lottery / crossword winnings are taxed at 30% flat (Section
   * 115BBH) and do NOT benefit from the basic exemption or slab
   * rates. For v1 we keep it simple and aggregate everything.
   */
  function computeNetOtherSources(os) {
    const interest = (+os.savings_account_interest || 0)
                  + (+os.fd_interest || 0)
                  + (+os.rd_interest || 0);
    const dividendGross = (+os.dividend_gross || 0);
    const other = (os.other_misc || []).reduce(
      (s, x) => s + (+x.amount || 0), 0
    );
    const familyPension = +os.family_pension || 0;
    // Lottery taxed at flat 30% — handled separately in the
    // computation (we add it to total income but tax it at 30% flat)
    const lottery = +os.lottery_winnings || 0;
    return {
      interest: interest,
      dividend_gross: dividendGross,
      other: other,
      family_pension: familyPension,
      lottery: lottery,
      // Total income from this head (lottery excluded for slab calc)
      net_other_sources: interest + dividendGross + other + familyPension,
    };
  }

  /**
   * Capital Gains head (Section 45-55).
   * For v1, the workbook just carries the totals. The user imports
   * them from the static Tax P&L app or types them in.
   * The key bit of math: ₹1L LTCG exemption under Section 112A.
   */
  function computeCapitalGains(cg) {
    // Apply brought-forward losses BEFORE computing tax
    const stcg111aBeforeCF = +cg.stcg_111a || 0;
    const ltcg112aBeforeCF = +cg.ltcg_112a || 0;
    const stcgOther = +cg.stcg_other || 0;
    const ltcgOther = +cg.ltcg_other || 0;

    // Set-off order (per Section 70):
    //   STCL brought forward can be set off against STCG first,
    //   then against LTCG.
    //   LTCL brought forward can be set off against LTCG first,
    //   then against STCG.
    // For v1 we do the straightforward set-off: first available
    // STCL against STCG (any kind), then LTCL against LTCG, then
    // any remaining against the other head.
    const stcl = Math.max(0, +cg.stcl_brought_forward || 0);
    const ltcl = Math.max(0, +cg.ltcl_brought_forward || 0);

    const totalStcgBeforeCF = stcg111aBeforeCF + stcgOther;
    const totalLtcgBeforeCF = ltcg112aBeforeCF + ltcgOther;

    // STCL absorbs STCG first
    const stclVsStcg = Math.min(stcl, totalStcgBeforeCF);
    const stclRemaining = stcl - stclVsStcg;
    const stclVsLtcg = Math.min(stclRemaining, totalLtcgBeforeCF);

    const stcgAfterCF = totalStcgBeforeCF - stclVsStcg;
    const ltcgAfterCF = totalLtcgBeforeCF - stclVsLtcg;

    // LTCL absorbs LTCG first
    const ltclVsLtcg = Math.min(ltcl, ltcgAfterCF);
    const ltclRemaining = ltcl - ltclVsLtcg;
    const ltclVsStcg = Math.min(ltclRemaining, stcgAfterCF);

    const finalStcg = stcgAfterCF - ltclVsStcg;
    const finalLtcg = ltcgAfterCF - ltclVsLtcg;

    // ₹1L exemption on 112A LTCG (Section 112A(2))
    const ltcgExemption = Math.min(finalLtcg, 100000);
    const taxableLtcg112a = Math.max(0, finalLtcg - ltcgExemption);

    return {
      stcg_111a_gross: stcg111aBeforeCF,
      stcg_other_gross: stcgOther,
      ltcg_112a_gross: ltcg112aBeforeCF,
      ltcg_other_gross: ltcgOther,
      stcl_used: stclVsStcg + stclVsLtcg,
      ltcl_used: ltclVsLtcg + ltclVsStcg,
      stcl_remaining: Math.max(0, stcl - (stclVsStcg + stclVsLtcg)),
      ltcl_remaining: Math.max(0, ltcl - (ltclVsLtcg + ltclVsStcg)),
      stcg_after_cf: Math.max(0, finalStcg),
      ltcg_exemption_applied: ltcgExemption,
      ltcg_after_cf: taxableLtcg112a,        // after ₹1L exemption
      // Net total for the head
      net_capital_gains: Math.max(0, finalStcg) + taxableLtcg112a,
    };
  }

  /**
   * Deductions under Chapter VI-A. Many sections cap at certain
   * amounts; we apply the caps here.
   * Also: some sections are only available in the OLD regime
   * (80TTA, 80TTB). 80CCD(2) is available in both.
   */
  function computeDeductions(deductions, regimeKind) {
    const c80c = Math.min(+deductions["80c_total"] || 0, 150000);
    const c80ccd1b = Math.min(+deductions["80ccd_1b"] || 0, 50000);
    const c80ccd2 = +deductions["80ccd_2"] || 0;            // no cap
    const c80d = Math.min(+deductions["80d_self_family"] || 0, 25000)
               + Math.min(+deductions["80d_parents"] || 0, 25000);
    // 80D doubles for senior citizens. v1 keeps it simple at 25K each.
    const c80e = +deductions["80e"] || 0;
    const c80g = (+deductions["80g_50pct"] || 0)
               + (+deductions["80g_100pct"] || 0);
    let c80tta = 0;
    let c80ttb = 0;
    if (regimeKind === "old") {
      c80tta = Math.min(+deductions["80tta"] || 0, 10000);
      c80ttb = Math.min(+deductions["80ttb"] || 0, 50000);
    }
    const total = c80c + c80ccd1b + c80ccd2 + c80d + c80e + c80g + c80tta + c80ttb;
    return {
      c80c, c80ccd1b, c80ccd2, c80d, c80e, c80g, c80tta, c80ttb,
      total_deductions: total,
    };
  }

  // ============================================================
  // Step 2: Apply slab rates to the taxable income
  // ============================================================

  /**
   * Compute income tax on a given taxable income using the slab
   * schedule. Returns the tax amount (pre-rebate, pre-cess).
   * Handles the "lower of (a) tax on full income at slab rates
   * and (b) tax on (income - threshold) + max tax below threshold"
   * for marginal relief, but the rebate u/s 87A is applied
   * separately below.
   */
  function computeSlabTax(taxableIncome, regime) {
    if (taxableIncome <= 0) return 0;
    let tax = 0;
    let prev = 0;
    for (const slab of regime.slabs) {
      if (taxableIncome > slab.upto) {
        tax += (slab.upto - prev) * slab.rate;
        prev = slab.upto;
      } else {
        tax += (taxableIncome - prev) * slab.rate;
        return tax;
      }
    }
    return tax;
  }

  /**
   * Apply rebate u/s 87A. If total income ≤ threshold, tax becomes
   * zero (the rebate pays for the entire tax up to the cap).
   * Marginal relief: if tax > total_income - threshold, tax is
   * capped at the excess. (Important when income is just above
   * the threshold.)
   *
   * Returns the POST-rebate tax amount. The rebate applied is
   * (pre-rebate tax) - (returned value).
   */
  function applyRebate87A(tax, totalIncome, regime) {
    if (totalIncome <= regime.rebate_87a_max_income) {
      // Full rebate (up to the cap): tax reduced to zero.
      // (If tax > max_tax for some reason — shouldn't happen for
      //  incomes at the threshold, but defensive — the cap kicks in.)
      const rebate = Math.min(tax, regime.rebate_87a_max_tax);
      return tax - rebate;
    }
    // Marginal relief: tax should not exceed totalIncome - threshold
    const excess = totalIncome - regime.rebate_87a_max_income;
    if (tax > excess) {
      return excess;
    }
    return tax;
  }

  /**
   * Apply surcharge. Surcharge is a percentage of the (post-rebate)
   * tax, depending on total income.
   * For the special 37% surcharge (old regime only), it applies
   * when income > ₹5 Cr AND capital gains are < 25% of total income.
   * v1 implements the standard brackets and ignores the 37% cap.
   */
  function computeSurcharge(tax, totalIncome, regime) {
    if (totalIncome <= 50e5) return { rate: 0, amount: 0 };
    for (const b of regime.surcharge.brackets) {
      if (totalIncome > b.lower && totalIncome <= b.upper) {
        const amount = tax * b.rate;
        return { rate: b.rate, amount };
      }
    }
    // Above the highest bracket — use the last one
    const last = regime.surcharge.brackets[regime.surcharge.brackets.length - 1];
    return { rate: last.rate, amount: tax * last.rate };
  }

  /**
   * Apply 4% Health & Education Cess on (tax + surcharge).
   */
  function computeCess(taxWithSurcharge, regime) {
    return taxWithSurcharge * regime.cess_rate;
  }

  // ============================================================
  // Top-level: compute everything for a workbook
  // ============================================================

  /**
   * Compute the full tax picture for one workbook, under one regime.
   * @param {Object} wb The workbook (data_model.js)
   * @param {"old"|"new"} regimeKind
   * @returns {Object} Detailed computation
   */
  function computeForRegime(wb, regimeKind) {
    // Defensive: if wb is null/undefined or has no ay, build an
    // empty AY 2025-26 workbook inline (so callers don't have to
    // null-check). Mirrors data_model.emptyWorkbook for AY 2025-26.
    if (!wb || !wb.ay) {
      const dm = (typeof window !== "undefined" && window.taxDataModel) ||
                  (typeof require !== "undefined" && require("./data_model.js"));
      if (dm && typeof dm.emptyWorkbook === "function") {
        wb = dm.emptyWorkbook("2025-26");
      } else {
        // Fallback: minimal empty shape (shouldn't happen in practice)
        wb = {
          ay: "2025-26",
          salary: { employers: [], tds_total: 0 },
          house_property: { properties: [] },
          other_sources: {},
          capital_gains: {},
          deductions: {},
          taxes_paid: {},
        };
      }
    }
    const ay = wb.ay;
    const cfgs = getRegimeConfigs(ay);
    const regime = regimeKind === "new" ? cfgs.new : cfgs.old;

    // --- Step 1: gross income by head ---
    const salary = computeNetSalary(wb.salary, regime);
    const house = computeNetHouseProperty(wb.house_property);
    const other = computeNetOtherSources(wb.other_sources);
    const cg = computeCapitalGains(wb.capital_gains);

    // Gross total income
    const gti = salary.net_salary
              + house.net_house_property
              + other.net_other_sources
              + cg.net_capital_gains;

    // --- Step 2: deductions ---
    const deductions = computeDeductions(wb.deductions, regimeKind);
    // Section 80CCD(2) — employer NPS — is deducted from salary, not
    // from GTI. For v1 we keep it in Chapter VI-A total for
    // simplicity. (The exact treatment: 80CCD(2) is allowed over
    // and above 80C, 80CCD(1), 80CCD(1B). We've already capped 80C
    // and 80CCD(1B). 80CCD(2) is uncapped.)

    // Taxable income (cannot be negative)
    const taxableIncome = Math.max(0, gti - deductions.total_deductions);

    // --- Step 3: slab tax ---
    let tax = computeSlabTax(taxableIncome, regime);

    // --- Step 4: special tax on lottery winnings ---
    // 30% flat, no rebate, no slab benefit
    const lotteryTax = other.lottery * 0.30;

    // --- Step 5: rebate 87A (applied to the slab tax) ---
    const preRebateTax = tax;
    tax = applyRebate87A(tax, gti, regime);
    const rebate87a = preRebateTax - tax;

    // --- Step 6: surcharge ---
    const surcharge = computeSurcharge(tax, gti, regime);

    // --- Step 7: cess on (tax + surcharge) ---
    const cess = computeCess(tax + surcharge.amount, regime);

    // --- Step 8: final tax before TDS adjustment ---
    const totalTaxLiability = tax + surcharge.amount + cess + lotteryTax;

    // --- Step 9: TDS / advance tax / self-assessment tax ---
    const tds = computeTotalTds(wb);

    // Tax payable or refund
    const netPayable = totalTaxLiability - tds;
    const result = netPayable >= 0 ? "payable" : "refund";
    const absAmount = Math.abs(netPayable);

    return {
      regime: regimeKind,
      regime_label: regime.label,
      // Income by head
      salary,
      house,
      other,
      cg,
      gti,
      deductions,
      taxable_income: taxableIncome,
      // Tax computation
      pre_rebate_tax: preRebateTax,
      rebate_87a: rebate87a,
      tax_after_rebate: tax,
      surcharge_rate: surcharge.rate,
      surcharge: surcharge.amount,
      cess,
      lottery_tax: lotteryTax,
      total_tax_liability: totalTaxLiability,
      // TDS / payments
      tds_total: tds,
      net_payable: netPayable,
      result: result,
      refund_due: result === "refund" ? absAmount : 0,
      tax_payable: result === "payable" ? absAmount : 0,
      // Round to whole rupees (ITR rounds)
      total_tax_rounded: Math.round(totalTaxLiability),
      refund_due_rounded: result === "refund" ? Math.round(absAmount) : 0,
      tax_payable_rounded: result === "payable" ? Math.round(absAmount) : 0,
    };
  }

  /**
   * Sum all TDS / advance tax / self-assessment tax paid.
   */
  function computeTotalTds(wb) {
    const tp = wb.taxes_paid || {};
    const sp = wb.salary || {};
    return (+sp.tds_total || 0)
         + (+tp.tds_other_than_salary || 0)
         + (+tp.advance_tax || 0)
         + (+tp.self_assessment_tax || 0)
         + (+tp.tcs || 0);
  }

  /**
   * Compute tax under BOTH regimes and return both side-by-side.
   * @param {Object} wb
   * @returns {{old: Object, new: Object, recommendation: "old"|"new", savings: number}}
   */
  function computeBothRegimes(wb) {
    const oldResult = computeForRegime(wb, "old");
    const newResult = computeForRegime(wb, "new");
    const diff = oldResult.total_tax_rounded - newResult.total_tax_rounded;
    return {
      old: oldResult,
      new: newResult,
      recommendation: diff > 0 ? "new" : (diff < 0 ? "old" : "tie"),
      savings: Math.abs(diff),
    };
  }

  // ============================================================
  // Schedule CG builder (for ITR preview)
  // ============================================================

  /**
   * Build the Schedule CG line items from the workbook's capital
   * gains. For v1, since the workbook stores aggregate numbers,
   * we generate one summary row per (head, gain/loss) combination.
   * When the user imports from the static app in a future version,
   * this will generate per-trade rows.
   */
  function buildScheduleCG(cg) {
    const rows = [];
    if (cg.stcg_111a && cg.stcg_111a !== 0) {
      rows.push({
        section: "Ai",  // 111A short-term
        description: "Short-term capital gain on listed equity (Section 111A)",
        amount: cg.stcg_111a,
        tax_rate: "15%",
      });
    }
    if (cg.ltcg_112a && cg.ltcg_112a !== 0) {
      rows.push({
        section: "Bii",  // 112A long-term
        description: "Long-term capital gain on listed equity (Section 112A), post-₹1L exemption",
        amount: cg.ltcg_112a,
        tax_rate: "10% above ₹1L",
      });
    }
    if (cg.stcg_other && cg.stcg_other !== 0) {
      rows.push({
        section: "Aiv",
        description: "Other short-term capital gain (slab rate)",
        amount: cg.stcg_other,
        tax_rate: "slab",
      });
    }
    if (cg.ltcg_other && cg.ltcg_other !== 0) {
      rows.push({
        section: "Biv",
        description: "Other long-term capital gain (Section 112, 20% with indexation)",
        amount: cg.ltcg_other,
        tax_rate: "20% w/ indexation",
      });
    }
    return rows;
  }

  // ============================================================
  // Public API
  // ============================================================

  return {
    getRegimeConfigs,
    // Head-level
    computeNetSalary,
    computeNetHouseProperty,
    computeNetOtherSources,
    computeCapitalGains,
    computeDeductions,
    // Tax-level
    computeSlabTax,
    applyRebate87A,
    computeSurcharge,
    computeCess,
    computeTotalTds,
    // Top-level
    computeForRegime,
    computeBothRegimes,
    // Schedule preview
    buildScheduleCG,
    // Constants (for inspection)
    REGIMES: { OLD_REGIME_2024_25, NEW_REGIME_2024_25, NEW_REGIME_2023_24 },
    fmtRs,
  };
});
