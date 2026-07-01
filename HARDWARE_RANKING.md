# Devices & Infrastructure for Your Portfolio Tracker — Ranked by ROI

**Project status as of 2026-07-01:**
- 26MB of historical data in `data/` (1 month of snapshots)
- 118 alert runs logged
- Single point of failure: your Mac + your broadband
- All schedulers run from your Mac (Angel One news at 8:55 AM, MF holdings at 4:30 PM, etc.)
- Server takes 7-15s to build portfolio snapshot (network I/O bound)
- 8 stock portfolio, ₹4-5L total value

---

## The Complete Ranked List (10 items)

### Tier 1: Buy These (real problems, real fixes)

#### 1. VPS (cloud server) — Run the schedulers 24/7

**What it is:** A small cloud server (DigitalOcean, Hetzner, AIC Cloud, etc.) that runs 24/7.

**What it solves:** Right now, if your Mac is asleep or you forgot to start the server, **none of your schedulers fire**. The 8:55 AM news digest, 4:30 PM MF holdings check, 4:35 PM shareholding check — all silently fail. This is your biggest silent failure mode.

**Cost:** ₹500-1500/month (DigitalOcean $6/mo Basic, Hetzner €4/mo, AIC Cloud ₹99/mo for India)

**Effort to set up:** 1 day to deploy your existing pipeline on a VPS, configure cron jobs, set up a simple systemd service

**ROI:** Pays for itself the first time you miss a 10% move because your Mac was asleep at 8:55 AM

**Recommendation:** DigitalOcean Bangalore datacenter ($6/mo, 5-15ms latency) or Hetzner Finland ($4/mo, 150ms latency but more reliable). Skip AIC Cloud unless you want UPI billing.

#### 2. UPS (battery backup) — Survive power cuts

**What it is:** A small UPS that keeps your Mac + router + modem running for 30-60 minutes during a power cut.

**What it solves:** India has frequent power cuts (especially in summer). Without a UPS, a power cut mid-write can corrupt your SQLite database. Even a 5-second blip can take your server offline.

**Cost:** ₹5,000-8,000 for a 600VA UPS (APC, Microtek, or local brand). Lasts 5-7 years.

**Effort to set up:** 30 minutes (plug in Mac + router, configure auto-shutdown)

**ROI:** Pays for itself the first time you avoid a database corruption. **One corrupted `data/db/history.db` file and you've lost all your historical snapshots.**

**Recommendation:** APC BX600C-IN (~₹5,500) or any 600VA pure sine wave UPS

#### 3. Cloud backup (your `data/` folder) — Survive Mac death

**What it is:** Automated, encrypted, off-site backup of your `data/` folder to cloud storage.

