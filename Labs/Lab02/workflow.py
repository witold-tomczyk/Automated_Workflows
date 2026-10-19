"""Supplied orchestration around the repaired Lab 01 discovery service."""
import contextlib
import json
import logging
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
import lab01_service as service

LOG = logging.getLogger('sentinel')
RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, stream_key TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
 search_start TEXT, search_until TEXT NOT NULL, pages INTEGER NOT NULL DEFAULT 0,
 sightings INTEGER NOT NULL DEFAULT 0, new_scenes INTEGER NOT NULL DEFAULT 0,
 latest_acquired TEXT, preview_item TEXT, preview_path TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS stream_scenes (
 stream_key TEXT NOT NULL, collection TEXT NOT NULL, item_id TEXT NOT NULL,
 first_run_id TEXT NOT NULL,
 PRIMARY KEY (stream_key, collection, item_id),
 FOREIGN KEY (collection, item_id) REFERENCES scenes(collection, item_id)
);
"""

def stream_spec(config):
    return {k: config[k] for k in ('endpoint', 'collection', 'bbox')}

def stream_key(config):
    import hashlib
    text = json.dumps(stream_spec(config), sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode()).hexdigest()

def validate_config(config):
    required = {'endpoint','collection','bbox','initial_start','overlap_days','page_size','max_pages','database','preview_directory','log_file'}
    if set(config) != required:
        raise ValueError(f'Configuration keys must be {sorted(required)}; received {sorted(config)}')
    url = urlsplit(config['endpoint'])
    if url.scheme != 'https' or not url.netloc or url.query or url.fragment:
        raise ValueError('endpoint must be an HTTPS base URL without query or fragment')
    if not isinstance(config['collection'], str) or not config['collection'].strip():
        raise ValueError('collection must be a nonempty string')
    bbox = config['bbox']
    if not isinstance(bbox,list) or len(bbox)!=4 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in bbox):
        raise ValueError('bbox needs four finite numbers: west, south, east, north')
    w,s,e,n = bbox
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError('bbox is outside bounds or reversed; antimeridian boxes are out of scope')
    service.utc_parse(config['initial_start'])
    for key,minimum in [('overlap_days',0),('page_size',1),('max_pages',1)]:
        value = config[key]
        if isinstance(value,bool) or not isinstance(value,int) or value < minimum:
            raise ValueError(f'{key} must be an integer >= {minimum}')
    for key in ['database','preview_directory','log_file']:
        if not isinstance(config[key],str) or not config[key].strip():
            raise ValueError(f'{key} must be a nonempty path string')
    return config

def load_config(path):
    with Path(path).open(encoding='utf-8') as f:
        return validate_config(json.load(f))

def live_source(config):
    def pages(start, until):
        query = {'collections':config['collection'], 'bbox':','.join(map(str,config['bbox'])),
                 'datetime':f'{start}/{until}', 'limit':config['page_size']}
        url = config['endpoint'].rstrip('/')+'/search?'+urlencode(query)
        yield from service.iter_pages(url, service.fetch_live_json, config['max_pages'])
    return pages

def connect(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    conn = service.connect_db(path)
    conn.executescript(RUN_SCHEMA)
    return conn

def configure_logging(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)sZ %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(path,encoding='utf-8'),logging.StreamHandler()],force=True)
    # Logging timestamps use UTC, like acquisition and query timestamps.
    import time
    logging.Formatter.converter = time.gmtime

def run_discovery(config, paths, until, preview=False, lookback_days=None, page_source=None, open_url=None):
    """One bounded run. Default is live HTTP, never an implicit offline fallback."""
    key = stream_key(config)
    run_id = str(uuid.uuid4())
    overlap = config['overlap_days'] if lookback_days is None else lookback_days
    if isinstance(overlap,bool) or not isinstance(overlap,int) or overlap < 0:
        raise ValueError('lookback-days must be a nonnegative integer')
    source = page_source if page_source is not None else live_source(config)
    with contextlib.closing(connect(paths['database'])) as conn:
        row = conn.execute('SELECT watermark FROM streams WHERE stream_key=?',(key,)).fetchone()
        initial = service.utc_parse(config['initial_start'])
        watermark = service.utc_parse(row['watermark']) if row else initial
        upper = service.utc_parse(until)
        if upper < watermark or initial > watermark:
            raise ValueError('Search end/start conflicts with saved progress; preserve initial_start and do not move backwards')
        lower = service.utc_text(max(initial,watermark-timedelta(days=overlap)))
        with conn:
            conn.execute('INSERT INTO runs(run_id,stream_key,started_at,status,search_start,search_until) VALUES (?,?,?,?,?,?)',
                         (run_id,key,service.now_utc(),'running',lower,until))
        counters = {'pages':0,'sightings':0}
        def observed_source(start, stop):
            LOG.info('run=%s search=%s/%s',run_id,start,stop)
            for page in source(start,stop):
                # discover commits this page before resuming this generator.
                yield page
                with conn:
                    for item in page['features']:
                        conn.execute('INSERT OR IGNORE INTO stream_scenes VALUES (?,?,?,?)',
                                     (key,item['collection'],item['id'],run_id))
                counters['pages'] += 1
                counters['sightings'] += len(page['features'])
                LOG.info('run=%s committed_page=%d sightings=%d',run_id,counters['pages'],counters['sightings'])
        def summary():
            new = conn.execute('SELECT COUNT(*) FROM stream_scenes WHERE first_run_id=?',(run_id,)).fetchone()[0]
            latest = conn.execute('''SELECT MAX(s.acquired_at) FROM scenes s JOIN stream_scenes m
                USING(collection,item_id) WHERE m.stream_key=?''',(key,)).fetchone()[0]
            return new,latest
        selected_id,selected_path = None,None
        try:
            result = service.discover(conn,stream_spec(config),config['initial_start'],until,overlap,observed_source)
            # Only candidates actually associated with this geographic query are considered.
            if preview:
                rows = conn.execute('''SELECT s.* FROM scenes s JOIN stream_scenes m USING(collection,item_id)
                    WHERE m.stream_key=? ORDER BY s.acquired_at DESC,s.item_id DESC''',(key,))
                for row in rows:
                    asset = json.loads(row['assets_json']).get('thumbnail',{})
                    if asset.get('type')=='image/jpeg' and urlsplit(asset.get('href','')).scheme=='https':
                        selected_id = row['item_id']
                        args = {} if open_url is None else {'open_url':open_url}
                        selected_path = service.download_asset(conn,row['collection'],row['item_id'],'thumbnail',paths['preview_directory'],**args)
                        break  # At most ONE preview per invocation, including a failed transfer.
                if selected_id is None:
                    LOG.info('run=%s no suitable JPEG preview; metadata discovery succeeded',run_id)
            new,latest = summary()
            with conn:
                conn.execute('''UPDATE runs SET finished_at=?,status='succeeded',pages=?,sightings=?,new_scenes=?,
                    latest_acquired=?,preview_item=?,preview_path=? WHERE run_id=?''',
                    (service.now_utc(),counters['pages'],counters['sightings'],new,latest,selected_id,selected_path,run_id))
            LOG.info('run=%s succeeded new_scenes=%d sightings=%d latest=%s',run_id,new,counters['sightings'],latest)
            return dict(conn.execute('SELECT * FROM runs WHERE run_id=?',(run_id,)).fetchone())
        except Exception as exc:
            new,latest = summary()
            with conn:
                conn.execute('''UPDATE runs SET finished_at=?,status='failed',pages=?,sightings=?,new_scenes=?,
                    latest_acquired=?,preview_item=?,preview_path=?,error=? WHERE run_id=?''',
                    (service.now_utc(),counters['pages'],counters['sightings'],new,latest,selected_id,selected_path,
                     f'{type(exc).__name__}: {exc}'[:1000],run_id))
            LOG.exception('run=%s failed; inspect run history and discovery watermark',run_id)
            raise

def report(config, paths):
    if not paths['database'].is_file():
        raise FileNotFoundError('No catalogue yet: run discover first')
    with contextlib.closing(connect(paths['database'])) as conn:
        key = stream_key(config)
        row = conn.execute('SELECT watermark FROM streams WHERE stream_key=?',(key,)).fetchone()
        latest = conn.execute('''SELECT MAX(s.acquired_at),COUNT(*) FROM scenes s
            JOIN stream_scenes m USING(collection,item_id) WHERE m.stream_key=?''',(key,)).fetchone()
        now = datetime.now(timezone.utc)
        return {'stream_key':key,'database':str(paths['database']), 'unique_scenes':latest[1],
                'latest_acquired':latest[0],
                'latest_age_hours':round((now-service.utc_parse(latest[0])).total_seconds()/3600,2) if latest[0] else None,
                'watermark':row['watermark'] if row else None,
                'runs':[dict(r) for r in conn.execute('SELECT * FROM runs WHERE stream_key=? ORDER BY started_at, rowid',(key,))]}
