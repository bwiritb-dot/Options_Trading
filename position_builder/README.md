# Deribit Position Builder

A real-time options position builder with PNL payoff diagrams, using live Deribit API data and Black-Scholes pricing.

## Features

✅ **Real Deribit Data** — Fetches live ETH options from Deribit public API
✅ **Live PNL Chart** — Interactive payoff diagrams with dynamic Greeks
✅ **Time Slider** — See P&L at different points in time
✅ **Volatility Adjustment** — Shift IV from -70% to +200%
✅ **Interest Rate Adjustment** — Shift rates from -20% to +20%
✅ **Position Management** — Add/remove positions, toggle on/off chart
✅ **Dark Theme** — Matches Deribit's UI exactly
✅ **Black-Scholes Pricing** — Accurate option valuations with Greeks

## Architecture

- **Backend**: Flask (Python) with Black-Scholes calculator
- **Frontend**: Vanilla HTML/CSS/JS with Canvas-based charting
- **API**: Deribit public REST API (no auth required)

## Setup & Run

### 1. Install Python dependencies

```bash
cd d:/Options_Trading/position_builder
pip install -r requirements.txt
```

### 2. Start the Flask backend

```bash
python app.py
```

The server will run on `http://localhost:5000`

### 3. Open the frontend

Open `index.html` in your browser, or use a simple HTTP server:

```bash
# Python 3
python -m http.server 8000

# Then visit: http://localhost:8000
```

## How It Works

### Position Builder
1. **Select instrument** — Choose an ETH option (e.g., `ETH-10APR26-2100-P`)
2. **Enter amount** — How many contracts
3. **Choose side** — Long (green) or Short (red)
4. **Add position** — Position added to portfolio

### Chart
- **Blue line** — Total PNL across all checked positions
- **Green area** — Profit zone (PNL > 0)
- **Red area** — Loss zone (PNL < 0)
- **Dashed line** — Current ETH spot price
- **X-axis** — ETH spot price range
- **Y-axis** — PNL in USD

### Controls
- **Time Slider** — Select date for theoretical P&L (accounts for theta decay)
- **Vol Slider** — Adjust implied volatility ±70% to +200%
- **Rate Slider** — Adjust risk-free rate ±20%
- **Checkbox** — Toggle position visibility on chart
- **Remove** — Delete position from portfolio

## Calculations

### Black-Scholes Pricing
```
C = S*N(d1) - K*e^(-rT)*N(d2)
P = K*e^(-rT)*N(-d2) - S*N(-d1)

Where:
d1 = (ln(S/K) + (r + σ²/2)T) / (σ√T)
d2 = d1 - σ√T
```

### PNL Calculation
```
PNL = (option_value_at_spot - entry_price) × quantity
```

For each spot price on the chart, the option is repriced using Black-Scholes.

### Greeks
- **Delta** — Sensitivity to price changes (∂V/∂S)
- **Gamma** — Delta sensitivity (∂²V/∂S²)
- **Vega** — Sensitivity to volatility (∂V/∂σ)
- **Theta** — Time decay (∂V/∂t)

## Data Sources

- **Instruments**: Deribit `/public/get_instruments` endpoint
- **Prices**: Deribit `/public/ticker` endpoint
- **IV**: Mark implied volatility from Deribit

## Performance Notes

- Instruments are cached on startup to reduce API calls
- PNL curve uses 100 spot price points for smooth visualization
- All calculations run in the backend (Python is fast for numerical work)

## Customization

### Change price range
Edit `app.js` line ~180:
```javascript
const spot_min = current_price * 0.7;  // 70% of spot
const spot_max = current_price * 1.3;  // 130% of spot
```

### Add more currencies
Add BTC instruments by modifying `app.py` to fetch multiple currencies in `fetch_eth_instruments()`

### Adjust chart colors
Edit `index.html` CSS:
- Profit area: `rgba(76, 175, 80, 0.2)` (green)
- Loss area: `rgba(244, 67, 54, 0.2)` (red)
- Line color: `#5499ff` (blue)

## Troubleshooting

**"Failed to load instruments"**
- Check if Flask backend is running on port 5000
- Check internet connection (Deribit API requires live connection)
- Verify `pip install` completed

**Chart not updating**
- Check browser console for errors (F12)
- Verify Flask is running and responding: `curl http://localhost:5000/api/current-price`

**Slow performance**
- Increase top of chart calculation range (fewer points = faster)
- Edit `app.py` line ~133: `spots = np.linspace(..., 50)` (was 100)

## Next Steps

- [ ] Add position Greeks to table
- [ ] Add portfolio-level Greeks
- [ ] Add P&L ladder (price/profit zones)
- [ ] Add Greeks ladder
- [ ] Save/load portfolios
- [ ] Add BTC instruments
- [ ] Add perps/futures
- [ ] Add real account connection

## License

MIT — Use freely.
