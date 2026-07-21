"""
GitHub Actions用 全物件スクレイパー
実行: python scraper/scrape_all.py
出力: data/results.json
"""
import sys, re, time, math, json, os, gc
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding='utf-8')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
adapter = requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2)
SESSION.mount('http://', adapter)
SESSION.mount('https://', adapter)

_geo_cache = {}
KANTO_BOUNDS = (34.5, 36.8, 138.5, 140.9)
_geo_last = [0.0]

def _in_kanto(lat, lon):
    return KANTO_BOUNDS[0] <= lat <= KANTO_BOUNDS[1] and KANTO_BOUNDS[2] <= lon <= KANTO_BOUNDS[3]

def _gsi_geocode(address):
    try:
        r = requests.get('https://msearch.gsi.go.jp/address-search/AddressSearch',
                         params={'q': address}, timeout=8,
                         headers={'User-Agent': 'PropertySearchApp/1.0'})
        if r.status_code == 200:
            data = r.json()
            if data:
                lon, lat = data[0]['geometry']['coordinates']
                if _in_kanto(float(lat), float(lon)):
                    return float(lat), float(lon)
    except Exception as e:
        print(f'[GSI] {address}: {e}')
    return None, None

def _nominatim(query):
    elapsed = time.time() - _geo_last[0]
    if elapsed < 1.2:
        time.sleep(1.2 - elapsed)
    try:
        r = requests.get('https://nominatim.openstreetmap.org/search',
                         params={'q': query, 'format': 'json', 'limit': 3, 'countrycodes': 'jp'},
                         timeout=8, headers={'User-Agent': 'PropertySearchApp/1.0'})
        _geo_last[0] = time.time()
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f'[NOM] {query}: {e}')
    _geo_last[0] = time.time()
    return []

KANJI_NUM = {'一':'1','二':'2','三':'3','四':'4','五':'5','六':'6','七':'7','八':'8','九':'9','〇':'0','○':'0'}

def normalize_address(addr):
    for k, v in KANJI_NUM.items():
        addr = addr.replace(k, v)
    return addr.strip()

def geocode_address(address, station_name=''):
    if address and len(address) >= 8:
        norm = normalize_address(address)
        key = 'addr:' + norm
        if key in _geo_cache:
            cached = _geo_cache[key]
            if cached != (None, None):
                return cached
        else:
            lat, lon = _gsi_geocode(norm)
            if lat:
                _geo_cache[key] = (lat, lon)
                return lat, lon
            items = _nominatim(norm)
            for item in items:
                lat, lon = float(item['lat']), float(item['lon'])
                if _in_kanto(lat, lon):
                    _geo_cache[key] = (lat, lon)
                    return lat, lon
            _geo_cache[key] = (None, None)
    if station_name:
        key2 = 'sta:' + station_name
        if key2 in _geo_cache:
            return _geo_cache[key2]
        # 「東京」を付けずに検索（埼玉・神奈川等の駅にも対応）
        items = _nominatim(station_name + ' 関東')
        if not items:
            items = _nominatim(station_name)
        for item in items:
            lat, lon = float(item['lat']), float(item['lon'])
            if _in_kanto(lat, lon):
                _geo_cache[key2] = (lat, lon)
                return lat, lon
        _geo_cache[key2] = (None, None)
    return None, None

def safe_get(url, timeout=10):
    try:
        r = SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        return r
    except Exception as e:
        print(f'[WARN] GET {url} -> {e}')
        return None

ZERO_FEE_TOKENS = {'-', 'ー', '−', '無', '無し', 'なし', '0', '0円', '0ヶ月'}

def is_zero_fee(text):
    return (text or '').strip() in ZERO_FEE_TOKENS

def parse_rent(text):
    if not text:
        return None
    text = re.sub(r'[,\s円　]', '', text)
    m = re.search(r'([\d.]+)万', text)
    if m:
        return int(float(m.group(1)) * 10000)
    m = re.search(r'(\d{4,})', text)
    if m:
        return int(m.group(1))
    return None

