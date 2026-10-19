"""Supplied acquisition service; only two bands of one scene per invocation."""
import contextlib,json,logging,math,os,shutil,sqlite3,uuid
from pathlib import Path
from urllib.parse import urlsplit,quote
import numpy as np
import rasterio
import geo_subset as geo
import lab03_store as store
from range_reader import RangeSession,RangeFailure
LOG=logging.getLogger('lab03')

def load_config(path):
    path=Path(path).resolve();cfg=json.loads(path.read_text(encoding='utf-8-sig'))
    bbox=cfg['aoi_bbox']
    if (not isinstance(bbox,list) or len(bbox)!=4 or any(isinstance(v,bool) or not isinstance(v,(float,int)) or not math.isfinite(v) for v in bbox)):
        raise ValueError('AOI must be four finite numbers: west, south, east, north')
    w,s,e,n=bbox
    if not(-180<=w<e<=180 and -90<=s<n<=90):raise ValueError('Invalid AOI bounds')
    if cfg['bands']!=['red','nir']:raise ValueError('This lab stores exactly red and nir, in that order')
    for key in ['max_pixels_per_band','range_byte_budget_per_band','max_output_bytes_per_band','workspace_budget_bytes']:
        if not isinstance(cfg[key],int) or isinstance(cfg[key],bool) or cfg[key]<=0:raise ValueError(f'{key} must be a positive integer')
    for key,name in [('database','_database'),('data_directory','_data'),('schema','_schema'),('log_file','_log')]:
        cfg[name]=(path.parent/cfg[key]).resolve()
    cfg['_config_path']=path
    return cfg

def setup_log(cfg):
    logging.getLogger('rasterio').setLevel(logging.WARNING)
    cfg['_log'].parent.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(cfg['_log'],encoding='utf-8'),logging.StreamHandler()],force=True)

def choose_item(c,cfg,override=None):
    wanted=override or cfg.get('item_id')
    if not wanted:raise ValueError('Choose and pin one item_id from the catalogue before downloading bands')
    row=c.execute('SELECT * FROM scenes WHERE collection=? AND item_id=?',(cfg['collection'],wanted)).fetchone()
    if row is None:raise ValueError('Chosen scene is not in this database; discover or inspect scenes first')
    # Lab 02 stores asset JSON, enough metadata for native windows; complete metadata is retained for the default fixture.
    assets=json.loads(row['assets_json'])
    item={'id':row['item_id'],'collection':row['collection'],'properties':{'datetime':row['acquired_at']},'assets':assets}
    fixture=Path(__file__).parent/'fixtures'/'real_catalogue.json'
    snapshot=json.loads(fixture.read_text(encoding='utf-8'))
    match=next((i for p in snapshot['pages'] for i in p['features'] if i['id']==wanted and i['collection']==cfg['collection']),None)
    if match:
        item['properties']=match['properties']
    if 'proj:epsg' not in item['properties'] and 'proj:code' not in item['properties']:
        url=cfg['endpoint'].rstrip('/')+'/collections/'+quote(cfg['collection'],safe='')+'/items/'+quote(wanted,safe='')
        item=__import__('lab01_service').fetch_live_json(url)
        if item['id']!=wanted or item['collection']!=cfg['collection']:raise ValueError('Unexpected item metadata response')
    return item

def plan(c,cfg,item_id=None):
    item=choose_item(c,cfg,item_id)
    plans=[geo.make_plan(item,k,cfg['aoi_bbox'],cfg['max_pixels_per_band']) for k in cfg['bands']]
    geo.matching_pair(plans)
    return item,plans

def available_artifact(c,subset_id):
    return c.execute('''SELECT f.*,l.location_id,l.local_path,l.status AS location_status
          FROM subset_requests s JOIN artifacts f ON f.artifact_id=s.latest_artifact_id
          JOIN artifact_locations l ON l.artifact_id=f.artifact_id WHERE s.subset_id=?
          ORDER BY l.checked_at DESC LIMIT 1''',(subset_id,)).fetchone()

