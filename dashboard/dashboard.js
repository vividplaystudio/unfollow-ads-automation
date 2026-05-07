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

  const lu = STATE.data.last_updated ? new Date(STATE.data.last_updated) : null;
  const luEl = document.getElementById("lastUpdated");
  if (lu) {
    const now = new Date();
    const mins = Math.round((now - lu) / 60000);
    let text;
    if (mins < 1) text = "just now";
    else if (mins < 60) text = `${mins} min ago`;
    else if (mins < 1440) text = `${Math.round(mins / 60)}h ago`;
    else text = `${Math.round(mins / 1440)}d ago`;
    luEl.textContent = text;
    luEl.title = lu.toLocaleString();
  } else {
    luEl.textContent = "unknown";
  }
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
};

// Returns {since, until} ISO date strings (inclusive) for the active range,
// or null if custom dates are missing.
function metaDateRange() {
  // Use the latest date present in the per-day data as "today" — Meta's
  // date_start is in account timezone, which may differ from the user's.
  const dates = (META.data?.ads || []).map(r => r.date).filter(Boolean).sort();
  const latest = dates.length ? dates[dates.length - 1] : new Date().toISOString().slice(0, 10);
  const earliest = dates.length ? dates[0] : latest;

  const sub = (iso, days) => {
    const d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() - days);
    return d.toISOString().slice(0, 10);
  };

  switch (META.range) {
    case "today":     return { since: latest, until: latest };
    case "yesterday": return { since: sub(latest, 1), until: sub(latest, 1) };
    case "7d":        return { since: sub(latest, 6),  until: latest };
    case "14d":       return { since: sub(latest, 13), until: latest };
    case "30d":       return { since: earliest, until: latest };
    case "custom":
      if (!META.customFrom || !META.customTo) return null;
      return { since: META.customFrom, until: META.customTo };
    default:          return { since: earliest, until: latest };
  }
}

async function loadMetaData() {
  try {
    const res = await fetch("meta_ads.json?v=" + Date.now());
    if (!res.ok) throw new Error("HTTP " + res.status);
    META.data = await res.json();
    await loadAdjustData();   // best-effort, doesn't block Meta render
    renderMeta();
  } catch (e) {
    console.error("Failed to load meta_ads.json:", e);
    document.getElementById("metaTableBody").innerHTML =
      `<tr><td colspan="20" class="empty-state"><div class="icon">⚠️</div>Could not load meta_ads.json<br><small>${e.message}</small></td></tr>`;
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
    const cur = map.get(k) || { installs: 0, revenue: 0, events: 0, clicks: 0 };
    cur.installs += +(row.installs || 0);
    cur.revenue  += +(row.all_revenue || 0);
    cur.events   += +(row.events || 0);
    cur.clicks   += +(row.clicks || 0);
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

function renderMeta() {
  if (!META.data) return;
  adjRebuildMapsForCurrentWindow();
  renderMetaKpis();
  renderMetaTable();

  const lu = META.data.generated_at ? new Date(META.data.generated_at) : null;
  const el = document.getElementById("metaUpdated");
  if (lu) {
    const mins = Math.round((Date.now() - lu) / 60000);
    let text;
    if (mins < 1) text = "just now";
    else if (mins < 60) text = `${mins} min ago`;
    else text = `${Math.round(mins / 60)}h ago`;
    el.textContent = "updated " + text;
    el.title = lu.toLocaleString();
  } else {
    el.textContent = "—";
  }
}

function renderMetaKpis() {
  const ads = aggregateMetaAds();
  let spend = 0, impressions = 0, clicks = 0, link_clicks = 0;
  let installs = 0, purchases = 0;
  for (const a of ads) {
    spend       += a.spend;
    impressions += a.impressions;
    clicks      += a.clicks;
    link_clicks += a.link_clicks || 0;
    installs    += a.installs;
    purchases   += a.purchases || 0;
  }
  const link_ctr = impressions > 0 ? link_clicks / impressions * 100 : 0;
  const cpm = impressions > 0 ? spend / impressions * 1000 : 0;
  const cpc_link = link_clicks > 0 ? spend / link_clicks : 0;
  const cpi = installs > 0 ? spend / installs : 0;
  const cpr = purchases > 0 ? spend / purchases : 0;

  const r = metaDateRange();
  const rangeLabel = r ? (r.since === r.until ? r.since : `${r.since} → ${r.until}`) : "—";

  document.getElementById("metaSpend").textContent = fmt.money(spend);
  document.getElementById("metaSpendSub").textContent = rangeLabel;

  document.getElementById("metaResults").textContent = fmt.num(purchases);
  document.getElementById("metaResultsSub").textContent =
    purchases > 0 ? rangeLabel : "no purchases tracked yet";

  document.getElementById("metaCPR").textContent = purchases > 0 ? fmt.money(cpr) : "—";

  document.getElementById("metaInstalls").textContent = fmt.num(installs);
  document.getElementById("metaInstallsSub").textContent = rangeLabel;

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
    };
    const a = byAd[k];
    a.spend       += r.spend || 0;
    a.impressions += r.impressions || 0;
    a.clicks      += r.clicks || 0;
    a.link_clicks += r.inline_link_clicks || 0;
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
    const adj = ADJ.byAdId.get(a.ad_id) || { installs: 0, revenue: 0, events: 0 };
    return {
      ...a,
      ctr: a.impressions > 0 ? a.clicks / a.impressions * 100 : 0,
      link_ctr: a.impressions > 0 ? a.link_clicks / a.impressions * 100 : 0,
      cpc: a.clicks > 0 ? a.spend / a.clicks : 0,
      cpm: a.impressions > 0 ? a.spend / a.impressions * 1000 : 0,
      cpi: a.installs > 0 ? a.spend / a.installs : 0,
      cpr: a.purchases > 0 ? a.spend / a.purchases : 0,
      adj_installs: adj.installs,
      adj_revenue: adj.revenue,
      adj_events: adj.events,
      roas: a.spend > 0 ? adj.revenue / a.spend * 100 : 0,
      profit: adj.revenue - a.spend,
    };
  });
}

