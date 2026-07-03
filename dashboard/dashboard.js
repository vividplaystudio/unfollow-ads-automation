// ═══════════════════════════════════════════════════════════════
// Ads Dashboard — clean ASA-style keyword management
// ═══════════════════════════════════════════════════════════════

const STATE = {
  data: null,
  range: "7d",
  country: "",
  campaign: "",
  tab: "campaigns",
  sortCol: null,
  sortDir: "desc",
  search: "",
  charts: {},
  selected: new Set(),   // keyword_id (string) of selected rows
  busy: new Set(),       // keyword ids currently processing
};

// ─── API calls (to PHP proxy) ──────────────────────────────────
async function asaCall(method, path, data) {
  const res = await fetch("api.php", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ method, path, data }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.error) {
    throw new Error(body.error || body.message || `HTTP ${res.status}`);
  }
  return body;
}

function toast(msg, type = "") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast show " + type;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 3500);
}

function flashRow(kwId, type) {
  const row = document.querySelector(`tr[data-kwid="${kwId}"]`);
  if (!row) return;
  row.classList.add(type === "success" ? "flash-success" : "flash-error");
  setTimeout(() => {
    row.classList.remove("flash-success");
    row.classList.remove("flash-error");
  }, 1400);
}

function updateSourceKeyword(kwId, patch) {
  const k = (STATE.data?.keywords || []).find(x => String(x.keyword_id) === String(kwId));
  if (k) Object.assign(k, patch);
}

async function setKeywordStatus(k, newStatus) {
  const kwId = String(k.keyword_id);
  STATE.busy.add(kwId);
  try {
    const path = `/campaigns/${k.campaign_id}/adgroups/${k.ad_group_id}/targetingkeywords/${k.keyword_id}`;
    await asaCall("PUT", path, {
      id: k.keyword_id,
      adGroupId: k.ad_group_id,
      status: newStatus,
    });
    updateSourceKeyword(kwId, { status: newStatus });
    flashRow(kwId, "success");
    return { ok: true };
  } catch (e) {
    flashRow(kwId, "error");
    return { ok: false, error: e.message };
  } finally {
    STATE.busy.delete(kwId);
  }
}

async function setKeywordBid(k, newBid) {
  if (!newBid || isNaN(newBid) || newBid <= 0) {
    toast("Invalid bid", "error");
    return { ok: false };
  }
  const kwId = String(k.keyword_id);
  STATE.busy.add(kwId);
  try {
    const path = `/campaigns/${k.campaign_id}/adgroups/${k.ad_group_id}/targetingkeywords/${k.keyword_id}`;
    await asaCall("PUT", path, {
      id: k.keyword_id,
      adGroupId: k.ad_group_id,
      bidAmount: { amount: String(newBid), currency: "USD" },
    });
    updateSourceKeyword(kwId, { bid: parseFloat(newBid) });
    flashRow(kwId, "success");
    return { ok: true };
  } catch (e) {
    flashRow(kwId, "error");
    return { ok: false, error: e.message };
  } finally {
    STATE.busy.delete(kwId);
  }
}

// ─── Bulk actions ─────────────────────────────────────────────
function getSelectedKeywords() {
  return [...STATE.selected]
    .map(id => (STATE.data?.keywords || []).find(k => String(k.keyword_id) === id))
    .filter(Boolean);
}

async function bulkStatusChange(newStatus, label) {
  const keywords = getSelectedKeywords();
  if (!keywords.length) return;
  if (!confirm(`${label} ${keywords.length} keyword${keywords.length === 1 ? "" : "s"}?`)) return;

  const progressToast = document.getElementById("toast");
  progressToast.className = "toast show";
  let done = 0, errors = 0;
  for (let i = 0; i < keywords.length; i += 5) {
    const chunk = keywords.slice(i, i + 5);
    progressToast.textContent = `${label} ${done + 1}–${Math.min(done + chunk.length, keywords.length)} of ${keywords.length}…`;
    const results = await Promise.all(chunk.map(k => setKeywordStatus(k, newStatus)));
    done += chunk.length;
    errors += results.filter(r => !r.ok).length;
    renderTable();
  }
  toast(errors === 0 ? `Done — ${label}d ${keywords.length}` : `${errors} errors`, errors === 0 ? "success" : "error");
  STATE.selected.clear();
  renderTable();
  renderBulkInfo();
}

async function bulkBidMultiply(mult) {
  const keywords = getSelectedKeywords();
  if (!keywords.length) return;
  const pct = ((mult - 1) * 100).toFixed(0);
  if (!confirm(`Change bids by ${mult > 1 ? "+" : ""}${pct}% for ${keywords.length} keyword${keywords.length === 1 ? "" : "s"}?`)) return;

  const progressToast = document.getElementById("toast");
  progressToast.className = "toast show";
  let done = 0, errors = 0;
  for (let i = 0; i < keywords.length; i += 5) {
    const chunk = keywords.slice(i, i + 5);
    progressToast.textContent = `Updating bids ${done + 1}–${Math.min(done + chunk.length, keywords.length)} of ${keywords.length}…`;
    const results = await Promise.all(chunk.map(k => {
      const newBid = Math.max(0.1, (k.bid || 1) * mult).toFixed(2);
      return setKeywordBid(k, newBid);
    }));
    done += chunk.length;
    errors += results.filter(r => !r.ok).length;
    renderTable();
  }
  toast(errors === 0 ? `Updated ${keywords.length} bids` : `${errors} errors`, errors === 0 ? "success" : "error");
  STATE.selected.clear();
  renderTable();
  renderBulkInfo();
}

async function bulkBidCustom() {
  const keywords = getSelectedKeywords();
  if (!keywords.length) return;
  const answer = prompt(`Set bid for ${keywords.length} keyword${keywords.length === 1 ? "" : "s"}\n\nEnter new bid amount (USD):`, "3.00");
  if (!answer) return;
  const bid = parseFloat(answer);
  if (!bid || bid <= 0) { toast("Invalid bid", "error"); return; }

  const progressToast = document.getElementById("toast");
  progressToast.className = "toast show";
  let done = 0, errors = 0;
  for (let i = 0; i < keywords.length; i += 5) {
    const chunk = keywords.slice(i, i + 5);
    progressToast.textContent = `Setting bid to $${bid.toFixed(2)} — ${done + 1}–${Math.min(done + chunk.length, keywords.length)} of ${keywords.length}…`;
    const results = await Promise.all(chunk.map(k => setKeywordBid(k, bid.toFixed(2))));
    done += chunk.length;
    errors += results.filter(r => !r.ok).length;
    renderTable();
  }
  toast(errors === 0 ? `Set bid to $${bid.toFixed(2)} for ${keywords.length}` : `${errors} errors`, errors === 0 ? "success" : "error");
  STATE.selected.clear();
  renderTable();
  renderBulkInfo();
}

// ─── Load data ─────────────────────────────────────────────────
async function loadData() {
  try {
    const res = await fetch("data.json?v=" + Date.now());
    if (!res.ok) throw new Error("HTTP " + res.status);
    STATE.data = await res.json();
    initFilters();
    render();
  } catch (e) {
    console.error("Failed to load data:", e);
    document.getElementById("tableBody").innerHTML =
      `<tr><td colspan="20" class="empty-state"><div class="icon">⚠️</div>Could not load data.json<br><small>${e.message}</small></td></tr>`;
  }
}

function initFilters() {
  if (!STATE.data) return;
  const countries = new Set();
  for (const c of STATE.data.campaigns || []) {
    if (c.country) countries.add(c.country);
  }
  const countrySel = document.getElementById("countryFilter");
  const currentCountry = countrySel.value;
  countrySel.innerHTML = '<option value="">All countries</option>';
  [...countries].sort().forEach(c => {
    countrySel.innerHTML += `<option value="${c}" ${c === currentCountry ? "selected" : ""}>${c}</option>`;
  });

  const campSel = document.getElementById("campaignFilter");
  const currentCamp = campSel.value;
  campSel.innerHTML = '<option value="">All campaigns</option>';
  (STATE.data.campaigns || [])
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name))
    .forEach(c => {
      campSel.innerHTML += `<option value="${c.name}" ${c.name === currentCamp ? "selected" : ""}>${c.name}</option>`;
    });

  const luEl = document.getElementById("lastUpdated");
  if (STATE.data.last_updated) {
    const s = computeStaleness(STATE.data.last_updated);
    luEl.textContent = `${s.emoji} ${s.label}`;
    luEl.title = s.local;
    luEl.className = "staleness-" + s.status;
  } else {
    luEl.textContent = "unknown";
    luEl.className = "staleness-unknown";
  }
  renderStalenessBanner();
}

// ─── Staleness helpers ─────────────────────────────────────────
// Tell the user when a data source has gone stale instead of silently
// showing old numbers. Every refresh path stamps its result with a
// timestamp; we compute age vs Date.now() and bucket into three states.
const STALE_THRESHOLDS_MIN = { fresh: 30, warn: 120 }; // <30m fresh, 30-120m warn, >120m stale

function computeStaleness(timestamp) {
  if (!timestamp) return { status: "unknown", ageMin: null, label: "no data", emoji: "?" };
  const ts = new Date(timestamp);
  if (isNaN(ts.getTime())) return { status: "unknown", ageMin: null, label: "bad ts", emoji: "?" };
  const ageMin = Math.round((Date.now() - ts.getTime()) / 60000);
  let label;
  if (ageMin < 1) label = "just now";
  else if (ageMin < 60) label = `${ageMin}m ago`;
  else if (ageMin < 1440) label = `${Math.round(ageMin / 60)}h ago`;
  else label = `${Math.round(ageMin / 1440)}d ago`;
  let status, emoji;
  if (ageMin <= STALE_THRESHOLDS_MIN.fresh) { status = "fresh"; emoji = "🟢"; }
  else if (ageMin <= STALE_THRESHOLDS_MIN.warn) { status = "warn"; emoji = "🟡"; }
  else { status = "stale"; emoji = "🔴"; }
  return { status, ageMin, label, emoji, iso: ts.toISOString(), local: ts.toLocaleString() };
}

// Cross-source divergence check: when RC daily revenue and Adjust
// daily revenue disagree by >20% on the same settled day, surface it.
// This catches the regional-pricing problem (Adjust uses static USD,
// RC uses what Apple actually collected) AND general pipeline staleness.
// Returns null if there's no concerning divergence in the trailing
// window; otherwise returns a short string describing the worst case.
function computeRcAdjustDivergence() {
  if (!RC.data || !Array.isArray(RC.data.daily_rc)) return null;
  if (!ADJ.data || !Array.isArray(ADJ.data.by_creative_daily)) return null;

  // Adjust events per day (all networks summed — gives the total revenue
  // Adjust thinks it tracked that day, which should approximate RC's
  // new-sub revenue but in static USD prices)
  const adjByDay = new Map();
  for (const r of ADJ.data.by_creative_daily) {
    const d = r.day;
    if (!d) continue;
    const rev = (+r.com_weekly_revenue || 0)
              + (+r.com_monthly_revenue || 0)
              + (+r.com_yearly_revenue || 0);
    adjByDay.set(d, (adjByDay.get(d) || 0) + rev);
  }

  // Check the last 7 SETTLED days (skip yesterday + today, RC still settles)
  const today = new Date();
  const minus = n => {
    const d = new Date(today);
    d.setUTCDate(d.getUTCDate() - n);
    return d.toISOString().slice(0, 10);
  };
  const settled = [];
  for (let n = 2; n <= 8; n++) settled.push(minus(n));

  const rcByDay = new Map(RC.data.daily_rc.map(r => [r.date, +r.revenue || 0]));

  const divergent = [];
  for (const d of settled) {
    const rc = rcByDay.get(d);
    const adj = adjByDay.get(d);
    if (!rc || !adj) continue;
    if (rc <= 200 && adj <= 200) continue; // ignore low-volume noise days
    const ratio = rc / adj;
    // RC includes renewals; Adjust counts new subs only. For this business
    // a typical day sees 50+ renewals = ~$300-500 of "RC-only" revenue
    // on top of ~$1000-1400 of new-sub revenue both sources track. That
    // alone produces a ratio of ~1.3-1.7, which is normal and not worth
    // flagging. We only fire on truly anomalous gaps:
    //   ratio < 0.7  → Adjust over-reports by >30% (the Jun 15 bug pattern)
    //   ratio > 2.0  → RC dramatically higher (Adjust pipeline likely broken)
    if (ratio < 0.7 || ratio > 2.0) {
      const pct = ((ratio - 1) * 100).toFixed(0);
      divergent.push({ date: d, rc, adj, pct });
    }
  }

  if (!divergent.length) return null;
  // Report the worst one
  divergent.sort((a, b) => Math.abs(b.pct) - Math.abs(a.pct));
  const w = divergent[0];
  return {
    summary: `RC vs Adjust diverge on ${w.date}: RC $${Math.round(w.rc)} vs Adjust $${Math.round(w.adj)} (${w.pct}%)`,
    count: divergent.length,
    all: divergent,
  };
}