def verify_one(c,run_id,subset_id,plan,record_success=True):
    row=available_artifact(c,subset_id)
    if row is None:return None
    attempt=store.start_attempt(c,subset_id,run_id,'cache_verify')
    path=Path(row['local_path']);code=None
    try:
        if not path.is_file():
            code='FILE_MISSING';raise FileNotFoundError(f'Recorded file is absent: {path.name}')
        if path.stat().st_size!=row['size_bytes'] or store.file_hash(path)!=row['sha256']:
            code='FILE_CORRUPT';raise ValueError(f'Size/hash mismatch for {path.name}')
        geo.validate_window(path,json.loads(row['raster_json']))
        # Includes exact request and source plan, so changed AOIs cannot reuse this record.
        if json.loads(row['raster_json'])!=plan:raise ValueError('Cached raster plan differs from the current request')
        with c:
            c.execute("UPDATE artifact_locations SET status='available',checked_at=? WHERE location_id=?",(store.now(),row['location_id']))
            c.execute("UPDATE subset_requests SET status='ready',updated_at=? WHERE subset_id=?",(store.now(),subset_id))
        store.end_attempt(c,attempt,'reused',artifact_id=row['artifact_id'])
        return {'subset_id':subset_id,'artifact_id':row['artifact_id'],'path':str(path),'outcome':'reused','network_bytes':0}
    except Exception as exc:
        code=code or 'FILE_INVALID'
        with c:
            c.execute('UPDATE artifact_locations SET status=?,checked_at=? WHERE location_id=?',
                      ('missing' if code=='FILE_MISSING' else 'corrupt',store.now(),row['location_id']))
            c.execute("UPDATE subset_requests SET status='failed',updated_at=? WHERE subset_id=?",(store.now(),subset_id))
        store.end_attempt(c,attempt,'failed',artifact_id=row['artifact_id'],error_code=code,error=exc)
        LOG.warning('attempt=%s code=%s %s',attempt,code,exc)
        return {'subset_id':subset_id,'artifact_id':row['artifact_id'],'outcome':'failed','error_code':code}

def workspace_check(cfg):
    root=cfg['_data'];root.mkdir(parents=True,exist_ok=True)
    used=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
    reserve=2*cfg['max_output_bytes_per_band']
    if used+reserve>cfg['workspace_budget_bytes']:raise ValueError('Workspace disk budget would be exceeded')
    if shutil.disk_usage(root).free<reserve+32*1024*1024:raise OSError('Insufficient free disk space')