**What it solves:** If your Mac dies (theft, hardware failure, liquid spill), you lose all 26MB of historical data. That includes:
- Historical portfolio snapshots (unrecoverable from APIs)
- MF holdings diffs (unrecoverable)
- News alert history (unrecoverable)
- Alert dedup state (you'd re-send duplicates)

**Cost:** $5-10/TB/month on Backblaze B2, Wasabi, or Cloudflare R2. For your 26MB dataset, you'd pay **pennies per month**.

**Effort to set up:** 1 hour. Install `rclone` on your Mac, configure a daily cron job.

**ROI:** Pays for itself the moment you have a hardware failure. **Right now if your Mac dies, you lose everything.**

**Recommendation:** Backblaze B2 with `rclone` (free tool) doing nightly sync. Set retention to 30 days, cost ~₹30-50/month.

---

### Tier 2: Strongly Consider (real improvements, clear ROI)

#### 4. 4G LTE USB modem (internet failover)

**What it is:** A USB 4G modem (JioFi M2S, Airtel 4G dongle) that takes over when your broadband drops.

**What it solves:** Single ISP failure. If your Jio/Airtel broadband is down for 4 hours (common in India), your entire system is offline. The 4G modem keeps you connected.

**Cost:** ₹2,000-3,000 one-time (JioFi M2S) + ₹500-1000/month for a backup SIM with limited data plan (10-20GB/month is enough for your use case)

**Effort to set up:** 30 minutes. Plug in, configure macOS to prefer ethernet with 4G fallback.

**ROI:** Pays for itself the first time your broadband is down for 6+ hours during market hours. **If your ISP goes down at 9 AM, you miss the entire day's news.**

**Recommendation:** JioFi M2S (₹2,800) with a cheap Jio data plan

#### 5. Better router (with automatic failover)

**What it is:** A dual-WAN router that automatically switches between broadband and 4G when one fails.

**What it solves:** Combines #4 (4G modem) with intelligent failover. When broadband dies, the router flips to 4G in seconds.

**Cost:** ₹4,000-10,000 for a MikroTik, TP-Link, or Ubiquiti dual-WAN router

**Effort to set up:** 1-2 hours to configure failover rules

**ROI:** Higher than #4 if you have unstable broadband. Skip if your broadband is reliable.

**Recommendation:** Only buy if you've had 3+ broadband outages in the last year. Otherwise, the 4G USB modem + manual failover is enough.

---

### Tier 3: Useful but Optional (real value, but only after the above)

#### 6. Pi 5 + Hailo-8 (vision-only projects)

**What it is:** A Raspberry Pi 5 ($80) + Hailo-8 AI HAT ($110) for vision-only projects.

**What it solves:** If you ever build a "security camera that detects when your stock hits a target price" or "door camera that sees the courier delivery", this is the right tool. 8GB unified memory, 25W, runs YOLO at 190 FPS.

**Cost:** $200 (₹16,000)

**Effort to set up:** 1 day to install HailoRT, deploy a model

**ROI:** Zero for your current project. You don't have a vision problem. **Skip unless you start a vision project.**

**Recommendation:** Buy only when you have a specific vision use case. Don't pre-buy.

#### 7. NAS (Network Attached Storage) — Local backup target

**What it is:** A small dedicated backup device (Synology, QNAP) that sits on your network and backs up your Mac automatically.

**What it solves:** Adds a local backup layer. With Mac Time Machine + NAS + cloud backup, you have 3-2-1 backup (3 copies, 2 different media, 1 offsite).

**Cost:** ₹15,000-30,000 for a basic 2-bay NAS

**Effort to set up:** 1-2 hours

**ROI:** Medium. The cloud backup (#3) covers the same use case, so don't do both unless you want a local-only backup for speed.

**Recommendation:** Skip the NAS. Cloud backup is enough.

#### 8. External SSD (Time Machine target)

**What it is:** A 1TB portable SSD that you plug in for Time Machine backups.

**What it solves:** Quick local recovery if you accidentally delete a file or your internal SSD fails.

**Cost:** ₹5,000-7,000 for 1TB

**Effort to set up:** 30 minutes (configure Time Machine)

**ROI:** Medium. If you have Time Machine already configured, this is cheap insurance. If not, it's the cheapest "I won't lose my data" upgrade you can buy.

**Recommendation:** Buy if you don't have Time Machine. **₹5k for a 1TB external SSD is the single cheapest reliability win.**

#### 9. UPS for your modem + router (not just Mac)

**What it is:** A small UPS for the network gear (modem + router) that you can't easily UPS separately.

**What it solves:** The Mac UPS covers the Mac. But the modem and router die the moment power cuts, so the Mac can't reach the internet anyway. A second smaller UPS for the network gear, OR a UPS with enough outlets for all 3 devices, solves this.

**Cost:** ₹3,000-5,000 for a 600VA UPS with 4-6 outlets (or use your existing one's spare outlets if it has them)

**Effort to set up:** 5 minutes (just plug in)

**ROI:** High. **Without this, your Mac UPS is useless during a power cut because the router is still dead.**

**Recommendation:** Get a UPS with 4-6 outlets, or add a second small UPS for the network gear.

---

### Tier 4: Don't Buy (no real benefit for your project)

#### 10. Jetson Orin Nano Super / any AI device

**Why not:** Your project is network-I/O bound, not compute bound. The Jetson wouldn't speed up any of your actual work. Save the ₹50-55k for a VPS + UPS + cloud backup + external SSD — that's 4 wins for less than half the price.

**When it WOULD help:** If you ever start a commercial product (an AI appliance you sell to other traders), then a Jetson becomes relevant. Until then, it's a distraction.

---

## The 5-Item Minimum Viable Setup

If you only do **5 things**, do these:

| # | Item | Cost | What it fixes |
|---|---|---|---|
| 1 | **VPS** (DigitalOcean $6/mo) | ₹500/mo | Schedulers run 24/7 |
| 2 | **UPS** (APC 600VA) | ₹5,500 | Survives power cuts |
| 3 | **Cloud backup** (Backblaze B2) | $5/mo | Survives Mac death |
| 4 | **4G USB modem** (JioFi M2S) | ₹2,800 + ₹500/mo | Survives broadband death |
| 5 | **External SSD** (1TB) | ₹5,500 | Local backup, fast recovery |

**Total first-year cost: ~₹26,000 + ₹10,000/month**

Compare to Jetson: ₹50-55k one-time, doesn't fix any of these problems.

---

## Decision Framework: "Should I buy X?"

Ask yourself these 3 questions before any purchase:

1. **What specific failure does this prevent?**
   - Mac dies? Power cut? Broadband down? Data corruption?
   - If you can't name a specific failure, don't buy.

2. **How often does that failure happen?**
   - Weekly? Monthly? Yearly? Once in 5 years?
   - If yearly or rarer, the math might not work.

3. **What's the cost of that failure?**
   - Miss 1 alert = ₹0 (alerts are redundant)
   - Lose 1 day of data = ₹0 (rebuildable)
   - Lose ALL data = priceless
   - Miss a 10% move because alert didn't fire = depends on portfolio

**If you can't clearly answer all 3, the purchase is probably premature.**

---

## What I'd Actually Build vs Buy for You

If you say go, here's the order I'd tackle:

1. **Cloud backup of `data/`** (1 hour of work, ₹300/month)
   - Install `rclone` on your Mac
   - Configure Backblaze B2 bucket
   - Daily cron to sync `data/` to B2
   - **Restores your project from any failure**

2. **Deploy schedulers to a VPS** (1 day of work, ₹500/month)
   - Set up DigitalOcean $6/mo droplet in Bangalore
   - Install Python, copy your `pipeline/` folder
   - Configure systemd service for each scheduler
   - Update the webapp to call `https://your-vps:8000/api/concalls/run` instead of localhost
   - **Makes your alerts 99.9% reliable instead of "whenever your Mac is awake"**

3. **External SSD for Time Machine** (30 min, ₹5,500)
   - Plug in 1TB SSD
   - Configure Time Machine in macOS Settings
   - **Cheapest reliability win possible**

4. **4G USB modem** (30 min, ₹2,800 + ₹500/month)
   - Buy JioFi M2S
   - Configure macOS to prefer ethernet with 4G fallback
   - **Survives broadband outages**

5. **(If broadband is unstable) Better router** (1-2 hours, ₹5,000-10,000)
   - Buy MikroTik or TP-Link dual-WAN
   - Configure automatic failover
   - **No manual intervention needed**

Total: ₹16,300 one-time + ₹1,800/month. Do this and your project is "production grade" in the sense that it survives all common failures.

---

## What I Would NOT Buy (for your project)

- **Jetson Orin Nano Super (₹50-55k)** — doesn't help anything in your project
- **Mac Studio M2 Ultra (₹2.5L)** — overkill, doesn't help
- **High-end GPU** — your code doesn't use CUDA
- **Dedicated co-located server (₹5L+/year)** — way too much
- **Smart-home hub, mesh WiFi, etc.** — solving problems you don't have
- **Another monitor, mechanical keyboard, etc.** — productivity theater, not real improvement

---

## The One Question to Ask Yourself

> "If my Mac dies tonight, how long until I'm back online with all my data?"

Right now the answer is: **weeks** (need to buy a new Mac, reinstall everything, re-fetch all historical data, which is mostly impossible to get back).

After doing items 1-5 above: **30 minutes** (buy a new Mac, restore from cloud backup, point the VPS at the new Mac, done).

**That's the question you should optimize for.** Everything else is noise.