// Render the global staleness banner across the top of the page. Hidden
// when everything is fresh; turns yellow/red when any source ages out.
function renderStalenessBanner() {
  const banner = document.getElementById("stalenessBanner");
  if (!banner) return;

  // Full RC refresh (last_updated) enriches customer graph / cohort tables.
  // daily_rc_updated_at (rc-fast) feeds the revenue KPIs the user actually
  // watches — True Daily Profit cards, RC daily table, everything money.
  // When daily_rc is fresh, the revenue data on screen IS accurate even if
  // the full walk is stuck. Report both, but demote "Full RC refresh" from
  // "stale" to "warn" whenever rc-fast is keeping revenue current — the
  // KPIs shouldn't drive the user into a panic when the numbers are right.
  const fullSt  = (STATE.data && STATE.data.last_updated)         ? computeStaleness(STATE.data.last_updated)         : null;
  const fastSt  = (STATE.data && STATE.data.daily_rc_updated_at)  ? computeStaleness(STATE.data.daily_rc_updated_at)  : null;
  const revIsFresh = fastSt && fastSt.status === "fresh";

  const sources = [];
  if (fullSt) {
    // Demote full-refresh staleness when rc-fast is holding the fort. The
    // customer walk being late is a cosmetic problem — cohort/retention
    // tables are stale, but the KPI cards are correct.
    const effectiveStatus = (revIsFresh && fullSt.status === "stale") ? "warn" : fullSt.status;
    sources.push({
      name: revIsFresh
        ? "Full RC refresh (cosmetic — revenue KPIs are fresh)"
        : "Full RC refresh",
      ...fullSt,
      status: effectiveStatus,
    });
  }
  if (fastSt) {
    sources.push({ name: "True Daily Profit (fast)", ...fastSt });
  }
  if (META.data && META.data.generated_at) {
    sources.push({ name: "Meta Ads", ...computeStaleness(META.data.generated_at) });
  }
  if (ADJ.data && ADJ.data.generated_at) {
    sources.push({ name: "Adjust", ...computeStaleness(ADJ.data.generated_at) });
  }

  // Cross-source check (Step 6): RC vs Adjust divergence
  const div = computeRcAdjustDivergence();

  // Worst status drives the banner color
  let worst = sources.reduce((acc, s) => {
    const order = { fresh: 0, warn: 1, stale: 2, unknown: 1 };
    return (order[s.status] || 0) > (order[acc] || 0) ? s.status : acc;
  }, "fresh");
  if (div) {
    // Treat divergence as at least a "warn" — it's not necessarily a hard
    // failure but it deserves attention.
    if (worst === "fresh") worst = "warn";
  }

  if (worst === "fresh" || (sources.length === 0 && !div)) {
    banner.style.display = "none";
    return;
  }

  banner.style.display = "";
  banner.className = "staleness-banner staleness-" + worst;
  let headline;
  if (worst === "stale") {
    headline = "⚠ Dashboard data is stale — some refreshes have not completed in over 2 hours.";
  } else if (div && sources.every(s => s.status === "fresh")) {
    headline = "⚡ RC and Adjust disagree on a recent day — verify which source is right.";
  } else {
    headline = "⚡ Some data sources haven't refreshed recently.";
  }
  const staleList = sources
    .filter(s => s.status !== "fresh")
    .map(s => `<li><strong>${s.name}</strong>: ${s.emoji} ${s.label} <span class="staleness-iso">(${s.local})</span></li>`)
    .join("");
  const divList = div
    ? `<li><strong>RC vs Adjust:</strong> ${div.summary}${div.count > 1 ? ` <span class="staleness-iso">(+${div.count - 1} more day(s))</span>` : ""}</li>`
    : "";
  banner.innerHTML = `
    <div class="staleness-headline">${headline}</div>
    <ul class="staleness-list">${staleList}${divList}</ul>
  `;
}

// ─── Helpers ───────────────────────────────────────────────────
const fmt = {
  money: v => v == null ? "—" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  num: v => v == null ? "—" : Number(v).toLocaleString(),
  pct: v => v == null || v === 0 ? "—" : (v < 100 ? v.toFixed(0) : Math.round(v)) + "%",
};

function roasClass(roas) {
  if (!roas || roas === 0) return "roas-none";
  if (roas >= 100) return "roas-high";
  if (roas >= 50) return "roas-mid";
  return "roas-low";
}

function roasBadge(roas, spend, revenue) {
  const profit = (revenue || 0) - (spend || 0);
  if (spend < 15) return `<span class="badge badge-wait">WAIT</span>`;
  if (!revenue || revenue === 0) {
    return `<span class="badge badge-pause">PAUSE · -${fmt.money(Math.abs(profit))}</span>`;
  }
  if (roas >= 100) return `<span class="badge badge-winner">WINNER · +${fmt.money(profit)}</span>`;
  if (roas >= 50) return `<span class="badge badge-watch">WATCH · ${fmt.money(profit)}</span>`;
  if (roas >= 30) return `<span class="badge badge-ok">OK · ${fmt.money(profit)}</span>`;
  return `<span class="badge badge-losing">LOSING · ${fmt.money(profit)}</span>`;
}

function profitHtml(spend, revenue) {
  if (!spend) return `<span class='muted'>—</span>`;
  const profit = (revenue || 0) - spend;
  if (profit > 0) return `<span class='profit-pos'>+${fmt.money(profit)}</span>`;
  return `<span class='profit-neg'>${fmt.money(profit)}</span>`;
}

function matchesFilters(row, includeCampaign = true) {
  if (STATE.country && row.country !== STATE.country) return false;
  if (includeCampaign && STATE.campaign) {
    if (row.campaign !== STATE.campaign && row.name !== STATE.campaign) return false;
  }
  return true;
}

function rangeKey() { return STATE.range; }

function getMetric(row, metric) {
  const val = row[metric + "_" + rangeKey()];
  return val != null ? val : 0;
}

function findKeyword(kwId) {
  return (STATE.data?.keywords || []).find(k => String(k.keyword_id) === String(kwId));
}

// ─── Render KPIs ───────────────────────────────────────────────
function renderKPIs() {
  const campaigns = (STATE.data?.campaigns || []).filter(r => matchesFilters(r, true));
  let spend = 0, revenue = 0, installs = 0, subs = 0;
  let renewals = 0, canceled = 0;
  let weekly = 0, monthly = 0, yearly = 0;
  for (const c of campaigns) {
    spend += getMetric(c, "spend");
    revenue += getMetric(c, "revenue");
    installs += getMetric(c, "installs");
    subs += getMetric(c, "subs");
    renewals += getMetric(c, "renewals");
    canceled += getMetric(c, "canceled");
    weekly += getMetric(c, "weekly_subs");
    monthly += getMetric(c, "monthly_subs");
    yearly += getMetric(c, "yearly_subs");
  }
  const roas = spend > 0 ? revenue / spend * 100 : 0;
  const cpa = installs > 0 ? spend / installs : 0;
  const profit = revenue - spend;

  document.getElementById("kpiSpend").textContent = fmt.money(spend);
  document.getElementById("kpiRevenue").textContent = fmt.money(revenue);
  document.getElementById("kpiRoas").textContent = fmt.pct(roas);
  document.getElementById("kpiSubs").textContent = fmt.num(subs);
  document.getElementById("kpiInstalls").textContent = fmt.num(installs);
  document.getElementById("kpiCpa").textContent = fmt.money(cpa);
  document.getElementById("kpiRenewals").textContent = fmt.num(renewals);
  document.getElementById("kpiCanceled").textContent = fmt.num(canceled);

  const profitEl = document.getElementById("kpiRoasSub");
  profitEl.textContent = (profit >= 0 ? "+" : "-") + fmt.money(Math.abs(profit)) + " profit";
  profitEl.style.color = profit >= 0 ? "var(--success)" : "var(--danger)";

  document.getElementById("kpiSpendSub").textContent = `${campaigns.length} campaigns`;
  document.getElementById("kpiInstallsSub").textContent = installs > 0 ? `${(subs / installs * 100).toFixed(1)}% pay rate` : "—";
  document.getElementById("kpiSubsSub").textContent = `W:${weekly} · M:${monthly} · Y:${yearly}`;
  document.getElementById("kpiCpaSub").textContent = subs > 0 ? `${fmt.money(revenue / subs)} per sub` : "—";
  document.getElementById("kpiRevenueSub").textContent = subs > 0 ? `${subs} paid subs` : "—";
  document.getElementById("kpiRenewalsSub").textContent = subs > 0 ? `${(renewals / subs).toFixed(1)} avg per sub` : "—";
  document.getElementById("kpiCanceledSub").textContent = subs > 0 ? `${(canceled / subs * 100).toFixed(0)}% churn` : "—";
}

// ─── Charts ────────────────────────────────────────────────────
function destroyChart(name) {
  if (STATE.charts[name]) {
    STATE.charts[name].destroy();
    STATE.charts[name] = null;
  }
}

function renderCharts() {
  const campaigns = (STATE.data?.campaigns || []).filter(r => matchesFilters(r, true));

  const byCountry = {};
  for (const c of campaigns) {
    const country = c.country || "?";
    if (!byCountry[country]) byCountry[country] = { spend: 0, revenue: 0 };
    byCountry[country].spend += getMetric(c, "spend");
    byCountry[country].revenue += getMetric(c, "revenue");
  }
  const countries = Object.keys(byCountry).filter(c => byCountry[c].spend > 0).sort();
  destroyChart("country");
  const ctx1 = document.getElementById("chartCountry").getContext("2d");
  STATE.charts.country = new Chart(ctx1, {
    type: "bar",
    data: {
      labels: countries,
      datasets: [
        { label: "Spend", data: countries.map(c => byCountry[c].spend), backgroundColor: "#c7d2fe", borderRadius: 4 },
        { label: "Revenue", data: countries.map(c => byCountry[c].revenue), backgroundColor: "#4f46e5", borderRadius: 4 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "bottom", labels: { usePointStyle: true, padding: 14, font: { size: 12 } } } },
      scales: { y: { beginAtZero: true, ticks: { callback: v => "$" + v } } },
    },
  });

  const sorted = campaigns.slice().sort((a, b) => getMetric(b, "spend") - getMetric(a, "spend")).slice(0, 8);
  destroyChart("campaigns");
  const ctx2 = document.getElementById("chartCampaignSpend").getContext("2d");
  STATE.charts.campaigns = new Chart(ctx2, {
    type: "bar",
    data: {
      labels: sorted.map(c => c.name.length > 28 ? c.name.slice(0, 26) + "…" : c.name),
      datasets: [
        { label: "Spend", data: sorted.map(c => getMetric(c, "spend")), backgroundColor: "#fca5a5", borderRadius: 4 },
        { label: "Revenue", data: sorted.map(c => getMetric(c, "revenue")), backgroundColor: "#10b981", borderRadius: 4 },
      ],
    },
    options: {
      indexAxis: "y", responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: "bottom", labels: { usePointStyle: true, padding: 14, font: { size: 12 } } } },
      scales: { x: { beginAtZero: true, ticks: { callback: v => "$" + v } } },
    },
  });
}

