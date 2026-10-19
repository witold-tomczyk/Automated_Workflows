"""Checks use real captured provider records and an actual JPEG from Lab 01.
Only clocks, availability and transport failures are controlled. No synthetic imagery.
"""
import argparse
import contextlib
import copy
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import student_tasks as tasks
import workflow
import lab01_service as service
ROOT = Path(__file__).resolve().parent
SNAPSHOT = json.loads((ROOT/'fixtures'/'real_catalogue.json').read_text(encoding='utf-8'))
ITEMS = [item for page in SNAPSHOT['pages'] for item in page['features']]
CONFIG = workflow.load_config(ROOT/'config'/'krakow_week.json')
HISTORICAL = dict(CONFIG,initial_start='2025-06-01T00:00:00Z',overlap_days=3,page_size=2)
LOGGER = logging.getLogger('checks')
LOGGER.addHandler(logging.NullHandler())

def exercise1():
    with tempfile.TemporaryDirectory() as d:
        config_path = Path(d)/'project'/'config'/'query.json'
        config = dict(CONFIG,database='../state/catalogue.sqlite',preview_directory='../state/previews',log_file='../state/events.log')
        expected = (config_path.parent/'../state/catalogue.sqlite').resolve()
        got = tasks.resolve_paths(config,config_path)
        assert got['database']==expected, f'Database must be relative to config, not terminal: {got["database"]}'
        absolute = dict(config,database=str(Path(d)/'absolute.sqlite'))
        assert tasks.resolve_paths(absolute,config_path)['database']==Path(absolute['database'])
        assert not expected.exists(),'Resolving paths must not create a database'

def exercise2():
    captured_now = datetime(2026,10,5,9,0,tzinfo=timezone.utc)
    calls = []
    def clock():
        calls.append(1)
        return captured_now
    assert tasks.choose_until(None,clock)== '2026-10-05T09:00:00Z','Default end must use the current UTC clock'
    assert len(calls)==1,'Capture the upper bound exactly once'
    assert tasks.choose_until('2025-06-08T02:00:00+02:00',clock)=='2025-06-08T00:00:00Z'
    assert len(calls)==1,'An explicit end must not consult the clock'
    try:
        tasks.choose_until('2025-06-08T00:00:00')
    except ValueError:
        pass
    else:
        raise AssertionError('Timezone-free input must be rejected')

def exercise3():
    code,result = tasks.execute_job(lambda:{'status':'succeeded','new_scenes':0},LOGGER)
    assert code==0 and result['new_scenes']==0,'Zero new scenes is a valid success'
    def broken():
        raise ConnectionError('connection dropped')
    code,result = tasks.execute_job(broken,LOGGER)
    assert code!=0 and result['status']=='failed','A failed command must return a nonzero exit code'
    assert 'connection dropped' in result.get('error',''),'Keep a useful diagnostic'
    process = subprocess.run([sys.executable,str(ROOT/'sentinel_cli.py'),'discover','--config',str(ROOT/'does_not_exist.json')],capture_output=True,text=True)
    assert process.returncode!=0,'The actual CLI must propagate a failed command'
    assert json.loads(process.stdout)['status']=='failed'

def replay(items,fail_on_page=None):
    def source(start,stop):
        filtered = [copy.deepcopy(i) for i in items if service.utc_parse(start)<=service.utc_parse(i['properties']['datetime'])<=service.utc_parse(stop)]
        for offset in range(0,len(filtered),2):
            number = offset//2+1
            if number==fail_on_page:
                raise ConnectionError('Controlled interruption after a committed page')
            yield {'features':filtered[offset:offset+2], 'links':[]}
    return source

def paths_in(directory):
    return {'database':Path(directory)/'catalogue.sqlite','preview_directory':Path(directory)/'previews','log_file':Path(directory)/'events.log'}

