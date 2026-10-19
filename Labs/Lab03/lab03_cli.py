"""Lab 03: explicit init, plan, acquisition, integrity verification and case studies."""
import argparse,contextlib,json,logging,shutil,sqlite3,sys
from pathlib import Path
import lab03_store as store
import lab03_service as service
import legacy_workflow


def make_case(cfg,name):
    case_root=Path(__file__).resolve().parent/'cases'/name
    if case_root.exists():raise FileExistsError(f'Case already exists. Inspect or recover with {case_root / "config.json"}')
    case_root.mkdir(parents=True)
    public={k:v for k,v in cfg.items() if not k.startswith('_')}
    public.update(database='state/catalogue.sqlite',data_directory='data',log_file='state/events.log',schema=str(cfg['_schema']),case_name=name)
    config_file=case_root/'config.json';config_file.write_text(json.dumps(public,indent=2)+'\n',encoding='utf-8')
    case=service.load_config(config_file)
    store.initialize(case)
    # Copy only scene metadata from the working project. All acquired files are local to this case.
    if cfg['_database'].is_file():
        with contextlib.closing(store.open_db(cfg['_database'])) as old,contextlib.closing(store.open_db(case['_database'])) as new:
            with new:
                for row in old.execute('SELECT * FROM scenes'):
                    new.execute('''INSERT OR REPLACE INTO scenes(collection,item_id,acquired_at,cloud_cover,assets_json,first_seen,last_seen)
                      VALUES (?,?,?,?,?,?,?)''',tuple(row[k] for k in ['collection','item_id','acquired_at','cloud_cover','assets_json','first_seen','last_seen']))
    service.setup_log(case)
    print('Case configuration:',config_file, file=sys.stderr)
    if name in ('network','interrupted'):
        return service.operate(case,simulate='network' if name=='network' else 'interrupt')
    service.operate(case)
    damage(case,'red','remove' if name=='missing' else 'truncate')
    return service.operate(case,command='verify')


def damage(cfg,band,mode):
    if not cfg.get('case_name'):raise ValueError('Damage is permitted only in a generated case workspace')
    with contextlib.closing(store.open_db(cfg['_database'])) as c:
        rows=c.execute('''SELECT l.local_path FROM subset_requests s JOIN assets a USING(asset_id)
           JOIN artifact_locations l ON l.artifact_id=s.latest_artifact_id WHERE a.asset_key=?''',(band,)).fetchall()
        if len(rows)!=1:raise ValueError('Expected one case file for this band')
        path=Path(rows[0]['local_path']).resolve()
        if not path.is_relative_to(cfg['_data']):raise ValueError('Refusing to change a file outside the case data folder')
        if mode=='remove':path.unlink()
        else:
            with path.open('r+b') as f:f.truncate(max(1,path.stat().st_size//2))
        return {'changed_case_file':str(path),'mode':mode}


def main(argv=None):
    root=argparse.ArgumentParser(description=__doc__)
    commands=root.add_subparsers(dest='command',required=True)
    for name in ['init','plan','acquire','verify','report','discover','case']:
        p=commands.add_parser(name);p.add_argument('--config',default='config/krakow_aoi.json')
        if name=='init':p.add_argument('--from-lab2',help='Optional original database to COPY using SQLite backup')
        if name in ['plan','acquire','verify']:p.add_argument('--item-id')
        if name=='acquire':
            p.add_argument('--simulate',choices=['network','interrupt']);p.add_argument('--simulate-band',choices=['red','nir'],default='nir')
        if name=='case':p.add_argument('--name',required=True,choices=['network','interrupted','missing','corrupt'])
        if name=='discover':p.add_argument('--until',help='Default is current UTC')
    args=root.parse_args(argv)
    try:
        cfg=service.load_config(args.config);service.setup_log(cfg)
        if args.command=='init':result=store.initialize(cfg,args.from_lab2)
        elif args.command=='plan':
            with contextlib.closing(store.open_db(cfg['_database'])) as c:
                store.check_shape(c);item,plans=service.plan(c,cfg,args.item_id)
                result={'item_id':item['id'],'plans':plans,'pair_raw_pixel_bytes':sum(p['raw_pixel_bytes'] for p in plans),
                        'note':'Storage estimate is uncompressed pixel payload; HTTP ranges include complete source blocks'}
        elif args.command in ['acquire','verify']:
            result=service.operate(cfg,args.command,args.item_id,getattr(args,'simulate',None),getattr(args,'simulate_band','nir'))
        elif args.command=='report':result=service.report(cfg)
        elif args.command=='case':result=make_case(cfg,args.name)
        elif args.command=='discover':
            with contextlib.closing(store.open_db(cfg['_database'])) as c:
                store.check_shape(c)
                legacy={'endpoint':cfg['endpoint'],'collection':cfg['collection'],'bbox':cfg['discovery_bbox'],'initial_start':cfg['discovery_start'],
                        'overlap_days':7,'page_size':20,'max_pages':100}
                paths={'database':cfg['_database'],'preview_directory':cfg['_data']/'previews','log_file':cfg['_log']}
            result=legacy_workflow.run_discovery(legacy,paths,args.until or store.now(),preview=False)
        print(json.dumps(result,indent=2));return 0
    except Exception as exc:
        logging.getLogger('lab03').error('%s: %s',type(exc).__name__,exc)
        print(json.dumps({'status':'failed','error':f'{type(exc).__name__}: {exc}'},indent=2));return 1

if __name__=='__main__':sys.exit(main())
