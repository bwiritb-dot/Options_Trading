const API = "http://localhost:5000/api";
let instruments = [];
let portfolio = [];
let currentPrice = 2000;
let selectedInstrument = null;
let activeExp = "ALL";
let currency = "USD";
let animFrame = null;

// Chart view state
let chartView = null;      // { minX, maxX } or null = auto-fit
let lastChartData = null;  // cached render params for crosshair + zoom handlers
let isDragging = false;
let dragStart = null;      // { clientX, viewMinX, viewMaxX }

// ===== INIT =====
document.addEventListener("DOMContentLoaded", async () => {
    setupControls();
    setupChartInteraction();
    await loadPrice();
    await loadInstruments();
    setTimeDates();
    drawChart();
    setInterval(loadPrice, 10000);
});

// ===== LOAD DATA =====
async function loadPrice() {
    try {
        const r = await fetch(`${API}/current-price`);
        const d = await r.json();
        currentPrice = d.price || 2000;
        if (portfolio.length) scheduleChart();
    } catch(e) { /* use cached */ }
}

async function loadInstruments() {
    try {
        const r = await fetch(`${API}/instruments`);
        const d = await r.json();
        instruments = d.instruments || [];
        buildDateTabs();
        renderInstrumentList();
    } catch(e) {
        showError("Backend not running. Start with: python app.py");
    }
}

// ===== DATE TABS =====
function buildDateTabs() {
    const expiries = new Set();
    instruments.forEach(i => {
        const p = i.instrument_name.split("-");
        if (p[1]) expiries.add(p[1]);
    });

    const tabs = document.getElementById("dateTabs");
    tabs.innerHTML = `<div class="date-tab active" data-exp="ALL">All</div>`;

    const sorted = Array.from(expiries).sort((a, b) => {
        const da = parseExpiry(a), db = parseExpiry(b);
        return da - db;
    });

    sorted.forEach(exp => {
        const d = parseExpiry(exp);
        const label = d ? d.toLocaleDateString("en-GB", {day:"2-digit", month:"short"}) : exp;
        const el = document.createElement("div");
        el.className = "date-tab";
        el.dataset.exp = exp;
        el.textContent = label;
        tabs.appendChild(el);
    });

    tabs.querySelectorAll(".date-tab").forEach(tab => {
        tab.addEventListener("click", () => {
            tabs.querySelectorAll(".date-tab").forEach(t => t.classList.remove("active"));
            tab.classList.add("active");
            activeExp = tab.dataset.exp;
            renderInstrumentList();
        });
    });
}

function parseExpiry(expStr) {
    try {
        const m = expStr.match(/(\d{1,2})([A-Z]{3})(\d{2})/);
        if (!m) return null;
        return new Date(`${m[2]} ${m[1]} 20${m[3]}`);
    } catch(e) { return null; }
}

// ===== INSTRUMENT LIST =====
function renderInstrumentList() {
    const search = document.getElementById("searchInput").value.toLowerCase();
    const container = document.getElementById("instrumentList");

    let filtered = instruments.filter(i => {
        const name = i.instrument_name;
        const matchSearch = name.toLowerCase().includes(search);
        const matchExp = activeExp === "ALL" || name.includes(activeExp);
        return matchSearch && matchExp;
    });

    if (filtered.length === 0) {
        container.innerHTML = `<div style="padding:10px;color:#555;font-size:12px;">No instruments found</div>`;
        return;
    }

    container.innerHTML = filtered.map(i => `
        <div class="instrument-item ${selectedInstrument?.instrument_name === i.instrument_name ? 'selected' : ''}"
             data-name="${i.instrument_name}">
            ${i.instrument_name}
        </div>
    `).join("");

    container.querySelectorAll(".instrument-item").forEach(el => {
        el.addEventListener("click", () => {
            const name = el.dataset.name;
            selectedInstrument = instruments.find(i => i.instrument_name === name);
            renderInstrumentList();
        });
    });
}

