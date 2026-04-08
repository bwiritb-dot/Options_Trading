// app.js — Position Builder main application
// Architecture: observable store → reactive PNL engine → canvas chart + UI

'use strict';

// ── 1. Constants ───────────────────────────────────────────────────────────────

const API              = 'http://localhost:5000/api';
const DERIBIT_WS_URL   = 'wss://www.deribit.com/ws/api/v2';
const YEAR_MS          = 365.25 * 24 * 3600 * 1000;
const PNL_STEP         = 10;           // $10 grid step for PNL curves
const SPOT_MIN_MULT    = 0.50;         // 50% of current spot
const SPOT_MAX_MULT    = 3.00;         // 300% of current spot
const MAX_IP           = 4;            // max interest points
const TAKER_FEE        = 0.0003;       // 0.03% of underlying per contract
const MAKER_FEE        = 0;
const DEBOUNCE_RECALC  = 500;          // ms debounce for curve recalculation
const PERSIST_DEBOUNCE = 1500;         // ms debounce for localStorage save
const IP_COLORS        = ['#ff6b6b', '#ffd93d', '#6bcb77', '#4d96ff'];

// ── 2. Observable store ────────────────────────────────────────────────────────

function createStore(initial) {
  let state = initial;
  const listeners = new Set();
  return {
    get:       ()  => state,
    set:       (p) => { state = { ...state, ...p }; listeners.forEach(fn => fn(state)); },
    subscribe: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
  };
}

const store = createStore({
  // Market data (from WS, never persisted)
  instruments: new Map(),    // instrumentName → InstrumentData
  spotPrice:   2000,
  lastUpdate:  null,
  wsConnected: false,

  // User settings (persisted)
  endDateMs:       nextFridayMs(),
  ethBalance:      10,
  commissionRole:  'taker',
  milpWeights:     { theta: 40, down: 40, up: 15, commission: 10, greek: 10 },
  samplingStep:    10,

  // Portfolio (persisted)
  portfolio: [],

  // Interest points (persisted)
  interestPoints: [],          // { id, price, targetNow, targetExpiry, constraint, color }

  // Volatility calendar (persisted): dateKey("YYYY-MM-DD") → IV decimal
  volCalendar: new Map(),

  // Computed PNL curves (auto-rebuilt)
  pnlCurveToday:  [],          // { price, pnl }[]
  pnlCurveExpiry: [],          // { price, pnl }[]

  // MILP
  milpStatus:  'idle',         // 'idle'|'running'|'done'|'error'
  milpResult:  null,           // { positions, score } | null
  milpPending: null,           // pending suggested positions before Apply
});

// ── 3. PNL engine ──────────────────────────────────────────────────────────────

function getIvForDate(evalDateMs, inst) {
  const key = new Date(evalDateMs).toISOString().slice(0, 10);
  const vc  = store.get().volCalendar;
  return (vc.size > 0 && vc.has(key)) ? vc.get(key) : (inst.markIV || 0.8);
}

function calcPortfolioPnlAtPrice(spotPrice, evalDateMs, portfolio) {
  const instruments = store.get().instruments;
  let total = 0;

  for (const pos of portfolio) {
    const inst = instruments.get(pos.instrumentName);
    if (!inst) continue;
    const T = Math.max(0, (inst.expiry - evalDateMs) / YEAR_MS);
    const iv = getIvForDate(evalDateMs, inst);
    const optVal = T < 0.001
      ? BS.intrinsic(spotPrice, inst.strike, inst.type)
      : BS.bs(spotPrice, inst.strike, T, iv, 0, inst.type);
    const entryVal   = pos.direction === 1 ? pos.askAtEntry : pos.bidAtEntry;
    const pnlEth     = pos.direction * (optVal - entryVal);
    total           += pnlEth * pos.contracts * spotPrice;
  }
  // Subtract fixed commission (sunk cost, paid at entry)
  for (const pos of portfolio) total -= (pos.commissionUsd || 0);
  return total;
}

function buildPnlCurve(portfolio, evalDateMs) {
  const spot    = store.get().spotPrice;
  const spotMin = spot * SPOT_MIN_MULT;
  const spotMax = spot * SPOT_MAX_MULT;
  const curve   = [];
  for (let p = spotMin; p <= spotMax + 1; p += PNL_STEP) {
    curve.push({ price: p, pnl: calcPortfolioPnlAtPrice(p, evalDateMs, portfolio) });
  }
  return curve;
}

function interpolateFromCurve(curve, price) {
  if (!curve.length) return 0;
  if (price <= curve[0].price)                  return curve[0].pnl;
  if (price >= curve[curve.length - 1].price)   return curve[curve.length - 1].pnl;
  let lo = 0, hi = curve.length - 1;
  while (lo + 1 < hi) { const mid = (lo + hi) >> 1; if (curve[mid].price <= price) lo = mid; else hi = mid; }
  const a = curve[lo], b = curve[lo + 1];
  const t = (price - a.price) / (b.price - a.price);
  return a.pnl + t * (b.pnl - a.pnl);
}

let recalcTimer = null;
function scheduleRecalc(delay = DEBOUNCE_RECALC) {
  clearTimeout(recalcTimer);
  recalcTimer = setTimeout(recalcAll, delay);
}

function recalcAll() {
  const s   = store.get();
  const now = Date.now();
  const pnlCurveToday  = s.portfolio.length ? buildPnlCurve(s.portfolio, now)         : [];
  const pnlCurveExpiry = s.portfolio.length ? buildPnlCurve(s.portfolio, s.endDateMs) : [];
  store.set({ pnlCurveToday, pnlCurveExpiry });
  scheduleChart();
  renderPortfolio();
  renderInterestPointCards();
  renderTable();
}

// Compute Greeks for a position at current spot
function calcGreeks(pos) {
  const inst = store.get().instruments.get(pos.instrumentName);
  if (!inst) return { delta: null, vega: null, theta: null };
  const spot = store.get().spotPrice;
  const T    = Math.max(0, (inst.expiry - Date.now()) / YEAR_MS);
  const iv   = inst.markIV || 0.8;
  return {
    delta: BS.bsDelta(spot, inst.strike, T, iv, 0, inst.type) * pos.direction,
    vega:  BS.bsVega( spot, inst.strike, T, iv) * spot * pos.direction * pos.contracts, // USD
    theta: BS.bsTheta(spot, inst.strike, T, iv, 0, inst.type) * pos.direction * pos.contracts * spot, // USD/day
  };
}

// ── 4. Deribit WebSocket ───────────────────────────────────────────────────────

let ws         = null;
let wsReconnTimer = null;
let wsRpcId    = 1;
const wsSubs   = new Set(); // subscribed channels

function wsConnect() {
  if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
  ws = new WebSocket(DERIBIT_WS_URL);

  ws.onopen = () => {
    store.set({ wsConnected: true });
    renderTopBar();
    // Subscribe to ETH spot price
    wsSend({ method: 'public/subscribe', params: { channels: ['deribit_price_index.eth_usd'] } });
    // Re-subscribe to all portfolio instruments
    store.get().portfolio.forEach(p => wsSubscribeTicker(p.instrumentName));
  };

  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.method === 'subscription') {
        const { channel, data } = msg.params;
        if (channel === 'deribit_price_index.eth_usd') {
          store.set({ spotPrice: data.price, lastUpdate: Date.now() });
          renderTopBar();
          scheduleRecalc();
        } else if (channel.startsWith('ticker.')) {
          const iName = channel.split('.')[1];
          updateInstrument(iName, data);
        }
      }
    } catch (_) {}
  };

  ws.onerror  = () => {};
  ws.onclose  = () => {
    store.set({ wsConnected: false });
    renderTopBar();
    clearTimeout(wsReconnTimer);
    wsReconnTimer = setTimeout(wsConnect, 5000);
  };
}

