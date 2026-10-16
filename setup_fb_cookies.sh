#!/bin/bash
# Facebook Cookies Setup Helper

echo "================================================"
echo "   Facebook Cookies Setup for Yad 1.5"
echo "================================================"
echo ""
echo "📋 Follow these steps to get your cookies:"
echo ""
echo "1. Open Facebook in your browser: https://www.facebook.com"
echo "   (Make sure you're logged in!)"
echo ""
echo "2. Press F12 to open Developer Tools"
echo ""
echo "3. Go to: Application (Chrome) or Storage (Firefox)"
echo "   → Cookies → https://www.facebook.com"
echo ""
echo "4. Find and copy these two values:"
echo "   - c_user  (looks like a number, e.g., 12345678)"
echo "   - xs      (long string with letters/numbers)"
echo ""
echo "================================================"
echo ""
read -p "Enter your c_user value: " c_user
read -p "Enter your xs value: " xs

echo ""
echo "Creating fb_cookies.json..."

cat > /home/doronbrooder/app/fb_cookies.json <<EOF
{
  "c_user": "$c_user",
  "xs": "$xs"
}
EOF

echo "✅ Cookies saved to fb_cookies.json"
echo ""
echo "Now restart the server to enable Facebook scraping!"
echo "Run: pkill -f 'python server.py' && YAD2_PROXY=\"\" python server.py &"
