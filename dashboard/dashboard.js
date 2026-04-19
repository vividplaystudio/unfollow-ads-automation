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
};

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

  // Populate country dropdown
  const countries = new Set();
  for (const c of STATE.data.campaigns || []) {
    if (c.country) countries.add(c.country);
  }
  const countrySel = document.getElementById("countryFilter");
  countrySel.innerHTML = '<option value="">All countries</option>';
  [...countries].sort().forEach(c => {
    countrySel.innerHTML += `<option value="${c}">${c}</option>`;
  });

  // Populate campaign dropdown
  const campSel = document.getElementById("campaignFilter");
  campSel.innerHTML = '<option value="">All campaigns</option>';
  (STATE.data.campaigns || [])
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name))
    .forEach(c => {
      campSel.innerHTML += `<option value="${c.name}">${c.name}</option>`;
    });

  // Last updated
  const lu = STATE.data.last_updated
    ? new Date(STATE.data.last_updated)
    : null;
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
  moneyShort: v => v == null ? "—" : "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 }),
  num: v => v == null ? "—" : Number(v).toLocaleString(),
  pct: v => v == null || v === 0 ? "—" : (v < 100 ? v.toFixed(0) : Math.round(v)) + "%",
};

function roasClass(roas) {
  if (!roas || roas === 0) return "roas-none";
  if (roas >= 100) return "roas-high";
  if (roas >= 50) return "roas-mid";
  return "roas-low";
}

function roasBadge(roas, spend) {
  if (spend < 15) return `<span class="badge badge-wait">WAIT</span>`;
  if (!roas || roas === 0) return `<span class="badge badge-pause">PAUSE</span>`;
  if (roas >= 100) return `<span class="badge badge-winner">WINNER</span>`;
  if (roas >= 50) return `<span class="badge badge-watch">WATCH</span>`;
  if (roas >= 30) return `<span class="badge badge-ok">OK</span>`;
  return `<span class="badge badge-losing">LOSING</span>`;
}

function matchesFilters(row) {
  if (STATE.country && row.country !== STATE.country) return false;
  if (STATE.campaign && row.campaign !== STATE.campaign && row.name !== STATE.campaign) return false;
  return true;
}

function rangeKey() {
  return STATE.range;
}

function getMetric(row, metric) {
  // Extract spend_7d, revenue_30d, etc.
  const key = metric + "_" + rangeKey();
  const val = row[key];
  return val != null ? val : 0;
}

// ─── Render KPIs ───────────────────────────────────────────────
function renderKPIs() {
  const campaigns = (STATE.data?.campaigns || []).filter(matchesFilters);

  let spend = 0, revenue = 0, installs = 0, subs = 0;
  for (const c of campaigns) {
    spend += getMetric(c, "spend");
    revenue += getMetric(c, "revenue");
    installs += getMetric(c, "installs");
    subs += getMetric(c, "subs");
  }
  const roas = spend > 0 ? revenue / spend * 100 : 0;
  const cpa = installs > 0 ? spend / installs : 0;

  document.getElementById("kpiSpend").textContent = fmt.money(spend);
  document.getElementById("kpiRevenue").textContent = fmt.money(revenue);
  document.getElementById("kpiRoas").textContent = fmt.pct(roas);
  document.getElementById("kpiSubs").textContent = fmt.num(subs);
  document.getElementById("kpiInstalls").textContent = fmt.num(installs);
  document.getElementById("kpiCpa").textContent = fmt.money(cpa);

  // Profit/loss sub
  const profit = revenue - spend;
  const profitEl = document.getElementById("kpiRoasSub");
  profitEl.textContent = (profit >= 0 ? "+" : "") + fmt.money(Math.abs(profit)) + " profit";
  profitEl.style.color = profit >= 0 ? "var(--success)" : "var(--danger)";

  document.getElementById("kpiSpendSub").textContent = `${campaigns.length} campaigns`;
  document.getElementById("kpiInstallsSub").textContent = installs > 0 ? `${(subs / installs * 100).toFixed(1)}% convert` : "—";
  document.getElementById("kpiSubsSub").textContent = subs > 0 ? fmt.money(revenue / subs) + " per sub" : "—";
  document.getElementById("kpiCpaSub").textContent = installs > 0 ? `${installs} installs` : "—";
  document.getElementById("kpiRevenueSub").textContent = "—";
}