function wsSend(payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ jsonrpc: '2.0', id: wsRpcId++, ...payload }));
}

function wsSubscribeTicker(instrumentName) {
  const channel = `ticker.${instrumentName}.100ms`;
  if (wsSubs.has(channel)) return;
  wsSubs.add(channel);
  wsSend({ method: 'public/subscribe', params: { channels: [channel] } });
}

function wsUnsubscribeTicker(instrumentName) {
  const channel = `ticker.${instrumentName}.100ms`;
  wsSubs.delete(channel);
  wsSend({ method: 'public/unsubscribe', params: { channels: [channel] } });
}

function updateInstrument(instrumentName, data) {
  const instruments = store.get().instruments;
  const existing    = instruments.get(instrumentName);
  if (!existing) return;
  instruments.set(instrumentName, {
    ...existing,
    markPrice: data.mark_price  || existing.markPrice,
    markIV:    (data.mark_iv    || existing.markIV * 100) / 100,
    bid:       data.best_bid_price || existing.bid,
    ask:       data.best_ask_price || existing.ask,
  });
  store.set({ instruments, lastUpdate: Date.now() });
  renderTopBar();
  renderTable();
  scheduleRecalc();
}

// ── 5. Chart state & utilities ─────────────────────────────────────────────────

const chart = {
  view:       null,   // { minX, maxX } or null = auto-fit
  dragIP:     -1,     // index of interest point being dragged
  dragPan:    null,   // { clientX, minX, maxX } for chart pan
  render:     null,   // last render metadata for coordinate transforms
};

let chartAF = null;
function scheduleChart() {
  if (chartAF) cancelAnimationFrame(chartAF);
  chartAF = requestAnimationFrame(drawChart);
}

function toChartX(price, render) {
  return render.PAD.left + ((price - render.minX) / (render.maxX - render.minX)) * render.cW;
}
function toChartY(pnl, render) {
  return render.PAD.top + render.cH - ((pnl - render.minY) / (render.maxY - render.minY)) * render.cH;
}
function fromCanvasX(canvasX, render) {
  return render.minX + ((canvasX - render.PAD.left) / render.cW) * (render.maxX - render.minX);
}
function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r); ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h); ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r); ctx.quadraticCurveTo(x, y, x + r, y); ctx.closePath();
}

// ── 6. Chart drawing ───────────────────────────────────────────────────────────