// ─── Table ─────────────────────────────────────────────────────
function renderTable() {
  const head = document.getElementById("tableHead");
  const body = document.getElementById("tableBody");

  let rows = [];
  let cols = [];
  const isKwTab = STATE.tab === "keywords" || STATE.tab === "winners" || STATE.tab === "losers";

  if (STATE.tab === "campaigns") {
    cols = [
      { key: "name", label: "Campaign" },
      { key: "country", label: "Country" },
      { key: "spend", label: "Spend", num: true },
      { key: "revenue", label: "Revenue", num: true },
      { key: "profit", label: "Profit", num: true },
      { key: "roas", label: "ROAS", num: true },
      { key: "subs", label: "Paid", num: true },
      { key: "weekly_subs", label: "W", num: true },
      { key: "monthly_subs", label: "M", num: true },
      { key: "yearly_subs", label: "Y", num: true },
      { key: "renewals", label: "Renew", num: true },
      { key: "canceled", label: "Cancel", num: true },
      { key: "installs", label: "Installs", num: true },
      { key: "cpa", label: "CPA", num: true },
      { key: "status", label: "Status" },
    ];
    rows = (STATE.data?.campaigns || [])
      .filter(r => matchesFilters(r, false))
      .map(c => {
        const spend = getMetric(c, "spend");
        const revenue = getMetric(c, "revenue");
        const installs = getMetric(c, "installs");
        const subs = getMetric(c, "subs");
        const roas = spend > 0 ? revenue / spend * 100 : 0;
        const cpa = installs > 0 ? spend / installs : 0;
        const profit = revenue - spend;
        return {
          ...c, spend, revenue, profit, installs, subs, roas, cpa,
          weekly_subs: getMetric(c, "weekly_subs"),
          monthly_subs: getMetric(c, "monthly_subs"),
          yearly_subs: getMetric(c, "yearly_subs"),
          renewals: getMetric(c, "renewals"),
          canceled: getMetric(c, "canceled"),
          _name: c.name,
        };
      });
  } else if (isKwTab) {
    cols = [
      { key: "select", label: "" },
      { key: "kw_state", label: "State" },
      { key: "keyword", label: "Keyword" },
      { key: "campaign", label: "Campaign" },
      { key: "country", label: "Country" },
      { key: "match", label: "Match" },
      { key: "bid", label: "Bid", num: true },
      { key: "spend", label: "Spend", num: true },
      { key: "revenue", label: "Revenue", num: true },
      { key: "profit", label: "Profit", num: true },
      { key: "roas", label: "ROAS", num: true },
      { key: "subs", label: "Paid", num: true },
      { key: "weekly_subs", label: "W", num: true },
      { key: "monthly_subs", label: "M", num: true },
      { key: "yearly_subs", label: "Y", num: true },
      { key: "installs", label: "Installs", num: true },
      { key: "cpa", label: "CPA", num: true },
      { key: "cpt", label: "CPT", num: true },
      { key: "ttr", label: "TTR", num: true },
      { key: "cr", label: "CR", num: true },
      { key: "status", label: "Status" },
    ];
    rows = (STATE.data?.keywords || [])
      .filter(k => matchesFilters(k, true))
      .map(k => {
        const spend = getMetric(k, "spend");
        const revenue = getMetric(k, "revenue");
        const installs = getMetric(k, "installs");
        const subs = getMetric(k, "subs");
        const taps = getMetric(k, "taps");
        const imp = getMetric(k, "impressions");
        const roas = spend > 0 ? revenue / spend * 100 : 0;
        const cpa = installs > 0 ? spend / installs : 0;
        const ttr = imp > 0 ? taps / imp * 100 : 0;
        const cr = taps > 0 ? installs / taps * 100 : 0;
        const cpt = taps > 0 ? spend / taps : 0;
        const profit = revenue - spend;
        return {
          ...k, spend, revenue, profit, installs, subs, taps, imp, roas, cpa, ttr, cr, cpt,
          weekly_subs: getMetric(k, "weekly_subs"),
          monthly_subs: getMetric(k, "monthly_subs"),
          yearly_subs: getMetric(k, "yearly_subs"),
        };
      });

    const minSpend = STATE.range === "today" ? 5 : STATE.range === "yesterday" ? 10 : 15;
    if (STATE.tab === "winners") {
      rows = rows.filter(r => r.spend >= minSpend && r.roas >= 100);
    } else if (STATE.tab === "losers") {
      rows = rows.filter(r => r.spend >= minSpend && (r.roas < 30 || r.revenue === 0));
    }
  } else if (STATE.tab === "ads") {
    cols = [
      { key: "name", label: "Ad / CPP" },
      { key: "campaign", label: "Campaign" },
      { key: "country", label: "Country" },
      { key: "cpp_id", label: "CPP ID" },
      { key: "spend", label: "Spend", num: true },
      { key: "installs", label: "Installs", num: true },
      { key: "impressions", label: "Impressions", num: true },
      { key: "taps", label: "Taps", num: true },
      { key: "cpa", label: "CPA", num: true },
      { key: "ttr", label: "TTR", num: true },
    ];
    rows = (STATE.data?.ads || [])
      .filter(r => matchesFilters(r, true))
      .map(a => {
        const spend = getMetric(a, "spend");
        const installs = getMetric(a, "installs");
        const taps = getMetric(a, "taps");
        const imp = getMetric(a, "impressions");
        const cpa = installs > 0 ? spend / installs : 0;
        const ttr = imp > 0 ? taps / imp * 100 : 0;
        return { ...a, spend, installs, taps, imp, impressions: imp, cpa, ttr };
      });
  } else if (STATE.tab === "channels") {
    cols = [
      { key: "channel", label: "Channel" },
      { key: "users", label: "Users", num: true },
      { key: "subs", label: "Paid", num: true },
      { key: "pay_rate", label: "Pay %", num: true },
      { key: "revenue", label: "Revenue", num: true },
      { key: "rev_per_user", label: "Rev/User", num: true },
      { key: "rev_per_sub", label: "Rev/Sub", num: true },
      { key: "weekly_subs", label: "W", num: true },
      { key: "monthly_subs", label: "M", num: true },
      { key: "yearly_subs", label: "Y", num: true },
      { key: "renewals", label: "Renew", num: true },
      { key: "canceled", label: "Cancel", num: true },
      { key: "active", label: "Active", num: true },
    ];
    rows = (STATE.data?.channels || []).map(c => {
      const users = getMetric(c, "users");
      const subs = getMetric(c, "subs");
      const revenue = getMetric(c, "revenue");
      return {
        ...c,
        users, subs, revenue,
        pay_rate: users > 0 ? (subs / users * 100) : 0,
        rev_per_user: users > 0 ? revenue / users : 0,
        rev_per_sub: subs > 0 ? revenue / subs : 0,
        active: getMetric(c, "active"),
        canceled: getMetric(c, "canceled"),
        renewals: getMetric(c, "renewals"),
        weekly_subs: getMetric(c, "weekly_subs"),
        monthly_subs: getMetric(c, "monthly_subs"),
        yearly_subs: getMetric(c, "yearly_subs"),
      };
    }).filter(r => r.users > 0 || r.revenue > 0);
  } else if (STATE.tab === "adgroups") {
    cols = [
      { key: "ad_group", label: "Ad Group" },
      { key: "campaign", label: "Campaign" },
      { key: "revenue", label: "Revenue", num: true },
      { key: "subs", label: "Paid", num: true },
      { key: "weekly_subs", label: "W", num: true },
      { key: "monthly_subs", label: "M", num: true },
      { key: "yearly_subs", label: "Y", num: true },
      { key: "active", label: "Active", num: true },
      { key: "canceled", label: "Cancel", num: true },
      { key: "renewals", label: "Renew", num: true },
    ];
    rows = (STATE.data?.ad_groups || [])
      .filter(r => matchesFilters(r, true))
      .map(a => ({
        ...a,
        revenue: getMetric(a, "revenue"),
        subs: getMetric(a, "subs"),
        active: getMetric(a, "active"),
        canceled: getMetric(a, "canceled"),
        renewals: getMetric(a, "renewals"),
        weekly_subs: getMetric(a, "weekly_subs"),
        monthly_subs: getMetric(a, "monthly_subs"),
        yearly_subs: getMetric(a, "yearly_subs"),
      }));
  }

  if (STATE.search) {
    const s = STATE.search.toLowerCase();
    rows = rows.filter(r => Object.values(r).some(v => String(v).toLowerCase().includes(s)));
  }

  const sortCol = STATE.sortCol || "spend";
  const dir = STATE.sortDir === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    let va = a[sortCol] ?? 0;
    let vb = b[sortCol] ?? 0;
    if (typeof va === "string" && typeof vb === "string") return va.localeCompare(vb) * dir;
    return (va - vb) * dir;
  });

  // Headers — with header checkbox for keyword tabs
  head.innerHTML = "<tr>" + cols.map(c => {
    if (c.key === "select") {
      // Select-all checkbox — checked if all visible are selected
      const visibleIds = rows.filter(r => r.keyword_id).map(r => String(r.keyword_id));
      const allSelected = visibleIds.length > 0 && visibleIds.every(id => STATE.selected.has(id));
      return `<th class="select-col"><input type="checkbox" class="row-check" id="selectAllCheck" ${allSelected ? "checked" : ""} /></th>`;
    }
    const isSorted = sortCol === c.key;
    const sortClass = isSorted ? `sorted${STATE.sortDir === "asc" ? "-asc" : ""}` : "";
    const title = c.label === "W" ? "Weekly Subs" : c.label === "M" ? "Monthly Subs" : c.label === "Y" ? "Yearly Subs" : c.label;
    return `<th class="${c.num ? "num" : ""} ${sortClass}" data-col="${c.key}" title="${title}">${c.label}</th>`;
  }).join("") + "</tr>";

  // Body
  if (rows.length === 0) {
    body.innerHTML = `<tr><td colspan="${cols.length}"><div class="empty-state"><div class="icon">📭</div>No data for this view/range</div></td></tr>`;
  } else {
    body.innerHTML = rows.map(r => {
      const classes = [];
      if (STATE.tab === "campaigns") classes.push("clickable");
      if (r.status === "PAUSED") classes.push("row-paused");
      if (r.keyword_id && STATE.selected.has(String(r.keyword_id))) classes.push("row-selected");
      const kwidAttr = r.keyword_id ? `data-kwid="${r.keyword_id}"` : "";
      return `<tr class='${classes.join(" ")}' ${kwidAttr} data-name='${(r.name || r.keyword || "").replace(/'/g, "&#39;")}'>` + cols.map(c => {
        let val = r[c.key];
        let content, cls = c.num ? "num" : "";
        switch (c.key) {
          case "select":
            content = r.keyword_id
              ? `<input type="checkbox" class="row-check" data-action="select" data-kw-id="${r.keyword_id}" ${STATE.selected.has(String(r.keyword_id)) ? "checked" : ""} />`
              : "";
            cls = "select-col";
            break;
          case "spend":
          case "revenue":
          case "cpa":
          case "cpt":
            content = val > 0 ? fmt.money(val) : "<span class='muted'>—</span>";
            break;
          case "profit":
            content = profitHtml(r.spend, r.revenue);
            break;
          case "roas":
            content = val > 0 ? `<span class="${roasClass(val)}">${fmt.pct(val)}</span>` : "<span class='muted'>—</span>";
            break;
          case "ttr":
          case "cr":
            content = val > 0 ? val.toFixed(1) + "%" : "<span class='muted'>—</span>";
            break;
          case "country":
            content = val ? `<span class="country-badge">${val}</span>` : "<span class='muted'>—</span>";
            break;
          case "match":
            content = val ? `<span class="match-badge">${val}</span>` : "<span class='muted'>—</span>";
            break;
          case "status":
            content = roasBadge(r.roas, r.spend, r.revenue);
            break;
          case "campaign":
            content = val ? (val.length > 28 ? val.slice(0, 26) + "…" : val) : "<span class='muted'>—</span>";
            break;
          case "keyword":
            content = `<strong>${val || ""}</strong>`;
            break;
          case "canceled":
            content = val > 0 ? `<span class='profit-neg'>${val}</span>` : "<span class='muted'>0</span>";
            break;
          case "renewals":
            content = val > 0 ? `<span class='profit-pos'>${val}</span>` : "<span class='muted'>0</span>";
            break;
          case "weekly_subs":
          case "monthly_subs":
          case "yearly_subs":
          case "active":
          case "subs":
          case "installs":
          case "taps":
          case "impressions":
            content = val > 0 ? fmt.num(val) : "<span class='muted'>0</span>";
            break;
          case "cpp_id":
            content = val ? `<span class='muted' style='font-size:11px'>${val.slice(0, 8)}…</span>` : "<span class='muted'>—</span>";
            break;
          case "channel":
            content = `<strong>${val || "—"}</strong>`;
            break;
          case "pay_rate":
            content = val > 0 ? `<span class="${val >= 15 ? 'roas-high' : val >= 8 ? 'roas-mid' : 'roas-low'}">${val.toFixed(1)}%</span>` : "<span class='muted'>—</span>";
            break;
          case "rev_per_user":
          case "rev_per_sub":
            content = val > 0 ? fmt.money(val) : "<span class='muted'>—</span>";
            break;
          case "users":
            content = val > 0 ? fmt.num(val) : "<span class='muted'>0</span>";
            break;
          case "kw_state": {
            const isPaused = r.status === "PAUSED";
            const isBusy = r.keyword_id && STATE.busy.has(String(r.keyword_id));
            const label = isBusy ? "…" : (isPaused ? "Paused" : "Running");
            const dot = isBusy ? "dot-error" : (isPaused ? "dot-paused" : "dot-running");
            content = `<span class="status-cell ${isPaused ? 'status-paused' : 'status-running'}"><span class="dot-icon ${dot}"></span>${label}</span>`;
            break;
          }
          case "bid":
            if (r.keyword_id && r.bid > 0) {
              content = `<span class="bid-cell" data-action="edit-bid" data-kw-id="${r.keyword_id}">${fmt.money(r.bid)}<span class="pencil-icon">✎</span></span>`;
              cls = "num bid-cell-wrap";
            } else {
              content = r.bid > 0 ? fmt.money(r.bid) : "<span class='muted'>—</span>";
            }
            break;
          default:
            content = val != null && val !== "" ? (typeof val === "number" ? fmt.num(val) : String(val)) : "<span class='muted'>—</span>";
        }
        return `<td class="${cls}">${content}</td>`;
      }).join("") + "</tr>";
    }).join("");

    // Campaigns tab → click a row to filter keywords
    if (STATE.tab === "campaigns") {
      body.querySelectorAll("tr.clickable").forEach(tr => {
        tr.addEventListener("click", () => {
          const name = tr.dataset.name;
          STATE.campaign = name;
          STATE.tab = "keywords";
          document.getElementById("campaignFilter").value = name;
          updateTabs();
          render();
          window.scrollTo({ top: document.querySelector(".table-section").offsetTop - 80, behavior: "smooth" });
        });
      });
    }
  }

  // Header click sorting (ignore the select column)
  head.querySelectorAll("th[data-col]").forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      if (STATE.sortCol === col) STATE.sortDir = STATE.sortDir === "desc" ? "asc" : "desc";
      else {
        STATE.sortCol = col;
        STATE.sortDir = "desc";
      }
      renderTable();
    });
  });

  // Select-all checkbox in header
  const selectAllCheck = document.getElementById("selectAllCheck");
  if (selectAllCheck) {
    selectAllCheck.addEventListener("change", (e) => {
      const visibleIds = [...document.querySelectorAll("tbody tr[data-kwid]")]
        .map(tr => tr.dataset.kwid);
      if (e.target.checked) visibleIds.forEach(id => STATE.selected.add(id));
      else visibleIds.forEach(id => STATE.selected.delete(id));
      renderTable();
      renderBulkInfo();
    });
  }

  document.getElementById("tableInfo").textContent = `${rows.length} row${rows.length === 1 ? "" : "s"}`;
  renderBulkInfo();
}

function updateTabs() {
  document.querySelectorAll(".tab").forEach(t => {
    t.classList.toggle("active", t.dataset.tab === STATE.tab);
  });
}

