// milp-worker.js — MILP portfolio optimizer (runs in a Web Worker)
// Loads GLPK.js for mixed-integer linear programming.

importScripts('https://cdn.jsdelivr.net/npm/glpk.js@4.0.1/dist/glpk.min.js');

// ── Inline Black-Scholes (can't import bs.js from worker) ─────────────────────

function normCdf(x) {
  if (!isFinite(x)) return x > 0 ? 1 : 0;
  const sign = x < 0 ? -1 : 1;
  const z = Math.abs(x) * 0.7071067811865476;
  const t = 1 / (1 + 0.3275911 * z);
  const p = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
  return 0.5 * (1 + sign * (1 - p * Math.exp(-z * z)));
}

function normPdf(x) { return 0.3989422804014327 * Math.exp(-0.5 * x * x); }

function _d1w(S, K, T, sigma) {
  return (Math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * Math.sqrt(T));
}

function bsPriceW(S, K, T, sigma, type) {
  if (T < 1e-6 || sigma < 1e-6) return Math.max(0, type === 'C' ? S - K : K - S);
  const d1 = _d1w(S, K, T, sigma), d2 = d1 - sigma * Math.sqrt(T);
  return type === 'C' ? S * normCdf(d1) - K * normCdf(d2) : K * normCdf(-d2) - S * normCdf(-d1);
}

function bsDeltaW(S, K, T, sigma, type) {
  if (T < 1e-6) return type === 'C' ? (S > K ? 1 : 0) : (S < K ? -1 : 0);
  return type === 'C' ? normCdf(_d1w(S, K, T, sigma)) : normCdf(_d1w(S, K, T, sigma)) - 1;
}

function bsVegaW(S, K, T, sigma) {
  if (T < 1e-6 || sigma < 1e-6) return 0;
  return S * normPdf(_d1w(S, K, T, sigma)) * Math.sqrt(T) * 0.01;
}

function bsThetaW(S, K, T, sigma, type) {
  if (T < 1e-6 || sigma < 1e-6) return 0;
  const d1 = _d1w(S, K, T, sigma), d2 = d1 - sigma * Math.sqrt(T);
  const base = -S * normPdf(d1) * sigma / (2 * Math.sqrt(T));
  return (type === 'C' ? base : base) / 365; // simplified (r=0)
}

// ── GLPK setup ─────────────────────────────────────────────────────────────────

let glpk = null;
GLPK().then(g => { glpk = g; }).catch(e => { self.postMessage({ type: 'ERROR', payload: { message: 'GLPK load failed: ' + e } }); });

// ── Message handler ─────────────────────────────────────────────────────────────

self.onmessage = async function (e) {
  if (e.data.type === 'OPTIMIZE') {
    try {
      while (!glpk) await sleep(50);
      await runOptimizer(e.data.payload);
    } catch (err) {
      self.postMessage({ type: 'ERROR', payload: { message: String(err) } });
    }
  }
};

// ── Main optimizer ──────────────────────────────────────────────────────────────

