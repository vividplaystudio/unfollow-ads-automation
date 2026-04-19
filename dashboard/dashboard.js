// ═══════════════════════════════════════════════════════════════
// Ads Dashboard — logic
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
  editMode: false,
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

async function pauseKeyword(k) {
  if (!confirm(`Pause keyword "${k.keyword}" in ${k.campaign}?`)) return;
  try {
    const path = `/campaigns/${k.campaign_id}/adgroups/${k.ad_group_id}/targetingkeywords/${k.keyword_id}`;
    await asaCall("PUT", path, {
      id: k.keyword_id,
      adGroupId: k.ad_group_id,
      status: "PAUSED",
    });
    toast(`Paused: ${k.keyword}`, "success");
    k.status = "PAUSED";
    renderTable();
  } catch (e) {
    toast(`Failed: ${e.message}`, "error");
  }
}

async function enableKeyword(k) {
  try {
    const path = `/campaigns/${k.campaign_id}/adgroups/${k.ad_group_id}/targetingkeywords/${k.keyword_id}`;
    await asaCall("PUT", path, {
      id: k.keyword_id,
      adGroupId: k.ad_group_id,
      status: "ACTIVE",
    });
    toast(`Enabled: ${k.keyword}`, "success");
    k.status = "ACTIVE";
    renderTable();
  } catch (e) {
    toast(`Failed: ${e.message}`, "error");
  }
}

async function updateBid(k, newBid) {
  if (!newBid || isNaN(newBid) || newBid <= 0) {
    toast("Invalid bid", "error");
    return;
  }
  if (!confirm(`Change bid for "${k.keyword}" to $${newBid}?`)) return;
  try {
    const path = `/campaigns/${k.campaign_id}/adgroups/${k.ad_group_id}/targetingkeywords/${k.keyword_id}`;
    await asaCall("PUT", path, {
      id: k.keyword_id,
      adGroupId: k.ad_group_id,
      bidAmount: { amount: String(newBid), currency: "USD" },
    });
    toast(`Bid updated: ${k.keyword} → $${newBid}`, "success");
    k.bid = parseFloat(newBid);
    renderTable();
  } catch (e) {
    toast(`Failed: ${e.message}`, "error");
  }
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

function renderActions(k) {
  // Only show actions for keywords with valid IDs
  if (!k.keyword_id || !k.ad_group_id || !k.campaign_id) {
    return `<span class='muted' style='font-size:11px'>—</span>`;
  }
  if (!STATE.editMode) {
    return `<span class='muted' style='font-size:11px'>Enable edit mode</span>`;
  }
  const isPaused = k.status === "PAUSED";
  const bidValue = (k.bid || 0).toFixed(2);
  return `
    <div class="action-cell">
      <div class="bid-editor">
        <input type="number" step="0.01" min="0.1" value="${bidValue}"
               data-kw-id="${k.keyword_id}" data-ag-id="${k.ad_group_id}" data-c-id="${k.campaign_id}"
               data-action="bid" />
        <button class="btn-action btn-save" data-action="save-bid" data-kw-id="${k.keyword_id}">Save</button>
      </div>
      ${isPaused
        ? `<button class="btn-action btn-enable" data-action="enable" data-kw-id="${k.keyword_id}">Enable</button>`
        : `<button class="btn-action btn-pause" data-action="pause" data-kw-id="${k.keyword_id}">Pause</button>`}
    </div>
  `;
}

function findKeyword(kwId) {
  return (STATE.data?.keywords || []).find(k => String(k.keyword_id) === String(kwId));
}

function matchesFilters(row, includeCampaign = true) {
  if (STATE.country && row.country !== STATE.country) return false;
  if (includeCampaign && STATE.campaign) {
    if (row.campaign !== STATE.campaign && row.name !== STATE.campaign) return false;
  }
  return true;
}

function rangeKey() {
  return STATE.range;
}

function getMetric(row, metric) {
  const val = row[metric + "_" + rangeKey()];
  return val != null ? val : 0;
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

  // Profit sub
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

// ─── Render Charts ─────────────────────────────────────────────
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
        {
          label: "Spend",
          data: countries.map(c => byCountry[c].spend),
          backgroundColor: "#c7d2fe",
          borderRadius: 4,
        },
        {
          label: "Revenue",
          data: countries.map(c => byCountry[c].revenue),
          backgroundColor: "#4f46e5",
          borderRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom", labels: { usePointStyle: true, padding: 14, font: { size: 12 } } },
      },
      scales: {
        y: { beginAtZero: true, ticks: { callback: v => "$" + v } },
      },
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
        {
          label: "Spend",
          data: sorted.map(c => getMetric(c, "spend")),
          backgroundColor: "#fca5a5",
          borderRadius: 4,
        },
        {
          label: "Revenue",
          data: sorted.map(c => getMetric(c, "revenue")),
          backgroundColor: "#10b981",
          borderRadius: 4,
        },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom", labels: { usePointStyle: true, padding: 14, font: { size: 12 } } },
      },
      scales: {
        x: { beginAtZero: true, ticks: { callback: v => "$" + v } },
      },
    },
  });
}