function renderBulkInfo() {
  const selInfo = document.getElementById("selectedInfo");
  const actionsBtn = document.getElementById("actionsBtn");
  const count = STATE.selected.size;
  const isKwTab = STATE.tab === "keywords" || STATE.tab === "winners" || STATE.tab === "losers";
  const dropdown = document.getElementById("actionsDropdown");

  if (!isKwTab) {
    dropdown.style.display = "none";
    selInfo.textContent = "";
    return;
  }
  dropdown.style.display = "inline-block";

  if (count === 0) {
    actionsBtn.disabled = true;
    selInfo.textContent = "Select keywords to enable actions";
    selInfo.classList.remove("has-selection");
  } else {
    actionsBtn.disabled = false;
    selInfo.textContent = `${count} keyword${count === 1 ? "" : "s"} selected`;
    selInfo.classList.add("has-selection");
  }
}

function render() {
  if (!STATE.data) return;
  renderKPIs();
  renderCharts();
  renderTable();
  renderBulkInfo();
}

// ─── Events ────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  // Date range pills
  document.querySelectorAll(".pill").forEach(p => {
    p.addEventListener("click", () => {
      document.querySelectorAll(".pill").forEach(x => x.classList.remove("active"));
      p.classList.add("active");
      STATE.range = p.dataset.range;
      render();
    });
  });

  document.getElementById("countryFilter").addEventListener("change", e => {
    STATE.country = e.target.value;
    render();
  });
  document.getElementById("campaignFilter").addEventListener("change", e => {
    STATE.campaign = e.target.value;
    render();
  });

  document.getElementById("clearFilters")?.addEventListener("click", () => {
    STATE.country = "";
    STATE.campaign = "";
    STATE.search = "";
    document.getElementById("countryFilter").value = "";
    document.getElementById("campaignFilter").value = "";
    document.getElementById("searchBox").value = "";
    render();
  });

  // Tabs
  document.querySelectorAll(".tab").forEach(t => {
    t.addEventListener("click", () => {
      const newTab = t.dataset.tab;
      if (newTab === "campaigns" && STATE.campaign) {
        STATE.campaign = "";
        document.getElementById("campaignFilter").value = "";
      }
      STATE.tab = newTab;
      STATE.sortCol = null;
      STATE.selected.clear();
      updateTabs();
      render();
    });
  });

  document.getElementById("searchBox").addEventListener("input", e => {
    STATE.search = e.target.value;
    renderTable();
  });

  // Actions dropdown toggle
  const actionsBtn = document.getElementById("actionsBtn");
  const actionsMenu = document.getElementById("actionsMenu");
  actionsBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    if (actionsBtn.disabled) return;
    actionsMenu.classList.toggle("show");
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".actions-dropdown")) {
      actionsMenu?.classList.remove("show");
    }
  });
  actionsMenu?.addEventListener("click", (e) => {
    const item = e.target.closest(".menu-item");
    if (!item) return;
    actionsMenu.classList.remove("show");
    const kind = item.dataset.bulk;
    if (kind === "pause") bulkStatusChange("PAUSED", "Pause");
    else if (kind === "enable") bulkStatusChange("ACTIVE", "Resume");
    else if (kind === "bid-up") bulkBidMultiply(1.20);
    else if (kind === "bid-down") bulkBidMultiply(0.80);
    else if (kind === "bid-custom") bulkBidCustom();
  });

  // Event delegation for checkboxes + inline bid edit in the table
  document.getElementById("tableBody").addEventListener("click", async (e) => {
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const action = el.dataset.action;

    if (action === "select") {
      e.stopPropagation();
      const kwId = el.dataset.kwId;
      if (el.checked) STATE.selected.add(String(kwId));
      else STATE.selected.delete(String(kwId));
      el.closest("tr").classList.toggle("row-selected", el.checked);
      renderBulkInfo();
      return;
    }

    if (action === "edit-bid") {
      e.stopPropagation();
      const kwId = el.dataset.kwId;
      const k = findKeyword(kwId);
      if (!k) return;
      const current = (k.bid || 0).toFixed(2);
      // Replace text with an input
      el.innerHTML = `<input type="number" step="0.01" min="0.1" value="${current}" />`;
      const input = el.querySelector("input");
      input.focus();
      input.select();

      const commit = async () => {
        const newBid = parseFloat(input.value);
        if (newBid === k.bid || isNaN(newBid)) {
          renderTable();
          return;
        }
        if (!confirm(`Change bid for "${k.keyword}" to $${newBid.toFixed(2)}?`)) {
          renderTable();
          return;
        }
        const r = await setKeywordBid(k, newBid.toFixed(2));
        if (r.ok) toast(`Bid updated: ${k.keyword} → $${newBid.toFixed(2)}`, "success");
        else toast(`Failed: ${r.error}`, "error");
        renderTable();
      };
      input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") commit();
        if (ev.key === "Escape") renderTable();
      });
      input.addEventListener("blur", commit);
    }
  });

  loadData();
  setInterval(loadData, 5 * 60 * 1000);

  initMeta();
  setInterval(loadMetaData, 5 * 60 * 1000);
});


// ═══════════════════════════════════════════════════════════════
// Meta Ads section (independent of ASA STATE)
// ═══════════════════════════════════════════════════════════════

const META = {
  data: null,
  tab: "campaigns",
  filterCampaignId: null,
  filterAdsetId: null,
  sortCol: "spend",
  sortDir: "desc",
  search: "",
  range: "7d",
  customFrom: null,  // "YYYY-MM-DD"
  customTo: null,
  activeOnly: false, // when true, hide paused/deleted from table
};

// Map Meta's effective_status to a compact badge + CSS class.
function metaStatusBadge(eff) {
  if (!eff) return '<span class="meta-status status-unknown" title="status unknown">?</span>';
  switch (eff) {
    case "ACTIVE":            return '<span class="meta-status status-active"   title="ACTIVE — currently running">●</span>';
    case "PAUSED":            return '<span class="meta-status status-paused"   title="PAUSED — turned off">⏸</span>';
    case "CAMPAIGN_PAUSED":   return '<span class="meta-status status-paused"   title="Parent campaign is paused">⏸</span>';
    case "ADSET_PAUSED":      return '<span class="meta-status status-paused"   title="Parent ad set is paused">⏸</span>';
    case "ARCHIVED":          return '<span class="meta-status status-archived" title="ARCHIVED">▣</span>';
    case "DELETED":           return '<span class="meta-status status-deleted"  title="DELETED">✕</span>';
    case "DISAPPROVED":       return '<span class="meta-status status-issue"    title="DISAPPROVED by Meta">⚠</span>';
    case "PENDING_REVIEW":    return '<span class="meta-status status-pending"  title="Pending Meta review">…</span>';
    case "WITH_ISSUES":       return '<span class="meta-status status-issue"    title="Running with issues">⚠</span>';
    case "PENDING_BILLING_INFO": return '<span class="meta-status status-issue" title="Billing issue">💳</span>';
    case "IN_PROCESS":        return '<span class="meta-status status-pending"  title="Being created/edited">⟳</span>';
    case "PREAPPROVED":       return '<span class="meta-status status-pending"  title="Pre-approved, not yet active">…</span>';
    default:                  return `<span class="meta-status status-unknown" title="${eff}">?</span>`;
  }
}

// Look up effective status for a given (id, level) — level: 'campaigns'|'adsets'|'ads'
function metaEffStatus(id, level) {
  const m = META.data?.statuses?.[level];
  return m && m[id] ? m[id].effective_status : null;
}

function isMetaActive(id, level) {
  return metaEffStatus(id, level) === "ACTIVE";
}

// Returns {since, until} ISO date strings (inclusive) for the active range,
// or null if custom dates are missing. Uses the user's LOCAL calendar date
// for "today/yesterday" (matches what Facebook Ads Manager shows them) and
// the data window for 7d/14d/30d totals.
function localTodayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

function metaDateRange() {
  const today = localTodayIso();
  const dates = (META.data?.ads || []).map(r => r.date).filter(Boolean).sort();
  const earliest = dates.length ? dates[0] : today;

  const sub = (iso, days) => {
    const d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() - days);
    return d.toISOString().slice(0, 10);
  };

  switch (META.range) {
    case "today":     return { since: today, until: today };
    case "yesterday": return { since: sub(today, 1), until: sub(today, 1) };
    case "7d":        return { since: sub(today, 6),  until: today };
    case "14d":       return { since: sub(today, 13), until: today };
    case "30d":       return { since: earliest, until: today };
    case "custom":
      if (!META.customFrom || !META.customTo) return null;
      return { since: META.customFrom, until: META.customTo };
    default:          return { since: earliest, until: today };
  }
}

async function loadMetaData() {
  try {
    const res = await fetch("meta_ads.json?v=" + Date.now());
    if (!res.ok) throw new Error("HTTP " + res.status);
    META.data = await res.json();
    await loadAdjustData();    // best-effort, doesn't block Meta render
    await loadRevenueCatData();// best-effort
    renderMeta();
    renderSubscriptionHealth();
    renderCohortRetention();
    renderDailyHealth();
  } catch (e) {
    console.error("Failed to load meta_ads.json:", e);
    document.getElementById("metaTableBody").innerHTML =
      `<tr><td colspan="20" class="empty-state"><div class="icon">⚠️</div>Could not load meta_ads.json<br><small>${e.message}</small></td></tr>`;
  }
}

// RevenueCat data (the existing data.json from refresh_dashboard_json.py)
const RC = { data: null };

async function loadRevenueCatData() {
  try {
    const res = await fetch("data.json?v=" + Date.now());
    if (!res.ok) throw new Error("HTTP " + res.status);
    RC.data = await res.json();
  } catch (e) {
    console.warn("RevenueCat data not available:", e.message);
    RC.data = null;
  }
}

// Adjust attribution data. Maps are rebuilt every render based on the
// current Meta date-range pill so the ROAS column matches the same
// window as Spend / Installs / etc.
const ADJ = {
  data: null,
  byAdId: new Map(),       // meta ad_id → {installs, revenue, events}
  byAdsetId: new Map(),    // meta adset_id → {...}
  byCampaignId: new Map(), // meta campaign_id → {...}
  windowKey: null,         // last range we built maps for (cache key)
};

function adjExtractId(s) {
  if (!s) return null;
  const m = String(s).match(/\((\d{6,})\)\s*$/);
  return m ? m[1] : null;
}

function adjIsMetaNetwork(network) {
  if (!network) return false;
  return network.indexOf("Facebook") >= 0 || network.indexOf("Instagram") >= 0;
}

async function loadAdjustData() {
  try {
    const res = await fetch("adjust.json?v=" + Date.now());
    if (!res.ok) throw new Error("HTTP " + res.status);
    ADJ.data = await res.json();
    ADJ.windowKey = null; // force rebuild
  } catch (e) {
    console.warn("Adjust data not available:", e.message);
    ADJ.data = null;
  }
  // Re-evaluate the global staleness banner now that ADJ data state has changed.
  if (typeof renderStalenessBanner === "function") renderStalenessBanner();
}

// Rebuild ADJ maps for the current Meta date window. Cheap (<2ms for ~700 rows).
function adjRebuildMapsForCurrentWindow() {
  if (!ADJ.data) return;
  const range = metaDateRange();
  const key = range ? `${range.since}:${range.until}` : "all";
  if (key === ADJ.windowKey) return;

  ADJ.byAdId.clear();
  ADJ.byAdsetId.clear();
  ADJ.byCampaignId.clear();
  ADJ.windowKey = key;

  const accumulate = (map, k, row) => {
    if (!k) return;
    const cur = map.get(k) || {
      installs: 0, revenue: 0, events: 0, clicks: 0,
      weekly: 0, monthly: 0, yearly: 0,
      weekly_rev: 0, monthly_rev: 0, yearly_rev: 0,
    };
    cur.installs    += +(row.installs || 0);
    cur.revenue     += +(row.all_revenue || 0);
    cur.events      += +(row.events || 0);
    cur.clicks      += +(row.clicks || 0);
    cur.weekly      += +(row.com_weekly_events || 0);
    cur.monthly     += +(row.com_monthly_events || 0);
    cur.yearly      += +(row.com_yearly_events || 0);
    cur.weekly_rev  += +(row.com_weekly_revenue || 0);
    cur.monthly_rev += +(row.com_monthly_revenue || 0);
    cur.yearly_rev  += +(row.com_yearly_revenue || 0);
    map.set(k, cur);
  };

  // Use per-day data when range is set so ROAS matches the date pill.
  // Each daily row carries network/campaign/adgroup/creative + day.
  const daily = ADJ.data.by_creative_daily || [];
  for (const r of daily) {
    if (!adjIsMetaNetwork(r.network)) continue;
    if (range && r.day && (r.day < range.since || r.day > range.until)) continue;
    accumulate(ADJ.byAdId,       adjExtractId(r.creative), r);
    accumulate(ADJ.byAdsetId,    adjExtractId(r.adgroup),  r);
    accumulate(ADJ.byCampaignId, adjExtractId(r.campaign), r);
  }

  // Fallback: if the daily dataset is empty for some reason, fall back to
  // 30d totals so the dashboard still shows something useful.
  if (ADJ.byAdId.size === 0 && ADJ.byCampaignId.size === 0) {
    for (const r of ADJ.data.by_creative || []) {
      if (!adjIsMetaNetwork(r.network)) continue;
      accumulate(ADJ.byAdId,       adjExtractId(r.creative), r);
      accumulate(ADJ.byAdsetId,    adjExtractId(r.adgroup),  r);
      accumulate(ADJ.byCampaignId, adjExtractId(r.campaign), r);
    }
  }
}

function metaInstalls(window) {
  const a = META.data?.summary?.[window]?.actions || {};
  return a.mobile_app_install || a.omni_app_install || 0;
}