function drawChart() {
  const canvas = document.getElementById('pnlCanvas');
  const wrap   = canvas.parentElement;
  const ctx    = canvas.getContext('2d');
  const dpr    = window.devicePixelRatio || 1;
  const W      = wrap.clientWidth, H = wrap.clientHeight;

  canvas.width  = W * dpr; canvas.height  = H * dpr;
  canvas.style.width  = W + 'px'; canvas.style.height = H + 'px';
  ctx.scale(dpr, dpr);

  const PAD  = { top: 28, right: 70, bottom: 36, left: 58 };
  const cW   = W - PAD.left - PAD.right;
  const cH   = H - PAD.top  - PAD.bottom;

  ctx.fillStyle = '#0e0e0e';
  ctx.fillRect(0, 0, W, H);

  const s           = store.get();
  const curveToday  = s.pnlCurveToday;
  const curveExpiry = s.pnlCurveExpiry;

  if (!curveToday.length) {
    ctx.fillStyle = '#2a2a2a'; ctx.font = '13px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('Add positions to see PNL chart', W / 2, H / 2);
    chart.render = null; return;
  }

  // X range
  const spots = curveToday.map(p => p.price);
  const dataMinX = spots[0], dataMaxX = spots[spots.length - 1];
  if (!chart.view) chart.view = { minX: dataMinX, maxX: dataMaxX };
  const vMinX = chart.view.minX, vMaxX = chart.view.maxX;

  // Y range (auto-fit visible points, always includes 0)
  const visY = [];
  for (let i = 0; i < spots.length; i++) {
    if (spots[i] < vMinX || spots[i] > vMaxX) continue;
    if (curveToday[i])  visY.push(curveToday[i].pnl);
    if (curveExpiry[i]) visY.push(curveExpiry[i].pnl);
  }
  if (!visY.length) { [...curveToday, ...curveExpiry].forEach(p => visY.push(p.pnl)); }
  const rawMin = Math.min(0, ...visY), rawMax = Math.max(0, ...visY);
  const yPad   = (rawMax - rawMin) * 0.13 || 200;
  const vMinY  = rawMin - yPad, vMaxY = rawMax + yPad;

  const R = { PAD, cW, cH, W, H, minX: vMinX, maxX: vMaxX, minY: vMinY, maxY: vMaxY };
  chart.render = R;

  const toX = p  => toChartX(p, R);
  const toY = pn => toChartY(pn, R);
  const zeroY = toY(0);

  // ── Grid
  ctx.strokeStyle = '#1a1a1a'; ctx.lineWidth = 1; ctx.font = '10px sans-serif';
  const xSteps = 6, ySteps = 6;
  for (let i = 0; i <= xSteps; i++) {
    const x = PAD.left + (i / xSteps) * cW;
    ctx.beginPath(); ctx.moveTo(x, PAD.top); ctx.lineTo(x, PAD.top + cH); ctx.stroke();
    ctx.fillStyle = '#444'; ctx.textAlign = 'center';
    ctx.fillText(Math.round(vMinX + (i / xSteps) * (vMaxX - vMinX)).toLocaleString(), x, H - PAD.bottom + 13);
  }
  for (let i = 0; i <= ySteps; i++) {
    const y   = PAD.top + (i / ySteps) * cH;
    const val = vMaxY - (i / ySteps) * (vMaxY - vMinY);
    ctx.beginPath(); ctx.moveTo(PAD.left, y); ctx.lineTo(W - PAD.right, y); ctx.stroke();
    ctx.fillStyle = '#444'; ctx.textAlign = 'right';
    ctx.fillText((val >= 0 ? '' : '-') + '$' + Math.abs(val).toFixed(0), PAD.left - 5, y + 3);
  }

  // ── Zero line
  ctx.strokeStyle = '#333'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(PAD.left, zeroY); ctx.lineTo(W - PAD.right, zeroY); ctx.stroke();

  // Clip for curves and fills
  ctx.save();
  ctx.beginPath(); ctx.rect(PAD.left, PAD.top, cW, cH); ctx.clip();

  // ── Fill under today curve (green above zero, red below)
  const drawFill = (curve, aboveCol, belowCol) => {
    if (!curve.length) return;
    ctx.beginPath();
    curve.forEach((p, i) => { const x = toX(p.price), y = toY(Math.max(p.pnl, 0)); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
    ctx.lineTo(toX(curve[curve.length - 1].price), zeroY); ctx.lineTo(toX(curve[0].price), zeroY); ctx.closePath();
    ctx.fillStyle = aboveCol; ctx.fill();
    ctx.beginPath();
    curve.forEach((p, i) => { const x = toX(p.price), y = toY(Math.min(p.pnl, 0)); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
    ctx.lineTo(toX(curve[curve.length - 1].price), zeroY); ctx.lineTo(toX(curve[0].price), zeroY); ctx.closePath();
    ctx.fillStyle = belowCol; ctx.fill();
  };
  drawFill(curveToday, 'rgba(22,60,35,0.55)', 'rgba(80,18,18,0.55)');

  // ── Today curve (green)
  const drawCurve = (curve, color, width) => {
    if (!curve.length) return;
    ctx.beginPath();
    curve.forEach((p, i) => { const x = toX(p.price), y = toY(p.pnl); i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); });
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke();
  };
  drawCurve(curveToday,  '#3dbf7c', 2);
  drawCurve(curveExpiry, '#a855f7', 2);

  // ── Current spot vertical line
  const cpX = toX(s.spotPrice);
  if (cpX >= PAD.left && cpX <= PAD.left + cW) {
    ctx.strokeStyle = 'rgba(84,153,255,0.35)'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(cpX, PAD.top); ctx.lineTo(cpX, PAD.top + cH); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(84,153,255,0.7)'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('$' + Math.round(s.spotPrice).toLocaleString(), cpX, PAD.top - 6);
  }

  // ── Breakeven lines
  const minGap = (dataMaxX - dataMinX) * 0.01;
  let lastBX = -Infinity;
  for (let i = 1; i < curveToday.length; i++) {
    const cross = (curveToday[i - 1].pnl < 0 && curveToday[i].pnl >= 0) ||
                  (curveToday[i - 1].pnl > 0 && curveToday[i].pnl <= 0);
    if (cross && (curveToday[i].price - lastBX) > minGap) {
      lastBX = curveToday[i].price;
      const bx = toX(curveToday[i].price);
      if (bx < PAD.left || bx > PAD.left + cW) continue;
      ctx.strokeStyle = '#555'; ctx.lineWidth = 1; ctx.setLineDash([2, 4]);
      ctx.beginPath(); ctx.moveTo(bx, PAD.top); ctx.lineTo(bx, PAD.top + cH); ctx.stroke();
      ctx.setLineDash([]);
      ctx.save(); ctx.translate(bx - 3, PAD.top + cH * 0.35); ctx.rotate(-Math.PI / 2);
      ctx.fillStyle = '#666'; ctx.font = '9px sans-serif'; ctx.textAlign = 'center';
      ctx.fillText('BE $' + Math.round(curveToday[i].price).toLocaleString(), 0, 0);
      ctx.restore();
    }
  }

  // ── Interest point lines with chips
  s.interestPoints.forEach((ip, idx) => {
    const color = IP_COLORS[idx % IP_COLORS.length];
    const x     = toX(ip.price);
    if (x < PAD.left - 2 || x > PAD.left + cW + 2) return;

    // Vertical line
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash([]);
    ctx.globalAlpha = 0.8;
    ctx.beginPath(); ctx.moveTo(x, PAD.top); ctx.lineTo(x, PAD.top + cH); ctx.stroke();
    ctx.globalAlpha = 1;

    // Price label at top
    ctx.fillStyle = color; ctx.font = '10px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('$' + Math.round(ip.price).toLocaleString(), x, PAD.top - 6);

    ctx.font = '10px sans-serif';

    // "Now" chip (green)
    const pnlNow = interpolateFromCurve(curveToday, ip.price);
    const nowLabel = (pnlNow >= 0 ? '+' : '') + '$' + Math.round(pnlNow);
    const nowY = Math.max(PAD.top + 16, Math.min(PAD.top + cH - 30, toY(pnlNow)));
    const nlW = Math.max(ctx.measureText('Now ' + nowLabel).width + 10, 60);
    ctx.fillStyle = 'rgba(22,60,35,0.92)';
    roundRect(ctx, x - nlW / 2, nowY - 9, nlW, 17, 3); ctx.fill();
    ctx.strokeStyle = '#3dbf7c'; ctx.lineWidth = 0.5; ctx.stroke();
    ctx.fillStyle = '#3dbf7c'; ctx.textAlign = 'center';
    ctx.fillText('Now ' + nowLabel, x, nowY + 3);

    // "Exp" chip (purple)
    const pnlExp = interpolateFromCurve(curveExpiry, ip.price);
    const expLabel = (pnlExp >= 0 ? '+' : '') + '$' + Math.round(pnlExp);
    const expY = Math.max(PAD.top + 35, Math.min(PAD.top + cH - 10, toY(pnlExp)));
    const elW = Math.max(ctx.measureText('Exp ' + expLabel).width + 10, 60);
    const chipY = Math.abs(expY - nowY) < 20 ? nowY + 22 : expY;
    ctx.fillStyle = 'rgba(40,15,60,0.92)';
    roundRect(ctx, x - elW / 2, chipY - 9, elW, 17, 3); ctx.fill();
    ctx.strokeStyle = '#a855f7'; ctx.lineWidth = 0.5; ctx.stroke();
    ctx.fillStyle = '#a855f7'; ctx.textAlign = 'center';
    ctx.fillText('Exp ' + expLabel, x, chipY + 3);
  });

  ctx.restore(); // end clip

  // ── Legend
  ctx.font = '10px sans-serif';
  const legendX = W - PAD.right - 4, legendY = PAD.top + 12;
  ctx.fillStyle = '#3dbf7c'; ctx.textAlign = 'right'; ctx.fillText('— Today', legendX, legendY);
  ctx.fillStyle = '#a855f7'; ctx.fillText('— Expiry', legendX, legendY + 14);
}

// ── 7. Overlay canvas (crosshair + drag cursor) ────────────────────────────────

function setupOverlay() {
  const overlay = document.getElementById('overlayCanvas');
  const main    = document.getElementById('pnlCanvas');
  const wrap    = main.parentElement;

  const syncSize = () => {
    const dpr = window.devicePixelRatio || 1;
    const W = wrap.clientWidth, H = wrap.clientHeight;
    overlay.width  = W * dpr; overlay.height  = H * dpr;
    overlay.style.width  = W + 'px'; overlay.style.height = H + 'px';
  };

  main.addEventListener('mousemove', (e) => {
    syncSize();
    const dpr = window.devicePixelRatio || 1;
    const ctx = overlay.getContext('2d');
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    if (!chart.render) return;

    const R    = chart.render;
    const rect = main.getBoundingClientRect();
    const mx   = e.clientX - rect.left, my = e.clientY - rect.top;
    if (mx < R.PAD.left || mx > R.PAD.left + R.cW) return;

    const dataX = fromCanvasX(mx, R);
    const curve = store.get().pnlCurveToday;
    // Find nearest curve point
    let ni = 0, nd = Infinity;
    for (let i = 0; i < curve.length; i++) { const d = Math.abs(curve[i].price - dataX); if (d < nd) { nd = d; ni = i; } }

    const cx  = toChartX(curve[ni].price, R);
    const gyT = toChartY(curve[ni].pnl, R);
    const gyE = toChartY((store.get().pnlCurveExpiry[ni] || { pnl: 0 }).pnl, R);

    ctx.save(); ctx.scale(dpr, dpr);

    // Vertical line
    ctx.strokeStyle = 'rgba(255,255,255,0.18)'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(cx, R.PAD.top); ctx.lineTo(cx, R.PAD.top + R.cH); ctx.stroke();
    ctx.setLineDash([]);

    // Dots on curves
    if (gyT >= R.PAD.top && gyT <= R.PAD.top + R.cH) {
      ctx.fillStyle = '#3dbf7c'; ctx.beginPath(); ctx.arc(cx, gyT, 4, 0, Math.PI * 2); ctx.fill();
    }
    if (gyE >= R.PAD.top && gyE <= R.PAD.top + R.cH) {
      ctx.fillStyle = '#a855f7'; ctx.beginPath(); ctx.arc(cx, gyE, 4, 0, Math.PI * 2); ctx.fill();
    }

    // Price badge top
    ctx.font = '10px sans-serif';
    const pLabel = '$' + Math.round(curve[ni].price).toLocaleString();
    const pLW    = ctx.measureText(pLabel).width + 10;
    const pbX    = Math.max(R.PAD.left, Math.min(R.PAD.left + R.cW - pLW, cx - pLW / 2));
    ctx.fillStyle = 'rgba(40,40,40,0.92)'; roundRect(ctx, pbX, R.PAD.top - 18, pLW, 15, 3); ctx.fill();
    ctx.fillStyle = '#ddd'; ctx.textAlign = 'center'; ctx.fillText(pLabel, pbX + pLW / 2, R.PAD.top - 7);

    // Right-axis PNL badges
    const drawBadge = (y, val, col, bgCol) => {
      if (y < R.PAD.top || y > R.PAD.top + R.cH) return;
      const label = (val >= 0 ? '+' : '') + '$' + Math.round(Math.abs(val));
      const lW    = Math.max(ctx.measureText(label).width + 10, 42);
      const lX    = R.PAD.left + R.cW + 3;
      const lY    = Math.max(R.PAD.top + 8, Math.min(R.PAD.top + R.cH - 8, y));
      ctx.fillStyle = bgCol; roundRect(ctx, lX, lY - 8, lW, 16, 3); ctx.fill();
      ctx.strokeStyle = col; ctx.lineWidth = 0.5; ctx.stroke();
      ctx.fillStyle = col; ctx.textAlign = 'left'; ctx.fillText(label, lX + 5, lY + 4);
    };
    drawBadge(gyT, curve[ni].pnl,                                   '#3dbf7c', 'rgba(22,50,35,0.95)');
    drawBadge(gyE, (store.get().pnlCurveExpiry[ni] || {pnl:0}).pnl, '#a855f7', 'rgba(30,15,50,0.95)');

    ctx.restore();
  });

  main.addEventListener('mouseleave', () => {
    const ctx = overlay.getContext('2d');
    ctx.clearRect(0, 0, overlay.width, overlay.height);
  });
}

// ── 8. Chart interaction (zoom, pan, interest points) ─────────────────────────

function setupChartInteraction() {
  const canvas = document.getElementById('pnlCanvas');

  // Zoom
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (!chart.render || !chart.view) return;
    const rect  = canvas.getBoundingClientRect();
    const mx    = e.clientX - rect.left;
    const R     = chart.render;
    if (mx < R.PAD.left || mx > R.PAD.left + R.cW) return;
    const frac  = (mx - R.PAD.left) / R.cW;
    const { minX, maxX } = chart.view;
    const dataX = minX + frac * (maxX - minX);
    const factor = e.deltaY > 0 ? 1.3 : 0.77;
    const nr = (maxX - minX) * factor;
    chart.view = { minX: dataX - frac * nr, maxX: dataX + (1 - frac) * nr };
    scheduleChart();
  }, { passive: false });

  // Pan & interest point drag
  canvas.addEventListener('mousedown', (e) => {
    if (!chart.render) return;
    const rect  = canvas.getBoundingClientRect();
    const mx    = e.clientX - rect.left;
    const R     = chart.render;
    if (mx < R.PAD.left || mx > R.PAD.left + R.cW) return;

    const price = fromCanvasX(mx, R);
    const s     = store.get();

    // Check if clicking near an interest point line (within 10px)
    let nearIdx = -1, nearDist = 10;
    s.interestPoints.forEach((ip, i) => {
      const ipX = toChartX(ip.price, R);
      const d   = Math.abs(ipX - mx);
      if (d < nearDist) { nearDist = d; nearIdx = i; }
    });

    if (nearIdx >= 0) {
      chart.dragIP = nearIdx;
      canvas.style.cursor = 'ew-resize';
    } else if (chart.view) {
      chart.dragPan = { clientX: e.clientX, minX: chart.view.minX, maxX: chart.view.maxX };
      canvas.style.cursor = 'grabbing';
    }
  });

  canvas.addEventListener('mousemove', (e) => {
    if (!chart.render) return;
    const rect = canvas.getBoundingClientRect();
    const mx   = e.clientX - rect.left;
    const R    = chart.render;

    if (chart.dragIP >= 0) {
      const price = Math.round(fromCanvasX(mx, R) / PNL_STEP) * PNL_STEP;
      const ips   = store.get().interestPoints.map((p, i) => i === chart.dragIP ? { ...p, price } : p);
      store.set({ interestPoints: ips });
      scheduleChart();
      renderInterestPointCards();
      schedulePersist();
    } else if (chart.dragPan && chart.view) {
      const dx    = e.clientX - chart.dragPan.clientX;
      const range = chart.dragPan.maxX - chart.dragPan.minX;
      const dataDx = (dx / R.cW) * range;
      chart.view = { minX: chart.dragPan.minX - dataDx, maxX: chart.dragPan.maxX - dataDx };
      scheduleChart();
    }
  });

  canvas.addEventListener('mouseup', () => {
    chart.dragIP  = -1;
    chart.dragPan = null;
    canvas.style.cursor = 'crosshair';
  });

  canvas.addEventListener('mouseleave', () => {
    chart.dragIP  = -1;
    chart.dragPan = null;
    canvas.style.cursor = 'crosshair';
  });

  // Single click: add interest point
  let lastClick = 0;
  canvas.addEventListener('click', (e) => {
    if (!chart.render) return;
    const now  = Date.now();
    const dbl  = (now - lastClick) < 280;
    lastClick  = now;
    const rect = canvas.getBoundingClientRect();
    const mx   = e.clientX - rect.left;
    const R    = chart.render;
    if (mx < R.PAD.left || mx > R.PAD.left + R.cW) return;

    const price = Math.round(fromCanvasX(mx, R) / PNL_STEP) * PNL_STEP;
    const s     = store.get();

    // Check if near existing IP
    let nearIdx = -1, nearDist = 12;
    s.interestPoints.forEach((ip, i) => {
      const d = Math.abs(toChartX(ip.price, R) - mx);
      if (d < nearDist) { nearDist = d; nearIdx = i; }
    });

    if (dbl && nearIdx >= 0) {
      // Double-click: delete IP
      removeInterestPoint(nearIdx);
    } else if (!dbl && nearIdx < 0 && s.interestPoints.length < MAX_IP) {
      // Single click on empty area: add IP
      addInterestPoint(price);
    }
  });

  // Recenter button
  document.getElementById('recenterBtn') && document.getElementById('recenterBtn').addEventListener('click', () => {
    chart.view = null; scheduleChart();
  });
}

// ── 9. Interest points management ─────────────────────────────────────────────

function addInterestPoint(price) {
  const s   = store.get();
  if (s.interestPoints.length >= MAX_IP) return;
  const idx = s.interestPoints.length;
  const ip  = { id: Date.now(), price, targetNow: 0, targetExpiry: 0, constraint: 'soft', color: IP_COLORS[idx] };
  store.set({ interestPoints: [...s.interestPoints, ip] });
  scheduleChart();
  renderInterestPointCards();
  schedulePersist();
}

function removeInterestPoint(idx) {
  const ips = store.get().interestPoints.filter((_, i) => i !== idx);
  store.set({ interestPoints: ips });
  scheduleChart();
  renderInterestPointCards();
  schedulePersist();
}

function updateIP(idx, patch) {
  const ips = store.get().interestPoints.map((p, i) => i === idx ? { ...p, ...patch } : p);
  store.set({ interestPoints: ips });
  renderInterestPointCards();
  schedulePersist();
}

// ── 10. UI: Top bar ──────────────────────────────────────────────────────────

function renderTopBar() {
  const s = store.get();
  const el = document.getElementById('spotDisplay');
  if (el) el.textContent = s.spotPrice ? '$' + s.spotPrice.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—';

  const dot = document.getElementById('wsDot');
  if (dot) {
    dot.className = 'tb-dot' + (s.wsConnected ? '' : ' dead');
    // Mark stale if no update > 30s
    if (s.lastUpdate && Date.now() - s.lastUpdate > 30000) dot.className = 'tb-dot dead';
  }

  const margin = s.spotPrice * s.ethBalance;
  const mEl    = document.getElementById('marginDisplay');
  if (mEl) mEl.textContent = '$' + Math.round(margin).toLocaleString();

  if (s.lastUpdate) {
    const secs = Math.round((Date.now() - s.lastUpdate) / 1000);
    const lu   = document.getElementById('lastUpdate');
    if (lu) lu.textContent = secs < 60 ? `${secs}s ago` : `${Math.round(secs / 60)}m ago`;
  }
}

function populateEndDateSelect() {
  const sel = document.getElementById('endDateSelect');
  if (!sel) return;
  const fridays = upcomingFridays(10);
  sel.innerHTML = '';
  fridays.forEach(d => {
    const opt  = document.createElement('option');
    opt.value  = d.getTime();
    opt.textContent = d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: '2-digit' });
    if (d.getTime() === store.get().endDateMs) opt.selected = true;
    sel.appendChild(opt);
  });
  // If stored endDate is not in list, prepend it
  if (!sel.value || Math.abs(+sel.value - store.get().endDateMs) > 60000) {
    const opt = document.createElement('option');
    opt.value = store.get().endDateMs;
    const d   = new Date(store.get().endDateMs);
    opt.textContent = d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: '2-digit' });
    opt.selected = true;
    sel.prepend(opt);
  }
}

