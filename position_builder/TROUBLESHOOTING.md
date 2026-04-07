# Troubleshooting Guide

## Problem: "net::ERR_CONNECTION_REFUSED on :5000"

**Cause**: Flask backend is NOT running.

**Fix**:
1. Open a **new terminal** (separate from the frontend server)
2. Navigate to the folder:
   ```bash
   cd d:\Options_Trading\position_builder
   ```
3. Start the backend:
   ```bash
   python app.py
   ```
4. You should see:
   ```
   ==================================================
   🚀 Deribit Position Builder - Backend
   ==================================================
   ✓ Backend ready!
   ```
5. **DO NOT close this terminal** — it must stay running
6. Open another terminal for the frontend server

---

## Problem: "Can't connect to backend at localhost:5000"

**Cause**: Frontend server is running, but Flask backend is not.

**Fix** (2 terminals needed):

**Terminal 1 (Backend)**:
```bash
cd d:\Options_Trading\position_builder
python app.py
```

**Terminal 2 (Frontend)**:
```bash
cd d:\Options_Trading\position_builder
python -m http.server 8000
```

Then open: `http://localhost:8000`

---

## Problem: "No instruments loaded"

**Cause**: One of:
1. Flask backend is running, but Deribit API is unreachable
2. Internet connection problem
3. Deribit API is down

**Checks**:
- Can you access `https://www.deribit.com` in your browser?
- Check your firewall/VPN
- Try stopping and restarting the backend

**See error details**:
- Look at the terminal where you ran `python app.py`
- It will show: `❌ Deribit API timeout` or `❌ Connection error`

---

## Problem: "Add Position" button not visible

**Cause**: UI layout issue (rare).

**Fix**:
1. Refresh the browser: `Ctrl+F5` (hard refresh)
2. Open browser DevTools: `F12`
3. Check Console for errors
4. If still broken, delete browser cache for localhost:8000

---

## Problem: "Time slider not visible"

**Cause**: HTML layout issue.

**Fix**:
1. Hard refresh: `Ctrl+F5`
2. Check that your browser window is wide enough (min 1000px)
3. Try resizing the window

---

## Problem: PNL chart not updating

**Cause**: Backend not responding to chart requests.

**Fix**:
1. Check if Flask terminal shows errors
2. Make sure you added a position (click "Add Position" button)
3. Check browser console (`F12` → Console tab)
4. Look for red error messages

---

## Problem: "Loading instruments..." stuck

**Cause**: Backend taking too long to fetch from Deribit API.

**Fix**:
1. Wait 10-15 seconds (first load can be slow)
2. Refresh the page
3. If still stuck, check Flask terminal for timeout messages
4. Restart Flask backend: Ctrl+C, then `python app.py` again

---

## Full Debug Checklist

- [ ] Flask terminal shows `✓ Backend ready!`
- [ ] Frontend server shows `Serving HTTP on 0.0.0.0 port 8000`
- [ ] Browser can reach `http://localhost:8000` (not 404)
- [ ] Browser console has no red errors (`F12`)
- [ ] You can select an instrument from dropdown
- [ ] "Add Position" button is clickable
- [ ] "Long" and "Short" buttons are visible
- [ ] Time slider date input is visible

---

## Still stuck?

**Check each step**:

1. **Backend running?**
   ```bash
   cd d:\Options_Trading\position_builder
   python app.py
   ```
   Should print:
   ```
   ✓ Deribit Position Builder - Backend
   ✓ Backend ready!
   ```

2. **Flask listening on port 5000?**
   Open new terminal:
   ```bash
   curl http://localhost:5000/api/current-price
   ```
   Should show: `{"price": 2000.0}` (or actual ETH price)

3. **Frontend server running?**
   Open new terminal:
   ```bash
   cd d:\Options_Trading\position_builder
   python -m http.server 8000
   ```

4. **Browser can reach backend?**
   In browser console, run:
   ```javascript
   fetch('http://localhost:5000/api/current-price').then(r => r.json()).then(console.log)
   ```
   Should print: `{price: 2000.0}`

---

## Requirements Met?

Check you have:
- `pip list` should show: Flask, scipy, numpy, requests
- Python 3.7+
- Internet connection (for Deribit API)

If scipy is missing:
```bash
pip install scipy
```

---

## Report Bug

If the above doesn't work:
1. Take screenshot of error
2. Copy terminal output
3. Run in browser console:
   ```javascript
   console.log(navigator.userAgent)
   ```
4. Create issue with these details