function updateMetaPillLabels() {
  const fmtDate = iso => {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  };
  const today = localTodayIso();
  const sub = (iso, days) => {
    const d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() - days);
    return d.toISOString().slice(0, 10);
  };
  const set = (range, txt) => {
    const el = document.querySelector(`[data-meta-range="${range}"]`);
    if (el) el.textContent = txt;
  };
  set("today", `Today (${fmtDate(today)})`);
  set("yesterday", `Yesterday (${fmtDate(sub(today, 1))})`);
}

function renderMeta() {
  if (!META.data) return;
  updateMetaPillLabels();
  adjRebuildMapsForCurrentWindow();
  renderMetaKpis();
  renderMetaTable();

  const el = document.getElementById("metaUpdated");
  if (META.data.generated_at) {
    const s = computeStaleness(META.data.generated_at);
    el.textContent = `updated ${s.emoji} ${s.label}`;
    el.title = s.local;
    el.className = "staleness-" + s.status;
  } else {
    el.textContent = "—";
  }
  renderStalenessBanner();
}

function renderMetaKpis() {
  const ads = aggregateMetaAds();
  let spend = 0, impressions = 0, clicks = 0, link_clicks = 0;
  let installs = 0, purchases = 0;
  let adjRev = 0, adjW = 0, adjM = 0, adjY = 0, adjInst = 0;
  for (const a of ads) {
    spend       += a.spend;
    impressions += a.impressions;
    clicks      += a.clicks;
    link_clicks += a.link_clicks || 0;
    installs    += a.installs;
    purchases   += a.purchases || 0;
    adjRev      += a.adj_revenue  || 0;
    adjW        += a.adj_weekly   || 0;
    adjM        += a.adj_monthly  || 0;
    adjY        += a.adj_yearly   || 0;
    adjInst     += a.adj_installs || 0;
  }
  const adjSubs = adjW + adjM + adjY;
  const link_ctr = impressions > 0 ? link_clicks / impressions * 100 : 0;
  const cpm = impressions > 0 ? spend / impressions * 1000 : 0;
  const cpc_link = link_clicks > 0 ? spend / link_clicks : 0;
  const cpi = installs > 0 ? spend / installs : 0;
  const roas = spend > 0 ? adjRev / spend * 100 : 0;
  const profit = adjRev - spend;
  const cps = adjSubs > 0 ? spend / adjSubs : 0;

  const r = metaDateRange();
  const rangeLabel = r ? (r.since === r.until ? r.since : `${r.since} → ${r.until}`) : "—";

  document.getElementById("metaSpend").textContent = fmt.money(spend);
  document.getElementById("metaSpendSub").textContent = rangeLabel;

  document.getElementById("metaRevenue").textContent = fmt.money(adjRev);
  const profitTxt = (profit >= 0 ? "+" : "−") + fmt.money(Math.abs(profit));
  const profitColor = profit >= 0 ? "var(--success, #10b981)" : "var(--danger, #ef4444)";
  const revSub = document.getElementById("metaRevenueSub");
  revSub.innerHTML = `<span style="color:${profitColor}">${profitTxt}</span> profit · ${rangeLabel}`;

  document.getElementById("metaROAS").textContent = roas > 0 ? roas.toFixed(0) + "%" : "—";
  const roasSub = document.getElementById("metaROASSub");
  roasSub.textContent = roas >= 100
    ? "profitable ✓"
    : roas > 0 ? `need ${(100 - roas).toFixed(0)}% more` : "—";
  roasSub.style.color = roas >= 100 ? "var(--success, #10b981)" : "var(--danger, #ef4444)";

  // Net profit after Apple's 15% commission.
  // ── Meta Net Profit — Meta-attributed only (reverted from Step 5) ──
  // Step 5 made this card use TOTAL RC revenue × 0.85 - Meta spend so it
  // would match the True Net card. That was wrong: this card sits inside
  // the Meta Ads section, so it should answer "what did Meta ads earn me
  // minus what I paid Meta?" — not "what's the whole business profit?"
  // The True Net card already exists for the all-channels view.
  //
  // Correct formula: (Adjust Meta-attributed revenue × 0.85) − Meta spend.
  // For a more accurate view (Adjust uses static USD prices, so non-US
  // conversions are off), the True Daily Profit table below shows BOTH
  // the Ad-Only Net (this number) and the True Net side-by-side, so the
  // user can see the difference at a glance.
  const netRev = adjRev * 0.85;
  const netSourceLabel = "Meta-attributed";
  const netProfit = netRev - spend;
  const netRoas = spend > 0 ? netRev / spend * 100 : 0;
  const npEl = document.getElementById("metaNetProfit");
  const npSub = document.getElementById("metaNetProfitSub");
  const npSign = netProfit >= 0 ? "+" : "−";
  npEl.textContent = npSign + fmt.money(Math.abs(netProfit));
  npEl.style.color = netProfit >= 0 ? "var(--success, #10b981)" : "var(--danger, #ef4444)";
  npSub.textContent = netRoas > 0
    ? `Net ROAS ${netRoas.toFixed(0)}% · ${netSourceLabel} · ${rangeLabel}`
    : `${netSourceLabel} · ${rangeLabel}`;

  // "Results" card now reflects Adjust subscribes (real attribution)
  // rather than Meta's app_custom_event.other proxy
  document.getElementById("metaResults").textContent = fmt.num(adjSubs);
  document.getElementById("metaResultsSub").textContent =
    adjSubs > 0 ? `${adjW} W · ${adjM} M · ${adjY} Y` : rangeLabel;

  document.getElementById("metaCPR").textContent = adjSubs > 0 ? fmt.money(cps) : "—";

  document.getElementById("metaInstalls").textContent = fmt.num(installs);
  document.getElementById("metaInstallsSub").textContent =
    adjInst > 0 ? `${fmt.num(adjInst)} (Adjust)` : rangeLabel;

  document.getElementById("metaCPI").textContent = fmt.money(cpi);

  document.getElementById("metaCTR").textContent = link_ctr.toFixed(2) + "%";
  document.getElementById("metaCTRSub").textContent =
    `${fmt.num(link_clicks)} link clicks · CPM ${fmt.money(cpm)} · link CPC ${fmt.money(cpc_link)}`;
}

function aggregateMetaAds() {
  const ads = META.data?.ads || [];
  const range = metaDateRange();
  const byAd = {};
  for (const r of ads) {
    if (range && r.date && (r.date < range.since || r.date > range.until)) continue;
    const k = r.ad_id;
    if (!byAd[k]) byAd[k] = {
      ad_id: r.ad_id, ad_name: r.ad_name,
      adset_id: r.adset_id, adset_name: r.adset_name,
      campaign_id: r.campaign_id, campaign_name: r.campaign_name,
      spend: 0, impressions: 0, clicks: 0, link_clicks: 0,
      installs: 0, purchases: 0,
      freq_weighted: 0,
    };
    const a = byAd[k];
    a.spend       += r.spend || 0;
    a.impressions += r.impressions || 0;
    a.clicks      += r.clicks || 0;
    a.link_clicks += r.inline_link_clicks || 0;
    a.freq_weighted += (r.frequency || 0) * (r.impressions || 0);
    a.installs    += (r.action_mobile_app_install || r.action_omni_app_install || 0);
    // Adjust → Meta MMP integration delivers subscribe events that the
    // Insights API buckets as app_custom_event.other (the user has no
    // trials, so this is effectively pure subscribes).
    a.purchases   += (
      r.action_subscribe ||
      r.action_omni_subscribe ||
      r["action_app_custom_event.fb_mobile_subscribe"] ||
      r.action_purchase ||
      r.action_omni_purchase ||
      r["action_app_custom_event.fb_mobile_purchase"] ||
      r["action_app_custom_event.other"] ||
      0
    );
  }
  return Object.values(byAd).map(a => {
    a.frequency = a.impressions > 0 ? a.freq_weighted / a.impressions : 0;
    const adj = ADJ.byAdId.get(a.ad_id) || {};
    return enrichWithAdj(a, adj);
  });
}

// Apple takes 15% commission (Small Business Program rate). Net columns
// reflect what actually lands in your bank after Apple's cut.
const APPLE_KEEP = 0.85;

// ─── Daily aggregation per ad set / ad (last 14 days) ────────────
// Walks META.data.ads + ADJ.by_creative_daily, returning a sorted array
// of {date, spend, impr, clicks, link, installs, rev, subs, W, M, Y, net, roas}
function getDailyForAdset(adsetId) {
  const days = {};
  for (const r of META.data?.ads || []) {
    if (r.adset_id !== adsetId || !r.date) continue;
    if (!days[r.date]) days[r.date] = dailyBlank(r.date);
    const d = days[r.date];
    d.spend += r.spend || 0;
    d.impr += r.impressions || 0;
    d.clicks += r.clicks || 0;
    d.link += r.inline_link_clicks || 0;
    d.installs += (r.action_mobile_app_install || r.action_omni_app_install || 0);
  }
  for (const r of ADJ.data?.by_creative_daily || []) {
    if (!adjIsMetaNetwork(r.network)) continue;
    if (adjExtractId(r.adgroup) !== adsetId || !r.day) continue;
    if (!days[r.day]) days[r.day] = dailyBlank(r.day);
    const d = days[r.day];
    d.rev += +(r.all_revenue || 0);
    d.W   += +(r.com_weekly_events || 0);
    d.M   += +(r.com_monthly_events || 0);
    d.Y   += +(r.com_yearly_events || 0);
  }
  return finalizeDaily(days);
}

function getDailyForAd(adId) {
  const days = {};
  for (const r of META.data?.ads || []) {
    if (r.ad_id !== adId || !r.date) continue;
    if (!days[r.date]) days[r.date] = dailyBlank(r.date);
    const d = days[r.date];
    d.spend += r.spend || 0;
    d.impr += r.impressions || 0;
    d.clicks += r.clicks || 0;
    d.link += r.inline_link_clicks || 0;
    d.installs += (r.action_mobile_app_install || r.action_omni_app_install || 0);
  }
  for (const r of ADJ.data?.by_creative_daily || []) {
    if (!adjIsMetaNetwork(r.network)) continue;
    if (adjExtractId(r.creative) !== adId || !r.day) continue;
    if (!days[r.day]) days[r.day] = dailyBlank(r.day);
    const d = days[r.day];
    d.rev += +(r.all_revenue || 0);
    d.W   += +(r.com_weekly_events || 0);
    d.M   += +(r.com_monthly_events || 0);
    d.Y   += +(r.com_yearly_events || 0);
  }
  return finalizeDaily(days);
}

function dailyBlank(date) {
  return { date, spend: 0, impr: 0, clicks: 0, link: 0, installs: 0,
           rev: 0, W: 0, M: 0, Y: 0 };
}

function finalizeDaily(daysObj) {
  return Object.values(daysObj)
    .sort((a, b) => a.date.localeCompare(b.date))
    .map(d => ({
      ...d,
      subs: d.W + d.M + d.Y,
      net: d.rev * APPLE_KEEP - d.spend,
      roas: d.spend > 0 ? d.rev * APPLE_KEEP / d.spend * 100 : 0,
      link_ctr: d.impr > 0 ? d.link / d.impr * 100 : 0,
      cpi: d.installs > 0 ? d.spend / d.installs : 0,
      cps: (d.W + d.M + d.Y) > 0 ? d.spend / (d.W + d.M + d.Y) : 0,
    }));
}