# ──────────────────────────────────────────────
# SUUMO
# ──────────────────────────────────────────────
SUUMO_BASE_RC = (
    "https://suumo.jp/jj/chintai/ichiran/FR301FC001/"
    "?ar=030&bs=040&ta=13"
    "&sc=13101&sc=13102&sc=13103&sc=13104&sc=13105"
    "&sc=13106&sc=13107&sc=13108&sc=13109&sc=13110"
    "&sc=13111&sc=13112&sc=13113"
    "&cb=0.0&ct=20.0&mb=0&mt=9999999&et=9999999&cn=9999999"
    "&shkr1=03&sngz=&pc=30"
)
SUUMO_BASE_SRC = SUUMO_BASE_RC.replace('shkr1=03', 'shkr1=04')

def _scrape_suumo_url(base_url, results):
    r = safe_get(base_url)
    if not r:
        return
    soup = BeautifulSoup(r.text, 'lxml')
    for item in soup.select('.cassetteitem'):
        name_el = item.select_one('.cassetteitem_content-title')
        name = name_el.text.strip() if name_el else ''
        if not name:
            continue
        area_el = item.select_one('.cassetteitem_detail-col1')
        area = area_el.text.strip() if area_el else ''
        access_el = item.select_one('.cassetteitem_detail-col2 .cassetteitem_detail-text')
        access_text = access_el.text.strip() if access_el else ''
        if '/' in access_text or '／' in access_text:
            parts = re.split(r'[/／]', access_text)
            line = parts[0].strip()
            station_raw = parts[1].strip() if len(parts) > 1 else ''
        else:
            line = ''; station_raw = access_text
        station_m = re.search(r'(\S+駅)', station_raw)
        station = station_m.group(1) if station_m else ''
        walk_m = re.search(r'歩(\d+)分', access_text)
        walk = int(walk_m.group(1)) if walk_m else 0
        full_text = item.text
        detected_structure = 'SRC造' if 'SRC' in full_text else 'RC造' if 'RC' in full_text else 'RC造'
        ins = '24h演奏可' if ('24時間' in full_text and '楽器' in full_text) else '楽器可' if ('楽器可' in full_text or '防音' in full_text) else '相談可'
        rows = item.select('tr.js-cassette_link') or [item]
        for row in rows[:3]:
            tds = row.select('td')
            rent_text = tds[3].text.strip() if len(tds) > 3 else ''
            rent_val = parse_rent(rent_text)
            if not rent_val:
                continue
            layout_text = tds[5].text.strip() if len(tds) > 5 else ''
            size_m = re.search(r'([\d.]+m\S*)', layout_text)
            size = size_m.group(1) if size_m else ''
            fee_parts = tds[4].text.split() if len(tds) > 4 else []
            no_deposit = is_zero_fee(fee_parts[0] if len(fee_parts) > 0 else '')
            no_keymoney = is_zero_fee(fee_parts[1] if len(fee_parts) > 1 else '')
            link = row.select_one('a[href*="/chintai/"]')
            url = ('https://suumo.jp' + link['href']) if link and not link['href'].startswith('http') else (link['href'] if link else base_url)
            lat, lng = geocode_address(area, station)
            results.append({'name': name, 'area': area, 'station': station, 'line': line,
                            'walk': walk, 'rentMin': rent_val, 'rentMax': rent_val,
                            'structure': detected_structure, 'size': size,
                            'instrument': ins, 'internet': '不明', 'url': url,
                            'noDeposit': no_deposit, 'noKeyMoney': no_keymoney,
                            'source': 'SUUMO', 'lat': lat, 'lng': lng})

def scrape_suumo():
    results = []
    _scrape_suumo_url(SUUMO_BASE_RC, results)
    _scrape_suumo_url(SUUMO_BASE_SRC, results)
    print(f'[SUUMO] {len(results)}件')
    return results