def recovery():
    with tempfile.TemporaryDirectory() as d:
        paths = paths_in(d)
        first = workflow.run_discovery(HISTORICAL,paths,'2025-06-15T00:00:00Z',page_source=replay(ITEMS))
        assert first['new_scenes']==len({(i['collection'],i['id']) for i in ITEMS})
        repeat = workflow.run_discovery(HISTORICAL,paths,'2025-06-15T00:00:00Z',page_source=replay(ITEMS))
        assert repeat['new_scenes']==0 and repeat['sightings']>0,'Overlap must revisit existing items without counting new scenes'
        old = workflow.report(HISTORICAL,paths)['watermark']
        def unavailable(start,stop):
            raise ConnectionError('Offline laptop')
            yield
        try:
            workflow.run_discovery(HISTORICAL,paths,'2025-06-16T00:00:00Z',page_source=unavailable)
        except ConnectionError:
            pass
        else:
            raise AssertionError('Interruption must propagate')
        r = workflow.report(HISTORICAL,paths)
        assert r['watermark']==old and r['runs'][-1]['status']=='failed'
        complete = workflow.run_discovery(HISTORICAL,paths,'2025-06-18T00:00:00Z',page_source=replay(ITEMS))
        assert complete['search_start']=='2025-06-12T00:00:00Z','Missed days must resume from saved progress'
    with tempfile.TemporaryDirectory() as d:
        paths = paths_in(d)
        try:
            workflow.run_discovery(HISTORICAL,paths,'2025-06-15T00:00:00Z',page_source=replay(ITEMS,fail_on_page=2))
        except ConnectionError:
            pass
        r = workflow.report(HISTORICAL,paths)
        assert r['unique_scenes']==2 and r['watermark']==HISTORICAL['initial_start']
        assert r['runs'][-1]['pages']==1 and r['runs'][-1]['new_scenes']==2
        recovered = workflow.run_discovery(HISTORICAL,paths,'2025-06-15T00:00:00Z',page_source=replay(ITEMS))
        assert recovered['new_scenes']==len(ITEMS)-2

def one_preview():
    item = next(i for i in ITEMS if i['id']==SNAPSHOT['preview_item_id'])
    data = (ROOT/'fixtures'/'real_preview.jpg').read_bytes()
    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(data)
            self.headers = {'Content-Length':str(len(data))}
    transfers = []
    def open_url(url):
        assert url==SNAPSHOT['preview_url'],'Use the recorded JPEG only for its actual scene'
        transfers.append(url)
        return Response()
    with tempfile.TemporaryDirectory() as d:
        paths = paths_in(d)
        result = workflow.run_discovery(HISTORICAL,paths,'2025-06-15T00:00:00Z',preview=True,page_source=replay([item]),open_url=open_url)
        assert len(transfers)==1 and Path(result['preview_path']).read_bytes()==data
        workflow.run_discovery(HISTORICAL,paths,'2025-06-15T00:00:00Z',preview=True,page_source=replay([item]),open_url=open_url)
        assert len(transfers)==1,'Repeated run must reuse a verified preview'
        Path(result['preview_path']).write_bytes(data[:len(data)//2])
        workflow.run_discovery(HISTORICAL,paths,'2025-06-15T00:00:00Z',preview=True,page_source=replay([item]),open_url=open_url)
        assert len(transfers)==2,'Damaged cache must be repaired'
        # A preview failure follows complete discovery: preserve the watermark.
        Path(result['preview_path']).unlink()
        def failure(url):
            raise ConnectionError('Preview transport failed')
        try:
            workflow.run_discovery(HISTORICAL,paths,'2025-06-16T00:00:00Z',preview=True,page_source=replay([item]),open_url=failure)
        except ConnectionError:
            pass
        r = workflow.report(HISTORICAL,paths)
        assert r['watermark']=='2025-06-16T00:00:00Z' and r['runs'][-1]['status']=='failed'

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exercise',choices=['1','2','3'])
    args = parser.parse_args()
    selected = {'1':exercise1,'2':exercise2,'3':exercise3} if not args.exercise else {args.exercise:globals()['exercise'+args.exercise]}
    if not args.exercise:
        selected.update({'service recovery':recovery,'one real preview':one_preview})
    failures = 0
    # Expected failure checks should not produce full tracebacks from the service.
    logging.getLogger('sentinel').addHandler(logging.NullHandler())
    logging.getLogger('sentinel').propagate=False
    for label,test in selected.items():
        try:
            test()
            print('PASS:',label)
        except Exception as exc:
            failures += 1
            print('FAIL:',label,'—',type(exc).__name__+':',exc)
    return 1 if failures else 0

if __name__=='__main__':
    sys.exit(main())