// ===== TIME DATES =====
function setTimeDates() {
    const now = new Date();
    const expiries = portfolio.map(p => {
        const parts = p.instrument_name.split("-");
        return parseExpiry(parts[1]);
    }).filter(Boolean);

    const maxExp = expiries.length ? new Date(Math.max(...expiries)) : new Date(now.getTime() + 5 * 86400000);

    document.getElementById("endDateBadge").textContent = fmt(maxExp) + " 📅";

    document.getElementById("timeSlider").dataset.start = now.getTime();
    document.getElementById("timeSlider").dataset.end   = maxExp.getTime();

    const selectedDate = getSelectedDate();
    document.getElementById("startDateBadge").textContent = fmt(selectedDate) + " 📅";
}

function fmt(d) {
    return d.toLocaleDateString("en-GB", {day:"2-digit", month:"2-digit", year:"numeric"}).replace(/\//g,"-");
}

function getSelectedDate() {
    const slider = document.getElementById("timeSlider");
    const start = parseFloat(slider.dataset.start || Date.now());
    const end   = parseFloat(slider.dataset.end   || Date.now() + 5*86400000);
    const val   = parseFloat(slider.value) / 100;
    const ts    = start + (end - start) * val;
    return new Date(ts);
}

// ===== CONTROLS =====
function setupControls() {
    document.getElementById("longBtn").addEventListener("click", () => addPosition(1));
    document.getElementById("shortBtn").addEventListener("click", () => addPosition(-1));
    document.getElementById("searchInput").addEventListener("input", renderInstrumentList);

    // Recenter: reset zoom/pan then redraw
    document.getElementById("recenterBtn").addEventListener("click", () => {
        chartView = null;
        scheduleChart();
    });

    document.getElementById("volSlider").addEventListener("input", e => {
        document.getElementById("volValue").textContent = e.target.value + "%";
        scheduleChart();
    });
    document.getElementById("rateSlider").addEventListener("input", e => {
        document.getElementById("rateValue").textContent = e.target.value + "%";
        scheduleChart();
    });
    document.getElementById("timeSlider").addEventListener("input", () => {
        const d = getSelectedDate();
        document.getElementById("startDateBadge").textContent = fmt(d) + " 📅";
        scheduleChart();
    });

    document.querySelectorAll("[data-cur]").forEach(btn => {
        btn.addEventListener("click", e => {
            document.querySelectorAll("[data-cur]").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            currency = btn.dataset.cur;
            scheduleChart();
        });
    });
}

function scheduleChart() {
    if (animFrame) cancelAnimationFrame(animFrame);
    animFrame = requestAnimationFrame(drawChart);
}

// ===== CHART INTERACTION (zoom + pan + crosshair) =====
function setupChartInteraction() {
    const canvas = document.getElementById("pnlChart");

    canvas.style.cursor = "crosshair";

    // Zoom with mouse wheel (X axis only, anchored at mouse position)
    canvas.addEventListener("wheel", (e) => {
        e.preventDefault();
        if (!lastChartData || !chartView) return;
        const rect = canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const { PAD, cW } = lastChartData;
        if (mx < PAD.left || mx > PAD.left + cW) return;

        const frac = (mx - PAD.left) / cW;
        const { minX, maxX } = chartView;
        const dataX = minX + frac * (maxX - minX);
        const factor = e.deltaY > 0 ? 1.25 : 0.8;
        const newRange = (maxX - minX) * factor;

        chartView = {
            minX: dataX - frac * newRange,
            maxX: dataX + (1 - frac) * newRange,
        };
        scheduleChart();
    }, { passive: false });

    // Pan with mouse drag
    canvas.addEventListener("mousedown", (e) => {
        if (e.button !== 0 || !chartView) return;
        isDragging = true;
        dragStart = { clientX: e.clientX, minX: chartView.minX, maxX: chartView.maxX };
        canvas.style.cursor = "grabbing";
    });

    canvas.addEventListener("mousemove", (e) => {
        if (isDragging && dragStart && chartView && lastChartData) {
            const dx = e.clientX - dragStart.clientX;
            const { cW } = lastChartData;
            const range = dragStart.maxX - dragStart.minX;
            const dataDx = (dx / cW) * range;
            chartView = {
                minX: dragStart.minX - dataDx,
                maxX: dragStart.maxX - dataDx,
            };
            scheduleChart();
        }
        drawCrosshair(e);
    });

    canvas.addEventListener("mouseup", () => {
        isDragging = false;
        canvas.style.cursor = "crosshair";
    });

    canvas.addEventListener("mouseleave", () => {
        isDragging = false;
        canvas.style.cursor = "crosshair";
        clearCrosshair();
    });
}

// ===== CROSSHAIR =====
function drawCrosshair(e) {
    if (!lastChartData || !chartView) return;
    const canvas = document.getElementById("pnlChart");
    const overlay = document.getElementById("crosshairCanvas");
    if (!overlay) return;

    const dpr = window.devicePixelRatio || 1;
    // Keep overlay in sync with main canvas
    if (overlay.width !== canvas.width || overlay.height !== canvas.height) {
        overlay.width  = canvas.width;
        overlay.height = canvas.height;
        overlay.style.width  = canvas.style.width;
        overlay.style.height = canvas.style.height;
    }

    const ctx = overlay.getContext("2d");
    ctx.clearRect(0, 0, overlay.width, overlay.height);

    const { PAD, cW, cH, W, H, spots, pnlNow, pnlTheo } = lastChartData;
    const { minX, maxX, minY, maxY } = lastChartData; // effective bounds used when drawn

    const xRange = maxX - minX || 1;
    const yRange = maxY - minY || 1;

    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;

    // Only draw when cursor is inside the chart area
    if (mx < PAD.left || mx > PAD.left + cW || my < PAD.top || my > PAD.top + cH) return;

    // Find nearest data spot to mouse X
    const dataX = minX + ((mx - PAD.left) / cW) * xRange;
    let nearestIdx = 0;
    let minDist = Infinity;
    for (let i = 0; i < spots.length; i++) {
        const d = Math.abs(spots[i] - dataX);
        if (d < minDist) { minDist = d; nearestIdx = i; }
    }

    const spotVal  = spots[nearestIdx];
    const greenVal = pnlTheo[nearestIdx];   // green = current theo line
    const blueVal  = pnlNow[nearestIdx];    // blue  = expiry payoff line

    const toX = s => PAD.left + ((s - minX) / xRange) * cW;
    const toY = v => PAD.top  + cH - ((v - minY) / yRange) * cH;

    const cx     = toX(spotVal);
    const greenY = toY(greenVal);
    const blueY  = toY(blueVal);

    ctx.save();
    ctx.scale(dpr, dpr);

    // ---- Vertical crosshair line ----
    ctx.strokeStyle = "rgba(255,255,255,0.25)";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(cx, PAD.top);
    ctx.lineTo(cx, PAD.top + cH);
    ctx.stroke();

    // ---- Horizontal line at green (theo) value ----
    if (greenY >= PAD.top && greenY <= PAD.top + cH) {
        ctx.strokeStyle = "rgba(61,191,124,0.55)";
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(PAD.left, greenY);
        ctx.lineTo(PAD.left + cW, greenY);
        ctx.stroke();
    }

    // ---- Horizontal line at blue (expiry) value ----
    if (blueY >= PAD.top && blueY <= PAD.top + cH) {
        ctx.strokeStyle = "rgba(123,106,255,0.55)";
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(PAD.left, blueY);
        ctx.lineTo(PAD.left + cW, blueY);
        ctx.stroke();
    }

    ctx.setLineDash([]);

    // ---- Dot on green line ----
    if (greenY >= PAD.top && greenY <= PAD.top + cH) {
        ctx.fillStyle = "#3dbf7c";
        ctx.beginPath();
        ctx.arc(cx, greenY, 4, 0, Math.PI * 2);
        ctx.fill();
    }

    // ---- Dot on blue line ----
    if (blueY >= PAD.top && blueY <= PAD.top + cH) {
        ctx.fillStyle = "#7b6aff";
        ctx.beginPath();
        ctx.arc(cx, blueY, 4, 0, Math.PI * 2);
        ctx.fill();
    }

    ctx.font = "10px sans-serif";

    // ---- Spot price badge at top of vertical line ----
    const spotLabel = Math.round(spotVal).toLocaleString();
    const spotLW = ctx.measureText(spotLabel).width + 10;
    let badgeX = cx - spotLW / 2;
    badgeX = Math.max(PAD.left, Math.min(PAD.left + cW - spotLW, badgeX));
    ctx.fillStyle = "rgba(40,40,40,0.92)";
    ctx.strokeStyle = "rgba(255,255,255,0.2)";
    ctx.lineWidth = 0.5;
    roundRect(ctx, badgeX, PAD.top - 20, spotLW, 16, 3);
    ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#ddd";
    ctx.textAlign = "center";
    ctx.fillText(spotLabel, cx, PAD.top - 8);

    // ---- Green value badge on right Y axis ----
    if (greenY >= PAD.top && greenY <= PAD.top + cH) {
        const gLabel = greenVal.toFixed(1);
        const gLW = ctx.measureText(gLabel).width + 10;
        const labelX = PAD.left + cW + 3;
        const labelY = Math.max(PAD.top + 8, Math.min(PAD.top + cH - 8, greenY));
        ctx.fillStyle = "rgba(30,60,45,0.95)";
        ctx.strokeStyle = "#3dbf7c";
        ctx.lineWidth = 0.5;
        roundRect(ctx, labelX, labelY - 8, Math.max(gLW, 36), 16, 3);
        ctx.fill(); ctx.stroke();
        ctx.fillStyle = "#3dbf7c";
        ctx.textAlign = "left";
        ctx.fillText(gLabel, labelX + 5, labelY + 4);
    }

    // ---- Blue value badge on right Y axis ----
    if (blueY >= PAD.top && blueY <= PAD.top + cH) {
        const bLabel = blueVal.toFixed(1);
        const bLW = ctx.measureText(bLabel).width + 10;
        const labelX = PAD.left + cW + 3;
        const labelY = Math.max(PAD.top + 8, Math.min(PAD.top + cH - 8, blueY));
        ctx.fillStyle = "rgba(25,20,60,0.95)";
        ctx.strokeStyle = "#7b6aff";
        ctx.lineWidth = 0.5;
        roundRect(ctx, labelX, labelY - 8, Math.max(bLW, 36), 16, 3);
        ctx.fill(); ctx.stroke();
        ctx.fillStyle = "#7b6aff";
        ctx.textAlign = "left";
        ctx.fillText(bLabel, labelX + 5, labelY + 4);
    }

    ctx.restore();
}

function clearCrosshair() {
    const overlay = document.getElementById("crosshairCanvas");
    if (!overlay) return;
    const ctx = overlay.getContext("2d");
    ctx.clearRect(0, 0, overlay.width, overlay.height);
}

function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
}