// ── 11. UI: Portfolio section ─────────────────────────────────────────────────

function renderPortfolio() {
  const s    = store.get();
  const wrap = document.getElementById('portfolioRows');
  const sumEl = document.getElementById('portfolioSummary');
  if (!wrap) return;

  if (!s.portfolio.length) {
    wrap.innerHTML = '<div style="color:#444; font-size:11px; padding:4px 0;">No positions yet.</div>';
    if (sumEl) sumEl.style.display = 'none';
    return;
  }

  let netTheta = 0, netDelta = 0, netVega = 0, netComm = 0;

  wrap.innerHTML = s.portfolio.map((pos, idx) => {
    const inst = s.instruments.get(pos.instrumentName);
    const gr   = calcGreeks(pos);
    if (gr.theta) netTheta += gr.theta;
    if (gr.delta) netDelta += gr.delta * pos.contracts;
    if (gr.vega)  netVega  += gr.vega;
    netComm += pos.commissionUsd || 0;

    const markVal = inst ? inst.markPrice : null;
    const thetaStr = gr.theta != null ? ((gr.theta >= 0 ? '+' : '') + '$' + Math.abs(gr.theta).toFixed(2)) : '—';
    return `<div class="pos-row" title="${pos.instrumentName}">
      <span class="pos-dir ${pos.direction === 1 ? 'long' : 'short'}">${pos.direction === 1 ? '+' : '−'}${pos.contracts}</span>
      <span class="pos-name">${pos.instrumentName.replace('ETH-', '')}</span>
      <span class="pos-mark">${markVal != null ? markVal.toFixed(4) : '—'}</span>
      <span class="pos-theta ${gr.theta != null && gr.theta >= 0 ? 'positive' : 'negative'}">${thetaStr}</span>
      <span class="pos-del" onclick="removePosition(${idx})" title="Remove">✕</span>
    </div>`;
  }).join('');

  if (sumEl) {
    sumEl.style.display = 'flex';
    document.getElementById('sumTheta').textContent = (netTheta >= 0 ? '+' : '') + '$' + Math.abs(netTheta).toFixed(2) + '/day';
    document.getElementById('sumTheta').className = netTheta >= 0 ? 'positive' : 'negative';
    document.getElementById('sumDelta').textContent = netDelta.toFixed(4);
    document.getElementById('sumVega').textContent  = '$' + netVega.toFixed(2);
    document.getElementById('sumComm').textContent  = '-$' + netComm.toFixed(2);
    document.getElementById('sumComm').className    = 'negative';
  }
}

