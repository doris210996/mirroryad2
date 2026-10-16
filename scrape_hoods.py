#!/usr/bin/env python3
"""Fetch all neighborhoods from Yad2 address-master API (politely: robots.txt, rate limit)."""
import json
from urllib.parse import urlencode

from polite_fetcher import FetchError, PoliteFetcher

fetcher = PoliteFetcher(min_interval=2.0, cache_ttl=0)
base = 'https://gw.yad2.co.il/address-master'


def get_json(path, params):
    return fetcher.get(f"{base}{path}?{urlencode(params)}").json()


# Load existing data if available
try:
    with open('city_to_neighborhoods.json', encoding='utf-8') as f:
        city_to_hoods = json.load(f)
    with open('neighborhood_to_city.json', encoding='utf-8') as f:
        hood_to_city = json.load(f)
    print(f"Loaded existing: {len(hood_to_city)} hoods, {len(city_to_hoods)} cities")
except:
    city_to_hoods = {}
    hood_to_city = {}

# Get city list
cities = get_json('/cities', {'limit': '2000'})['data']['data']
name_to_id = {c.get('city_heb', ''): c.get('city_id', '') for c in cities}

remaining = [
    'חולון', 'רמת השרון', 'טבריה', 'עפולה', 'כרמיאל', 'יבנה',
    'נצרת', 'אילת', 'בית שמש', 'גדרה', 'קריית אתא',
    'קריית ביאליק', 'קריית מוצקין', 'קריית ים', 'קריית גת',
    'כפר יונה', 'זכרון יעקב', 'מגדל העמק', 'שדרות', 'דימונה',
    'נס ציונה', 'ראש העין', 'עכו', 'קריית שמונה', 'חדרה',
    'באר שבע', 'רחובות', 'אשדוד', 'לוד', 'רמלה', 'נהריה',
    'כפר סבא', 'הוד השרון',
]

for city_name in remaining:
    if city_name in city_to_hoods:
        print(f"  {city_name}: already have {len(city_to_hoods[city_name])} hoods")
        continue
    cid = name_to_id.get(city_name, '')
    city_name_actual = city_name
    if not cid:
        for n, nid in name_to_id.items():
            if city_name in n or n in city_name:
                cid = nid
                city_name_actual = n
                break
        else:
            print(f"  {city_name}: NO ID FOUND")
            continue

    try:
        hoods = get_json('/hoods', {'city_id': cid, 'limit': '200', 'result_type': 'extended'}).get('data', {}).get('data', [])
        seen = set()
        unique = []
        for h in hoods:
            hn = h.get('hood_heb', '')
            hi = h.get('hood_id', '')
            if hn and hi not in seen:
                seen.add(hi)
                unique.append({'name': hn, 'hood_id': hi})
                hood_to_city[hn] = {'city': city_name_actual, 'city_id': cid, 'hood_id': hi}
        city_to_hoods[city_name_actual] = unique
        print(f"  {city_name_actual}: {len(unique)} hoods")
    except FetchError as e:
        print(f"  {city_name_actual}: ERR {e}")

# Save
with open('neighborhood_to_city.json', 'w', encoding='utf-8') as f:
    json.dump(hood_to_city, f, ensure_ascii=False, indent=2)
with open('city_to_neighborhoods.json', 'w', encoding='utf-8') as f:
    json.dump(city_to_hoods, f, ensure_ascii=False, indent=2)

print(f"\nTotal hoods: {len(hood_to_city)}, Cities: {len(city_to_hoods)}")

# Summary
print("\nAll cities:")
for city, hoods in sorted(city_to_hoods.items(), key=lambda x: len(x[1]), reverse=True):
    print(f"  {city}: {len(hoods)}")