// ===== ADD POSITION =====
async function addPosition(side) {
    if (!selectedInstrument) { showError("Select an instrument first"); return; }
    const amount = parseFloat(document.getElementById("amountInput").value);
    if (!amount || amount <= 0) { showError("Enter a valid amount"); return; }

    const existing = portfolio.find(p => p.instrument_name === selectedInstrument.instrument_name);
    if (existing) {
        existing.amount += amount * side;
        renderTable();
        scheduleChart();
        return;
    }

    let markPrice = 0, iv = 0;
    try {
        const r = await fetch(`${API}/ticker?instrument=${selectedInstrument.instrument_name}`);
        const d = await r.json();
        markPrice = d.mark_price || 0;
        iv        = d.mark_iv || 0;
    } catch(e) {
        markPrice = selectedInstrument.mark_price || 0;
        iv        = selectedInstrument.mark_iv || 0;
    }

    const pos = {
        id: Date.now(),
        instrument_name: selectedInstrument.instrument_name,
        amount: amount * side,
        avg_price: markPrice,
        mark_price: markPrice,
        iv: iv,
        delta: null, vega: null, theta: null,
        checked: true,
    };
    portfolio.push(pos);

    try {
        const gr = await fetch(`${API}/position-greeks`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                instrument_name: pos.instrument_name,
                spot: currentPrice,
                iv: iv,
                vol_shift: parseFloat(document.getElementById("volSlider").value),
                rate_shift: parseFloat(document.getElementById("rateSlider").value),
            })
        });
        const gd = await gr.json();
        pos.delta = gd.delta;
        pos.vega  = gd.vega;
        pos.theta = gd.theta;
    } catch(e) { /* Greeks stay null */ }

    setTimeDates();
    renderTable();
    scheduleChart();
}

