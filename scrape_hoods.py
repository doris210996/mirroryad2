#!/usr/bin/env python3
"""Scrape all neighborhoods from Yad2 address-master API."""
from curl_cffi import requests as cfreq
import json, time

session = cfreq.Session(impersonate='chrome')
session.get('https://www.yad2.co.il')
time.sleep(2)

base = 'https://gw.yad2.co.il/address-master'

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
r = session.get(base + '/cities', params={'limit': '2000'}, timeout=15)
cities = r.json()['data']['data']
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
        r = session.get(
            base + '/hoods',
            params={'city_id': cid, 'limit': '200', 'result_type': 'extended'},
            timeout=15
        )
        if r.status_code != 200:
            print(f"  {city_name_actual}: HTTP {r.status_code}")
            time.sleep(5)
            continue
        hoods = r.json().get('data', {}).get('data', [])
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
        time.sleep(1.5)
    except Exception as e:
        print(f"  {city_name_actual}: ERR {e}")
        time.sleep(5)

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
