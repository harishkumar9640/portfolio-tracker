"""
Per-stock "what to watch on results day" mechanism notes for the 8 stocks
in your portfolio.

Each entry follows the same [WHAT] -> [WHO] -> [HOW] pattern used in
sentiment.py: a concrete cause-and-effect story that a normal person can read
and immediately understand.

Used by:
  - earnings_alert.py (the T-2 and T-0 alerts)
  - portfolio_impact.py (could be wired in later as a richer impact model)

Schema (per stock):
  {
    "sector": str,
    "primary_drivers": [str, ...],   # the 3-5 numbers that actually move the stock
    "watch_items": [str, ...],       # what to listen for in the con-call
    "results_day_history": str,      # historical pattern of stock moves on results day
    "management_bellwethers": [str, ...],  # phrases / tone to listen for
  }
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Sector mechanisms for each of the 8 portfolio stocks.
# ---------------------------------------------------------------------------

MECHANISMS: dict[str, dict] = {

    # -----------------------------------------------------------------------
    "ITC": {
        "name": "ITC Limited",
        "sector": "FMCG / Tobacco / Hotels",
        "primary_drivers": [
            "Cigarette net realisations per stick — the single biggest swing "
            "factor. A 5% increase typically lifts cigarette EBIT by 12-15% "
            "because costs (tobacco, packaging) are largely fixed.",
            "Cigarette volume growth — even a 1-2% volume surprise moves the "
            "stock 3-5% on results day because the category is 50%+ of profits "
            "and optically traders extrapolate volumes to future quarters.",
            "FMCG non-tobacco revenue growth — currently 25%+ of revenue but "
            "<10% of profits. The market pays a premium if this segment "
            "delivers >15% YoY growth because it justifies the de-tobacco thesis.",
            "Hotel RevPAR (Revenue per Available Room) — modest in P&L "
            "(<5% of profit) but a strong sentiment trigger because it reflects "
            "discretionary spending and is reported quarterly with crisp numbers.",
            "Paperboards, agri-business — cyclical, low-margin, mostly ignored "
            "by the market unless there's a one-off spike or write-down.",
        ],
        "watch_items": [
            "Did management take a cigarette price hike in the quarter? "
            "(Even Rs 0.50-1 per stick matters.) If yes, expect margin "
            "expansion in the next 2 quarters.",
            "Are cigarette volumes flat-to-positive YoY? Volumes have been "
            "stagnant for years; any growth is treated as a structural "
            "positive by the market.",
            "FMCG margins — are they still in the 'investment phase' (loss-making) "
            "or starting to contribute? ITC's dairy, oats, and snacks segments "
            "have been loss-making for years.",
            "Any state tax/VAT hike on cigarettes announced during the quarter? "
            "Maharashtra, Karnataka, and Tamil Nadu are the ones to watch.",
            "Hotel occupancy and ARR (Average Room Rate) — post-COVID recovery "
            "story; anything >75% occupancy with 15%+ ARR growth is a positive.",
        ],
        "results_day_history": "ITC has historically moved 1.5-3% on results day "
            "in either direction. The stock is a 'slow grind' name — big moves "
            "are rare except on surprise cigarette price hikes or tax shocks. "
            "Average absolute move over the last 8 quarters: ~2.1%.",
        "management_bellwethers": [
            "Phrases like 'cigarette pricing environment remains stable' / "
            "'tax stability' = bullish (no near-term margin pressure).",
            "'FMCG losses moderated' / 'path to profitability' = bullish.",
            "'Cigarette volumes under pressure' / 'illicit trade' = bearish "
            "(illicit trade is ITC's code word for volume loss to smuggled "
            "cheaper brands).",
            "'State tax hikes in X states' = bearish, immediate margin hit.",
        ],
    },

    # -----------------------------------------------------------------------
    "RELIANCE": {
        "name": "Reliance Industries",
        "sector": "Oil-to-Chemicals (O2C) / Telecom (Jio) / Retail",
        "primary_drivers": [
            "GRM (Gross Refining Margin) — the USD/bbl margin Reliance earns "
            "on refining crude. Singapore complex margins are the benchmark. "
            "A $1/bbl GRM move = ~Rs 1,500-2,000 cr in refining EBIT per quarter.",
            "Jio ARPU (Average Revenue Per User) — currently around Rs 200+. "
            "Every Rs 5 ARPU increase = ~Rs 4,000 cr annualised revenue. The "
            "stock moves 2-3% on ARPU beats because it implies tariff-hike "
            "pricing power.",
            "Jio subscriber net adds — 8-10 million/quarter is the steady "
            "state. Anything below 6 million raises 5G capex payback concerns.",
            "Retail store count + same-store sales growth — Reliance Retail "
            "adds 300-500 stores/quarter. The market focuses on same-store "
            "sales growth (SSSG) more than store count because it shows "
            "underlying demand.",
            "Crude oil price — Reliance is a net buyer of crude (imports most "
            "of what it refines). When crude falls, refining margins expand "
            "temporarily because inventory gains kick in. When crude spikes, "
            "GRMs are usually healthy but consumer demand for petrol/diesel "
            "softens at the pump.",
        ],
        "watch_items": [
            "GRM vs Singapore complex — is Reliance earning above or below the "
            "benchmark? Above = operational outperformance.",
            "Jio ARPU trajectory — is it moving up Rs 5-10 per quarter? "
            "Tariff hikes from late 2024 should be flowing through by now.",
            "Retail SSSG (Same Store Sales Growth) — Reliance Retail's single most-watched demand metric. Below 5% YoY = concerning consumer-spending signal; above 10% = strong consumer demand across both fashion and grocery formats.",
            "Any new energy / new commerce capex commentary — the AGM is "
            "where big announcements happen, not the results call.",
            "Net debt trajectory — has it come down QoQ? Deleveraging is a "
            "key bull case; rising net debt is a red flag.",
        ],
        "results_day_history": "Reliance has the widest single-day moves of any "
            "Indian large-cap on results day, often 3-5%, occasionally 7-8% on "
            "big GRM surprises or new-energy announcements. The AGM (June) is "
            "more volatile than the quarterly results.",
        "management_bellwethers": [
            "'GRM above Singapore complex' = bullish operational story.",
            "'Tariff hike fully reflected in ARPU' = bullish Jio monetisation.",
            "'Retail SSSG strong double-digit' = bullish consumer story.",
            "'Capex peak behind us' / 'free cash flow turning positive' = bullish.",
            "'Subsidy under-recoveries' / 'regulatory headwinds' = cautious.",
        ],
    },

    # -----------------------------------------------------------------------
    "JIOFIN": {
        "name": "Jio Financial Services",
        "sector": "NBFC / Fintech / Consumer Credit",
        "primary_drivers": [
            "Loan book size + growth — JIOFIN is in build-out mode. Loan book "
            "growing 30%+ QoQ is the bull case; anything <15% raises the "
            "'can they execute?' question.",
            "Loan mix — what % is personal loans, home loans, MSME, vehicle "
            "finance? Personal loans carry highest yield but also highest "
            "credit cost. RBI has been flagging unsecured personal loans "
            "since late 2023.",
            "Net Interest Margin (NIM) — currently 7-8% range. NIM compression "
            "is expected as the loan book scales; the question is the rate "
            "of compression.",
            "Credit cost / asset quality — JIOFIN is a new lender with no "
            "cycle track record. Any uptick in Stage 2/3 assets is a red flag.",
            "Distribution deals — JIOFIN leverages Jio's 400M+ subscriber base. "
            "Any new partnership announcements (with RIL retail, with external "
            "fintechs) are material.",
        ],
        "watch_items": [
            "Loan book QoQ growth — anything below 20% = market will worry "
            "about execution speed.",
            "Personal loan % of book — if it crosses 40%, RBI scrutiny risk "
            "goes up meaningfully.",
            "Cost-to-income ratio — should be falling as scale builds. If "
            "rising, the operating leverage story is broken.",
            "Any new product launches — used cars, gold loans, supply chain "
            "finance, etc. Each new vertical adds optionality.",
            "Capital adequacy — JIOFIN doesn't need fresh equity capital right now (CRAR well above regulatory minimum), but a Pre-IPO placement or QIP fundraise would be a key event: it would lift loan-book growth capacity and trigger re-rating.",
        ],
        "results_day_history": "JIOFIN only listed in Aug 2023, so history is "
            "limited (5-6 quarters). Moves have been 3-6% on results day, "
            "often on loan-book growth surprises. The stock is high-beta — "
            "expect wide swings on small data points.",
        "management_bellwethers": [
            "'Loan book scaled to Rs X cr' — bullish if above guidance.",
            "'Diversifying loan mix away from unsecured personal loans' — bullish, "
            "addresses RBI concern.",
            "'Distribution synergies with Jio' — bullish, validates the thesis.",
            "'Credit costs normalised' / 'asset quality stable' — bullish.",
            "'Conservative provisioning' — could be bullish (clean book) or "
            "bearish (hiding something); watch the actual numbers.",
        ],
    },

    # -----------------------------------------------------------------------
    "BANKBARODA": {
        "name": "Bank of Baroda",
        "sector": "PSU Bank / Public Sector Bank",
        "primary_drivers": [
            "NIM (Net Interest Margin) — currently ~3.3%. Every 10 bps move = "
            "~Rs 800-1,000 cr in net interest income annualised. RBI rate cuts "
            "in late 2024 take 1-2 quarters to flow through to BoB's NIM "
            "(deposits re-price faster than loans).",
            "GNPA / NNPA (Gross + Net Non-Performing Assets) — the asset "
            "quality cycle. PSU banks have been in a 4-year clean-up; any "
            "fresh slippages from the unsecured personal loan book (RBI's "
            "Sep 2024 warning) is a key watch.",
            "Credit growth — currently 12-15% YoY for PSU banks. Below 10% "
            "is weak; above 15% is strong.",
            "CASA ratio (Current Account Savings Account) — currently ~42%. "
            "BoB's CASA is above peer average. A drop below 40% = funding "
            "cost pressure.",
            "Slippages from corporate vs retail — corporate stress (tier-2 "
            "infra, real estate) vs retail (personal loans, credit cards) "
            "tells different stories.",
        ],
        "watch_items": [
            "NIM trajectory — has it expanded or compressed QoQ? RBI rate "
            "cycle is the biggest swing factor.",
            "GNPA + slippage ratio — anything above 2.5% GNPA with rising "
            "slippages is bearish.",
            "Unsecured personal loan book size — RBI has flagged this for "
            "all PSU banks. Watch this line item carefully.",
            "Provision coverage ratio (PCR) — above 75% is comfortable.",
            "Any merger / consolidation rumours — BoB has been mentioned in every PSU bank consolidation cycle. Ignore the rumour, but watch CRAR (Capital to Risk-Weighted Assets Ratio) and CET1: any capital raise to fund inorganic growth is a dilution event.",
        ],
        "results_day_history": "PSU banks have seen dramatic results-day moves "
            "in recent years: 5-10% on big NPA surprises, 3-6% on NIM beats. "
            "BoB specifically has averaged ~3.5% absolute move on results day "
            "over the last 8 quarters. The stock is highly sensitive to "
            "guidance and slippage commentary.",
        "management_bellwethers": [
            "'NIM expanded QoQ' / 'NIM at top of guidance' — bullish.",
            "'Slippages moderated' / 'fresh stress from corporate book low' — bullish.",
            "'Unsecured retail under watch' / 'tightened underwriting' — neutral "
            "to slightly bearish (RBI pressure).",
            "'Credit growth healthy at X%' — bullish if above peer.",
            "'Provision utilisation lower than expected' — bullish one-off.",
            "'Watch list / standard asset overhang' — bearish if material.",
        ],
    },

    # -----------------------------------------------------------------------
    "NTPCGREEN": {
        "name": "NTPC Green Energy",
        "sector": "Renewable Energy / Solar / Wind",
        "primary_drivers": [
            "Capacity addition (MW commissioned) — the single biggest metric. "
            "NTPC Green is building from 3 GW operational to 12+ GW by FY27. "
            "Quarterly commissioning matters because it directly drives "
            "revenue from the next quarter.",
            "PLF (Plant Load Factor) / Capacity Utilisation — for solar, "
            "22-26% is typical. Below 20% is concerning; above 27% is strong "
            "(could be Rajasthan/Gujarat high-irradiance projects).",
            "Tariff realisation — the PPA (Power Purchase Agreement) tariff "
            "locked in at the time of bidding. Old PPAs at Rs 3-4/unit vs new "
            "ones at Rs 2.5-3/unit — older assets are higher-margin.",
            "Capital work in progress (CWIP) — how much is under construction. "
            "Rising CWIP = future revenue; falling CWIP + rising operational "
            "capacity = execution in full swing.",
            "Module / equipment costs — solar module prices have crashed 50%+ "
            "since 2023. Lower costs = better project IRRs, but already-bid "
            "tariffs don't change (fixed by PPA).",
        ],
        "watch_items": [
            "Quarterly capacity addition — how many MW went operational? "
            "Anything above 500 MW/quarter is a strong execution signal.",
            "Tariff mix disclosure — is the company bidding more aggressively "
            "to win market share (lower IRRs) or holding discipline?",
            "Receivables from discoms — NTPC Green sells mostly to state "
            "discoms, which are notoriously slow payers. Rising receivables "
            ">120 days = working capital stress.",
            "Any new SECI / state tenders won — each GW of new tender win = "
            "future revenue.",
            "Subsidy / SNA (State Nodal Agency) payments for older projects "
            "under central schemes.",
        ],
        "results_day_history": "NTPC Green listed Nov 2023 — limited history. "
            "Moves have been 4-8% on results day, often on capacity-addition "
            "surprises. The stock is sensitive to solar-policy news more than "
            "quarterly numbers, so results-day impact is moderate vs the sector.",
        "management_bellwethers": [
            "'On track for X GW by FY27' — bullish if reaffirmed.",
            "'Tariff discipline maintained' — bullish (no margin destruction).",
            "'Receivables under control' — bullish (working-capital health).",
            "'New tenders won: X GW' — bullish pipeline.",
            "'Module costs easing' — bullish for new project IRRs.",
        ],
    },

    # -----------------------------------------------------------------------
    "KNRCON": {
        "name": "KNR Constructions",
        "sector": "Infrastructure / Highways / EPC",
        "primary_drivers": [
            "Order book / order inflow — KNR's order book is typically 3-4x "
            "annual revenue. New order inflows in the quarter = future revenue "
            "visibility. NHAI awards and state highway orders are the primary "
            "source.",
            "Execution % / revenue growth — how much of the order book is "
            "being converted into revenue each quarter. Execution rate of "
            "25-30% of order book annually is healthy.",
            "Working capital cycle — EPC contractors get stuck when their "
            "working capital blows out (mobilisation advances not released, "
            "retention money locked, sub-contractor dues). Watch debtor days.",
            "Asset divestment — KNR sold its 6-laning hybrid annuity assets "
            "to Reliance Infra in 2023. Any new asset-sale announcements free "
            "up capital for new bids.",
            "Toll collection / HAM project traffic — for the BOT/HAM assets "
            "KNR retains, traffic growth is a steady annuity. Strong traffic "
            "= better-than-estimated cash flows.",
        ],
        "watch_items": [
            "New order inflows in the quarter — any NHAI or state highway "
            "package above Rs 1,000 cr is material for a mid-cap like KNR.",
            "Order book / revenue ratio — should be above 3x for execution "
            "visibility.",
            "Working-capital cycle — debtor days above 90 is a red flag for "
            "an EPC contractor.",
            "Net debt trajectory — KNR has been net-debt-positive for years. "
            "Any move to net cash post asset sales is a positive.",
            "Execution guidance for FY26 — does management reaffirm or "
            "walk back growth guidance?",
        ],
        "results_day_history": "KNR has averaged ~4-5% absolute moves on "
            "results day. The stock is highly sensitive to new order wins — "
            "a single Rs 2,000+ cr NHAI order can move the stock 5-8% even "
            "outside results season.",
        "management_bellwethers": [
            "'Order book at Xx revenue' — bullish if 3.5x+.",
            "'Working capital cycle improved' — bullish.",
            "'New asset divestment completed' — bullish (capital recycling).",
            "'Bid pipeline strong' — bullish forward visibility.",
            "'Sub-contractor / vendor pressure' — bearish if material.",
            "'Election / policy uncertainty' — neutral, just delays orders.",
        ],
    },

    # -----------------------------------------------------------------------
    "IRCON": {
        "name": "IRCON International",
        "sector": "Railway / Infrastructure / PSU EPC",
        "primary_drivers": [
            "Railway capex flow-through — Union Railway Budget allocation is "
            "the biggest top-line driver. Any upward revision in the rail "
            "capex outlook directly translates to IRCON's order book.",
            "Order book size + diversification — IRCON historically was "
            "100% rail. Now ~25-30% comes from international projects, metro, "
            "and highway diversification. Diversification progress is a key "
            "narrative.",
            "Execution rate — railway projects are multi-year. The execution "
            "rate (revenue / order book) for IRCON is typically 20-25% per "
            "year. Faster execution = better cash flows.",
            "International projects — IRCON has projects in Bangladesh, Sri "
            "Lanka, Nepal, Malaysia, Algeria. Forex moves and geopolitical "
            "events (Sri Lanka bankruptcy, Bangladesh political turmoil) "
            "directly affect project margins and receivables.",
            "Working capital and receivables — IRCON's biggest pain point. "
            "Government clients (railways, NHAI) are slow payers. Receivable "
            "days above 120 = working capital stress.",
        ],
        "watch_items": [
            "New order inflows — any new railway project above Rs 2,000 cr "
            "is material. International project wins are bigger news.",
            "Order book diversification % — is the share of non-rail "
            "revenue growing? Above 30% = diversification thesis playing out.",
            "Receivable days + retention money — both have been chronic pain points for IRCON because Indian Railways releases payments in arrears. Any improvement in receivable days below 120 is a working-capital tailwind and frees up cash for new bids.",
            "International project status — Sri Lanka projects were stalled during their forex crisis; any restart of disbursement releases receivables and adds to order-book execution. Bangladesh projects face political risk; watch for contract renegotiation news.",
            "Margin guidance — railway EPC margins are thin (5-8%). Any "
            "expansion = operational outperformance.",
        ],
        "results_day_history": "IRCON moves 3-6% on results day typically. "
            "Government-policy days (Rail Budget, Union Budget) move the "
            "stock more than quarterly numbers. The stock has lower day-to-day "
            "volatility than mid-cap EPC names because of PSU status.",
        "management_bellwethers": [
            "'Railway capex momentum strong' — bullish top-line visibility.",
            "'International projects scaling' — bullish diversification.",
            "'Receivables improving / collections better' — bullish.",
            "'Diversification to non-rail' — bullish if execution follows.",
            "'Forex / geopolitical impact on international projects' — bearish "
            "if material.",
        ],
    },

    # -----------------------------------------------------------------------
    "BALRAMCHIN": {
        "name": "Balrampur Chini Mills",
        "sector": "Sugar / Ethanol / Distillery",
        "primary_drivers": [
            "Sugar realisation (Rs/quintal) — the headline price. Domestic "
            "sugar prices move with state Advised Prices, FRP cane cost, and "
            "global sugar futures (ICE Sugar #11). A Rs 50/quintal move = "
            "~Rs 100-150 cr in EBIT for a mill of BCML's size.",
            "Ethanol realisation + offtake — BCML has ~650 KLPD distillery "
            "capacity. Ethanol procurement prices are set by the government "
            "(Rs/litre, varies by feedstock). Government offtake has been "
            "lumpy — when it flows, margins expand sharply.",
            "Cane crushing + recovery % — how much sugar was extracted from "
            "each tonne of cane. Industry average is 10-11%. Above 11.5% = "
            "operational outperformance; below 10% = drought/quality issues.",
            "Sugar export quota — the government allows export quotas "
            "annually. A quota allocation = direct export revenue at global "
            "prices (which are usually 30-40% above Indian MSP).",
            "FRP (Fair and Remunerative Price) hike — the cane price paid to "
            "farmers. Govt raises FRP every 2 years. A higher FRP without "
            "matching sugar price hike = margin compression.",
        ],
        "watch_items": [
            "Sugar realisation QoQ — is the company getting above Rs 35/kg? "
            "Anything below Rs 32 = margin pressure.",
            "Ethanol offtake update — government has been clearing offtake "
            "lumpily. Any pickup in Q offtake = positive surprise.",
            "Crushing season guidance — BCML's year is Nov-Mar. Quarterly "
            "numbers are dominated by Q3 (Jan-Mar) and Q4 (Apr-Jun).",
            "FRP revision commentary — any hint of an upcoming FRP hike.",
            "Export quota — any allocation for the current sugar year.",
            "Power export (cogeneration) — small but steady revenue; "
            "above 50% PLF is healthy.",
        ],
        "results_day_history": "Sugar stocks move 5-10% on results day, often "
            "the highest single-day volatility in the portfolio. Sugar is "
            "a commodity + policy play — both can swing hard. BCML specifically "
            "has averaged ~6% absolute moves on results day over the last "
            "8 quarters.",
        "management_bellwethers": [
            "'Sugar realisation firm at Rs X/kg' — bullish if above industry avg.",
            "'Ethanol offtake improving / pricing stable' — bullish.",
            "'Crushing season on track' / 'recovery % above X%' — bullish operations.",
            "'Export quota allocated' — direct bullish.",
            "'FRP hike concerns / cane cost pressure' — bearish.",
            "'Sugar inventory buildup' — bearish (supply overhang).",
        ],
    },

}


def get_mechanism(ticker: str) -> dict | None:
    """Return the mechanism dict for a ticker, or None if not configured."""
    return MECHANISMS.get(ticker.upper())


def list_configured_tickers() -> list[str]:
    """Return the list of tickers that have mechanism notes configured."""
    return sorted(MECHANISMS.keys())