# ──────────────────────────────────────────────
# ミュージション
# ──────────────────────────────────────────────
def scrape_musision():
    results = []
    r = safe_get('https://www.musision.jp/props')
    if not r:
        print('[ミュージション] 接続失敗')
        return results
    soup = BeautifulSoup(r.text, 'lxml')
    seen = set()
    prop_links = []
    for a in soup.select('a[href*="/props/"]'):
        href = a.get('href', '')
        if re.search(r'/props/\d+', href) and href not in seen:
            seen.add(href)
            if not href.startswith('http'):
                href = 'https://www.musision.jp' + href
            prop_links.append(href)
    for href in prop_links[:8]:
        time.sleep(0.5)
        detail = safe_get(href)
        if not detail:
            continue
        d = BeautifulSoup(detail.text, 'lxml')
        title_el = d.select_one('title')
        raw = title_el.text if title_el else ''
        m = re.search(r'(ミュージション[^\s（(【】|｜]+)', raw)
        name = m.group(1).strip() if m else ''
        if not name:
            continue
        area = ''; station = ''; walk = 0; rent_val = None
        for el in d.select('h2, h3, p, li, td, th, div'):
            txt = el.text.strip()
            if '所在地' in txt or '住所' in txt:
                m2 = re.search(r'((東京都|神奈川県|埼玉県|千葉県|茨城県|栃木県|群馬県)[^\n\r]{4,30})', txt)
                if m2:
                    area = m2.group(1).strip()
            if '駅' in txt and '徒歩' in txt and not station:
                clean = re.sub(r'[「」『』【】◎]', '', txt)
                sm = re.search(r'([^\s・/／,。、\d（）()]{1,10}駅)', clean)
                wm = re.search(r'徒歩(\d+)分', txt)
                if sm:
                    station = sm.group(1)
                if wm:
                    walk = int(wm.group(1))
            if '万円' in txt and not rent_val:
                m3 = re.search(r'([\d.]+)万円', txt)
                if m3:
                    rent_val = int(float(m3.group(1)) * 10000)
        lat, lng = geocode_address(area, station)
        results.append({'name': name, 'area': area, 'station': station, 'line': '',
                        'walk': walk, 'rentMin': rent_val or 0, 'rentMax': rent_val or 0,
                        'structure': 'RC造', 'size': '',
                        'instrument': '24h演奏可', 'internet': '不明', 'url': href,
                        'noDeposit': False, 'noKeyMoney': False,
                        'source': 'ミュージション', 'lat': lat, 'lng': lng})
    print(f'[ミュージション] {len(results)}件')
    return results

# ──────────────────────────────────────────────
# 防音賃貸.com
# ──────────────────────────────────────────────
def scrape_bouon():
    results = []
    r = safe_get('https://www.bouonchintai.com/')
    if not r:
        print('[防音賃貸.com] 接続失敗')
        return results
    soup = BeautifulSoup(r.text, 'lxml')
    seen = set()
    prop_links = []
    for a in soup.select('a[href]'):
        href = a.get('href', '')
        if re.search(r'build-\d+/room-\d+\.html', href) and href not in seen:
            seen.add(href)
            if not href.startswith('http'):
                href = 'https://www.bouonchintai.com' + href
            prop_links.append(href)
    for href in prop_links[:8]:
        time.sleep(0.5)
        detail = safe_get(href)
        if not detail:
            continue
        d = BeautifulSoup(detail.text, 'lxml')
        name_el = d.select_one('h2.section-title')
        name = name_el.text.strip() if name_el else ''
        if not name:
            continue
        rent_val = None; station = ''; walk = 0; area = '東京都'; structure = 'RC造'; size = ''
        no_deposit = False; no_keymoney = False
        for row in d.select('.detail-list-item'):
            title_el = row.select_one('.detail-list-item-title')
            content_el = row.select_one('.detail-list-item-content, p, span:not(.detail-list-item-title)')
            if not title_el or not content_el:
                continue
            label = title_el.text.strip(); value = content_el.text.strip()
            if '賃料' in label:
                price_el = row.select_one('span.price, p.price')
                price_txt = price_el.text if price_el else value
                m = re.search(r'([\d,]+)円', price_txt)
                if m:
                    rent_val = int(m.group(1).replace(',', ''))
            elif '交通' in label:
                sm = re.search(r'([^\s]{1,10}駅)', value)
                wm = re.search(r'徒歩(\d+)分', value)
                if sm: station = sm.group(1)
                if wm: walk = int(wm.group(1))
            elif '住所' in label:
                area = value.strip() if value else area
            elif '建物構造' in label:
                structure = 'SRC造' if 'SRC' in value else 'RC造'
            elif '専有面積' in label or '間取り' in label:
                sm2 = re.search(r'([\d.]+㎡)', value)
                if sm2: size = sm2.group(1)
            elif '敷金' in label and '礼金' in label:
                fee_parts = value.split('/')
                no_deposit = is_zero_fee(fee_parts[0] if len(fee_parts) > 0 else '')
                no_keymoney = is_zero_fee(fee_parts[1] if len(fee_parts) > 1 else '')
        lat, lng = geocode_address(area, station)
        results.append({'name': name, 'area': area, 'station': station, 'line': '',
                        'walk': walk, 'rentMin': rent_val or 0, 'rentMax': rent_val or 0,
                        'structure': structure, 'size': size,
                        'instrument': '24h演奏可', 'internet': '不明', 'url': href,
                        'noDeposit': no_deposit, 'noKeyMoney': no_keymoney,
                        'source': '防音賃貸.com', 'lat': lat, 'lng': lng})
    print(f'[防音賃貸.com] {len(results)}件')
    return results

