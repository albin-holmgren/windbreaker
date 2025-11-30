#!/bin/bash
#
# Test single scan - run this when rate limit resets
#
cd "$(dirname "$0")/.."

echo "Testing Jupiter API..."
RESPONSE=$(curl -s "https://lite-api.jup.ag/swap/v1/quote?inputMint=So11111111111111111111111111111111111111112&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v&amount=100000000")

if echo "$RESPONSE" | grep -q "Rate limit"; then
    echo "❌ Still rate limited. Wait a few more minutes."
    exit 1
fi

echo "✅ API working! Response:"
echo "$RESPONSE" | head -c 200
echo ""
echo ""
echo "Starting bot now..."
source .venv/bin/activate
python -m src.main
