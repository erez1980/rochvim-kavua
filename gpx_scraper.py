#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
רוכבים קבוע — סקרייפר GPX רב-מקורי
=====================================
מוריד קבצי GPX למסלולים מ-6 מקורות (קק"ל קודם, ואז fallback),
שומר אותם בתיקיית gpx/ עם שם תקני, ומעדכן את index.html אוטומטית.

הרצה:
    python3 gpx_scraper.py

דרישות: Python 3.8+ ו-requests  (אם חסר: pip3 install requests)
"""

import os, re, json, time, sys, unicodedata
from urllib.parse import urljoin, urlparse, parse_qs, unquote

try:
    import requests
except ImportError:
    print("חסרה הספרייה requests. התקן עם:  pip3 install requests")
    sys.exit(1)

# ---------- הגדרות ----------
HERE       = os.path.dirname(os.path.abspath(__file__))
TRACKS_JSON= os.path.join(HERE, "tracks.json")
INDEX_HTML = os.path.join(HERE, "index.html")
GPX_DIR    = os.path.join(HERE, "gpx")
HEADERS    = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT    = 25
PAUSE      = 1.0      # השהיה בין בקשות (כבוד לשרתים)

session = requests.Session()
session.headers.update(HEADERS)

# ---------- עזרי שם קובץ ----------
def safe_filename(name):
    """ממיר שם מסלול לשם קובץ תקין (שומר עברית, מסיר תווים בעייתיים)."""
    name = name.replace("—", "-").replace("–", "-")
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name + ".gpx"

def looks_like_gpx(content):
    """בודק אם תוכן הקובץ הוא באמת GPX תקין."""
    head = content[:600].decode("utf-8", "ignore").lower()
    return ("<gpx" in head) or ("<?xml" in head and "gpx" in head)

# ---------- מקורות (כל אחד מחזיר URL ישיר ל-GPX או None) ----------

def src_kkl(track):
    """מקור 1: קק"ל — מחלץ קישור GPX מתוך דף המסלול."""
    src = track["src"]
    if "kkl.org.il/bike/trips" not in src:
        return None
    try:
        r = session.get(src, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        # מחפש קישור לקובץ GPX: יכול להיות מלא, יחסי-עם-לוכסן, או יחסי (files/...)
        # סדר עדיפות: קישור מלא > יחסי
        m = re.search(r'(https?://[^"\'\s]*Navigation_files/[^"\'\s]+\.gpx)', r.text, re.I)
        if m:
            return m.group(1)
        # קישור יחסי — מתחיל ב-files/ או /files/ או /bike/
        m = re.search(r'((?:/)?(?:bike/)?files/Navigation_files/[^"\'\s]+\.gpx)', r.text, re.I)
        if m:
            rel = m.group(1).lstrip("/")
            # הקישור היחסי הוא ביחס ל-/bike/ באתר קק"ל
            if not rel.startswith("bike/"):
                rel = "bike/" + rel
            return "https://www.kkl.org.il/" + rel
        return None
    except Exception:
        return None

def _find_gpx_links_in_page(url):
    """עזר כללי: מוריד דף ומחזיר את כל הקישורים שנראים כמו קבצי GPX."""
    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        links = re.findall(r'(https?://[^"\'\s]+\.gpx)', r.text, re.I)
        # גם קישורי הורדה נפוצים
        links += re.findall(r'(https?://[^"\'\s]*(?:download|gpx|navigation)[^"\'\s]*)', r.text, re.I)
        return list(dict.fromkeys(links))   # ייחודי, שומר סדר
    except Exception:
        return []

def src_page_scan(track):
    """מקור 2: סריקת דף המקור עצמו לקישורי GPX (shvilnet, bikepanel וכו')."""
    for link in _find_gpx_links_in_page(track["src"]):
        if link.lower().endswith(".gpx"):
            return link
    return None

def src_israelhiking(track):
    """מקור 3: Israel Hiking Map — חיפוש לפי שם והפקת GPX (אם נמצא relation)."""
    # מנסה דרך geosearch של IHM
    try:
        q = track["name"].split("(")[0].strip()
        url = f"https://israelhiking.osm.org.il/api/search/{requests.utils.quote(q)}/he"
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code == 200 and r.json():
            feat = r.json()[0]
            fid = feat.get("id") or feat.get("properties", {}).get("identifier")
            if fid:
                gpx_url = f"https://israelhiking.osm.org.il/api/files?url=https://israelhiking.osm.org.il/api/poi/OSM/{fid}&format=gpx"
                return gpx_url
    except Exception:
        pass
    return None

def src_osm_overpass(track):
    """מקור 4: OSM Overpass — מסלול לפי קואורדינטות (way/relation קרוב)."""
    # מנסה למצוא route relation של אופניים ליד הקואורדינטה
    try:
        lat, lon = track["lat"], track["lon"]
        query = f"""[out:xml][timeout:25];
(relation["route"="mtb"](around:800,{lat},{lon});
 relation["route"="bicycle"](around:800,{lat},{lon}););
out geom;"""
        r = session.post("https://overpass-api.de/api/interpreter",
                         data={"data": query}, timeout=TIMEOUT)
        if r.status_code == 200 and "<nd" in r.text or "<member" in r.text:
            # Overpass מחזיר OSM XML — נשמור כ-GPX רק אם יש גאומטריה
            if "lat=" in r.text and "lon=" in r.text:
                return ("OVERPASS_XML", r.text)
    except Exception:
        pass
    return None

def src_twonav(track):
    """מקור 5: TwoNav — דף סינגלים של קק"ל. מחלץ קישורי הורדה (sendspace/drive/go.twonav)
       ומנסה להתאים לפי קרבת שם."""
    try:
        url = "https://www.twonav.co.il/page/" + requests.utils.quote("ניווט-קולי-בסינגלים-של-קקל")
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        txt = r.content.decode("windows-1255", "ignore")
        # חיפוש קישורי GPX ישירים של go.twonav (אלה ניתנים להורדה)
        links = re.findall(r'(https?://go\.twonav\.com/public/shared/[^\s"\'<>]+)', txt)
        # התאמה גסה: אם שם המסלול (או חלקו) מופיע סמוך לקישור — לא מובטח, אז מחזיר None
        # מחזיר את הקישור הראשון רק אם יש בדף אזכור לשם המסלול
        key = track["name"].split("(")[0].split("—")[0].strip()
        if key and key[:6] in txt and links:
            return links[0]
    except Exception:
        pass
    return None

def src_wikiloc_search(track):
    """מקור 6: Wikiloc — חיפוש מסלול ציבורי לפי שם והורדת GPX (אם פתוח)."""
    try:
        q = track["name"].split("(")[0].split("—")[0].strip()
        url = "https://www.wikiloc.com/wikiloc/find.do?q=" + requests.utils.quote(q)
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code == 200:
            # מחפש קישור ישיר לקובץ GPX בתוצאות
            m = re.search(r'(https?://[^"\'\s]+\.gpx)', r.text, re.I)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None

def src_osmrm(track):
    """מקור 7: osmrm — ממיר OSM route relation ל-GPX לפי relation קרוב."""
    try:
        lat, lon = track["lat"], track["lon"]
        # מאתר relation של רכיבה ליד הקואורדינטה דרך Overpass
        q = f'[out:json][timeout:25];relation["route"~"mtb|bicycle|hiking"](around:600,{lat},{lon});out ids 1;'
        r = session.post("https://overpass-api.de/api/interpreter", data={"data": q}, timeout=TIMEOUT)
        if r.status_code == 200:
            els = r.json().get("elements", [])
            if els:
                rid = els[0]["id"]
                return f"https://osmrm.openstreetmap.de/gpx.jsp?relation={rid}"
    except Exception:
        pass
    return None

SOURCES = [
    ("קק\"ל",          src_kkl),
    ("סריקת דף מקור",  src_page_scan),
    ("Israel Hiking",  src_israelhiking),
    ("OSM Overpass",   src_osm_overpass),
    ("osmrm",          src_osmrm),
    ("TwoNav",         src_twonav),
    ("Wikiloc",        src_wikiloc_search),
]

# ---------- הורדה ----------
def download_gpx(url, dest):
    try:
        if isinstance(url, tuple) and url[0] == "OVERPASS_XML":
            # ממיר OSM XML בסיסי ל-GPX (נקודות בלבד)
            pts = re.findall(r'lat="([\d.]+)" lon="([\d.]+)"', url[1])
            if len(pts) < 5:
                return False
            gpx = ['<?xml version="1.0" encoding="UTF-8"?>',
                   '<gpx version="1.1" creator="Rochvim Kavua" xmlns="http://www.topografix.com/GPX/1/1">',
                   '<trk><trkseg>']
            for la, lo in pts:
                gpx.append(f'<trkpt lat="{la}" lon="{lo}"></trkpt>')
            gpx.append('</trkseg></trk></gpx>')
            open(dest, "w", encoding="utf-8").write("\n".join(gpx))
            return True
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and looks_like_gpx(r.content):
            open(dest, "wb").write(r.content)
            return True
    except Exception:
        pass
    return False

# ---------- ראשי ----------
def main():
    if not os.path.exists(TRACKS_JSON):
        print(f"לא נמצא {TRACKS_JSON}. ודא שהקובץ באותה תיקייה.")
        sys.exit(1)

    tracks = json.load(open(TRACKS_JSON, encoding="utf-8"))
    os.makedirs(GPX_DIR, exist_ok=True)

    print(f"מתחיל הורדת GPX ל-{len(tracks)} מסלולים מ-{len(SOURCES)} מקורות…\n")
    results = {}   # name -> filename
    stats = {name: 0 for name, _ in SOURCES}
    failed = []

    for i, t in enumerate(tracks, 1):
        name = t["name"]
        fname = safe_filename(name)
        dest = os.path.join(GPX_DIR, fname)

        # אם כבר קיים קישור gpx ישיר ב-JSON — מוריד אותו
        if t.get("gpx"):
            if download_gpx(t["gpx"], dest):
                results[name] = fname
                stats["קק\"ל"] += 1
                print(f"[{i:>3}/{len(tracks)}] ✓ {name[:40]:40} (gpx ישיר)")
                time.sleep(PAUSE)
                continue

        got = False
        for sname, fn in SOURCES:
            try:
                url = fn(t)
            except Exception:
                url = None
            if url and download_gpx(url, dest):
                results[name] = fname
                stats[sname] += 1
                print(f"[{i:>3}/{len(tracks)}] ✓ {name[:40]:40} ({sname})")
                got = True
                break
            time.sleep(0.3)
        if not got:
            failed.append(name)
            print(f"[{i:>3}/{len(tracks)}] ✗ {name[:40]:40} (לא נמצא)")
        time.sleep(PAUSE)

    # ---------- דוח ----------
    print("\n" + "=" * 50)
    print(f"הושלם: {len(results)}/{len(tracks)} קבצים הורדו")
    print("פילוח לפי מקור:")
    for sname, c in stats.items():
        if c: print(f"  {sname}: {c}")
    if failed:
        print(f"\nלא נמצאו ({len(failed)}):")
        for n in failed: print(f"  - {n}")

    # ---------- שמירת מיפוי ועדכון index.html ----------
    json.dump(results, open(os.path.join(HERE, "gpx_map.json"), "w"),
              ensure_ascii=False, indent=1)
    print(f"\nנשמר gpx_map.json ({len(results)} שיוכים)")
    update_index(results)

def update_index(results):
    """מוסיף שדה localgpx לכל מסלול שירד קובץ עבורו ב-index.html."""
    if not results:
        print("אין קבצים לעדכון ב-index.html")
        return
    html = open(INDEX_HTML, encoding="utf-8").read()
    count = 0
    for name, fname in results.items():
        # מוצא את הבלוק של המסלול ומוסיף localgpx אם אין
        # מחפש: name:"<name>", ... src:"...."}
        pattern = r'(\{name:"' + re.escape(name) + r'".*?src:"[^"]+")(,\s*gpx:"[^"]+")?(\})'
        def repl(m):
            nonlocal count
            base, existing_gpx, close = m.group(1), m.group(2) or "", m.group(3)
            if "localgpx:" in base:
                return m.group(0)
            count += 1
            return f'{base}{existing_gpx},localgpx:"gpx/{fname}"{close}'
        html = re.sub(pattern, repl, html, count=1, flags=re.S)
    open(INDEX_HTML, "w", encoding="utf-8").write(html)
    print(f"עודכן index.html: נוספו {count} שדות localgpx")
    print("\nהשלב הבא: העלה ל-GitHub עם:")
    print("  git add index.html gpx/ && git commit -m 'GPX מקומי' && git push")

if __name__ == "__main__":
    main()
