"""Working Lab 01 functions, extracted from instructor v3, 5 October 2026.
No notebook cells execute when importing this module. Supplied, do not edit.
"""
import hashlib, json, os, sqlite3, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
MAX_PAGES = 100
MAX_BYTES = 5 * 1024 * 1024
def utc_parse(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Use a timezone in every timestamp')
    return result.astimezone(timezone.utc)

def utc_text(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

def now_utc():
    return utc_text(datetime.now(timezone.utc))

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    stream_key TEXT PRIMARY KEY,
    query_json TEXT NOT NULL,
    watermark TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenes (
    collection TEXT NOT NULL,
    item_id TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    cloud_cover REAL,
    assets_json TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (collection, item_id)
);
CREATE TABLE IF NOT EXISTS downloads (
    collection TEXT NOT NULL,
    item_id TEXT NOT NULL,
    asset_key TEXT NOT NULL,
    source_url TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'downloaded', 'failed')),
    local_path TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (collection, item_id, asset_key),
    FOREIGN KEY (collection, item_id) REFERENCES scenes (collection, item_id)
);
"""

def connect_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.executescript(SCHEMA)
    return conn

def store_item(conn, item):
    props = item['properties']
    stamp = now_utc()
    acquired = utc_text(utc_parse(props['datetime']))
    conn.execute("""INSERT INTO scenes
        (collection, item_id, acquired_at, cloud_cover, assets_json, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(collection, item_id) DO UPDATE SET
        acquired_at=excluded.acquired_at, cloud_cover=excluded.cloud_cover,
        assets_json=excluded.assets_json, last_seen=excluded.last_seen""",
        (item['collection'], item['id'], acquired, props.get('eo:cloud_cover'),
         json.dumps(item.get('assets', {}), sort_keys=True), stamp, stamp))

def open_http(url, attempts=3):
    if urlsplit(url).scheme != 'https':
        raise ValueError('This lab uses public HTTPS assets and searches only')
    for attempt in range(attempts):
        try:
            return urlopen(Request(url, headers={'User-Agent': 'AGH-Sentinel-Lab/3.0'}), timeout=30)
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise
            # Bounded backoff for this small lab; Retry-After support is an extension.
            time.sleep(2 ** attempt)
        except (URLError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)

def fetch_live_json(url):
    with open_http(url) as response:
        return json.load(response)

def iter_pages(first_url, fetch_json, max_pages=MAX_PAGES):
    if max_pages < 1:
        raise ValueError('max_pages must be positive')
    url, seen, count = first_url, set(), 0
    while url is not None:
        if url in seen:
            raise RuntimeError('Repeated next URL')
        if count >= max_pages:
            raise RuntimeError('Page cap reached before search completion')
        seen.add(url)
        page = fetch_json(url)
        if not isinstance(page.get('features'), list):
            raise ValueError('Expected a STAC FeatureCollection with features')
        count += 1
        yield page
        links = [link for link in page.get('links', []) if link.get('rel') == 'next']
        if len(links) > 1:
            raise ValueError('Ambiguous next links')
        if not links:
            url = None
        else:
            link = links[0]
            if link.get('method', 'GET').upper() != 'GET':
                raise ValueError('Only GET pagination is implemented in this lab')
            url = urljoin(url, link['href'])

def discover(conn, spec, initial_start, until, overlap_days, page_source):
    if overlap_days < 0:
        raise ValueError('Overlap must be nonnegative')
    query_json = json.dumps(spec, sort_keys=True, separators=(',', ':'))
    key = hashlib.sha256(query_json.encode()).hexdigest()
    initial = utc_parse(initial_start)
    upper = utc_parse(until)
    row = conn.execute('SELECT watermark FROM streams WHERE stream_key=?', (key,)).fetchone()
    watermark = utc_parse(row['watermark']) if row else initial
    if initial > watermark:
        raise ValueError('Changed initial start: use a fresh stream or preserve the original start')
    if upper < watermark:
        raise ValueError('Do not move the watermark backwards')
    start = max(initial, watermark - timedelta(days=overlap_days))
    start_text, upper_text = utc_text(start), utc_text(upper)
    with conn:
        conn.execute('INSERT INTO streams(stream_key, query_json, watermark) VALUES (?, ?, ?) ON CONFLICT(stream_key) DO NOTHING',
                     (key, query_json, utc_text(watermark)))
    pages, sightings = 0, 0
    for page in page_source(start_text, upper_text):
        with conn:
            for item in page['features']:
                store_item(conn, item)
                sightings += 1  # Includes repeat sightings, not unique items.
        pages += 1
    with conn:
        conn.execute('UPDATE streams SET watermark=? WHERE stream_key=?', (upper_text, key))
    return {'stream_key': key, 'start': start_text, 'until': upper_text,
            'pages': pages, 'item_sightings': sightings}

def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()

def download_asset(conn, collection, item_id, asset_key, directory,
                   open_url=open_http, max_bytes=MAX_BYTES):
    # The course starter permits exactly one preview asset per call.
    if asset_key != 'thumbnail':
        raise ValueError('Only the thumbnail JPEG is supported in this lab')
    if max_bytes <= 0:
        raise ValueError('max_bytes must be positive')
    row = conn.execute('SELECT assets_json FROM scenes WHERE collection=? AND item_id=?',
                       (collection, item_id)).fetchone()
    if row is None:
        raise KeyError('Discover the item before downloading')
    asset = json.loads(row['assets_json'])[asset_key]
    if asset.get('type') != 'image/jpeg':
        raise ValueError('The selected preview must be declared image/jpeg')
    url = asset['href']
    if urlsplit(url).scheme != 'https':
        raise ValueError('This downloader accepts public HTTPS assets only')
    identity = (collection, item_id, asset_key)
    previous = conn.execute('SELECT * FROM downloads WHERE collection=? AND item_id=? AND asset_key=?', identity).fetchone()
    if previous and previous['status'] == 'downloaded' and previous['source_url'] == url:
        saved = Path(previous['local_path'])
        if (saved.is_file() and saved.stat().st_size == previous['size_bytes']
                and file_sha256(saved) == previous['sha256']):
            return str(saved)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    suffix = '.jpg'
    final = (directory / (name + suffix)).resolve()
    part = final.with_name(final.name + '.part')
    with conn:
        conn.execute("""INSERT INTO downloads
            (collection, item_id, asset_key, source_url, status, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            ON CONFLICT(collection, item_id, asset_key) DO UPDATE SET
            source_url=excluded.source_url, status='pending', local_path=NULL,
            size_bytes=NULL, sha256=NULL, error=NULL, updated_at=excluded.updated_at""",
            (*identity, url, now_utc()))
    try:
        digest, total = hashlib.sha256(), 0
        with open_url(url) as response:
            raw_length = response.headers.get('Content-Length')
            expected = int(raw_length) if raw_length is not None else None
            if expected is not None and (expected < 0 or expected > max_bytes):
                raise ValueError('Announced asset size exceeds cap or is invalid')
            with part.open('wb') as output:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError('Streaming asset size exceeds cap')
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
        if total == 0 or (expected is not None and total != expected):
            raise IOError('Empty or incomplete asset transfer')
        with part.open('rb') as probe:
            beginning = probe.read(2)
            probe.seek(-2, os.SEEK_END)
            ending = probe.read(2)
        if beginning != b'\xff\xd8' or ending != b'\xff\xd9':
            raise IOError('Response is not a complete JPEG')
        os.replace(part, final)
        with conn:
            conn.execute("""UPDATE downloads SET status='downloaded', local_path=?,
                size_bytes=?, sha256=?, error=NULL, updated_at=?
                WHERE collection=? AND item_id=? AND asset_key=?""",
                (str(final), total, digest.hexdigest(), now_utc(), *identity))
        return str(final)
    except Exception as exc:
        part.unlink(missing_ok=True)
        with conn:
            conn.execute("""UPDATE downloads SET status='failed', error=?, updated_at=?
                WHERE collection=? AND item_id=? AND asset_key=?""",
                (f'{type(exc).__name__}: {exc}'[:1000], now_utc(), *identity))
        raise