def acquire_one(c,cfg,run_id,item,plan,subset_id,aoi_key,asset_id,simulate=None):
    cached=verify_one(c,run_id,subset_id,plan)
    if cached and cached['outcome']=='reused':return cached
    workspace_check(cfg)
    attempt=store.start_attempt(c,subset_id,run_id,'read_window')
    temporary=None
    session=RangeSession(plan['source_url'],cfg['range_byte_budget_per_band'],simulate=simulate=='network')
    try:
        directory=cfg['_data']/item['id']/aoi_key[:12]
        directory.mkdir(parents=True,exist_ok=True)
        temporary=directory/(plan['asset_key']+'-'+attempt+'.part.tif')
        LOG.info('attempt=%s band=%s window=%s starting bounded ranges',attempt,plan['asset_key'],plan['window'])
        geo.write_window(plan,temporary,session,fail_before_finalize=simulate=='interrupt',max_output_bytes=cfg['max_output_bytes_per_band'],on_stage=lambda value:store.stage(c,attempt,value))
        store.stage(c,attempt,'finalize')
        sha=store.file_hash(temporary);size=temporary.stat().st_size
        artifact_id=store.digest([subset_id,sha])
        final=directory/(plan['asset_key']+'-'+artifact_id[:16]+'.tif')
        os.replace(temporary,final)
        store.stage(c,attempt,'register')
        with c:
            c.execute('''INSERT OR IGNORE INTO artifacts(artifact_id,subset_id,created_by_attempt_id,sha256,size_bytes,raster_json,created_at)
                         VALUES (?,?,?,?,?,?,?)''',(artifact_id,subset_id,attempt,sha,size,store.canonical(plan),store.now()))
            location_id=store.digest([artifact_id,str(final)])
            c.execute('''INSERT INTO artifact_locations(location_id,artifact_id,local_path,status,checked_at)
                         VALUES (?,?,?,'available',?) ON CONFLICT(location_id) DO UPDATE SET status='available',checked_at=excluded.checked_at''',
                      (location_id,artifact_id,str(final),store.now()))
            c.execute("UPDATE subset_requests SET status='ready',latest_artifact_id=?,updated_at=? WHERE subset_id=?",(artifact_id,store.now(),subset_id))
            c.execute('''UPDATE download_attempts SET status='succeeded',stage='complete',finished_at=?,network_bytes=?,artifact_id=?,http_status=?
                         WHERE attempt_id=?''',(store.now(),session.network_bytes,artifact_id,session.last_http_status,attempt))
        LOG.info('attempt=%s succeeded network_bytes=%d saved_bytes=%d',attempt,session.network_bytes,size)
        return {'subset_id':subset_id,'artifact_id':artifact_id,'path':str(final),'outcome':'succeeded','network_bytes':session.network_bytes,'saved_bytes':size}
    except Exception as exc:
        if temporary is not None:temporary.unlink(missing_ok=True)
        code='CONTROLLED_INTERRUPT' if isinstance(exc,InterruptedError) else 'RANGE_READ_FAILED' if isinstance(exc,RangeFailure) else 'SUBSET_FAILED'
        store.end_attempt(c,attempt,'failed',network_bytes=session.network_bytes,error_code=code,error=exc,http_status=session.last_http_status)
        with c:c.execute("UPDATE subset_requests SET status='failed',updated_at=? WHERE subset_id=?",(store.now(),subset_id))
        raise

def operate(cfg,command='acquire',item_id=None,simulate=None,simulate_band='nir'):
    if not cfg['_database'].is_file():raise FileNotFoundError('Run init before acquiring data')
    with contextlib.closing(store.open_db(cfg['_database'])) as c:
        store.check_shape(c)
        run=store.start_run(c,cfg,command)
        outputs=[]
        try:
            item,plans=plan(c,cfg,item_id)
            for p in plans:
                subset,aoi,asset=store.register_request(c,item,p['asset_key'],cfg['aoi_bbox'],geo.RECIPE)
                if command=='verify':
                    result=verify_one(c,run,subset,p)
                    if result is None:result={'subset_id':subset,'outcome':'not_acquired'}
                else:
                    result=acquire_one(c,cfg,run,item,p,subset,aoi,asset,simulate if p['asset_key']==simulate_band else None)
                outputs.append(result)
            if any(o['outcome'] in ('failed','not_acquired') for o in outputs):
                raise ValueError('One or more subsets are missing or invalid; inspect history and rerun acquire')
            store.finish_run(c,run)
            return {'run_id':run,'status':'succeeded','outputs':outputs}
        except Exception as exc:
            store.finish_run(c,run,exc)
            LOG.error('run=%s failed: %s',run,exc)
            raise

def report(cfg):
    with contextlib.closing(store.open_db(cfg['_database'])) as c:
        store.check_shape(c)
        return {'database':str(cfg['_database']),'attempts':store.history(c),
          'subsets':[dict(r) for r in c.execute('SELECT subset_id,asset_id,aoi_key,status,latest_artifact_id FROM subset_requests')],
          'locations':[dict(r) for r in c.execute('SELECT * FROM artifact_locations')],
          'runs':[dict(r) for r in c.execute("SELECT run_id,command,status,error,started_at FROM runs WHERE run_id LIKE 'lab03-%' ORDER BY rowid")]}