// ── 12. UI: Interest point cards ──────────────────────────────────────────────

function renderInterestPointCards() {
  const s    = store.get();
  const wrap = document.getElementById('interestPointCards');
  const hint = document.getElementById('ipAddHint');
  if (!wrap) return;

  if (hint) hint.style.display = s.interestPoints.length >= MAX_IP ? 'none' : 'block';

  wrap.innerHTML = s.interestPoints.map((ip, idx) => {
    const color    = IP_COLORS[idx % IP_COLORS.length];
    const pnlNow   = interpolateFromCurve(s.pnlCurveToday,  ip.price);
    const pnlExp   = interpolateFromCurve(s.pnlCurveExpiry, ip.price);
    const fmtPnl   = v => (v >= 0 ? '+' : '') + '$' + Math.round(Math.abs(v));

    return `<div class="ip-card" style="border-left-color:${color}">
      <div class="ip-card-header">
        <span style="color:${color}; font-size:12px;">●</span>
        <span style="color:#888; font-size:10px;">$</span>
        <input class="ip-price-input" type="number" value="${ip.price}" step="${PNL_STEP}"
          oninput="updateIP(${idx},{price:+this.value}); scheduleChart();"
          onchange="scheduleRecalc(0);">
        <span class="ip-delete" onclick="removeInterestPoint(${idx})">✕</span>
      </div>
      <div class="ip-grid">
        <label>Now PNL</label>  <span class="ip-val ${pnlNow>=0?'positive':'negative'}">${fmtPnl(pnlNow)}</span>
        <label>Expiry PNL</label><span class="ip-val ${pnlExp>=0?'positive':'negative'}">${fmtPnl(pnlExp)}</span>
        <label>Target (now)</label>
        <div class="ip-target-row">
          <input class="ip-target-input" type="number" value="${ip.targetNow}"
            oninput="updateIP(${idx},{targetNow:+this.value})">
        </div>
        <label>Target (expiry)</label>
        <div class="ip-target-row">
          <input class="ip-target-input" type="number" value="${ip.targetExpiry}"
            oninput="updateIP(${idx},{targetExpiry:+this.value})">
        </div>
      </div>
      <div class="ip-constraint-row">
        <button class="ip-constraint-btn ${ip.constraint==='soft'?'active':''}"
          onclick="updateIP(${idx},{constraint:'soft'})">Soft</button>
        <button class="ip-constraint-btn ${ip.constraint==='hard'?'active':''}"
          onclick="updateIP(${idx},{constraint:'hard'})">Hard</button>
      </div>
    </div>`;
  }).join('');
}

// ── 13. UI: Bottom table ──────────────────────────────────────────────────────

function renderTable() {
  const s    = store.get();
  const tbody = document.getElementById('posTable');
  if (!tbody) return;

  if (!s.portfolio.length) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#333;padding:14px;">No positions</td></tr>';
    return;
  }

  tbody.innerHTML = s.portfolio.map((pos, idx) => {
    const inst = s.instruments.get(pos.instrumentName);
    const gr   = calcGreeks(pos);
    const entryVal   = pos.direction === 1 ? pos.askAtEntry : pos.bidAtEntry;
    const markVal    = inst ? inst.markPrice : entryVal;
    const pnlEth     = pos.direction * (markVal - entryVal);
    const pnlUsd     = pnlEth * pos.contracts * s.spotPrice - (pos.commissionUsd || 0);
    const pnlCls     = pnlUsd > 0 ? 'positive' : pnlUsd < 0 ? 'negative' : 'neutral';
    const ivPct      = inst ? (inst.markIV * 100).toFixed(1) + '%' : '—';
    const fmt3       = v => v != null ? v.toFixed(3) : '—';
    const fmtTheta   = v => v != null ? (v >= 0 ? '+' : '') + '$' + Math.abs(v).toFixed(2) : '—';

    return `<tr>
      <td title="${pos.instrumentName}">${pos.instrumentName.replace('ETH-', '')}</td>
      <td class="${pos.direction===1?'positive':'negative'}">${pos.direction===1?'+':'-'}${pos.contracts}</td>
      <td class="neutral">${entryVal.toFixed(4)}</td>
      <td class="neutral">${markVal.toFixed(4)}</td>
      <td class="${pnlCls}">${pnlUsd>=0?'+':''}$${Math.abs(pnlUsd).toFixed(2)}</td>
      <td class="neutral">${ivPct}</td>
      <td class="neutral">${fmt3(gr.delta)}</td>
      <td class="neutral">${fmt3(gr.vega)}</td>
      <td class="${gr.theta!=null&&gr.theta>=0?'positive':'negative'}">${fmtTheta(gr.theta)}</td>
      <td><button class="rm-btn" onclick="removePosition(${idx})">✕</button></td>
    </tr>`;
  }).join('');
}

