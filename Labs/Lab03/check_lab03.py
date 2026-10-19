"""Offline checks: your schema, real AOI GeoTIFFs, cache integrity and range refusal."""
import argparse,contextlib,copy,hashlib,json,sqlite3,tempfile
from pathlib import Path
import lab03_store as store
import lab03_service as service
import geo_subset as geo
import range_reader
ROOT=Path(__file__).resolve().parent

def reject(c,label,sql,args=()):
    c.execute('SAVEPOINT test_rejection')
    try:
        c.execute(sql,args)
    except sqlite3.IntegrityError:
        pass
    else:raise AssertionError(label+' was accepted; add the appropriate key/CHECK constraint')
    finally:
        c.execute('ROLLBACK TO test_rejection');c.execute('RELEASE test_rejection')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--schema',default=str(ROOT/'student_schema.sql'))
    args=parser.parse_args();groups=[]
    try:
        cfg=service.load_config(ROOT/'config'/'krakow_aoi.json');cfg['_schema']=Path(args.schema).resolve()
        with tempfile.TemporaryDirectory() as d:
            cfg['_database']=Path(d)/'catalogue.sqlite';cfg['_data']=Path(d)/'data'
            store.initialize(cfg)
            groups.append('five complete table definitions and column contract')
            with contextlib.closing(store.open_db(cfg['_database'])) as c:
                run=store.start_run(c,cfg,'schema-check')
                item,plans=service.plan(c,cfg)
                ids=[]
                for p in plans:ids.append(store.register_request(c,item,p['asset_key'],cfg['aoi_bbox'],geo.RECIPE))
                subset,aoi,asset=ids[0]
                required_pk={'assets':['asset_id'],'subset_requests':['subset_id'],'download_attempts':['attempt_id'],
                             'artifacts':['artifact_id'],'artifact_locations':['location_id']}
                for table,key in required_pk.items():
                    actual=[r['name'] for r in c.execute(f'PRAGMA table_info({table})') if r['pk']]
                    assert actual==key,f'{table}: give each row the required primary key'
                reject(c,'orphan source scene',"INSERT INTO assets(asset_id,collection,item_id,asset_key,source_url,metadata_json,metadata_sha256,created_at) VALUES ('orphan','bad','missing','red','https://example.invalid','{}','x',?)",(store.now(),))
                reject(c,'orphan source asset',"INSERT INTO subset_requests(subset_id,asset_id,aoi_key,aoi_json,recipe_version,status,latest_artifact_id,updated_at) VALUES ('orphan','missing','a','{}','v','pending',NULL,?)",(store.now(),))
                reject(c,'orphan run',"INSERT INTO download_attempts(attempt_id,subset_id,run_id,status,stage,started_at,network_bytes) VALUES ('orphan',?,'missing','running','test',?,0)",(subset,store.now()))
                groups.append('primary keys and scene/asset/request/run foreign keys')
                # Two AOIs of one band must coexist; the same request identity must not repeat.
                alternate=[19.92,50.045,19.935,50.055]
                other=store.register_request(c,item,'red',alternate,geo.RECIPE)[0]
                assert other!=subset
                reject(c,'duplicate asset/AOI/recipe',"INSERT INTO subset_requests SELECT 'duplicate',asset_id,aoi_key,aoi_json,recipe_version,status,latest_artifact_id,updated_at FROM subset_requests WHERE subset_id=?",(subset,))
                reject(c,'invalid request status',"UPDATE subset_requests SET status='finished_maybe' WHERE subset_id=?",(subset,))
                reject(c,'ready request without an artifact',"UPDATE subset_requests SET status='ready' WHERE subset_id=?",(subset,))
                groups.append('AOI identity, uniqueness and allowed states')
                at=store.start_attempt(c,subset,run,'schema-check')
                reject(c,'negative byte count',"UPDATE download_attempts SET network_bytes=-1 WHERE attempt_id=?",(at,))
                reject(c,'finished running attempt',"UPDATE download_attempts SET finished_at=? WHERE attempt_id=?",(store.now(),at))
                reject(c,'successful attempt without artifact',"UPDATE download_attempts SET status='succeeded',finished_at=? WHERE attempt_id=?",(store.now(),at))
                groups.append('attempt outcomes, timestamps and byte counts')
                manifest=json.loads((ROOT/'fixtures'/'aoi_manifest.json').read_text())
                f=manifest['red'];plan=f['plan'];source=ROOT/'fixtures'/f['file']
                assert store.file_hash(source)==f['sha256'];geo.validate_window(source,plan)
                artifact=store.digest([subset,f['sha256']])
                with c:
                    c.execute('''INSERT INTO artifacts(artifact_id,subset_id,created_by_attempt_id,sha256,size_bytes,raster_json,created_at)
                               VALUES (?,?,?,?,?,?,?)''',(artifact,subset,at,f['sha256'],f['size_bytes'],store.canonical(plan),store.now()))
                    c.execute("UPDATE subset_requests SET latest_artifact_id=?,status='ready' WHERE subset_id=?",(artifact,subset))
                    c.execute("UPDATE download_attempts SET artifact_id=?,status='succeeded',finished_at=? WHERE attempt_id=?",(artifact,store.now(),at))
                reject(c,'artifact attached to another AOI',"UPDATE subset_requests SET latest_artifact_id=? WHERE subset_id=?",(artifact,other))
                attempt_other=store.start_attempt(c,other,run,'schema-check')
                reject(c,'attempt referencing another request artifact',"UPDATE download_attempts SET artifact_id=? WHERE attempt_id=?",(artifact,attempt_other))
                groups.append('composite relationships prevent cross-AOI artifact references')
                dest=Path(d)/'real_red_aoi.tif';dest.write_bytes(source.read_bytes())
                with c:c.execute('INSERT INTO artifact_locations(location_id,artifact_id,local_path,status,checked_at) VALUES (?,?,?,\'available\',?)',('location',artifact,str(dest),store.now()))
                valid=service.verify_one(c,run,subset,plan);assert valid['outcome']=='reused'
                dest.unlink()
                missing=service.verify_one(c,run,subset,plan);assert missing['error_code']=='FILE_MISSING'
                assert c.execute('SELECT status FROM artifact_locations').fetchone()[0]=='missing'
                dest.write_bytes(source.read_bytes()[:1000])
                corrupt=service.verify_one(c,run,subset,plan);assert corrupt['error_code']=='FILE_CORRUPT'
                assert c.execute('SELECT status FROM download_attempts WHERE attempt_id=?',(at,)).fetchone()[0]=='succeeded'
                assert not list(c.execute('PRAGMA foreign_key_check'))
                groups.append('real-file verification, missing/corrupt diagnosis and preserved success history')
                for band in ['red','nir']:
                    f=manifest[band];geo.validate_window(ROOT/'fixtures'/f['file'],f['plan'])
                geo.matching_pair([manifest[b]['plan'] for b in ['red','nir']])
                try:geo.make_plan(item,'red',[19.7,49.9,20.1,50.2],cfg['max_pixels_per_band'])
                except ValueError:pass
                else:raise AssertionError('Large AOI was not rejected before raster reading')
                groups.append('real B04/B08 grids, AOI masking, scale/offset and pixel cap')
        old=range_reader.urlopen
        class IgnoredRange:
            status=200;headers={}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):raise AssertionError('An ignored Range response body must never be read')
        try:
            range_reader.urlopen=lambda *a,**kw:IgnoredRange()
            try:range_reader.RangeSession('https://example.invalid/band.tif').get_size()
            except range_reader.RangeFailure:pass
            else:raise AssertionError('Full tile fallback was allowed')
        finally:range_reader.urlopen=old
        groups.append('server ignoring Range is refused without reading its body')
    except Exception as exc:
        for g in groups:print('PASS:',g)
        print('FAIL:',type(exc).__name__+':',exc)
        return 1
    for g in groups:print('PASS:',g)
    print('All checks used actual captured EO data; no live network requests were made.')
    return 0

if __name__=='__main__':raise SystemExit(main())