// ===== TABLE =====
function renderTable() {
    const tbody = document.getElementById("posTable");
    if (!portfolio.length) {
        tbody.innerHTML = `<tr><td colspan="11" style="text-align:center; color:#444; padding:20px;">No positions — select an instrument and click Long or Short</td></tr>`;
        return;
    }

    tbody.innerHTML = portfolio.map(pos => {
        const pnl = (pos.mark_price - pos.avg_price) * pos.amount * currentPrice;
        const pnlCls = pnl > 0 ? "positive" : pnl < 0 ? "negative" : "neutral";
        const fmt4 = v => (v == null ? "—" : v.toFixed(4));
        const fmt3 = v => (v == null ? "—" : v.toFixed(3));

        return `
        <tr>
            <td><input type="checkbox" ${pos.checked ? "checked" : ""} onchange="togglePos(${pos.id})"></td>
            <td style="text-align:left;">${pos.instrument_name}</td>
            <td>
                <span class="editable" onclick="editCell(this, ${pos.id}, 'amount')">${pos.amount}</span>
            </td>
            <td>
                <span class="editable" onclick="editCell(this, ${pos.id}, 'avg_price')">${pos.avg_price.toFixed(4)}</span>
            </td>
            <td class="neutral">${pos.mark_price.toFixed(4)}</td>
            <td class="${pnlCls}">${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}</td>
            <td class="neutral">${pos.iv ? pos.iv.toFixed(1) + "%" : "—"}</td>
            <td class="neutral">${fmt3(pos.delta)}</td>
            <td class="neutral">${fmt3(pos.vega)}</td>
            <td class="neutral">${fmt3(pos.theta)}</td>
            <td><button class="remove-btn" onclick="removePos(${pos.id})">✕</button></td>
        </tr>`;
    }).join("");
}

