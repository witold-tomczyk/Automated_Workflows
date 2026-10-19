"""Supplied database operations. New table definitions come only from student_schema.sql."""
import contextlib,hashlib,json,sqlite3,uuid
from datetime import datetime,timezone
from pathlib import Path
import lab01_service
import legacy_workflow

def now():return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value):return hashlib.sha256(value if isinstance(value,bytes) else canonical(value).encode()).hexdigest()
def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(65536),b''):h.update(b)
    return h.hexdigest()

COLUMNS={
 'assets':{'asset_id','collection','item_id','asset_key','source_url','metadata_json','metadata_sha256','created_at'},
 'subset_requests':{'subset_id','asset_id','aoi_key','aoi_json','recipe_version','status','latest_artifact_id','updated_at'},
 'download_attempts':{'attempt_id','subset_id','run_id','status','stage','started_at','finished_at','network_bytes','http_status','artifact_id','error_code','error_message'},
 'artifacts':{'artifact_id','subset_id','created_by_attempt_id','sha256','size_bytes','raster_json','created_at'},
 'artifact_locations':{'location_id','artifact_id','local_path','status','checked_at'}
}

def open_db(path):
    c=sqlite3.connect(path);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
    return c

def check_shape(c):
    errors=[]
    for table,required in COLUMNS.items():
        present={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
        if not present:errors.append(f'missing table {table}')
        elif missing:=required-present:errors.append(f'{table}: missing '+', '.join(sorted(missing)))
    if errors:raise ValueError('Complete student_schema.sql and run init. '+'; '.join(errors))
    if c.execute('PRAGMA foreign_keys').fetchone()[0]!=1:raise ValueError('Foreign key enforcement must be ON')

def initialize(config,from_lab2=None):
    db=config['_database'];db.parent.mkdir(parents=True,exist_ok=True)
    if from_lab2 is not None:
        source=Path(from_lab2).resolve()
        if not source.is_file():raise FileNotFoundError(source)
        if db.exists():raise FileExistsError('Destination exists; omit --from-lab2 after the first copy')
        with contextlib.closing(sqlite3.connect(f'{source.as_uri()}?mode=ro',uri=True)) as old:
            with contextlib.closing(sqlite3.connect(db)) as new:old.backup(new)
    with contextlib.closing(legacy_workflow.connect(db)) as c:
        # Two supplied additions to the existing runs table, not new student tables.
        existing={r['name'] for r in c.execute('PRAGMA table_info(runs)')}
        if 'command' not in existing:c.execute("ALTER TABLE runs ADD COLUMN command TEXT NOT NULL DEFAULT 'discover'")
        if 'config_json' not in existing:c.execute("ALTER TABLE runs ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'")
        c.commit()
        existing_new={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}&set(COLUMNS)
        if not existing_new:
            script=config['_schema'].read_text(encoding='utf-8-sig')
            try:
                c.executescript('BEGIN;\n'+script)
                check_shape(c)
                c.commit()
            except Exception:
                c.rollback();raise
        check_shape(c)
        # Captured actual provider records only; no invented scenes or imagery.
        if c.execute('SELECT COUNT(*) FROM scenes').fetchone()[0]==0:
            snap=json.loads((Path(__file__).parent/'fixtures'/'real_catalogue.json').read_text(encoding='utf-8'))
            with c:
                for page in snap['pages']:
                    for item in page['features']:lab01_service.store_item(c,item)
        c.commit()
    return {'database':str(db),'message':'New schema present; run check_lab03.py to verify its constraints'}

def start_run(c,config,command):
    run_id='lab03-'+str(uuid.uuid4())
    spec={'endpoint':config['endpoint'],'collection':config['collection'],'bbox':config['discovery_bbox']}
    key=digest(spec);stamp=now()
    public={k:v for k,v in config.items() if not k.startswith('_')}
    public['resolved_database']=str(config['_database']);public['resolved_data_directory']=str(config['_data'])
    with c:
        c.execute('INSERT OR IGNORE INTO streams VALUES (?,?,?)',(key,canonical(spec),config['discovery_start']))
        c.execute('''INSERT INTO runs(run_id,stream_key,started_at,status,search_until,command,config_json)
                     VALUES (?,?,?,'running',?,?,?)''',(run_id,key,stamp,stamp,command,canonical(public)))
    return run_id

def finish_run(c,run_id,error=None):
    with c:c.execute('UPDATE runs SET status=?,finished_at=?,error=? WHERE run_id=?',
                    ('failed' if error else 'succeeded',now(),str(error)[:1000] if error else None,run_id))

def register_request(c,item,asset_key,bbox,recipe):
    meta=item['assets'][asset_key];meta_sha=digest(meta)
    asset_id=digest([item['collection'],item['id'],asset_key,meta_sha])
    aoi_key=digest({'crs':'EPSG:4326','bbox':bbox,'pixel_rule':'center-inside'})
    subset_id=digest([asset_id,aoi_key,recipe])
    with c:
        c.execute('''INSERT OR IGNORE INTO assets(asset_id,collection,item_id,asset_key,source_url,metadata_json,metadata_sha256,created_at)
                     VALUES (?,?,?,?,?,?,?,?)''',(asset_id,item['collection'],item['id'],asset_key,meta['href'],canonical(meta),meta_sha,now()))
        c.execute('''INSERT OR IGNORE INTO subset_requests(subset_id,asset_id,aoi_key,aoi_json,recipe_version,status,updated_at)
                     VALUES (?,?,?,?,?,'pending',?)''',(subset_id,asset_id,aoi_key,canonical({'crs':'EPSG:4326','bbox':bbox}),recipe,now()))
    return subset_id,aoi_key,asset_id

def start_attempt(c,subset_id,run_id,stage):
    attempt='attempt-'+str(uuid.uuid4())
    with c:c.execute('''INSERT INTO download_attempts(attempt_id,subset_id,run_id,status,stage,started_at,network_bytes)
                         VALUES (?,?,?,'running',?,?,0)''',(attempt,subset_id,run_id,stage,now()))
    return attempt

def stage(c,attempt_id,value):
    with c:c.execute('UPDATE download_attempts SET stage=? WHERE attempt_id=?',(value,attempt_id))

def end_attempt(c,attempt_id,status,network_bytes=0,artifact_id=None,error_code=None,error=None,http_status=None):
    with c:c.execute('''UPDATE download_attempts SET status=?,finished_at=?,network_bytes=?,artifact_id=?,error_code=?,error_message=?,http_status=?
                         WHERE attempt_id=?''',(status,now(),network_bytes,artifact_id,error_code,str(error)[:1000] if error else None,http_status,attempt_id))

def history(c):
    return [dict(r) for r in c.execute('''SELECT d.attempt_id,d.run_id,a.item_id,a.asset_key,s.aoi_key,d.status,d.stage,
       d.network_bytes,d.http_status,d.error_code,d.error_message,d.artifact_id,d.started_at
       FROM download_attempts d JOIN subset_requests s USING(subset_id) JOIN assets a USING(asset_id)
       ORDER BY d.started_at,d.attempt_id''')]