// ─── Verdict engine ───────────────────────────────────────────────
// Looks at the last 7 active days and returns a labeled prediction.
// Algorithm:
//   - Trend = linear-regression slope of daily net profit ($/day)
//   - Avg ROAS over the 7d window (net after Apple)
//   - Volatility = coefficient of variation of daily net
//   - Recent (last 3 days) vs prior-4 to detect inflection
// Rules ranked top-down (first match wins):
function computeHealthVerdict(daily) {
  // Take up to last 14 days; verdict uses last 7 active days
  const recent = daily.slice(-14);
  const active = recent.filter(d => d.spend > 0.1);

  if (active.length === 0) {
    return { label: "OFF",   icon: "⏸",  cls: "vd-off",
             reason: "No spend in window — likely paused." };
  }
  if (active.length < 3) {
    return { label: "NEW",   icon: "🔵", cls: "vd-new",
             reason: `Only ${active.length} active day(s) — need 3+ to call it.` };
  }

  // Check if effectively dead now (last 3 days all $0 spend)
  const last3 = recent.slice(-3);
  const last3Spend = last3.reduce((s, d) => s + d.spend, 0);
  if (last3Spend < 1) {
    return { label: "DEAD",  icon: "❌", cls: "vd-dead",
             reason: "$0 spend last 3 days — paused/archived." };
  }

  // Focus on last 7 active days for the score
  const window = active.slice(-7);
  const nets   = window.map(d => d.net);
  const spends = window.map(d => d.spend);
  const totalSpend = spends.reduce((s, v) => s + v, 0);
  const totalRev   = window.reduce((s, d) => s + d.rev, 0);
  const totalNet   = totalRev * APPLE_KEEP - totalSpend;
  const avgRoas    = totalSpend > 0 ? totalRev * APPLE_KEEP / totalSpend * 100 : 0;
  const avgDailyNet= totalNet / window.length;
  const totalSubs  = window.reduce((s, d) => s + d.subs, 0);
  const totalInst  = window.reduce((s, d) => s + d.installs, 0);

  // Trend slope (linear regression) of net profit over the window
  const n = nets.length;
  const xMean = (n - 1) / 2;
  const yMean = nets.reduce((s, v) => s + v, 0) / n;
  let num = 0, den = 0;
  for (let i = 0; i < n; i++) {
    num += (i - xMean) * (nets[i] - yMean);
    den += (i - xMean) ** 2;
  }
  const trend = den > 0 ? num / den : 0;  // $/day change

  // Volatility (std-dev / abs(mean), clipped)
  const variance = nets.reduce((s, v) => s + (v - yMean) ** 2, 0) / n;
  const stdDev = Math.sqrt(variance);
  const volatility = Math.abs(yMean) > 1 ? stdDev / Math.abs(yMean) : (stdDev > 8 ? 5 : 0);

  // Recent 3 vs prior 4
  const last3Net = last3.reduce((s, d) => s + d.net, 0);
  const recent3Avg = last3Net / 3;

  const fmtRoas = `${avgRoas.toFixed(0)}%`;
  const fmtNet  = `${avgDailyNet >= 0 ? "+" : ""}$${avgDailyNet.toFixed(0)}/day`;
  const fmtTrend = `${trend >= 0 ? "+" : ""}$${trend.toFixed(0)}/day`;
  const stats = { avgRoas, totalNet, avgDailyNet, trend, volatility, totalSubs, totalInst, window: window.length };

  // ── Decision tree ──
  if (avgRoas >= 150 && recent3Avg > 5 && trend >= -5) {
    return { label: "SCALE",     icon: "🚀", cls: "vd-scale",
             reason: `Strong winner: ROAS ${fmtRoas}, ${fmtNet} avg. Trend ${fmtTrend}. Scale +20-40%.`,
             stats };
  }
  if (avgRoas >= 110 && recent3Avg > 0) {
    return { label: "KEEP",      icon: "🟢", cls: "vd-keep",
             reason: `Profitable: ROAS ${fmtRoas}, ${fmtNet} avg. Hold current budget.`,
             stats };
  }
  if (trend >= 5 && recent3Avg > avgDailyNet) {
    return { label: "IMPROVING", icon: "📈", cls: "vd-improving",
             reason: `Upward trend: ${fmtTrend} improvement. Last 3d better than prior. Give time.`,
             stats };
  }
  // STABLE BREAK-EVEN (user's rule: brings installs, not losing much)
  if (avgRoas >= 80 && volatility < 0.8 && avgDailyNet > -5 && totalInst >= 5) {
    return { label: "STABLE",    icon: "⚖",  cls: "vd-stable",
             reason: `Break-even (${fmtNet}) but stable — brings ${totalInst} installs. Keep, low risk.`,
             stats };
  }
  if (trend <= -5 && avgDailyNet < 0) {
    return { label: "DYING",     icon: "📉", cls: "vd-dying",
             reason: `Declining: ${fmtTrend}, ${fmtNet}. Fix creative or pause.`,
             stats };
  }
  if (avgRoas < 70 && avgDailyNet < -5) {
    return { label: "KILL",      icon: "🔴", cls: "vd-kill",
             reason: `Bleeding: ROAS ${fmtRoas}, losing $${Math.abs(avgDailyNet).toFixed(0)}/day. Kill.`,
             stats };
  }
  if (volatility > 1.5) {
    return { label: "VOLATILE",  icon: "⚠",  cls: "vd-volatile",
             reason: `Unpredictable: high day-to-day swings. ROAS ${fmtRoas}. Watch closely.`,
             stats };
  }
  return { label: "WATCH",       icon: "🟡", cls: "vd-watch",
           reason: `ROAS ${fmtRoas}, ${fmtNet}. Marginal — needs 2-3 more days.`,
           stats };
}

// Merge a Meta row with its Adjust counterpart, computing all derived
// columns (CTR/CPI/CPR/ROAS/profit/yearly mix). Used both per-ad and at
// every group level (campaign / adset).
function enrichWithAdj(row, adj) {
  adj = adj || {};
  const yearly  = adj.yearly  || 0;
  const monthly = adj.monthly || 0;
  const weekly  = adj.weekly  || 0;
  const subs    = yearly + monthly + weekly;
  const adjRev  = adj.revenue || 0;
  const netRev  = adjRev * APPLE_KEEP;
  return {
    ...row,
    ctr: row.impressions > 0 ? row.clicks / row.impressions * 100 : 0,
    link_ctr: row.impressions > 0 ? (row.link_clicks || 0) / row.impressions * 100 : 0,
    cpc: row.clicks > 0 ? row.spend / row.clicks : 0,
    cpm: row.impressions > 0 ? row.spend / row.impressions * 1000 : 0,
    cpi: row.installs > 0 ? row.spend / row.installs : 0,
    cpr: row.purchases > 0 ? row.spend / row.purchases : 0,
    adj_installs: adj.installs || 0,
    adj_revenue: adjRev,
    adj_events: adj.events || 0,
    adj_weekly: weekly,
    adj_monthly: monthly,
    adj_yearly: yearly,
    adj_subs_total: subs,
    adj_yearly_pct: subs > 0 ? yearly / subs * 100 : 0,
    cps: subs > 0 ? row.spend / subs : 0,                         // cost per sub (Adjust)
    roas: row.spend > 0 ? adjRev / row.spend * 100 : 0,
    profit: adjRev - row.spend,
    // After Apple's 15% commission — true take-home
    net_revenue: netRev,
    net_roas: row.spend > 0 ? netRev / row.spend * 100 : 0,
    net_profit: netRev - row.spend,
  };
}

function metaCols() {
  // Adjust columns now respect the active date pill (matched against the
  // per-day per-creative dataset). W/M/Y are the live paywall events
  // (Com_Weekly $4.99, Com_Monthly $9.99, Com_Yearly $22.99).
  // Net columns are gross × 0.85 (after Apple's 15% commission).
  const adjCols = [
    { key: "adj_revenue",   label: "Revenue",   num: true, fmt: v => v > 0 ? fmt.money(v) : "—" },
    { key: "net_revenue",   label: "Rev (×0.85)", num: true, fmt: v => v > 0 ? fmt.money(v) : "—",
      title: "Revenue after Apple's 15% commission" },
    { key: "roas",          label: "ROAS",      num: true, fmt: v => v > 0 ? v.toFixed(0) + "%" : "—" },
    { key: "net_roas",      label: "Net ROAS",  num: true, fmt: v => v > 0 ? v.toFixed(0) + "%" : "—",
      title: "ROAS after Apple's 15% commission — true return on ad spend" },
    { key: "profit",        label: "Profit",    num: true, fmt: profitFmt },
    { key: "net_profit",    label: "Net Profit", num: true, fmt: profitFmt,
      title: "True take-home: (Revenue × 0.85) − Spend" },
    { key: "adj_subs_total",label: "Subs",      num: true },
    { key: "cps",           label: "Cost/Sub",  num: true, fmt: v => v > 0 ? fmt.money(v) : "—" },
    { key: "adj_weekly",    label: "W",         num: true, title: "Com_Weekly $4.99" },
    { key: "adj_monthly",   label: "M",         num: true, title: "Com_Monthly $9.99" },
    { key: "adj_yearly",    label: "Y",         num: true, title: "Com_Yearly $22.99" },
    { key: "adj_yearly_pct",label: "Y %",       num: true, fmt: v => v > 0 ? v.toFixed(0) + "%" : "—",
      title: "Yearly subs as % of total — higher = better LTV mix" },
    { key: "adj_installs",  label: "Adj Inst",  num: true },
  ];
  if (META.tab === "campaigns") return [
    { key: "campaign_name", label: "Campaign", drill: "campaign" },
    { key: "spend",       label: "Spend",      num: true, fmt: fmt.money },
    ...adjCols,
    { key: "installs",    label: "Meta Inst",  num: true },
    { key: "cpi",         label: "CPI",        num: true, fmt: fmt.money },
    { key: "link_ctr",    label: "CTR (link)", num: true, fmt: v => v.toFixed(2) + "%" },
    { key: "frequency",   label: "Freq",       num: true, fmt: freqFmt, title: "Avg times each reached user saw the ad. <1.5 fresh · 1.5-2 watch · 2-2.5 approaching fatigue · 2.5+ rotate creative" },
    { key: "cpm",         label: "CPM",        num: true, fmt: fmt.money },
  ];
  if (META.tab === "adsets") return [
    { key: "adset_name",    label: "Ad Set", drill: "adset" },
    { key: "campaign_name", label: "Campaign" },
    { key: "spend",       label: "Spend",      num: true, fmt: fmt.money },
    ...adjCols,
    { key: "cpi",         label: "CPI",        num: true, fmt: fmt.money },
    { key: "link_ctr",    label: "CTR (link)", num: true, fmt: v => v.toFixed(2) + "%" },
    { key: "frequency",   label: "Freq",       num: true, fmt: freqFmt, title: "Avg times each reached user saw the ad. <1.5 fresh · 1.5-2 watch · 2-2.5 approaching fatigue · 2.5+ rotate creative" },
  ];
  return [
    { key: "ad_name",       label: "Ad" },
    { key: "adset_name",    label: "Ad Set" },
    { key: "campaign_name", label: "Campaign" },
    { key: "spend",       label: "Spend",      num: true, fmt: fmt.money },
    ...adjCols,
    { key: "cpi",         label: "CPI",        num: true, fmt: fmt.money },
    { key: "link_ctr",    label: "CTR (link)", num: true, fmt: v => v.toFixed(2) + "%" },
    { key: "frequency",   label: "Freq",       num: true, fmt: freqFmt, title: "Avg times each reached user saw the ad. <1.5 fresh · 1.5-2 watch · 2-2.5 approaching fatigue · 2.5+ rotate creative" },
  ];
}

function profitFmt(v) {
  if (v == null || v === 0) return "—";
  if (v > 0) return `<span class="profit-pos">+${fmt.money(v)}</span>`;
  return `<span class="profit-neg">${fmt.money(v)}</span>`;
}

// Frequency = avg times each reached user has seen the ad in the window.
// <1.5 fresh · 1.5-2.0 watch · 2.0-2.5 approaching fatigue · 2.5+ rotate creative.
function freqFmt(v) {
  if (v == null || v === 0) return "—";
  let color, title;
  if      (v < 1.5)  { color = "#16a34a"; title = "Fresh — scale freely"; }
  else if (v < 2.0)  { color = "#ca8a04"; title = "Watch — audience saturation starting"; }
  else if (v < 2.5)  { color = "#ea580c"; title = "Approaching fatigue — prep fresh creative"; }
  else if (v < 3.0)  { color = "#dc2626"; title = "Fatigue confirmed — rotate creative"; }
  else               { color = "#991b1b"; title = "Severe fatigue — kill or refresh now"; }
  return `<span style="color:${color};font-weight:600" title="${title}">${v.toFixed(2)}x</span>`;
}

function metaRowsForTab() {
  const ads = aggregateMetaAds();
  if (META.tab === "ads") {
    return ads.filter(a => {
      if (META.filterCampaignId && a.campaign_id !== META.filterCampaignId) return false;
      if (META.filterAdsetId   && a.adset_id    !== META.filterAdsetId)    return false;
      return true;
    });
  }
  const groupKey = META.tab === "campaigns" ? "campaign_id" : "adset_id";
  const agg = {};
  for (const a of ads) {
    if (META.tab === "adsets" && META.filterCampaignId && a.campaign_id !== META.filterCampaignId) continue;
    const k = a[groupKey];
    if (!agg[k]) agg[k] = {
      campaign_id: a.campaign_id, campaign_name: a.campaign_name,
      adset_id: a.adset_id, adset_name: a.adset_name,
      spend: 0, impressions: 0, clicks: 0, link_clicks: 0,
      installs: 0, purchases: 0,
      freq_weighted: 0,
    };
    agg[k].spend       += a.spend;
    agg[k].impressions += a.impressions;
    agg[k].clicks      += a.clicks;
    agg[k].link_clicks += a.link_clicks || 0;
    agg[k].installs    += a.installs;
    agg[k].purchases   += a.purchases || 0;
    agg[k].freq_weighted += (a.frequency || 0) * (a.impressions || 0);
  }
  return Object.values(agg).map(r => {
    r.frequency = r.impressions > 0 ? r.freq_weighted / r.impressions : 0;
    const adjMap = META.tab === "campaigns" ? ADJ.byCampaignId : ADJ.byAdsetId;
    const adjKey = META.tab === "campaigns" ? r.campaign_id : r.adset_id;
    return enrichWithAdj(r, adjMap.get(adjKey));
  });
}