function togglePos(id) {
    const p = portfolio.find(x => x.id === id);
    if (p) { p.checked = !p.checked; scheduleChart(); }
}

function removePos(id) {
    portfolio = portfolio.filter(x => x.id !== id);
    renderTable();
    scheduleChart();
}

function editCell(el, id, field) {
    const pos = portfolio.find(x => x.id === id);
    if (!pos) return;
    const current = pos[field];

    const input = document.createElement("input");
    input.type = "number";
    input.className = "edit-input";
    input.value = current;
    input.step = field === "amount" ? "0.1" : "0.0001";

    el.replaceWith(input);
    input.focus();
    input.select();

    function save() {
        const val = parseFloat(input.value);
        if (!isNaN(val)) pos[field] = val;
        renderTable();
        scheduleChart();
    }

    input.addEventListener("blur", save);
    input.addEventListener("keydown", e => { if (e.key === "Enter") save(); if (e.key === "Escape") renderTable(); });
}

// ===== PNL CHART =====
async function drawChart() {
    const canvas = document.getElementById("pnlChart");
    const wrap   = canvas.parentElement;
    const ctx    = canvas.getContext("2d");

    const dpr = window.devicePixelRatio || 1;
    canvas.width  = wrap.clientWidth  * dpr;
    canvas.height = wrap.clientHeight * dpr;
    canvas.style.width  = wrap.clientWidth  + "px";
    canvas.style.height = wrap.clientHeight + "px";
    ctx.scale(dpr, dpr);
    const W = wrap.clientWidth, H = wrap.clientHeight;
    const PAD = { top: 24, right: 64, bottom: 40, left: 60 };
    const cW = W - PAD.left - PAD.right;
    const cH = H - PAD.top  - PAD.bottom;

    ctx.fillStyle = "#0e0e0e";
    ctx.fillRect(0, 0, W, H);

    const active = portfolio.filter(p => p.checked);

    if (!active.length) {
        ctx.fillStyle = "#2a2a2a";
        ctx.font = "14px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("Add positions to see PNL chart", W/2, H/2);
        lastChartData = null;
        return;
    }

    const volShift  = parseFloat(document.getElementById("volSlider").value);
    const rateShift = parseFloat(document.getElementById("rateSlider").value);
    const selDate   = getSelectedDate().toISOString();

    let spots = [], pnlNow = [], pnlTheo = [];

    try {
        const r = await fetch(`${API}/pnl-curve`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                positions: active,
                time_date: selDate,
                vol_shift: volShift,
                rate_shift: rateShift,
                current_price: currentPrice,
                currency: currency,
            })
        });
        const d = await r.json();
        spots   = d.spots;
        pnlNow  = d.pnl_now;
        pnlTheo = d.pnl_theo;
    } catch(e) {
        ctx.fillStyle = "#555";
        ctx.font = "12px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("Backend not responding", W/2, H/2);
        return;
    }

    // ---- Compute base data bounds ----
    const dataMinX = spots[0];
    const dataMaxX = spots[spots.length - 1];

    // Initialize chartView on first draw or after recenter
    if (!chartView) {
        chartView = { minX: dataMinX, maxX: dataMaxX };
    }

    const vMinX = chartView.minX;
    const vMaxX = chartView.maxX;
    const xRange = vMaxX - vMinX || 1;

    // Y range: auto-fit to visible data points only
    const visY = [];
    for (let i = 0; i < spots.length; i++) {
        if (spots[i] >= vMinX && spots[i] <= vMaxX) {
            visY.push(pnlNow[i], pnlTheo[i]);
        }
    }
    if (!visY.length) { visY.push(...pnlNow, ...pnlTheo); }

    const rawMin  = Math.min(0, ...visY);
    const rawMax  = Math.max(0, ...visY);
    const yPad    = (rawMax - rawMin) * 0.12 || 100;
    const vMinY   = rawMin - yPad;
    const vMaxY   = rawMax + yPad;
    const yRange  = vMaxY - vMinY || 1;

    const toX = s => PAD.left + ((s - vMinX) / xRange) * cW;
    const toY = v => PAD.top  + cH - ((v - vMinY) / yRange) * cH;
    const zeroY = toY(0);

    // Cache for crosshair and zoom/pan handlers
    lastChartData = {
        spots, pnlNow, pnlTheo,
        minX: vMinX, maxX: vMaxX, minY: vMinY, maxY: vMaxY,
        PAD, cW, cH, W, H,
    };

    // ---- Grid ----
    ctx.strokeStyle = "#1e1e1e";
    ctx.lineWidth = 1;
    ctx.font = "10px sans-serif";
    ctx.fillStyle = "#555";

    const xSteps = 6, ySteps = 6;

    for (let i = 0; i <= xSteps; i++) {
        const x = PAD.left + (i / xSteps) * cW;
        ctx.beginPath(); ctx.moveTo(x, PAD.top); ctx.lineTo(x, PAD.top + cH); ctx.stroke();
        const val = vMinX + (i / xSteps) * xRange;
        ctx.textAlign = "center";
        ctx.fillText(Math.round(val).toLocaleString(), x, H - PAD.bottom + 14);
    }

    for (let i = 0; i <= ySteps; i++) {
        const y = PAD.top + (i / ySteps) * cH;
        ctx.beginPath(); ctx.moveTo(PAD.left, y); ctx.lineTo(W - PAD.right, y); ctx.stroke();
        const val = vMaxY - (i / ySteps) * yRange;
        ctx.textAlign = "right";
        ctx.fillText(val.toFixed(0), PAD.left - 6, y + 3);
    }

    // ---- Zero line ----
    ctx.strokeStyle = "#333";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(PAD.left, zeroY); ctx.lineTo(W - PAD.right, zeroY); ctx.stroke();

    // Clip to chart area for all curve drawing
    ctx.save();
    ctx.beginPath();
    ctx.rect(PAD.left, PAD.top, cW, cH);
    ctx.clip();

    // ---- Fill areas ----
    const fillArea = (pnl, aboveColor, belowColor) => {
        ctx.beginPath();
        spots.forEach((s, i) => {
            const y = toY(Math.max(pnl[i], 0));
            i === 0 ? ctx.moveTo(toX(s), y) : ctx.lineTo(toX(s), y);
        });
        ctx.lineTo(toX(spots[spots.length - 1]), zeroY);
        ctx.lineTo(toX(spots[0]), zeroY);
        ctx.closePath();
        ctx.fillStyle = aboveColor;
        ctx.fill();

        ctx.beginPath();
        spots.forEach((s, i) => {
            const y = toY(Math.min(pnl[i], 0));
            i === 0 ? ctx.moveTo(toX(s), y) : ctx.lineTo(toX(s), y);
        });
        ctx.lineTo(toX(spots[spots.length - 1]), zeroY);
        ctx.lineTo(toX(spots[0]), zeroY);
        ctx.closePath();
        ctx.fillStyle = belowColor;
        ctx.fill();
    };

    fillArea(pnlTheo, "rgba(22,60,35,0.7)", "rgba(80,18,18,0.7)");

    // ---- pnl_theo line (green = current theo) ----
    ctx.beginPath();
    spots.forEach((s, i) => {
        const x = toX(s), y = toY(pnlTheo[i]);
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#3dbf7c";
    ctx.lineWidth   = 2;
    ctx.stroke();

    // ---- pnl_now line (blue = expiry payoff) ----
    ctx.beginPath();
    spots.forEach((s, i) => {
        const x = toX(s), y = toY(pnlNow[i]);
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#7b6aff";
    ctx.lineWidth   = 2;
    ctx.stroke();

    // ---- Current price vertical line ----
    const cpX = toX(currentPrice);
    ctx.strokeStyle = "#5499ff44";
    ctx.lineWidth   = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(cpX, PAD.top); ctx.lineTo(cpX, PAD.top + cH); ctx.stroke();
    ctx.setLineDash([]);

    // ---- Breakeven lines ----
    const minGap = (dataMaxX - dataMinX) * 0.01;
    let lastBX = -Infinity;
    for (let i = 1; i < spots.length; i++) {
        const crossed = (pnlTheo[i-1] < 0 && pnlTheo[i] >= 0) ||
                        (pnlTheo[i-1] > 0 && pnlTheo[i] <= 0);
        if (crossed && (spots[i] - lastBX) > minGap) {
            lastBX = spots[i];
            const bx  = toX(spots[i]);
            const pct = ((spots[i] - currentPrice) / currentPrice * 100).toFixed(1);
            ctx.strokeStyle = "#888";
            ctx.lineWidth   = 1;
            ctx.setLineDash([3, 3]);
            ctx.beginPath(); ctx.moveTo(bx, PAD.top); ctx.lineTo(bx, PAD.top + cH); ctx.stroke();
            ctx.setLineDash([]);

            ctx.save();
            ctx.translate(bx - 3, PAD.top + cH * 0.4);
            ctx.rotate(-Math.PI / 2);
            ctx.fillStyle = "#aaa";
            ctx.font = "10px sans-serif";
            ctx.textAlign = "center";
            ctx.fillText(`BE ${pct}%`, 0, 0);
            ctx.restore();
        }
    }

    ctx.restore(); // end clip

    // ---- Axis labels ----
    ctx.fillStyle = "#555";
    ctx.font = "10px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("Index", W/2, H - 5);

    ctx.save();
    ctx.translate(12, H/2);
    ctx.rotate(-Math.PI/2);
    ctx.fillText("PNL", 0, 0);
    ctx.restore();

    // ---- Zoom hint (shown once when data is first loaded) ----
    if (!chartView._hintShown) {
        chartView._hintShown = true;
        ctx.fillStyle = "rgba(255,255,255,0.12)";
        ctx.font = "10px sans-serif";
        ctx.textAlign = "right";
        ctx.fillText("Scroll to zoom  •  Drag to pan", W - PAD.right - 4, PAD.top + 14);
    }
}

// ===== UTILS =====
function showError(msg) {
    const b = document.getElementById("errorBar");
    b.textContent = msg;
    b.classList.add("show");
    setTimeout(() => b.classList.remove("show"), 5000);
}

window.addEventListener("resize", scheduleChart);