// ── 14. UI: Instrument list (right panel) ─────────────────────────────────────

let allInstruments  = [];   // raw from backend
let selectedInstName = null;
let activeExp = 'ALL';

function renderInstList() {
  const search    = (document.getElementById('searchInp')?.value || '').toLowerCase();
  const container = document.getElementById('instList');
  if (!container) return;

  const filtered = allInstruments.filter(i => {
    const n = i.instrument_name;
    return n.toLowerCase().includes(search) && (activeExp === 'ALL' || n.includes(activeExp));
  });

  if (!filtered.length) {
    container.innerHTML = '<div style="padding:8px;color:#444;font-size:11px;">No results</div>';
    return;
  }

  container.innerHTML = filtered.slice(0, 200).map(i =>
    `<div class="inst-item ${i.instrument_name === selectedInstName ? 'sel' : ''}"
      data-name="${i.instrument_name}">${i.instrument_name}</div>`
  ).join('');

  container.querySelectorAll('.inst-item').forEach(el => {
    el.addEventListener('click', () => {
      selectedInstName = el.dataset.name;
      renderInstList();
      renderInstDetail();
    });
  });
}

function renderInstDetail() {
  const el = document.getElementById('instDetail');
  if (!el) return;
  if (!selectedInstName) { el.style.display = 'none'; return; }

  const inst = store.get().instruments.get(selectedInstName);
  if (!inst) { el.style.display = 'none'; return; }

  const T     = Math.max(0, (inst.expiry - Date.now()) / YEAR_MS);
  const iv    = inst.markIV || 0.8;
  const spot  = store.get().spotPrice;
  const delta = BS.bsDelta(spot, inst.strike, T, iv, 0, inst.type);
  const theta = BS.bsTheta(spot, inst.strike, T, iv, 0, inst.type) * spot;

  el.style.display = 'block';
  el.innerHTML = `
    <div class="inst-detail-row"><span>Mark</span><span>${inst.markPrice?.toFixed(4) || '—'}</span></div>
    <div class="inst-detail-row"><span>Bid/Ask</span><span>${(inst.bid||0).toFixed(4)} / ${(inst.ask||0).toFixed(4)}</span></div>
    <div class="inst-detail-row"><span>IV</span><span>${((inst.markIV||0)*100).toFixed(1)}%</span></div>
    <div class="inst-detail-row"><span>Δ</span><span>${delta.toFixed(4)}</span></div>
    <div class="inst-detail-row"><span>Θ/day</span><span>${theta.toFixed(3)}</span></div>
  `;
}

function buildExpTabs() {
  const expiries = new Set();
  allInstruments.forEach(i => { const p = i.instrument_name.split('-'); if (p[1]) expiries.add(p[1]); });
  const tabs = document.getElementById('expTabs');
  if (!tabs) return;

  tabs.innerHTML = '<div class="exp-tab active" data-exp="ALL">All</div>';
  const sorted = Array.from(expiries).sort((a, b) => parseExpiryStr(a) - parseExpiryStr(b));
  sorted.forEach(exp => {
    const d = parseExpiryStr(exp);
    const div = document.createElement('div');
    div.className = 'exp-tab';
    div.dataset.exp = exp;
    div.textContent = d ? new Date(d).toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) : exp;
    tabs.appendChild(div);
  });
  tabs.querySelectorAll('.exp-tab').forEach(t => t.addEventListener('click', () => {
    tabs.querySelectorAll('.exp-tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    activeExp = t.dataset.exp;
    renderInstList();
  }));
}

// ── 15. Position management ───────────────────────────────────────────────────

async function addPosition(direction) {
  if (!selectedInstName) { showError('Select an instrument first'); return; }
  const contracts = parseInt(document.getElementById('amountInp')?.value) || 1;
  if (contracts <= 0) { showError('Enter a valid contract count'); return; }

  const s    = store.get();
  let inst   = s.instruments.get(selectedInstName);

  // Try fetching fresh ticker if not in store
  if (!inst || !inst.ask) {
    try {
      const r = await fetch(`${API}/ticker?instrument=${selectedInstName}`);
      const d = await r.json();
      const parsed = parseInstrumentName(selectedInstName);
      inst = { ...parsed, markPrice: d.mark_price||0, markIV: (d.mark_iv||80)/100, bid: d.best_bid||0, ask: d.best_ask||0 };
      const instruments = s.instruments;
      instruments.set(selectedInstName, inst);
      store.set({ instruments });
    } catch (_) {
      showError('Could not fetch ticker data'); return;
    }
  }

  const commissionUsd = (s.commissionRole === 'taker' ? TAKER_FEE : 0) * s.spotPrice * contracts;
  const pos = {
    instrumentName: selectedInstName,
    contracts,
    direction,
    askAtEntry:    inst.ask || inst.markPrice || 0,
    bidAtEntry:    inst.bid || inst.markPrice || 0,
    commissionUsd,
    addedAt:       Date.now(),
  };

  store.set({ portfolio: [...s.portfolio, pos] });
  wsSubscribeTicker(selectedInstName);
  scheduleRecalc(0);
  schedulePersist();
}

function removePosition(idx) {
  const s        = store.get();
  const removed  = s.portfolio[idx];
  const newPort  = s.portfolio.filter((_, i) => i !== idx);
  store.set({ portfolio: newPort });

  // Unsubscribe from WS if no longer needed
  const stillNeeded = newPort.some(p => p.instrumentName === removed.instrumentName);
  if (!stillNeeded) wsUnsubscribeTicker(removed.instrumentName);

  scheduleRecalc(0);
  schedulePersist();
}

// ── 16. Vol Calendar modal ────────────────────────────────────────────────────

let volDraft = new Map(); // working copy before Apply

function openVolCalendar() {
  const s        = store.get();
  const endDate  = new Date(s.endDateMs);
  const today    = new Date(); today.setHours(0, 0, 0, 0);
  const days     = [];
  for (let d = new Date(today); d <= endDate; d.setDate(d.getDate() + 1)) {
    days.push(new Date(d));
  }

  volDraft = new Map(s.volCalendar);

  const rowsEl = document.getElementById('volRows');
  if (!rowsEl) return;

  rowsEl.innerHTML = days.map(d => {
    const key    = d.toISOString().slice(0, 10);
    const ivPct  = volDraft.has(key) ? Math.round(volDraft.get(key) * 100) : 80;
    const label  = d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' }) +
      (d.getTime() === today.getTime() ? ' (today)' :
       d.getTime() === endDate.getTime() ? ' (end)' : '');
    return `<div class="vol-row">
      <span class="vol-date-label">${label}</span>
      <input class="vol-slider" type="range" min="10" max="500" value="${ivPct}"
        data-key="${key}" oninput="volDraftUpdate(this)">
      <span class="vol-pct" id="vp_${key}">${ivPct}%</span>
    </div>`;
  }).join('');

  document.getElementById('volModal').classList.add('open');
}

