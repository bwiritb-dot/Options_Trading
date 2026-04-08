// bs.js — Client-side Black-Scholes option pricing
// Exposed as window.BS (also available in workers via importScripts)

(function (root) {
  'use strict';

  // Abramowitz & Stegun rational approximation for normal CDF (max error 1.5e-7)
  function normCdf(x) {
    if (!isFinite(x)) return x > 0 ? 1 : 0;
    const sign = x < 0 ? -1 : 1;
    const z = Math.abs(x) * 0.7071067811865476; // |x| / sqrt(2)
    const t = 1 / (1 + 0.3275911 * z);
    const poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))));
    return 0.5 * (1 + sign * (1 - poly * Math.exp(-z * z)));
  }

  function normPdf(x) {
    return 0.3989422804014327 * Math.exp(-0.5 * x * x);
  }

  // d1 in Black-Scholes (r = risk-free rate, defaults to 0)
  function _d1(S, K, T, sigma, r) {
    return (Math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * Math.sqrt(T));
  }

  // Option price. type: 'C' = call, 'P' = put. sigma: IV as decimal (0.85 = 85%).
  function bs(S, K, T, sigma, r, type) {
    if (T < 1e-6 || sigma < 1e-6 || S <= 0 || K <= 0) return intrinsic(S, K, type);
    const d1 = _d1(S, K, T, sigma, r || 0);
    const d2 = d1 - sigma * Math.sqrt(T);
    return type === 'C'
      ? S * normCdf(d1) - K * Math.exp(-(r || 0) * T) * normCdf(d2)
      : K * Math.exp(-(r || 0) * T) * normCdf(-d2) - S * normCdf(-d1);
  }

  // Intrinsic (payoff at expiry)
  function intrinsic(S, K, type) {
    return Math.max(0, type === 'C' ? S - K : K - S);
  }

  // Delta
  function bsDelta(S, K, T, sigma, r, type) {
    if (T < 1e-6) return type === 'C' ? (S > K ? 1 : 0) : (S < K ? -1 : 0);
    const d1 = _d1(S, K, T, sigma, r || 0);
    return type === 'C' ? normCdf(d1) : normCdf(d1) - 1;
  }

  // Vega per 1% IV change (in same units as option price)
  function bsVega(S, K, T, sigma) {
    if (T < 1e-6 || sigma < 1e-6 || S <= 0) return 0;
    const d1 = _d1(S, K, T, sigma, 0);
    return S * normPdf(d1) * Math.sqrt(T) * 0.01;
  }

  // Theta per calendar day (in same units as option price)
  function bsTheta(S, K, T, sigma, r, type) {
    if (T < 1e-6 || sigma < 1e-6) return 0;
    const d1 = _d1(S, K, T, sigma, r || 0);
    const d2 = d1 - sigma * Math.sqrt(T);
    const base = -S * normPdf(d1) * sigma / (2 * Math.sqrt(T));
    return (type === 'C'
      ? base - (r || 0) * K * Math.exp(-(r || 0) * T) * normCdf(d2)
      : base + (r || 0) * K * Math.exp(-(r || 0) * T) * normCdf(-d2)
    ) / 365;
  }

  // Gamma
  function bsGamma(S, K, T, sigma) {
    if (T < 1e-6 || sigma < 1e-6 || S <= 0) return 0;
    const d1 = _d1(S, K, T, sigma, 0);
    return normPdf(d1) / (S * sigma * Math.sqrt(T));
  }

  root.BS = { bs, intrinsic, bsDelta, bsVega, bsTheta, bsGamma, normCdf, normPdf };

})(typeof self !== 'undefined' ? self : window);
