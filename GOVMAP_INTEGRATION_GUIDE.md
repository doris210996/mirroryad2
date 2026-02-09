# GovMap API Integration Guide

## Research Summary

After extensive testing, I found that GovMap API has the following characteristics:

### API Architecture
- **Primary Interface**: JavaScript client library (`govmap.api.js`)
- **Backend Services**: REST endpoints at `ags.govmap.gov.il` and `es.govmap.gov.il`
- **Authentication**: Token-based (UUID format)
- **Access**: Requires registration

### Test Results
```
✓ JavaScript API: https://www.govmap.gov.il/govmap/api/govmap.api.js (Working)
✗ REST API: https://ags.govmap.gov.il/arcgis/rest/services (Returns HTML, needs auth)
✗ Search API: https://es.govmap.gov.il/TldSearch/api/* (Access restricted)
```

## How to Get GovMap API Access

### Step 1: Register for API Access

1. Visit [GovMap API Documentation](https://api.govmap.gov.il/)
2. Look for registration link or contact information
3. Fill out API access request form
4. Provide:
   - Organization name
   - Use case description ("Real estate market analysis")
   - Contact details

### Step 2: Obtain API Token

Once approved, you'll receive:
- API Token (UUID format: `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`)
- API Documentation
- Rate limits and usage terms

### Step 3: Alternative - Use Selenium Approach

If direct REST API access is not available, use Selenium to interact with GovMap's JavaScript API:

```python
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

def query_govmap_with_selenium(address, api_token):
    """Query GovMap using Selenium with JavaScript API"""

    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    driver = webdriver.Chrome(options=options)

    try:
        # Load a page with GovMap API
        driver.get('data:text/html,<html><head>' +
                   '<script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>' +
                   '<script src="https://www.govmap.gov.il/govmap/api/govmap.api.js"></script>' +
                   '</head><body><div id="map"></div></body></html>')

        # Wait for API to load
        WebDriverWait(driver, 10).until(
            lambda d: d.execute_script('return typeof govmap !== "undefined"')
        )

        # Execute GovMap geocode query
        result = driver.execute_script(f'''
            return new Promise((resolve) => {{
                govmap.createMap("map", {{
                    token: "{api_token}",
                    layers: ["REAL_ESTATE_16"]
                }});

                govmap.geocode({{
                    keyword: "{address}"
                }}, govmap.geocodeType.FullResult).then(resolve);
            }});
        ''')

        return result

    finally:
        driver.quit()
```

### Step 4: Configure Environment

```bash
# Add to .env or environment variables
export GOVMAP_API_TOKEN="your-token-here"

# Install dependencies
pip install selenium webdriver-manager
```

### Step 5: Update server.py

```python
import os
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

GOVMAP_API_TOKEN = os.environ.get("GOVMAP_API_TOKEN", "")

def try_govmap_api(city, neighborhood, rooms):
    """Fetch real data from GovMap using Selenium"""

    if not GOVMAP_API_TOKEN:
        print("[GovMap] No API token configured")
        return generate_sample_transactions(city, neighborhood, rooms)

    try:
        search_query = f"{neighborhood}, {city}" if neighborhood else city

        # Use Selenium to query GovMap
        result = query_govmap_with_selenium(search_query, GOVMAP_API_TOKEN)

        # Parse result and extract transactions
        transactions = parse_govmap_selenium_result(result, rooms)

        if transactions:
            print(f"[GovMap] Retrieved {len(transactions)} real transactions")
            return transactions

    except Exception as e:
        print(f"[GovMap] Selenium query failed: {e}")

    # Fallback to sample data
    return generate_sample_transactions(city, neighborhood, rooms)
```

## Alternative: Data.gov.il Option

If GovMap proves difficult, consider Israel's open data portal:

1. Visit [data.gov.il](https://data.gov.il/)
2. Search for "נדל\"ן" (real estate) datasets
3. Check if real estate transaction datasets are available
4. Most datasets are CSV/JSON files that can be downloaded

Example:
```python
import requests
import pandas as pd

def fetch_from_data_gov_il():
    """Fetch real estate data from Israel's open data portal"""

    # Search for real estate datasets
    search_url = "https://data.gov.il/api/3/action/package_search"
    params = {"q": "נדלן עסקאות"}

    resp = requests.get(search_url, params=params)
    datasets = resp.json()

    # Process datasets...
```

## Contact Information

**GovMap Support:**
- Website: https://www.govmap.gov.il/
- API Docs: https://api.govmap.gov.il/
- Email: Check website for contact form

**Alternative Resources:**
- [Bellingcat's GovMap Guide](https://bellingcat.gitbook.io/toolkit/more/all-tools/govmap)
- [GovMap GitHub Examples](https://github.com/yovavsanders/govmap)
- [GovMap API Examples Page](https://www.govmap.gov.il/sites/api_examples.html)

## Recommended Next Steps

1. **Short-term**: Keep using sample data for demonstration
2. **Medium-term**: Register for GovMap API access
3. **Long-term**: Implement Selenium-based solution once token is obtained
4. **Alternative**: Explore data.gov.il datasets for static data

## Cost Considerations

- GovMap API: Likely free for government data, check terms
- Selenium: Requires compute resources for headless browser
- data.gov.il: Free, public domain data

---

**Status**: 🔍 Researched | 📋 Registration Required | 🛠️ Implementation Ready (pending token)