# ──────────────────────────────────────────────
# Musicman不動産
# ──────────────────────────────────────────────
MUSICMAN_BASE = 'https://estate.musicman.co.jp'

def scrape_musicman():
    results = []
    r = safe_get(f'{MUSICMAN_BASE}/estate/')
    if not r:
        print('[Musicman] 接続失敗')
        return results
    soup = BeautifulSoup(r.text, 'lxml')
    seen_urls = set()
    prop_links = []
    for a in soup.select('a[href*="/estate/"]'):
        href = a.get('href', '')
        if re.search(r'/estate/\d+', href) and href not in seen_urls:
            seen_urls.add(href)
            if not href.startswith('http'):
                href = MUSICMAN_BASE + href
            prop_links.append(href)
    for href in prop_links[:8]:
        time.sleep(0.5)
        detail = safe_get(href)
        if not detail:
            continue
        d = BeautifulSoup(detail.text, 'lxml')
        name_el = d.select_one('h1')
        name = name_el.text.strip() if name_el else ''
        if not name:
            continue
        rent_val = None; station = ''; walk = 0; area = '東京都'; structure = 'RC造'; size = ''
        no_deposit = False; no_keymoney = False
        for tr in d.select('tr'):
            cells = tr.select('th, td')
            if len(cells) < 2:
                continue
            label = cells[0].text.strip(); value = cells[1].text.strip()
            if '賃料' in label:
                m = re.search(r'([\d,]+)円', value)
                if m: rent_val = int(m.group(1).replace(',', ''))
            elif 'アクセス' in label:
                clean = re.sub(r'[「」『』]', '', value)
                sm = re.search(r'([^\s・/／,。、\d]{1,10}駅)', clean)
                wm = re.search(r'徒歩(\d+)分', value)
                if sm: station = sm.group(1)
                if wm: walk = int(wm.group(1))
            elif '所在地' in label:
                area = value.strip()
            elif '建築構造' in label or '建物構造' in label:
                structure = 'SRC造' if 'SRC' in value else 'RC造'
            elif '面積' in label:
                sm2 = re.search(r'([\d.]+)㎡', value)
                if sm2: size = sm2.group(1) + '㎡'
            elif '敷金' in label:
                no_deposit = is_zero_fee(value)
            elif '礼金' in label:
                no_keymoney = is_zero_fee(value)
        lat, lng = geocode_address(area, station)
        results.append({'name': name, 'area': area, 'station': station, 'line': '',
                        'walk': walk, 'rentMin': rent_val or 0, 'rentMax': rent_val or 0,
                        'structure': structure, 'size': size,
                        'instrument': '24h演奏可', 'internet': '不明', 'url': href,
                        'noDeposit': no_deposit, 'noKeyMoney': no_keymoney,
                        'source': 'Musicman不動産', 'lat': lat, 'lng': lng})
    print(f'[Musicman不動産] {len(results)}件')
    return results

# ──────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────
def main():
    all_results = []
    for name, fn in [('SUUMO', scrape_suumo), ('ミュージション', scrape_musision),
                     ('防音賃貸.com', scrape_bouon), ('Musicman不動産', scrape_musicman)]:
        try:
            res = fn()
            all_results.extend(res)
        except Exception as e:
            print(f'[ERROR] {name}: {e}')
        gc.collect()

    # 重複除去
    seen = set(); unique = []
    for p in all_results:
        key = (p['name'].strip(), p['station'].strip())
        if key not in seen:
            seen.add(key)
            unique.append(p)

    output = {
        'updated': datetime.now(timezone.utc).isoformat(),
        'total': len(unique),
        'properties': unique
    }

    os.makedirs('data', exist_ok=True)
    with open('data/results.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f'\n✅ 保存完了: {len(unique)}件 → data/results.json')

if __name__ == '__main__':
    main()