function renderMetaTable() {
  const head = document.getElementById("metaTableHead");
  const body = document.getElementById("metaTableBody");
  const cols = metaCols();
  let rows = metaRowsForTab();

  const q = (META.search || "").toLowerCase().trim();
  if (q) {
    rows = rows.filter(r =>
      (r.ad_name || "").toLowerCase().includes(q) ||
      (r.adset_name || "").toLowerCase().includes(q) ||
      (r.campaign_name || "").toLowerCase().includes(q)
    );
  }

  // "Active only" filter — hide anything paused/deleted/archived
  if (META.activeOnly) {
    const idKey = META.tab === "campaigns" ? "campaign_id"
                : META.tab === "adsets"    ? "adset_id"
                : "ad_id";
    const lvlKey = META.tab; // 'campaigns' | 'adsets' | 'ads'
    rows = rows.filter(r => isMetaActive(r[idKey], lvlKey));
  }

  rows.sort((a, b) => {
    const va = a[META.sortCol], vb = b[META.sortCol];
    if (typeof va === "string" || typeof vb === "string") {
      return META.sortDir === "asc"
        ? String(va || "").localeCompare(String(vb || ""))
        : String(vb || "").localeCompare(String(va || ""));
    }
    return META.sortDir === "asc" ? (va || 0) - (vb || 0) : (vb || 0) - (va || 0);
  });

  head.innerHTML = "<tr>" + cols.map(c => {
    const arrow = META.sortCol === c.key ? (META.sortDir === "asc" ? " ▲" : " ▼") : "";
    const title = c.title ? ` title="${c.title.replace(/"/g, "&quot;")}"` : "";
    return `<th data-meta-sort="${c.key}" class="${c.num ? "num" : ""}"${title}>${c.label}${arrow}</th>`;
  }).join("") + "</tr>";

  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="${cols.length}" class="empty-state">No data</td></tr>`;
  } else {
    body.innerHTML = rows.map(r => {
      // Determine row-level effective status (uses the most specific id for the current tab)
      const rowStatusId = r.ad_id || r.adset_id || r.campaign_id;
      const rowStatusLvl = r.ad_id ? "ads" : r.adset_id ? "adsets" : "campaigns";
      const rowEff = metaEffStatus(rowStatusId, rowStatusLvl);
      const rowCls = rowEff && rowEff !== "ACTIVE" ? "meta-row-inactive" : "";

      const cells = cols.map(c => {
        let v = r[c.key];
        if (v == null || v === "") v = "—";
        else if (c.fmt) v = c.fmt(v);
        else if (c.num) v = fmt.num(v);
        let drill = "";
        if (c.drill === "campaign") drill = `data-drill-campaign="${r.campaign_id}"`;
        if (c.drill === "adset")    drill = `data-drill-adset="${r.adset_id}" data-drill-camp="${r.campaign_id}"`;
        const cls = (c.num ? "num " : "") + (c.drill ? "drill" : "");
        // Prepend a status badge to the name column for the current level
        let prefix = "";
        if (c.key === "ad_name" && META.tab === "ads") {
          prefix = metaStatusBadge(metaEffStatus(r.ad_id, "ads")) + " ";
        } else if (c.key === "adset_name" && META.tab === "adsets") {
          prefix = metaStatusBadge(metaEffStatus(r.adset_id, "adsets")) + " ";
        } else if (c.key === "campaign_name" && META.tab === "campaigns") {
          prefix = metaStatusBadge(metaEffStatus(r.campaign_id, "campaigns")) + " ";
        }
        return `<td class="${cls.trim()}" ${drill}>${prefix}${v}</td>`;
      }).join("");
      return `<tr class="${rowCls}">${cells}</tr>`;
    }).join("");
  }

  const bc = document.getElementById("metaBreadcrumb");
  const parts = [];
  if (META.filterCampaignId) {
    const r = (META.data.campaigns || []).find(c => c.campaign_id === META.filterCampaignId);
    parts.push(`Campaign: <b>${r?.campaign_name || META.filterCampaignId}</b>`);
  }
  if (META.filterAdsetId) {
    const r = (META.data.adsets || []).find(s => s.adset_id === META.filterAdsetId);
    parts.push(`Ad Set: <b>${r?.adset_name || META.filterAdsetId}</b>`);
  }
  bc.innerHTML = parts.length
    ? parts.join(" › ") + ` <a href="#" id="metaClearFilter">clear</a>`
    : "";

  document.getElementById("metaTableInfo").textContent = `${rows.length} rows`;
}

// ─── True Daily Profit (RC truth - all revenue minus all ad spend) ─────
// This is the REAL business profit, not just Meta-attributed.
// Includes renewals from past cohorts, organic, ASA, yearly subs.
function renderTrueDailyProfit() {
  if (!RC.data || !RC.data.daily_rc) return;
  const APPLE_KEEP = 0.85;

  // Helper: get YYYY-MM-DD for N days ago (UTC)
  const dateNDaysAgo = (n) => {
    const d = new Date();
    d.setUTCDate(d.getUTCDate() - n);
    return d.toISOString().slice(0, 10);
  };
  const yday = dateNDaysAgo(1);

  // Build daily-keyed maps
  const rcByDay = new Map((RC.data.daily_rc || []).map(r => [r.date, r]));
  const metaSpendByDay = new Map();
  for (const r of (META.data?.ads || [])) {
    metaSpendByDay.set(r.date, (metaSpendByDay.get(r.date) || 0) + (r.spend || 0));
  }
  const adjByDay = new Map();
  for (const r of (ADJ.data?.by_creative_daily || [])) {
    const net = (r.network || "").toLowerCase();
    if (!(net.includes("meta") || net.includes("facebook") || net.includes("instagram"))) continue;
    const day = r.day;
    if (!day) continue;
    const rev = (+r.com_weekly_revenue || 0) + (+r.com_monthly_revenue || 0) + (+r.com_yearly_revenue || 0);
    adjByDay.set(day, (adjByDay.get(day) || 0) + rev);
  }

  // Yesterday metrics
  const rcYday = rcByDay.get(yday) || { revenue: 0 };
  const spendYday = metaSpendByDay.get(yday) || 0;
  const adjYday = adjByDay.get(yday) || 0;
  const nonAdYday = (rcYday.revenue || 0) - adjYday;
  const trueNetYday = (rcYday.revenue || 0) * APPLE_KEEP - spendYday;
  const adOnlyNetYday = adjYday * APPLE_KEEP - spendYday;

  // 7-day totals
  let rc7 = 0, spend7 = 0, adj7 = 0;
  for (let i = 1; i <= 7; i++) {
    const d = dateNDaysAgo(i);
    rc7 += (rcByDay.get(d)?.revenue || 0);
    spend7 += (metaSpendByDay.get(d) || 0);
    adj7 += (adjByDay.get(d) || 0);
  }
  const trueNet7 = rc7 * APPLE_KEEP - spend7;
  const adOnlyNet7 = adj7 * APPLE_KEEP - spend7;
  const hiddenProfit7 = trueNet7 - adOnlyNet7;

  // 30-day totals (excluding today which is partial). Backed by the
  // 31-day daily_rc window — days 1..30 give us a complete trailing-30
  // view that mirrors the 7d card. Any missing day silently contributes
  // zero (and is reflected in the "days covered" sub-label so the user
  // sees if the window is short).
  let rc30 = 0, spend30 = 0, daysCovered = 0;
  for (let i = 1; i <= 30; i++) {
    const d = dateNDaysAgo(i);
    const rcDay = rcByDay.get(d);
    if (rcDay) {
      rc30 += (rcDay.revenue || 0);
      daysCovered++;
    }
    spend30 += (metaSpendByDay.get(d) || 0);
  }
  const trueNet30 = rc30 * APPLE_KEEP - spend30;

  // Render
  const colorFor = (v) => v > 0 ? "var(--success, #10b981)" : v < 0 ? "var(--danger, #ef4444)" : "";
  const signed = (v) => (v >= 0 ? "+" : "−") + fmt.money(Math.abs(v));

  const trueNetEl = document.getElementById("trueNetYday");
  if (trueNetEl) {
    trueNetEl.textContent = signed(trueNetYday);
    trueNetEl.style.color = colorFor(trueNetYday);
    const diff = trueNetYday - adOnlyNetYday;
    document.getElementById("trueNetYdaySub").textContent =
      `vs ad-only ${signed(adOnlyNetYday)} (+${fmt.money(diff)} hidden)`;
  }

  const rcRevEl = document.getElementById("rcRevYday");
  if (rcRevEl) {
    rcRevEl.textContent = fmt.money(rcYday.revenue || 0);
    document.getElementById("rcRevYdaySub").textContent =
      `${rcYday.new_subs || 0} new · ${rcYday.renewals || 0} renewals`;
  }

  const nonAdEl = document.getElementById("nonAdRevYday");
  if (nonAdEl) {
    nonAdEl.textContent = fmt.money(nonAdYday);
    const pct = (rcYday.revenue || 0) > 0 ? (nonAdYday / rcYday.revenue * 100) : 0;
    nonAdEl.style.color = "var(--success, #10b981)";
    document.getElementById("nonAdRevYdaySub").textContent =
      `${pct.toFixed(0)}% of total revenue · pure profit (no ad cost)`;
  }

  const trueNet7El = document.getElementById("trueNet7d");
  if (trueNet7El) {
    trueNet7El.textContent = signed(trueNet7);
    trueNet7El.style.color = colorFor(trueNet7);
    document.getElementById("trueNet7dSub").textContent =
      `ad-only would be ${signed(adOnlyNet7)} · ${signed(hiddenProfit7)} extra you weren't seeing`;
  }

  const trueNet30El = document.getElementById("trueNet30d");
  if (trueNet30El) {
    trueNet30El.textContent = signed(trueNet30);
    trueNet30El.style.color = colorFor(trueNet30);
    // Sub-label: avg per day + days covered (so the user knows if RC's
    // 30-day window is short — e.g. fresh setup or after rotation).
    const avgPerDay = daysCovered > 0 ? trueNet30 / daysCovered : 0;
    document.getElementById("trueNet30dSub").textContent =
      `${daysCovered} day(s) covered · ${signed(avgPerDay)}/day avg`;
  }

  // ─── Daily breakdown table (newest first) ───
  const tbody = document.getElementById("trueDailyTableBody");
  if (tbody) {
    // All days that have RC data, newest first
    const days = [...rcByDay.keys()].sort().reverse();
    if (!days.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="empty-state">No daily data yet</td></tr>`;
    } else {
      tbody.innerHTML = days.map(d => {
        const rc = rcByDay.get(d) || { revenue: 0, new_subs: 0, renewals: 0 };
        const spend = metaSpendByDay.get(d) || 0;
        const adjRev = adjByDay.get(d) || 0;
        const rcRev = rc.revenue || 0;
        const nonAd = rcRev - adjRev;
        const trueNet = rcRev * APPLE_KEEP - spend;       // after Apple 15% − ad spend
        const adOnlyNet = adjRev * APPLE_KEEP - spend;
        const cls = trueNet > 0 ? "profit-pos" : trueNet < 0 ? "profit-neg" : "";
        const adCls = adOnlyNet > 0 ? "profit-pos" : adOnlyNet < 0 ? "profit-neg" : "";
        return `<tr>
          <td>${d}</td>
          <td class="num">${fmt.money(rcRev)}</td>
          <td class="num">${fmt.money(adjRev)}</td>
          <td class="num" style="color:var(--success,#10b981)">${fmt.money(nonAd)}</td>
          <td class="num">${spend > 0 ? fmt.money(spend) : "—"}</td>
          <td class="num"><span class="${adCls}">${signed(adOnlyNet)}</span></td>
          <td class="num"><strong class="${cls}">${signed(trueNet)}</strong></td>
        </tr>`;
      }).join("");
    }
  }
}

// ─── Subscription Health (global, from RevenueCat) ──────────
function renderSubscriptionHealth() {
  if (!RC.data) return;
  // Also refresh the True Daily Profit cards (they share the same data)
  renderTrueDailyProfit();
  const channels = RC.data.channels || [];
  let revenue = 0, paidSubs = 0, active = 0, canceled = 0, renewals = 0;
  let weekly = 0, monthly = 0, yearly = 0;
  for (const c of channels) {
    revenue  += +(c.revenue_all || 0);
    paidSubs += +(c.subs_all || 0);
    active   += +(c.active_all || 0);
    canceled += +(c.canceled_all || 0);
    renewals += +(c.renewals_all || 0);
    weekly   += +(c.weekly_subs_all || 0);
    monthly  += +(c.monthly_subs_all || 0);
    yearly   += +(c.yearly_subs_all || 0);
  }
  const ltv = paidSubs > 0 ? revenue / paidSubs : 0;
  const churn = paidSubs > 0 ? canceled / paidSubs * 100 : 0;
  const renewRate = paidSubs > 0 ? renewals / paidSubs * 100 : 0;

  document.getElementById("rcActive").textContent = fmt.num(active);
  document.getElementById("rcActiveSub").textContent = `${fmt.num(paidSubs)} total subs`;

  document.getElementById("rcLtv").textContent = fmt.money(ltv);

  document.getElementById("rcChurn").textContent = churn.toFixed(1) + "%";
  document.getElementById("rcChurnSub").textContent = `${fmt.num(canceled)} canceled all-time`;

  document.getElementById("rcRenewal").textContent = renewRate.toFixed(0) + "%";
  document.getElementById("rcRenewalSub").textContent = `${fmt.num(renewals)} renewal events`;

  document.getElementById("rcTotalRev").textContent = fmt.money(revenue);
  // Channel breakdown
  const byChannel = channels.slice().sort((a, b) => (b.revenue_all || 0) - (a.revenue_all || 0));
  document.getElementById("rcTotalRevSub").textContent =
    byChannel.slice(0, 3).map(c => `${c.channel.replace(" / Unattributed", "")} ${fmt.money(c.revenue_all || 0)}`).join(" · ");

  document.getElementById("rcTotalSubs").textContent = fmt.num(paidSubs);
  document.getElementById("rcTotalSubsSub").textContent = `${weekly} W · ${monthly} M · ${yearly} Y`;

  // Refund rate (30d). Sum gross revenue for last 30 days from daily_rc
  // for the denominator; refunds.last_30d_amount is the numerator.
  const refunds = RC.data.refunds || {};
  const daily30 = RC.data.daily_rc || [];
  const grossRev30d = daily30.reduce((s, r) => s + (r.revenue || 0), 0);
  const refundAmt30d = refunds.last_30d_amount || 0;
  const refundCount30d = refunds.last_30d_count || 0;
  const refundRate = grossRev30d > 0 ? refundAmt30d / grossRev30d * 100 : 0;
  const rateEl = document.getElementById("rcRefundRate");
  rateEl.textContent = refundRate.toFixed(1) + "%";
  // Color: green if <2%, neutral 2-5%, red if >5% (industry red flag)
  rateEl.style.color = refundRate < 2 ? "var(--success, #10b981)"
    : refundRate > 5 ? "var(--danger, #ef4444)"
    : "";
  document.getElementById("rcRefundRateSub").textContent =
    refundCount30d > 0
      ? `${refundCount30d} refunds · ${fmt.money(refundAmt30d)} of ${fmt.money(grossRev30d)}`
      : "no refunds in last 30d";

  const lu = RC.data.last_updated ? new Date(RC.data.last_updated) : null;
  if (lu) {
    const mins = Math.round((Date.now() - lu) / 60000);
    document.getElementById("rcUpdated").textContent =
      "updated " + (mins < 60 ? `${mins}m ago` : `${Math.round(mins/60)}h ago`);
  }
}

// ─── Cohort Retention vs industry benchmarks ───────────────
// Utility-app benchmarks (sourced from RevenueCat State of Subscription
// Apps + Apple category data). [low, median, high] in percent.
const RETENTION_BENCHMARKS = {
  weekly: {
    D7:  [25, 40, 55],
    D14: [15, 25, 40],
    D28: [8,  13, 22],
    D56: [4,  7,  12],
    D84: [2,  4,  8],
  },
  monthly: {
    D30:  [40, 55, 70],
    D60:  [25, 35, 50],
    D90:  [15, 25, 35],
    D180: [8,  15, 25],
  },
  yearly: {
    D365: [55, 65, 78],
  },
};

function retentionVerdict(rate, bench) {
  if (!bench) return { label: "—", cls: "" };
  const [low, med, high] = bench;
  if (rate >= high) return { label: "top quartile", cls: "profit-pos" };
  if (rate >= med)  return { label: "above median", cls: "profit-pos" };
  if (rate >= low)  return { label: "at median",    cls: "" };
  return { label: "below median", cls: "profit-neg" };
}

function renderCohortRetention() {
  const head = document.getElementById("retentionTableHead");
  const body = document.getElementById("retentionTableBody");
  if (!head || !body) return;

  const ret = RC.data?.cohort_retention || {};
  const cols = [
    { label: "Tier" },
    { label: "Checkpoint", title: "Days since first paid transaction" },
    { label: "Cohort", num: true, title: "Subscribers who started ≥ N days ago" },
    { label: "Retained", num: true, title: "Of those, how many had enough renewals to still be subscribed at day N" },
    { label: "Rate", num: true },
    { label: "Industry (low–med–high)", num: true, title: "Utility-app benchmarks: bottom quartile, median, top quartile" },
    { label: "Verdict" },
  ];
  head.innerHTML = "<tr>" + cols.map(c =>
    `<th class="${c.num ? "num" : ""}"${c.title ? ` title="${c.title}"` : ""}>${c.label}</th>`
  ).join("") + "</tr>";

  const tierLabel = { weekly: "Weekly $4.99", monthly: "Monthly $9.99", yearly: "Yearly $22.99" };
  const rows = [];
  for (const tier of ["weekly", "monthly", "yearly"]) {
    const tierData = ret[tier] || {};
    const checkpoints = Object.keys(tierData);
    if (!checkpoints.length) continue;
    let firstRow = true;
    for (const cp of checkpoints) {
      const r = tierData[cp];
      const bench = RETENTION_BENCHMARKS[tier]?.[cp];
      const v = retentionVerdict(r.rate, bench);
      const benchTxt = bench ? `${bench[0]}% – ${bench[1]}% – ${bench[2]}%` : "—";
      rows.push(`<tr>
        <td>${firstRow ? `<strong>${tierLabel[tier]}</strong>` : ""}</td>
        <td>${cp}</td>
        <td class="num">${fmt.num(r.cohort_size)}</td>
        <td class="num">${fmt.num(r.retained)}</td>
        <td class="num"><strong>${r.rate.toFixed(1)}%</strong></td>
        <td class="num"><span class="muted">${benchTxt}</span></td>
        <td><span class="${v.cls}">${v.label}</span></td>
      </tr>`);
      firstRow = false;
    }
  }
  body.innerHTML = rows.length
    ? rows.join("")
    : `<tr><td colspan="${cols.length}" class="empty-state">No retention data yet — needs 7+ days of history</td></tr>`;
}

// ─── Daily Health: Meta + Adjust + RC side-by-side ──────────
function renderDailyHealth() {
  const head = document.getElementById("dailyTableHead");
  const body = document.getElementById("dailyTableBody");
  if (!head || !body) return;

  // Build per-day Meta totals
  const metaByDay = new Map();
  for (const r of META.data?.ads || []) {
    if (!r.date) continue;
    const cur = metaByDay.get(r.date) || { spend: 0, installs: 0, clicks: 0 };
    cur.spend    += r.spend || 0;
    cur.installs += (r.action_mobile_app_install || r.action_omni_app_install || 0);
    cur.clicks   += r.clicks || 0;
    metaByDay.set(r.date, cur);
  }

  // Build per-day Adjust totals (Meta-attributed networks only)
  const adjByDay = new Map();
  for (const r of ADJ.data?.by_creative_daily || []) {
    if (!adjIsMetaNetwork(r.network)) continue;
    const day = r.day;
    if (!day) continue;
    const cur = adjByDay.get(day) || {
      installs: 0, revenue: 0, weekly: 0, monthly: 0, yearly: 0,
    };
    cur.installs += +(r.installs || 0);
    cur.revenue  += +(r.all_revenue || 0);
    cur.weekly   += +(r.com_weekly_events || 0);
    cur.monthly  += +(r.com_monthly_events || 0);
    cur.yearly   += +(r.com_yearly_events || 0);
    adjByDay.set(day, cur);
  }

  // RC daily (global, all channels)
  const rcByDay = new Map();
  for (const r of RC.data?.daily_rc || []) {
    rcByDay.set(r.date, r);
  }

  // Union of all days, sorted newest first
  const allDays = new Set([...metaByDay.keys(), ...adjByDay.keys(), ...rcByDay.keys()]);
  const days = [...allDays].sort().reverse();

  const cols = [
    { label: "Date" },
    { label: "Meta Spend",  num: true },
    { label: "Meta Inst",   num: true },
    { label: "Adj Inst",    num: true },
    { label: "Adj Rev",     num: true },
    { label: "Adj Subs (W·M·Y)", num: true, title: "Weekly · Monthly · Yearly subscribes attributed to Meta" },
    { label: "RC Rev",      num: true, title: "RevenueCat real revenue (all channels including renewals)" },
    { label: "RC New",      num: true, title: "First-time subscribers across all sources" },
    { label: "RC Renewals", num: true },
    { label: "Match",       num: true, title: "Adj Rev ÷ RC Rev — should be near 100% for Meta-attributed share" },
    { label: "Net Profit",  num: true, title: "RC revenue − Meta spend (positive = made money today)" },
    { label: "Net (after 15%)", num: true, title: "(RC revenue × 0.85) − Meta spend — your real take-home after Apple's commission" },
  ];
  head.innerHTML = "<tr>" + cols.map(c =>
    `<th class="${c.num ? "num" : ""}"${c.title ? ` title="${c.title}"` : ""}>${c.label}</th>`
  ).join("") + "</tr>";

  if (!days.length) {
    body.innerHTML = `<tr><td colspan="${cols.length}" class="empty-state">No daily data yet</td></tr>`;
    return;
  }

  body.innerHTML = days.map(d => {
    const m = metaByDay.get(d) || { spend: 0, installs: 0, clicks: 0 };
    const a = adjByDay.get(d) || { installs: 0, revenue: 0, weekly: 0, monthly: 0, yearly: 0 };
    const r = rcByDay.get(d) || { revenue: 0, new_subs: 0, renewals: 0 };
    const adjSubs = a.weekly + a.monthly + a.yearly;
    const matchPct = (r.revenue || 0) > 0 ? (a.revenue / r.revenue * 100) : 0;
    const matchClass = matchPct === 0 ? "" : matchPct > 80 ? "profit-pos" : matchPct < 30 ? "profit-neg" : "";

    // Net profit: RC revenue (truth) − Meta ad spend. Empty if neither side has data.
    const hasFinancials = (r.revenue || 0) > 0 || m.spend > 0;
    const profit = (r.revenue || 0) - (m.spend || 0);
    const profitAfterCut = (r.revenue || 0) * 0.85 - (m.spend || 0);
    const cellFor = (val) => {
      if (!hasFinancials) return "—";
      const sign = val >= 0 ? "+" : "−";
      const cls = val >= 0 ? "profit-pos" : "profit-neg";
      return `<strong class="${cls}">${sign}${fmt.money(Math.abs(val))}</strong>`;
    };

    return `<tr>
      <td>${d}</td>
      <td class="num">${m.spend > 0 ? fmt.money(m.spend) : "—"}</td>
      <td class="num">${m.installs ? fmt.num(m.installs) : "—"}</td>
      <td class="num">${a.installs ? fmt.num(a.installs) : "—"}</td>
      <td class="num">${a.revenue > 0 ? fmt.money(a.revenue) : "—"}</td>
      <td class="num">${adjSubs > 0 ? `${a.weekly}·${a.monthly}·${a.yearly}` : "—"}</td>
      <td class="num">${r.revenue > 0 ? fmt.money(r.revenue) : "—"}</td>
      <td class="num">${r.new_subs ? fmt.num(r.new_subs) : "—"}</td>
      <td class="num">${r.renewals ? fmt.num(r.renewals) : "—"}</td>
      <td class="num"><span class="${matchClass}">${matchPct > 0 ? matchPct.toFixed(0) + "%" : "—"}</span></td>
      <td class="num">${cellFor(profit)}</td>
      <td class="num">${cellFor(profitAfterCut)}</td>
    </tr>`;
  }).join("");
}

function setMetaTab(name) {
  META.tab = name;
  document.querySelectorAll("[data-meta-tab]").forEach(b =>
    b.classList.toggle("active", b.dataset.metaTab === name)
  );
  renderMetaTable();
}

function initMeta() {
  // Date range pills
  document.querySelectorAll("[data-meta-range]").forEach(p => {
    p.addEventListener("click", () => {
      document.querySelectorAll("[data-meta-range]").forEach(x => x.classList.remove("active"));
      p.classList.add("active");
      META.range = p.dataset.metaRange;
      const customEl = document.getElementById("metaCustomRange");
      if (META.range === "custom") {
        customEl.style.display = "";
        // Default to last 7d if not set yet
        if (!META.customFrom || !META.customTo) {
          const dates = (META.data?.ads || []).map(r => r.date).filter(Boolean).sort();
          const latest = dates.length ? dates[dates.length - 1] : new Date().toISOString().slice(0, 10);
          const earlier = new Date(latest + "T00:00:00Z");
          earlier.setUTCDate(earlier.getUTCDate() - 6);
          META.customFrom = earlier.toISOString().slice(0, 10);
          META.customTo = latest;
          document.getElementById("metaCustomFrom").value = META.customFrom;
          document.getElementById("metaCustomTo").value = META.customTo;
        }
      } else {
        customEl.style.display = "none";
      }
      renderMeta();
    });
  });

  document.getElementById("metaCustomFrom").addEventListener("change", e => {
    META.customFrom = e.target.value;
    if (META.range === "custom") renderMeta();
  });
  document.getElementById("metaCustomTo").addEventListener("change", e => {
    META.customTo = e.target.value;
    if (META.range === "custom") renderMeta();
  });

  document.querySelectorAll("[data-meta-tab]").forEach(btn => {
    btn.addEventListener("click", () => {
      META.sortCol = "spend";
      META.sortDir = "desc";
      setMetaTab(btn.dataset.metaTab);
    });
  });

  document.getElementById("metaTableHead").addEventListener("click", e => {
    const th = e.target.closest("th[data-meta-sort]");
    if (!th) return;
    const col = th.dataset.metaSort;
    if (META.sortCol === col) META.sortDir = META.sortDir === "asc" ? "desc" : "asc";
    else { META.sortCol = col; META.sortDir = "desc"; }
    renderMetaTable();
  });

  document.getElementById("metaTableBody").addEventListener("click", e => {
    const td = e.target.closest("td");
    if (!td) return;
    if (td.dataset.drillCampaign) {
      META.filterCampaignId = td.dataset.drillCampaign;
      META.filterAdsetId = null;
      META.sortCol = "spend"; META.sortDir = "desc";
      setMetaTab("adsets");
    } else if (td.dataset.drillAdset) {
      META.filterAdsetId = td.dataset.drillAdset;
      if (td.dataset.drillCamp) META.filterCampaignId = td.dataset.drillCamp;
      META.sortCol = "spend"; META.sortDir = "desc";
      setMetaTab("ads");
    }
  });

  document.getElementById("metaBreadcrumb").addEventListener("click", e => {
    if (e.target.id === "metaClearFilter") {
      e.preventDefault();
      META.filterCampaignId = null;
      META.filterAdsetId = null;
      renderMetaTable();
    }
  });

  document.getElementById("metaSearch").addEventListener("input", e => {
    META.search = e.target.value;
    renderMetaTable();
  });

  const activeOnlyEl = document.getElementById("metaActiveOnly");
  if (activeOnlyEl) {
    activeOnlyEl.addEventListener("change", e => {
      META.activeOnly = e.target.checked;
      renderMetaTable();
    });
  }

  loadMetaData();
}