// ─── Render Table ──────────────────────────────────────────────
function renderTable() {
  const head = document.getElementById("tableHead");
  const body = document.getElementById("tableBody");

  let rows = [];
  let cols = [];

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
    // Campaigns tab — always show ALL campaigns (ignore campaign filter)
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
  } else if (STATE.tab === "keywords" || STATE.tab === "winners" || STATE.tab === "losers") {
    cols = [
      { key: "keyword", label: "Keyword" },
      { key: "campaign", label: "Campaign" },
      { key: "country", label: "Country" },
      { key: "match", label: "Match" },
      { key: "kw_state", label: "State" },
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
      { key: "actions", label: "Actions" },
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

    // Winners/Losers thresholds adapt to range
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

  // Apply search
  if (STATE.search) {
    const s = STATE.search.toLowerCase();
    rows = rows.filter(r => Object.values(r).some(v => String(v).toLowerCase().includes(s)));
  }

  // Sort
  const sortCol = STATE.sortCol || "spend";
  const dir = STATE.sortDir === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    let va = a[sortCol] ?? 0;
    let vb = b[sortCol] ?? 0;
    if (typeof va === "string" && typeof vb === "string") return va.localeCompare(vb) * dir;
    return (va - vb) * dir;
  });

  // Render headers
  head.innerHTML = "<tr>" + cols.map(c => {
    const isSorted = sortCol === c.key;
    const sortClass = isSorted ? `sorted${STATE.sortDir === "asc" ? "-asc" : ""}` : "";
    const title = c.label === "W" ? "Weekly Subs" : c.label === "M" ? "Monthly Subs" : c.label === "Y" ? "Yearly Subs" : c.label;
    return `<th class="${c.num ? "num" : ""} ${sortClass}" data-col="${c.key}" title="${title}">${c.label}</th>`;
  }).join("") + "</tr>";

  // Render body
  if (rows.length === 0) {
    body.innerHTML = `<tr><td colspan="${cols.length}"><div class="empty-state"><div class="icon">📭</div>No data for this view/range</div></td></tr>`;
  } else {
    body.innerHTML = rows.map(r => {
      const clickable = STATE.tab === "campaigns" ? "clickable" : "";
      return `<tr class='${clickable}' data-name='${(r.name || r.keyword || "").replace(/'/g, "&#39;")}'>` + cols.map(c => {
        let val = r[c.key];
        let content, cls = c.num ? "num" : "";
        switch (c.key) {
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
          case "kw_state":
            // Show the ASA keyword status (ACTIVE/PAUSED/etc) as a badge
            content = r.status ? `<span class="kw-status-badge kw-status-${r.status}">${r.status}</span>` : "<span class='muted'>—</span>";
            break;
          case "bid":
            content = r.bid > 0 ? fmt.money(r.bid) : "<span class='muted'>—</span>";
            break;
          case "actions":
            content = renderActions(r);
            break;
          default:
            content = val != null && val !== "" ? (typeof val === "number" ? fmt.num(val) : String(val)) : "<span class='muted'>—</span>";
        }
        return `<td class="${cls}">${content}</td>`;
      }).join("") + "</tr>";
    }).join("");

    // Campaigns tab → click a row to filter keywords to that campaign
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

  // Header click → sort
  head.querySelectorAll("th").forEach(th => {
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

  document.getElementById("tableInfo").textContent = `${rows.length} row${rows.length === 1 ? "" : "s"}`;
}

function updateTabs() {
  document.querySelectorAll(".tab").forEach(t => {
    t.classList.toggle("active", t.dataset.tab === STATE.tab);
  });
}

function render() {
  if (!STATE.data) return;
  renderKPIs();
  renderCharts();
  renderTable();
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

  // Country filter
  document.getElementById("countryFilter").addEventListener("change", e => {
    STATE.country = e.target.value;
    render();
  });

  // Campaign filter
  document.getElementById("campaignFilter").addEventListener("change", e => {
    STATE.campaign = e.target.value;
    render();
  });

  // Clear filters button
  const clearBtn = document.getElementById("clearFilters");
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      STATE.country = "";
      STATE.campaign = "";
      STATE.search = "";
      document.getElementById("countryFilter").value = "";
      document.getElementById("campaignFilter").value = "";
      document.getElementById("searchBox").value = "";
      render();
    });
  }

  // Tabs — when clicking Campaigns, clear campaign filter so we see all
  document.querySelectorAll(".tab").forEach(t => {
    t.addEventListener("click", () => {
      const newTab = t.dataset.tab;
      // If clicking Campaigns and there's a campaign filter, clear it so we see all campaigns
      if (newTab === "campaigns" && STATE.campaign) {
        STATE.campaign = "";
        document.getElementById("campaignFilter").value = "";
      }
      STATE.tab = newTab;
      STATE.sortCol = null;
      updateTabs();
      render();
    });
  });

  // Search
  document.getElementById("searchBox").addEventListener("input", e => {
    STATE.search = e.target.value;
    renderTable();
  });

  // Edit mode toggle
  const editCheckbox = document.getElementById("editMode");
  if (editCheckbox) {
    editCheckbox.addEventListener("change", e => {
      STATE.editMode = e.target.checked;
      document.body.classList.toggle("edit-on", STATE.editMode);
      if (STATE.editMode) {
        toast("⚠ Edit mode ON — actions will apply live to Apple Search Ads", "");
      }
      renderTable();
    });
  }

  // Event delegation for action buttons in table
  document.getElementById("tableBody").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    const kwId = btn.dataset.kwId;
    const k = findKeyword(kwId);
    if (!k) return;

    if (action === "pause") pauseKeyword(k);
    else if (action === "enable") enableKeyword(k);
    else if (action === "save-bid") {
      const input = btn.parentElement.querySelector("input[data-action='bid']");
      if (input) updateBid(k, parseFloat(input.value));
    }
  });

  loadData();
  setInterval(loadData, 5 * 60 * 1000);
});