async function runOptimizer(cfg) {
  const {
    instruments,       // FilteredInstrument[]
    existingPnlByGrid, // number[] — fixed PNL from current portfolio at each grid price
    interestPoints,    // {price, targetExpiry, constraint}[]
    grid,              // number[] — sampling prices
    endDate,           // ms timestamp
    spotPrice,
    marginUsd,
    commissionRole,
    weights,           // {theta, down, up, commission, greek}
    volCalendar,       // {[dateKey]: number (decimal IV)}
  } = cfg;

  postProg(10, 'Computing PNL matrix…');

  const N = instruments.length;
  const J = grid.length;
  const now = Date.now();
  const YEAR_MS = 365.25 * 24 * 3600 * 1000;
  const feeRate = commissionRole === 'taker' ? 0.0003 : 0;
  const endDateKey = new Date(endDate).toISOString().slice(0, 10);

  function getIv(inst) {
    return (volCalendar && volCalendar[endDateKey]) || inst.markIV || 0.8;
  }

  // Build pnlLong[i][j] and pnlShort[i][j]
  const pnlLong = [], pnlShort = [];
  for (let i = 0; i < N; i++) {
    const inst = instruments[i];
    const T = Math.max(0, (inst.expiry - endDate) / YEAR_MS);
    const iv = getIv(inst);
    const comm = feeRate * spotPrice;
    pnlLong[i] = new Float64Array(J);
    pnlShort[i] = new Float64Array(J);
    for (let j = 0; j < J; j++) {
      const S = grid[j];
      const optVal = bsPriceW(S, inst.strike, T, iv, inst.type);
      pnlLong[i][j]  = (optVal - inst.ask) * S - comm;
      pnlShort[i][j] = (inst.bid - optVal) * S - comm;
    }
  }

  postProg(33, 'Building MILP problem…');

  // Normalization
  const thetasUsd = instruments.map(inst => {
    const T = Math.max(0, (inst.expiry - now) / YEAR_MS);
    return Math.abs(bsThetaW(spotPrice, inst.strike, T, inst.markIV || 0.8, inst.type)) * spotPrice;
  });
  const NORM_THETA = Math.max(1, Math.max(...thetasUsd)) * 10;
  const NORM_DOWN  = marginUsd * 0.5;
  const NORM_UP    = marginUsd * 0.5;
  const NORM_COMM  = Math.max(1, N * feeRate * spotPrice * 5);
  const NORM_GREEK = 10;

  const W = weights;
  const ws = W.theta + W.down + W.up + W.commission + W.greek || 1;
  const wt = W.theta / ws, wd = W.down / ws, wu = W.up / ws, wc = W.commission / ws, wg = W.greek / ws;

  const bounds = [], generals = [], objVars = [], subjectTo = [];

  // pos[i] / neg[i] — long / short contracts
  for (let i = 0; i < N; i++) {
    const p = `p${i}`, n = `n${i}`;
    bounds.push({ name: p, type: glpk.GLP_DB, lb: 0, ub: 100 });
    bounds.push({ name: n, type: glpk.GLP_DB, lb: 0, ub: 100 });
    generals.push(p, n);

    const thetaUsd = thetasUsd[i];
    const thetaSign = bsThetaW(spotPrice, instruments[i].strike,
      Math.max(0, (instruments[i].expiry - now) / YEAR_MS),
      instruments[i].markIV || 0.8, instruments[i].type) < 0 ? -1 : 1;

    if (NORM_THETA > 0) {
      // Positive theta for long if instrument has positive theta (puts near expiry), else negative
      // More precisely: long theta = theta_per_contract (negative for most options)
      // We want to maximize net theta, so:
      //   obj += wt/NORM_THETA * theta[i] * p[i]   (could be negative — short maximizes when long is negative)
      //   obj += wt/NORM_THETA * (-theta[i]) * n[i]
      const th = bsThetaW(spotPrice, instruments[i].strike,
        Math.max(0, (instruments[i].expiry - now) / YEAR_MS),
        instruments[i].markIV || 0.8, instruments[i].type) * spotPrice;
      objVars.push({ name: p, coef:  wt * th / NORM_THETA });
      objVars.push({ name: n, coef: -wt * th / NORM_THETA });
    }
    // Commission penalty
    const commCoef = feeRate * spotPrice;
    if (NORM_COMM > 0) {
      objVars.push({ name: p, coef: -wc * commCoef / NORM_COMM });
      objVars.push({ name: n, coef: -wc * commCoef / NORM_COMM });
    }
  }

  // slack_down[j], slack_up[j] — deviation from target at grid point j
  for (let j = 0; j < J; j++) {
    const sd = `sd${j}`, su = `su${j}`;
    bounds.push({ name: sd, type: glpk.GLP_LO, lb: 0, ub: 0 });
    bounds.push({ name: su, type: glpk.GLP_LO, lb: 0, ub: 0 });
    if (NORM_DOWN > 0) objVars.push({ name: sd, coef: -wd / (J * NORM_DOWN) });
    if (NORM_UP   > 0) objVars.push({ name: su, coef:  wu / (J * NORM_UP)   });
  }

  // Greek linearization variables
  ['dp', 'dn', 'vp', 'vn'].forEach(v =>
    bounds.push({ name: v, type: glpk.GLP_LO, lb: 0, ub: 0 }));
  if (NORM_GREEK > 0) {
    objVars.push({ name: 'dp', coef: -wg / NORM_GREEK });
    objVars.push({ name: 'dn', coef: -wg / NORM_GREEK });
    objVars.push({ name: 'vp', coef: -wg * 0.1 / NORM_GREEK });
    objVars.push({ name: 'vn', coef: -wg * 0.1 / NORM_GREEK });
  }

  // Map interest points to nearest grid indices
  const targets    = new Float64Array(J); // target PNL at each grid point (default 0)
  const hardSet    = new Set();
  interestPoints.forEach(ip => {
    let best = 0, bestDist = Infinity;
    for (let j = 0; j < J; j++) {
      const d = Math.abs(grid[j] - ip.price);
      if (d < bestDist) { bestDist = d; best = j; }
    }
    targets[best] = ip.targetExpiry || 0;
    if (ip.constraint === 'hard') hardSet.add(best);
  });

  // PNL constraints at each grid point
  for (let j = 0; j < J; j++) {
    const tgt    = targets[j];
    const offset = existingPnlByGrid[j] || 0;
    const sd = `sd${j}`, su = `su${j}`;

    // Build [pos/neg terms] for this grid point
    const pnlVars = [];
    for (let i = 0; i < N; i++) {
      if (Math.abs(pnlLong[i][j])  > 1e-9) pnlVars.push({ name: `p${i}`, coef:  pnlLong[i][j]  });
      if (Math.abs(pnlShort[i][j]) > 1e-9) pnlVars.push({ name: `n${i}`, coef:  pnlShort[i][j] });
    }

    // Hard constraint: pnl[j] >= target  ⟹  Σ pnl_vars >= tgt - offset
    if (hardSet.has(j)) {
      subjectTo.push({
        name: `hrd${j}`,
        vars: pnlVars.map(v => ({ ...v })),
        bnds: { type: glpk.GLP_LO, lb: tgt - offset, ub: 0 }
      });
    }

    // Slack_down: sd[j] + Σ pnl_vars >= tgt - offset
    subjectTo.push({
      name: `sld${j}`,
      vars: [...pnlVars.map(v => ({ ...v })), { name: sd, coef: 1 }],
      bnds: { type: glpk.GLP_LO, lb: tgt - offset, ub: 0 }
    });

    // Slack_up: su[j] - Σ pnl_vars >= offset - tgt  (equiv: su >= pnl - tgt)
    subjectTo.push({
      name: `slu${j}`,
      vars: [...pnlVars.map(v => ({ ...v, coef: -v.coef })), { name: su, coef: 1 }],
      bnds: { type: glpk.GLP_LO, lb: offset - tgt, ub: 0 }
    });
  }

  // Margin: Σ 0.1*spot*(p[i]+n[i]) <= marginUsd
  const marginPerContract = 0.1 * spotPrice;
  subjectTo.push({
    name: 'margin',
    vars: instruments.flatMap((_, i) => [
      { name: `p${i}`, coef: marginPerContract },
      { name: `n${i}`, coef: marginPerContract }
    ]),
    bnds: { type: glpk.GLP_UP, ub: marginUsd, lb: 0 }
  });

  // Non-empty: Σ(p[i]+n[i]) >= 1
  subjectTo.push({
    name: 'nonempty',
    vars: instruments.flatMap((_, i) => [{ name: `p${i}`, coef: 1 }, { name: `n${i}`, coef: 1 }]),
    bnds: { type: glpk.GLP_LO, lb: 1, ub: 0 }
  });

  // Delta linearization: Σ delta[i]*(p[i]-n[i]) = dp - dn
  const deltaVars = [{ name: 'dp', coef: 1 }, { name: 'dn', coef: -1 }];
  for (let i = 0; i < N; i++) {
    const T = Math.max(0, (instruments[i].expiry - now) / YEAR_MS);
    const d = bsDeltaW(spotPrice, instruments[i].strike, T, instruments[i].markIV || 0.8, instruments[i].type);
    deltaVars.push({ name: `p${i}`, coef: -d }, { name: `n${i}`, coef: d });
  }
  subjectTo.push({ name: 'delta_lin', vars: deltaVars, bnds: { type: glpk.GLP_FX, lb: 0, ub: 0 } });

  // Vega linearization
  const vegaVars = [{ name: 'vp', coef: 1 }, { name: 'vn', coef: -1 }];
  for (let i = 0; i < N; i++) {
    const T = Math.max(0, (instruments[i].expiry - now) / YEAR_MS);
    const v = bsVegaW(spotPrice, instruments[i].strike, T, instruments[i].markIV || 0.8);
    vegaVars.push({ name: `p${i}`, coef: -v }, { name: `n${i}`, coef: v });
  }
  subjectTo.push({ name: 'vega_lin', vars: vegaVars, bnds: { type: glpk.GLP_FX, lb: 0, ub: 0 } });

  const lp = {
    name: 'portfolio_opt',
    objective: { direction: glpk.GLP_MAX, name: 'obj', vars: objVars },
    subjectTo,
    bounds,
    generals
  };

  postProg(50, 'Solving…');

  const result = glpk.solve(lp, {
    msLim: 30000,
    mipGap: 0.05,
    presolve: glpk.GLP_ON
  });

  postProg(90, 'Parsing result…');

  if (!result || !result.result) throw new Error('Solver returned no result');

  // GLP status codes: 5=OPT, 2=FEAS, 3=INFEAS, 4=NOFEAS, 6=UNBND
  const status = result.result.status;
  if (status === 4 || status === 3) {
    self.postMessage({ type: 'ERROR', payload: { message: 'No feasible solution. Relax hard constraints or increase margin.' } });
    return;
  }

  const vals = result.result.vars;
  const positions = [];
  for (let i = 0; i < N; i++) {
    const posC = Math.round(vals[`p${i}`] || 0);
    const negC = Math.round(vals[`n${i}`] || 0);
    if (posC > 0) positions.push({ instrumentName: instruments[i].instrumentName, contracts: posC,  direction:  1 });
    if (negC > 0) positions.push({ instrumentName: instruments[i].instrumentName, contracts: negC,  direction: -1 });
  }

  self.postMessage({ type: 'RESULT', payload: { positions, score: result.result.z, status } });
}

function postProg(pct, msg) { self.postMessage({ type: 'PROGRESS', payload: { pct, msg } }); }
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
