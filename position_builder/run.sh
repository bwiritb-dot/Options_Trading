#!/bin/bash

# Position Builder Startup Script

echo "🚀 Starting Deribit Position Builder..."
echo ""
echo "Installing dependencies..."
pip install -r requirements.txt

echo ""
echo "Starting Flask backend..."
echo "Backend running on: http://localhost:5000"
echo ""
echo "🌐 Open this in your browser:"
echo "   http://localhost:8000 (if using Python HTTP server)"
echo "   Or directly: file://$(pwd)/index.html"
echo ""
echo "In another terminal, run:"
echo "   cd $(pwd)"
echo "   python -m http.server 8000"
echo ""
echo "Press Ctrl+C to stop the backend"
echo ""

python app.py