// ─── Render Charts ─────────────────────────────────────────────
function destroyChart(name) {
  if (STATE.charts[name]) {
    STATE.charts[name].destroy();
    STATE.charts[name] = null;
  }
}

function renderCharts() {
  const campaigns = (STATE.data?.campaigns || []).filter(matchesFilters);

  // Chart 1: ROAS by country
  const byCountry = {};
  for (const c of campaigns) {
    const country = c.country || "?";
    if (!byCountry[country]) byCountry[country] = { spend: 0, revenue: 0 };
    byCountry[country].spend += getMetric(c, "spend");
    byCountry[country].revenue += getMetric(c, "revenue");
  }
  const countries = Object.keys(byCountry).sort();
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

  // Chart 2: Top campaigns by spend
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
          backgroundColor: "#f59e0b",
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
  const rk = rangeKey();

  let rows = [];
  let cols = [];

  if (STATE.tab === "campaigns") {
    cols = [
      { key: "name", label: "Campaign" },
      { key: "country", label: "Country" },
      { key: "spend", label: "Spend", num: true },
      { key: "revenue", label: "Revenue", num: true },
      { key: "subs", label: "Subs", num: true },
      { key: "installs", label: "Installs", num: true },
      { key: "roas", label: "ROAS", num: true },
      { key: "cpa", label: "CPA", num: true },
      { key: "status", label: "Status" },
    ];
    rows = (STATE.data?.campaigns || [])
      .filter(matchesFilters)
      .map(c => {
        const spend = getMetric(c, "spend");
        const revenue = getMetric(c, "revenue");
        const installs = getMetric(c, "installs");
        const subs = getMetric(c, "subs");
        const roas = spend > 0 ? revenue / spend * 100 : 0;
        const cpa = installs > 0 ? spend / installs : 0;
        return { ...c, spend, revenue, installs, subs, roas, cpa, _name: c.name };
      });
  } else if (STATE.tab === "keywords" || STATE.tab === "winners" || STATE.tab === "losers") {
    cols = [
      { key: "keyword", label: "Keyword" },
      { key: "campaign", label: "Campaign" },
      { key: "country", label: "Country" },
      { key: "match", label: "Match" },
      { key: "spend", label: "Spend", num: true },
      { key: "revenue", label: "Revenue", num: true },
      { key: "subs", label: "Subs", num: true },
      { key: "installs", label: "Installs", num: true },
      { key: "roas", label: "ROAS", num: true },
      { key: "cpa", label: "CPA", num: true },
      { key: "cpt", label: "CPT", num: true },
      { key: "ttr", label: "TTR", num: true },
      { key: "cr", label: "CR", num: true },
      { key: "status", label: "Status" },
    ];
    rows = (STATE.data?.keywords || [])
      .filter(matchesFilters)
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
        return { ...k, spend, revenue, installs, subs, taps, imp, roas, cpa, ttr, cr, cpt };
      });

    // Filter Winners/Losers
    if (STATE.tab === "winners") {
      rows = rows.filter(r => r.spend >= 15 && r.roas >= 100);
    } else if (STATE.tab === "losers") {
      rows = rows.filter(r => r.spend >= 15 && r.roas < 30);
    }
  } else if (STATE.tab === "ads") {
    cols = [
      { key: "name", label: "Ad / CPP" },
      { key: "campaign", label: "Campaign" },
      { key: "country", label: "Country" },
      { key: "spend", label: "Spend", num: true },
      { key: "installs", label: "Installs", num: true },
      { key: "impressions", label: "Impressions", num: true },
      { key: "taps", label: "Taps", num: true },
      { key: "cpa", label: "CPA", num: true },
    ];
    rows = (STATE.data?.ads || []).filter(matchesFilters);
  }

  // Apply search
  if (STATE.search) {
    const s = STATE.search.toLowerCase();
    rows = rows.filter(r => {
      return Object.values(r).some(v => String(v).toLowerCase().includes(s));
    });
  }

  // Sort
  const sortCol = STATE.sortCol || "spend";
  const dir = STATE.sortDir === "asc" ? 1 : -1;
  rows.sort((a, b) => {
    let va = a[sortCol] ?? 0;
    let vb = b[sortCol] ?? 0;
    if (typeof va === "string" && typeof vb === "string") {
      return va.localeCompare(vb) * dir;
    }
    return (va - vb) * dir;
  });

  // Render headers
  head.innerHTML = "<tr>" + cols.map(c => {
    const isSorted = sortCol === c.key;
    const sortClass = isSorted ? `sorted${STATE.sortDir === "asc" ? "-asc" : ""}` : "";
    return `<th class="${c.num ? "num" : ""} ${sortClass}" data-col="${c.key}">${c.label}</th>`;
  }).join("") + "</tr>";

  // Render body
  if (rows.length === 0) {
    body.innerHTML = `<tr><td colspan="${cols.length}"><div class="empty-state"><div class="icon">📭</div>No data yet for this view</div></td></tr>`;
  } else {
    body.innerHTML = rows.map(r => {
      return "<tr class='" + (STATE.tab === "campaigns" ? "clickable" : "") + "' data-name='" + (r.name || r.keyword || "") + "'>" + cols.map(c => {
        let val = r[c.key];
        let content, cls = c.num ? "num" : "";
        switch (c.key) {
          case "spend":
          case "revenue":
          case "cpa":
          case "cpt":
            content = val > 0 ? fmt.money(val) : "<span class='muted'>—</span>";
            break;
          case "roas":
            if (val > 0) {
              content = `<span class="${roasClass(val)}">${fmt.pct(val)}</span>`;
            } else {
              content = "<span class='muted'>—</span>";
            }
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
            content = roasBadge(r.roas, r.spend);
            break;
          case "campaign":
            content = val ? (val.length > 32 ? val.slice(0, 30) + "…" : val) : "<span class='muted'>—</span>";
            break;
          case "keyword":
            content = `<strong>${val || ""}</strong>`;
            break;
          default:
            content = val != null ? (typeof val === "number" ? fmt.num(val) : String(val)) : "<span class='muted'>—</span>";
        }
        return `<td class="${cls}">${content}</td>`;
      }).join("") + "</tr>";
    }).join("");

    // Row click handlers (campaigns → jump to keywords)
    if (STATE.tab === "campaigns") {
      body.querySelectorAll("tr.clickable").forEach(tr => {
        tr.addEventListener("click", () => {
          const name = tr.dataset.name;
          STATE.campaign = name;
          STATE.tab = "keywords";
          document.getElementById("campaignFilter").value = name;
          updateTabs();
          render();
        });
      });
    }
  }

  // Header click → sort
  head.querySelectorAll("th").forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      if (STATE.sortCol === col) {
        STATE.sortDir = STATE.sortDir === "desc" ? "asc" : "desc";
      } else {
        STATE.sortCol = col;
        STATE.sortDir = "desc";
      }
      renderTable();
    });
  });

  // Update info
  document.getElementById("tableInfo").textContent = `${rows.length} row${rows.length === 1 ? "" : "s"}`;
}

function updateTabs() {
  document.querySelectorAll(".tab").forEach(t => {
    t.classList.toggle("active", t.dataset.tab === STATE.tab);
  });
}

// ─── Main render ───────────────────────────────────────────────
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

  // Tabs
  document.querySelectorAll(".tab").forEach(t => {
    t.addEventListener("click", () => {
      STATE.tab = t.dataset.tab;
      STATE.sortCol = null;
      updateTabs();
      renderTable();
    });
  });

  // Search
  document.getElementById("searchBox").addEventListener("input", e => {
    STATE.search = e.target.value;
    renderTable();
  });

  // Initial load
  loadData();

  // Auto-refresh every 5 min
  setInterval(loadData, 5 * 60 * 1000);
});