function volDraftUpdate(slider) {
  const key  = slider.dataset.key;
  const pct  = parseInt(slider.value);
  volDraft.set(key, pct / 100);
  const el = document.getElementById(`vp_${key}`);
  if (el) el.textContent = pct + '%';
}

function applyVolCalendar() {
  store.set({ volCalendar: new Map(volDraft) });
  closeVolModal();
  scheduleRecalc(0);
  schedulePersist();
}

function resetVolCalendar() {
  volDraft = new Map();
  // Reset all sliders to 80%
  document.querySelectorAll('.vol-slider').forEach(sl => {
    sl.value = '80';
    const el = document.getElementById(`vp_${sl.dataset.key}`);
    if (el) el.textContent = '80%';
    volDraft.set(sl.dataset.key, 0.8);
  });
}

function closeVolModal() {
  document.getElementById('volModal').classList.remove('open');
}

// ── 17. MILP optimizer ───────────────────────────────────────────────────────

let milpWorker = null;

function ensureMilpWorker() {
  if (milpWorker) return milpWorker;
  const src = document.getElementById('milp-worker-src').textContent;
  const blob = new Blob([src], { type: 'application/javascript' });
  milpWorker = new Worker(URL.createObjectURL(blob));
  milpWorker.onmessage = (e) => {
    const { type, payload } = e.data;
    if (type === 'PROGRESS') {
      document.getElementById('milpBar').style.width = payload.pct + '%';
      document.getElementById('milpStatusMsg').textContent = payload.msg;
    } else if (type === 'RESULT') {
      onMilpResult(payload);
    } else if (type === 'ERROR') {
      onMilpError(payload.message);
    }
  };
  milpWorker.onerror = (e) => onMilpError(e.message);
  return milpWorker;
}

async function runMilp() {
  const s = store.get();
  if (s.milpStatus === 'running') return;

  // Fetch fresh market data
  let marketData = [];
  try {
    const r = await fetch(`${API}/market-data`);
    const d = await r.json();
    marketData = d.data || [];
  } catch (_) {
    showError('Could not fetch market data for optimizer');
    return;
  }

  // Filter universe
  const now         = Date.now();
  const endMs       = s.endDateMs;
  const filteredInst = marketData
    .filter(md => {
      const name  = md.instrument_name;
      if (!name.startsWith('ETH-') || !name.endsWith('-C') && !name.endsWith('-P')) return false;
      const bid   = md.bid_price || 0, ask = md.ask_price || 0;
      if (!bid || !ask) return false;
      const spread = (ask - bid) / ((ask + bid) / 2);
      if (spread > 0.5) return false;
      const parsed = parseInstrumentName(name);
      if (!parsed) return false;
      if (parsed.expiry <= now) return false;
      if (parsed.expiry > endMs + 90 * 24 * 3600 * 1000) return false;
      return true;
    })
    .map(md => {
      const parsed = parseInstrumentName(md.instrument_name);
      return {
        instrumentName: md.instrument_name,
        strike:   parsed.strike,
        type:     parsed.type,
        expiry:   parsed.expiry,
        markIV:   (md.mark_iv || 80) / 100,
        bid:      md.bid_price || 0,
        ask:      md.ask_price || 0,
      };
    });

  if (!filteredInst.length) { showError('No liquid instruments available'); return; }

  // Build sampling grid
  const step = s.samplingStep || 10;
  let gridMin, gridMax;
  if (s.interestPoints.length >= 2) {
    gridMin = Math.min(...s.interestPoints.map(p => p.price));
    gridMax = Math.max(...s.interestPoints.map(p => p.price));
  } else {
    gridMin = s.spotPrice * 0.7;
    gridMax = s.spotPrice * 1.3;
  }
  const grid = [];
  for (let p = gridMin; p <= gridMax + 1; p += step) grid.push(p);

  // Pre-compute existing portfolio PNL at each grid point
  const existingPnlByGrid = grid.map(p => calcPortfolioPnlAtPrice(p, endMs, s.portfolio));

  // Margin
  const marginUsd = s.spotPrice * s.ethBalance * 0.9; // 90% of balance as margin

  store.set({ milpStatus: 'running', milpResult: null });
  document.getElementById('milpProgress').style.display = 'block';
  document.getElementById('milpResult').style.display   = 'none';
  document.getElementById('runMilpBtn').disabled        = true;

  const worker = ensureMilpWorker();
  worker.postMessage({
    type: 'OPTIMIZE',
    payload: {
      instruments:       filteredInst,
      existingPnlByGrid,
      interestPoints:    s.interestPoints,
      grid,
      endDate:           endMs,
      spotPrice:         s.spotPrice,
      marginUsd,
      commissionRole:    s.commissionRole,
      weights:           s.milpWeights,
      volCalendar:       Object.fromEntries(s.volCalendar),
    }
  });
}

function onMilpResult(payload) {
  store.set({ milpStatus: 'done', milpResult: payload });
  document.getElementById('milpProgress').style.display = 'none';
  document.getElementById('runMilpBtn').disabled        = false;

  const card = document.getElementById('milpResult');
  const rows = document.getElementById('milpPositionRows');
  const scoreEl = document.getElementById('milpScoreLabel');
  if (!card || !rows || !scoreEl) return;

  scoreEl.textContent = `Solution found — score: ${payload.score?.toFixed(3) || '?'}`;
  rows.innerHTML = payload.positions.map(p =>
    `<div class="milp-result-row">
      <span class="${p.direction===1?'positive':'negative'}">${p.direction===1?'+':'-'}${p.contracts}</span>
      <span style="color:#ccc; font-size:11px;">${p.instrumentName.replace('ETH-','')}</span>
    </div>`
  ).join('');
  card.style.display = 'block';
}

function onMilpError(msg) {
  store.set({ milpStatus: 'error' });
  document.getElementById('milpProgress').style.display = 'none';
  document.getElementById('runMilpBtn').disabled        = false;
  showError('Optimizer: ' + msg);
}

function applyMilpResult() {
  const result = store.get().milpResult;
  if (!result) return;
  const s = store.get();
  const newPositions = result.positions.map(p => {
    const inst = s.instruments.get(p.instrumentName) || { ask: 0, bid: 0 };
    return {
      instrumentName: p.instrumentName,
      contracts:      p.contracts,
      direction:      p.direction,
      askAtEntry:     inst.ask || 0,
      bidAtEntry:     inst.bid || 0,
      commissionUsd:  (s.commissionRole === 'taker' ? TAKER_FEE : 0) * s.spotPrice * p.contracts,
      addedAt:        Date.now(),
    };
  });
  store.set({ portfolio: [...s.portfolio, ...newPositions], milpResult: null });
  document.getElementById('milpResult').style.display = 'none';
  newPositions.forEach(p => wsSubscribeTicker(p.instrumentName));
  scheduleRecalc(0);
  schedulePersist();
}

function discardMilpResult() {
  store.set({ milpResult: null });
  document.getElementById('milpResult').style.display = 'none';
}

// ── 18. Persistence (localStorage) ───────────────────────────────────────────

const PERSIST_KEYS = ['portfolio', 'interestPoints', 'endDateMs', 'ethBalance', 'commissionRole', 'milpWeights', 'samplingStep', 'volCalendar'];

let persistTimer = null;
function schedulePersist() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(saveState, PERSIST_DEBOUNCE);
}

