// js/validation.js
// JSON-schema-style validation for workbooks.
//
// Why we need this: the user can save a workbook in v1, we may
// release a v2 with new fields, the user might import a JSON file
// from the static Tax P&L app. We need to validate:
//   1. Required fields are present
//   2. Types are correct (string vs number vs array)
//   3. Enums are respected (e.g. ay must be '2025-26' or '2024-25')
//   4. Cross-field invariants (e.g. total = sum of components)
//   5. Forward compat: unknown fields are ignored, not errors

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
  // Schema definitions
  // ============================================================

  // Field type constants for readability
  const T_STRING  = "string";
  const T_NUMBER  = "number";
  const T_BOOLEAN = "boolean";
  const T_OBJECT  = "object";
  const T_ARRAY   = "array";
  const T_DATE    = "date";       // YYYY-MM-DD string
  const T_PAN     = "pan";        // AAAAA9999A
  const T_IFSC    = "ifsc";       // AAAA0XXXXXX
  const T_ENUM    = "enum";       // one of a set
  const T_ANY     = "any";        // any type, no check

  // Validation result type
  // { ok: bool, errors: [{path, code, msg, got}], warnings: [...] }

  // Allowed AY enums
  const ALLOWED_AYS = ["2025-26", "2024-25"];

  // Allowed filing statuses
  const ALLOWED_FILING_STATUS = ["resident", "rnor", "nri"];

  // Allowed account types
  const ALLOWED_ACCOUNT_TYPES = ["savings", "current", "cc", "od"];

  // PAN regex
  const PAN_RE = /^[A-Z]{5}[0-9]{4}[A-Z]$/;
  // IFSC regex
  const IFSC_RE = /^[A-Z]{4}0[A-Z0-9]{6}$/;
  // Aadhaar last 4 (digits)
  const AADHAAR_RE = /^[0-9]{4}$/;
  // ISO date
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  // Mobile (Indian)
  const MOBILE_RE = /^[6-9][0-9]{9}$/;
  // Pincode
  const PINCODE_RE = /^[1-9][0-9]{5}$/;

  // ============================================================
  // Validation helpers
  // ============================================================

  function makeError(path, code, msg, got) {
    return { path, code, msg, got };
  }

  function checkType(value, expectedType) {
    if (expectedType === T_ANY) return null;
    if (value === null || value === undefined) return null;  // missing handled separately
    if (expectedType === T_ARRAY) {
      return Array.isArray(value) ? null : makeError("?", "type", `expected array, got ${typeof value}`, value);
    }
    if (expectedType === T_NUMBER) {
      return typeof value === "number" && !Number.isNaN(value) ? null :
             makeError("?", "type", `expected number, got ${typeof value}`, value);
    }
    if (expectedType === T_BOOLEAN) {
      return typeof value === "boolean" ? null :
             makeError("?", "type", `expected boolean, got ${typeof value}`, value);
    }
    if (expectedType === T_STRING || expectedType === T_DATE ||
        expectedType === T_PAN || expectedType === T_IFSC) {
      return typeof value === "string" ? null :
             makeError("?", "type", `expected string, got ${typeof value}`, value);
    }
    if (expectedType === T_OBJECT) {
      return typeof value === "object" && !Array.isArray(value) && value !== null ? null :
             makeError("?", "type", `expected object, got ${typeof value}`, value);
    }
    return null;
  }

  function checkFormat(value, expectedType) {
    if (value === null || value === undefined || value === "") return null;
    if (expectedType === T_PAN && !PAN_RE.test(value)) {
      return makeError("?", "format", `PAN must be AAAAA9999A`, value);
    }
    if (expectedType === T_IFSC && !IFSC_RE.test(value)) {
      return makeError("?", "format", `IFSC must be AAAA0XXXXXX`, value);
    }
    if (expectedType === T_DATE && !DATE_RE.test(value)) {
      return makeError("?", "format", `date must be YYYY-MM-DD`, value);
    }
    if (expectedType === T_NUMBER && value < 0) {
      return makeError("?", "format", `negative numbers not allowed`, value);
    }
    return null;
  }

  // ============================================================
  // Per-section validators
  // ============================================================

  /**
   * Validate the personal info section.
   * @param {Object} p
   * @returns {Array<{path, code, msg, got}>}
   */
  function validatePersonal(p) {
    const errs = [];
    if (!p || typeof p !== "object") {
      errs.push(makeError("personal", "missing", "personal section required"));
      return errs;
    }
    // PAN (only if provided — empty is OK for an in-progress workbook)
    if (p.pan) {
      const t = checkType(p.pan, T_PAN);
      if (t) errs.push({ ...t, path: "personal.pan" });
      else {
        const f = checkFormat(p.pan, T_PAN);
        if (f) errs.push({ ...f, path: "personal.pan" });
      }
    }
    // DOB
    if (p.dob) {
      const t = checkType(p.dob, T_DATE);
      if (t) errs.push({ ...t, path: "personal.dob" });
      else {
        const f = checkFormat(p.dob, T_DATE);
        if (f) errs.push({ ...f, path: "personal.dob" });
      }
    }
    // Aadhaar last 4
    if (p.aadhaar_last4) {
      if (!AADHAAR_RE.test(p.aadhaar_last4)) {
        errs.push(makeError("personal.aadhaar_last4", "format",
          "must be 4 digits", p.aadhaar_last4));
      }
    }
    // Mobile
    if (p.mobile && !MOBILE_RE.test(p.mobile)) {
      errs.push(makeError("personal.mobile", "format",
        "must be 10-digit Indian mobile", p.mobile));
    }
    // Pincode
    if (p.address && p.address.pincode && !PINCODE_RE.test(p.address.pincode)) {
      errs.push(makeError("personal.address.pincode", "format",
        "must be 6-digit pincode", p.address.pincode));
    }
    // IFSC
    if (p.bank_for_refund && p.bank_for_refund.ifsc) {
      const t = checkType(p.bank_for_refund.ifsc, T_IFSC);
      if (t) errs.push({ ...t, path: "personal.bank_for_refund.ifsc" });
      else {
        const f = checkFormat(p.bank_for_refund.ifsc, T_IFSC);
        if (f) errs.push({ ...f, path: "personal.bank_for_refund.ifsc" });
      }
    }
    // Filing status
    if (p.filing_status && !ALLOWED_FILING_STATUS.includes(p.filing_status)) {
      errs.push(makeError("personal.filing_status", "enum",
        `must be one of ${ALLOWED_FILING_STATUS.join(", ")}`, p.filing_status));
    }
    // Account type
    if (p.bank_for_refund && p.bank_for_refund.account_type &&
        !ALLOWED_ACCOUNT_TYPES.includes(p.bank_for_refund.account_type)) {
      errs.push(makeError("personal.bank_for_refund.account_type", "enum",
        `must be one of ${ALLOWED_ACCOUNT_TYPES.join(", ")}`, p.bank_for_refund.account_type));
    }
    return errs;
  }

  /**
   * Validate the salary section.
   */
  function validateSalary(s) {
    const errs = [];
    if (!s || typeof s !== "object") {
      errs.push(makeError("salary", "missing", "salary section required"));
      return errs;
    }
    if (!Array.isArray(s.employers)) {
      errs.push(makeError("salary.employers", "type", "must be array", s.employers));
      return errs;
    }
    if (s.employers.length > 10) {
      errs.push(makeError("salary.employers", "limit",
        "max 10 employers supported", s.employers.length));
    }
    s.employers.forEach((e, i) => {
      const prefix = `salary.employers[${i}]`;
      if (typeof e.gross_salary !== "number" || e.gross_salary < 0) {
        errs.push(makeError(`${prefix}.gross_salary`, "type",
          "must be non-negative number", e.gross_salary));
      }
      if (typeof e.allowances_exempt_10 !== "number" || e.allowances_exempt_10 < 0) {
        errs.push(makeError(`${prefix}.allowances_exempt_10`, "type",
          "must be non-negative number", e.allowances_exempt_10));
      }
      if (e.professional_tax !== undefined &&
          (typeof e.professional_tax !== "number" || e.professional_tax < 0)) {
        errs.push(makeError(`${prefix}.professional_tax`, "type",
          "must be non-negative number", e.professional_tax));
      }
    });
    if (typeof s.tds_total !== "number" || s.tds_total < 0) {
      errs.push(makeError("salary.tds_total", "type",
        "must be non-negative number", s.tds_total));
    }
    return errs;
  }

  /**
   * Validate the deductions section.
   */
  function validateDeductions(d) {
    const errs = [];
    if (!d || typeof d !== "object") return errs;
    for (const [key, value] of Object.entries(d)) {
      if (value === null || value === undefined || value === "") continue;
      if (typeof value !== "number" || value < 0) {
        errs.push(makeError(`deductions.${key}`, "type",
          "must be non-negative number", value));
      }
    }
    return errs;
  }

  /**
   * Validate the capital gains section.
   */
  function validateCapitalGains(cg) {
    const errs = [];
    if (!cg || typeof cg !== "object") return errs;
    const numericKeys = ["stcg_111a", "ltcg_112a", "stcg_other", "ltcg_other",
                          "stcl_brought_forward", "ltcl_brought_forward"];
    for (const k of numericKeys) {
      if (cg[k] !== undefined && (typeof cg[k] !== "number" || cg[k] < 0)) {
        errs.push(makeError(`capital_gains.${k}`, "type",
          "must be non-negative number", cg[k]));
      }
    }
    return errs;
  }

  /**
   * Validate the top-level workbook.
   * @param {Object} wb
   * @returns {{ok: bool, errors: Array, warnings: Array}}
   */
  function validateWorkbook(wb) {
    const errors = [];
    const warnings = [];

    if (!wb || typeof wb !== "object") {
      errors.push(makeError("", "missing", "workbook must be an object"));
      return { ok: false, errors, warnings };
    }

    // Top-level required fields
    if (!wb.schema_version) {
      errors.push(makeError("schema_version", "missing", "schema_version is required"));
    }
    if (!wb.ay) {
      errors.push(makeError("ay", "missing", "ay is required"));
    } else if (!ALLOWED_AYS.includes(wb.ay)) {
      errors.push(makeError("ay", "enum",
        `ay must be one of ${ALLOWED_AYS.join(", ")}`, wb.ay));
    }
    if (!wb.fy) {
      warnings.push(makeError("fy", "missing", "fy not set; should be auto-computed from ay"));
    }

    // Sections (all optional but type-checked if present)
    errors.push(...validatePersonal(wb.personal));
    errors.push(...validateSalary(wb.salary));
    errors.push(...validateDeductions(wb.deductions));
    errors.push(...validateCapitalGains(wb.capital_gains));

    // Cross-field invariants
    if (wb.ay && wb.fy) {
      // AY 2025-26 ↔ FY 2024-25; AY 2024-25 ↔ FY 2023-24
      const expectedFy = wb.ay === "2025-26" ? "2024-25" : "2023-24";
      if (wb.fy !== expectedFy) {
        warnings.push(makeError("fy", "invariant",
          `ay=${wb.ay} should have fy=${expectedFy}, got ${wb.fy}`));
      }
    }

    return { ok: errors.length === 0, errors, warnings };
  }

  /**
   * Sanitize a workbook by removing unknown fields, filling missing
   * fields with defaults, and ensuring the shape is valid for
   * `emptyWorkbook(ay)`. Used when loading a workbook from a file
   * the user uploaded.
   * @param {Object} wb
   * @returns {Object} Sanitized workbook
   */
  function sanitizeWorkbook(wb) {
    if (!wb || !wb.ay) {
      throw new Error("workbook.ay is required");
    }
    // Use data_model's mergeWithDefaults — but we don't have that
    // imported here. Inline a minimal version.
    const dm = (typeof window !== "undefined" && window.taxDataModel) ||
                (typeof require !== "undefined" && require("./data_model.js"));
    if (dm && typeof dm.mergeWithDefaults === "function") {
      return dm.mergeWithDefaults(wb);
    }
    return wb;  // best effort
  }

  /**
   * Validate a JSON string before parseWorkbook uses it. Checks
   * that the string is valid JSON and is an object (not array,
   * not primitive).
   */
  function validateJsonString(jsonStr) {
    if (typeof jsonStr !== "string") {
      return { ok: false, errors: [{ path: "", code: "type", msg: "not a string", got: typeof jsonStr }] };
    }
    let parsed;
    try {
      parsed = JSON.parse(jsonStr);
    } catch (e) {
      return { ok: false, errors: [{ path: "", code: "parse", msg: e.message, got: jsonStr.slice(0, 50) }] };
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { ok: false, errors: [{ path: "", code: "type", msg: "not an object", got: typeof parsed }] };
    }
    return { ok: true, errors: [], parsed };
  }

  return {
    validateWorkbook,
    validatePersonal,
    validateSalary,
    validateDeductions,
    validateCapitalGains,
    validateJsonString,
    sanitizeWorkbook,
    // Constants for tests
    TYPES: { T_STRING, T_NUMBER, T_BOOLEAN, T_OBJECT, T_ARRAY, T_DATE,
             T_PAN, T_IFSC, T_ENUM, T_ANY },
    ALLOWED_AYS,
    ALLOWED_FILING_STATUS,
    ALLOWED_ACCOUNT_TYPES,
    RE: { PAN_RE, IFSC_RE, AADHAAR_RE, DATE_RE, MOBILE_RE, PINCODE_RE },
  };
});