function metaCols() {
  // Adjust columns now respect the active date pill (matched against the
  // per-day per-creative dataset).
  const adjCols = [
    { key: "adj_revenue", label: "Revenue",  num: true, fmt: v => v > 0 ? fmt.money(v) : "—" },
    { key: "roas",        label: "ROAS",     num: true, fmt: v => v > 0 ? v.toFixed(0) + "%" : "—" },
    { key: "profit",      label: "Profit",   num: true, fmt: profitFmt },
    { key: "adj_installs",label: "Adj Inst", num: true },
  ];
  if (META.tab === "campaigns") return [
    { key: "campaign_name", label: "Campaign", drill: "campaign" },
    { key: "spend",       label: "Spend",      num: true, fmt: fmt.money },
    ...adjCols,
    { key: "purchases",   label: "Meta Subs",  num: true },
    { key: "cpr",         label: "Cost/Result",num: true, fmt: v => v > 0 ? fmt.money(v) : "—" },
    { key: "installs",    label: "Meta Inst",  num: true },
    { key: "cpi",         label: "CPI",        num: true, fmt: fmt.money },
    { key: "link_ctr",    label: "CTR (link)", num: true, fmt: v => v.toFixed(2) + "%" },
    { key: "cpm",         label: "CPM",        num: true, fmt: fmt.money },
  ];
  if (META.tab === "adsets") return [
    { key: "adset_name",    label: "Ad Set", drill: "adset" },
    { key: "campaign_name", label: "Campaign" },
    { key: "spend",       label: "Spend",      num: true, fmt: fmt.money },
    ...adjCols,
    { key: "purchases",   label: "Meta Subs",  num: true },
    { key: "cpr",         label: "Cost/Result",num: true, fmt: v => v > 0 ? fmt.money(v) : "—" },
    { key: "cpi",         label: "CPI",        num: true, fmt: fmt.money },
    { key: "link_ctr",    label: "CTR (link)", num: true, fmt: v => v.toFixed(2) + "%" },
  ];
  return [
    { key: "ad_name",       label: "Ad" },
    { key: "adset_name",    label: "Ad Set" },
    { key: "campaign_name", label: "Campaign" },
    { key: "spend",       label: "Spend",      num: true, fmt: fmt.money },
    ...adjCols,
    { key: "purchases",   label: "Meta Subs",  num: true },
    { key: "cpr",         label: "Cost/Result",num: true, fmt: v => v > 0 ? fmt.money(v) : "—" },
    { key: "cpi",         label: "CPI",        num: true, fmt: fmt.money },
    { key: "link_ctr",    label: "CTR (link)", num: true, fmt: v => v.toFixed(2) + "%" },
  ];
}

function profitFmt(v) {
  if (v == null || v === 0) return "—";
  if (v > 0) return `<span class="profit-pos">+${fmt.money(v)}</span>`;
  return `<span class="profit-neg">${fmt.money(v)}</span>`;
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
    };
    agg[k].spend       += a.spend;
    agg[k].impressions += a.impressions;
    agg[k].clicks      += a.clicks;
    agg[k].link_clicks += a.link_clicks || 0;
    agg[k].installs    += a.installs;
    agg[k].purchases   += a.purchases || 0;
  }
  return Object.values(agg).map(r => {
    const adjMap = META.tab === "campaigns" ? ADJ.byCampaignId : ADJ.byAdsetId;
    const adjKey = META.tab === "campaigns" ? r.campaign_id : r.adset_id;
    const adj = adjMap.get(adjKey) || { installs: 0, revenue: 0, events: 0 };
    return {
      ...r,
      ctr: r.impressions > 0 ? r.clicks / r.impressions * 100 : 0,
      link_ctr: r.impressions > 0 ? r.link_clicks / r.impressions * 100 : 0,
      cpc: r.clicks > 0 ? r.spend / r.clicks : 0,
      cpm: r.impressions > 0 ? r.spend / r.impressions * 1000 : 0,
      cpi: r.installs > 0 ? r.spend / r.installs : 0,
      cpr: r.purchases > 0 ? r.spend / r.purchases : 0,
      adj_installs: adj.installs,
      adj_revenue: adj.revenue,
      adj_events: adj.events,
      roas: r.spend > 0 ? adj.revenue / r.spend * 100 : 0,
      profit: adj.revenue - r.spend,
    };
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
    return `<th data-meta-sort="${c.key}" class="${c.num ? "num" : ""}">${c.label}${arrow}</th>`;
  }).join("") + "</tr>";

  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="${cols.length}" class="empty-state">No data</td></tr>`;
  } else {
    body.innerHTML = rows.map(r => {
      const cells = cols.map(c => {
        let v = r[c.key];
        if (v == null || v === "") v = "—";
        else if (c.fmt) v = c.fmt(v);
        else if (c.num) v = fmt.num(v);
        let drill = "";
        if (c.drill === "campaign") drill = `data-drill-campaign="${r.campaign_id}"`;
        if (c.drill === "adset")    drill = `data-drill-adset="${r.adset_id}" data-drill-camp="${r.campaign_id}"`;
        const cls = (c.num ? "num " : "") + (c.drill ? "drill" : "");
        return `<td class="${cls.trim()}" ${drill}>${v}</td>`;
      }).join("");
      return `<tr>${cells}</tr>`;
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

  loadMetaData();
}