function saveState() {
  try {
    const s = store.get();
    const data = {};
    PERSIST_KEYS.forEach(k => {
      data[k] = k === 'volCalendar' ? Array.from(s[k].entries()) : s[k];
    });
    localStorage.setItem('pb_state', JSON.stringify(data));
  } catch (_) {}
}

function loadState() {
  try {
    const raw = localStorage.getItem('pb_state');
    if (!raw) return;
    const data = JSON.parse(raw);
    const patch = {};
    PERSIST_KEYS.forEach(k => {
      if (data[k] !== undefined) {
        patch[k] = k === 'volCalendar' ? new Map(data[k]) : data[k];
      }
    });
    store.set(patch);
  } catch (_) {}
}

// ── 19. Helpers ───────────────────────────────────────────────────────────────

function parseExpiryStr(expStr) {
  try {
    const m = expStr.match(/(\d{1,2})([A-Z]{3})(\d{2})/);
    if (!m) return 0;
    return new Date(`${m[2]} ${m[1]} 20${m[3]} 08:00:00 UTC`).getTime();
  } catch (_) { return 0; }
}

function parseInstrumentName(name) {
  try {
    const p = name.split('-');
    if (p.length < 4) return null;
    return {
      instrumentName: name,
      strike:  parseFloat(p[2]),
      type:    p[3],   // 'C' or 'P'
      expiry:  parseExpiryStr(p[1]),
    };
  } catch (_) { return null; }
}

function nextFridayMs() {
  const d = new Date();
  d.setHours(8, 0, 0, 0);
  const dow = d.getDay(); // 0=Sun, 5=Fri
  const days = dow <= 5 ? (5 - dow || 7) : 6;
  d.setDate(d.getDate() + days);
  return d.getTime();
}

function upcomingFridays(n) {
  const result = [];
  const d = new Date();
  d.setHours(8, 0, 0, 0);
  const dow = d.getDay();
  d.setDate(d.getDate() + ((5 - dow + 7) % 7 || 7));
  for (let i = 0; i < n; i++) {
    result.push(new Date(d));
    d.setDate(d.getDate() + 7);
  }
  return result;
}

function showError(msg) {
  const el = document.getElementById('errorBar');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 5000);
}

function toggleSection(id) {
  const body   = document.getElementById(id + 'Body');
  const toggle = document.getElementById(id + 'Toggle');
  if (!body || !toggle) return;
  const collapsed = body.classList.toggle('collapsed');
  toggle.textContent = collapsed ? '▶' : '▼';
}

// Expose to onclick handlers in HTML
window.removePosition = removePosition;
window.removeInterestPoint = removeInterestPoint;
window.updateIP = updateIP;
window.scheduleChart = scheduleChart;
window.scheduleRecalc = scheduleRecalc;
window.toggleSection = toggleSection;

// ── 20. Initialization ────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', async () => {
  loadState();
  populateEndDateSelect();
  setupOverlay();
  setupChartInteraction();
  setupEventListeners();

  // Connect to Deribit WS for live data
  wsConnect();

  // Load instruments from backend
  try {
    const r   = await fetch(`${API}/instruments`);
    const d   = await r.json();
    allInstruments = d.instruments || [];

    // Populate instruments Map in store
    const instruments = store.get().instruments;
    allInstruments.forEach(i => {
      const parsed = parseInstrumentName(i.instrument_name);
      if (!parsed) return;
      instruments.set(i.instrument_name, {
        ...parsed,
        markPrice: i.mark_price || 0,
        markIV:    (i.mark_iv || 80) / 100,
        bid:       i.bid_price || 0,
        ask:       i.ask_price || 0,
      });
    });
    store.set({ instruments });
    buildExpTabs();
    renderInstList();
  } catch (_) {
    showError('Backend not running. Start with: python app.py');
  }

  // Also try REST price as fallback (WS may take a moment)
  try {
    const r = await fetch(`${API}/current-price`);
    const d = await r.json();
    if (d.price && !store.get().lastUpdate) {
      store.set({ spotPrice: d.price, lastUpdate: Date.now() });
      renderTopBar();
    }
  } catch (_) {}

  // Restore WS subscriptions for existing portfolio
  store.get().portfolio.forEach(p => wsSubscribeTicker(p.instrumentName));

  // Initial render
  renderTopBar();
  renderPortfolio();
  renderInterestPointCards();
  renderTable();
  scheduleRecalc(0);

  // Update "last update" ticker every 10s
  setInterval(renderTopBar, 10000);
});

function setupEventListeners() {
  // Balance input
  document.getElementById('balanceInput')?.addEventListener('input', (e) => {
    store.set({ ethBalance: parseFloat(e.target.value) || 0 });
    renderTopBar();
    schedulePersist();
  });
  document.getElementById('balanceInput').value = store.get().ethBalance;

  // End date select
  document.getElementById('endDateSelect')?.addEventListener('change', (e) => {
    store.set({ endDateMs: parseInt(e.target.value) });
    scheduleRecalc();
    schedulePersist();
  });

  // Role select
  document.getElementById('roleSelect').value = store.get().commissionRole;
  document.getElementById('roleSelect')?.addEventListener('change', (e) => {
    store.set({ commissionRole: e.target.value });
    schedulePersist();
  });

  // Vol calendar
  document.getElementById('volCalBtn')?.addEventListener('click', openVolCalendar);
  document.getElementById('volModalClose')?.addEventListener('click', closeVolModal);
  document.getElementById('volApplyBtn')?.addEventListener('click', applyVolCalendar);
  document.getElementById('volResetBtn')?.addEventListener('click', resetVolCalendar);
  document.getElementById('volModal')?.addEventListener('click', (e) => {
    if (e.target === document.getElementById('volModal')) closeVolModal();
  });

  // Instrument search
  document.getElementById('searchInp')?.addEventListener('input', renderInstList);

  // Add position buttons
  document.getElementById('longBtn')?.addEventListener('click',  () => addPosition( 1));
  document.getElementById('shortBtn')?.addEventListener('click', () => addPosition(-1));

  // MILP weights
  ['Theta','Down','Up','Comm','Greek'].forEach(name => {
    const key = 'w' + name;
    const slider = document.getElementById(key);
    const label  = document.getElementById(key + 'Val');
    if (!slider) return;
    slider.value = store.get().milpWeights[name.toLowerCase().replace('comm','commission').replace('greek','greek')] || 10;
    slider.addEventListener('input', () => {
      if (label) label.textContent = slider.value + '%';
      const w = store.get().milpWeights;
      const keyMap = { Theta:'theta', Down:'down', Up:'up', Comm:'commission', Greek:'greek' };
      w[keyMap[name]] = parseInt(slider.value);
      store.set({ milpWeights: { ...w } });
      schedulePersist();
    });
  });

  // Sampling step
  document.getElementById('samplingStep')?.addEventListener('change', (e) => {
    store.set({ samplingStep: parseInt(e.target.value) });
    schedulePersist();
  });

  // MILP run / apply / discard
  document.getElementById('runMilpBtn')?.addEventListener('click', runMilp);
  document.getElementById('applyMilpBtn')?.addEventListener('click', applyMilpResult);
  document.getElementById('discardMilpBtn')?.addEventListener('click', discardMilpResult);

  // Bottom table toggle
  const bottomWrap   = document.getElementById('bottomWrap');
  const bottomToggle = document.getElementById('bottomToggle');
  const toggleIcon   = document.getElementById('bottomToggleIcon');
  bottomToggle?.addEventListener('click', () => {
    const exp = bottomWrap.classList.toggle('expanded');
    bottomWrap.classList.toggle('collapsed', !exp);
    if (toggleIcon) toggleIcon.textContent = exp ? '▼' : '▶';
    scheduleChart();
  });

  // Resize
  window.addEventListener('resize', scheduleChart);
}
