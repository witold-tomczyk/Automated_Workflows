"""Supplied: bounded 10 m red/B08 COG windows, preserving radiometric metadata."""
import json,math,os
from pathlib import Path
import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from rasterio.warp import transform,transform_bounds
from rasterio.windows import Window,from_bounds
from range_reader import RangeSession,RangeFailure

RECIPE='native10m-wgs84-mask-v1'

def polygon_in_crs(bbox,crs):
    w,s,e,n=bbox
    ring=[]
    for a,b in [((w,s),(e,s)),((e,s),(e,n)),((e,n),(w,n)),((w,n),(w,s))]:
        ring.extend((a[0]+(b[0]-a[0])*k/32,a[1]+(b[1]-a[1])*k/32) for k in range(32))
    ring.append(ring[0])
    xs,ys=transform('EPSG:4326',crs,[p[0] for p in ring],[p[1] for p in ring])
    return {'type':'Polygon','coordinates':[list(zip(xs,ys))]}

def make_plan(item,asset_key,bbox,max_pixels):
    if asset_key not in ('red','nir'):raise ValueError('Only red and B08/nir are allowed')
    asset=item['assets'].get(asset_key)
    if not asset:raise ValueError(f'Scene does not advertise {asset_key}')
    if 'cloud-optimized' not in asset.get('type',''):
        raise ValueError('A declared COG asset is required; JP2/full-product fallback is disabled')
    rb=asset.get('raster:bands',[{}])[0]
    if rb.get('data_type')!='uint16' or float(asset.get('gsd',0))!=10:
        raise ValueError('This lab requires the native uint16 10 m asset')
    band=asset.get('eo:bands',[{}])[0]
    expected_name='B04' if asset_key=='red' else 'B08'
    if band.get('name')!=expected_name:raise ValueError('Unexpected spectral band identity')
    epsg=asset.get('proj:epsg',item.get('properties',{}).get('proj:epsg'))
    code=asset.get('proj:code',item.get('properties',{}).get('proj:code'))
    if epsg is None and code is None:raise ValueError('Missing source CRS metadata')
    crs=rasterio.crs.CRS.from_user_input(f'EPSG:{epsg}' if epsg is not None else code)
    values=asset['proj:transform'];affine=Affine(*values[:6])
    height,width=asset['proj:shape']
    if not (affine.a==10 and affine.e==-10 and affine.b==0 and affine.d==0):
        raise ValueError('Expected a north-up native 10 m grid')
    bounds=transform_bounds('EPSG:4326',crs,*bbox,densify_pts=21)
    fractional=from_bounds(*bounds,transform=affine)
    left=math.floor(fractional.col_off);top=math.floor(fractional.row_off)
    right=math.ceil(fractional.col_off+fractional.width);bottom=math.ceil(fractional.row_off+fractional.height)
    if left<0 or top<0 or right>width or bottom>height:
        raise ValueError('AOI is not completely inside this source tile; choose one covering tile or a smaller AOI')
    if right<=left or bottom<=top or (right-left)*(bottom-top)>max_pixels:
        raise ValueError('AOI window is empty or exceeds the pixel budget')
    window=Window(left,top,right-left,bottom-top)
    subset_transform=rasterio.windows.transform(window,affine)
    scale=rb.get('scale');offset=rb.get('offset');nodata=rb.get('nodata')
    if scale is None or offset is None or nodata is None:raise ValueError('Missing scale/offset/nodata metadata')
    result={'asset_key':asset_key,'source_url':asset['href'],'crs':crs.to_string(),
      'source_shape':[height,width],'source_transform':list(affine)[:6],
      'window':[left,top,right-left,bottom-top],'shape':[bottom-top,right-left],
      'transform':list(subset_transform)[:6],'dtype':'uint16','nodata':int(nodata),
      'scale':float(scale),'offset':float(offset),'aoi_bbox':bbox,'recipe':RECIPE,
      'raw_pixel_bytes':(right-left)*(bottom-top)*2}
    return result

def matching_pair(plans):
    red,nir=plans
    for key in ['crs','shape','transform','window']:
        if red[key]!=nir[key]:raise ValueError(f'Band grids disagree: {key}; automatic resampling is outside this lab')

def write_window(plan,path,session,fail_before_finalize=False,max_output_bytes=8*1024*1024,on_stage=lambda value:None):
    path=Path(path)
    try:session.get_size()  # Probe outside GDAL's C callbacks.
    except Exception as exc:raise RangeFailure(session.last_error or str(exc)) from exc
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR',GDAL_CACHEMAX=32*1024*1024):
        try:
            with rasterio.open(session.url,opener=session.opener) as src:
                if (src.crs.to_string()!=plan['crs'] or list(src.transform)[:6]!=plan['source_transform']
                    or [src.height,src.width]!=plan['source_shape'] or src.count!=1 or src.dtypes[0]!='uint16'):
                    raise ValueError('Remote raster header disagrees with recorded source metadata')
                window=Window(*plan['window'])
                data=src.read(1,window=window)  # Never read the complete tile.
                if session.last_error:raise RangeFailure(session.last_error)
                if data.shape!=tuple(plan['shape']):raise ValueError('Unexpected raster shape')
        except Exception as exc:
            if session.last_error:raise RangeFailure(session.last_error) from exc
            raise
    subset_transform=Affine(*plan['transform'])
    inside=geometry_mask([polygon_in_crs(plan['aoi_bbox'],plan['crs'])],out_shape=data.shape,
                         transform=subset_transform,invert=True,all_touched=False)
    data[~inside]=plan['nodata']
    if not np.any(inside & (data!=plan['nodata'])):
        raise ValueError('The selected AOI has no valid source pixels')
    on_stage('write_subset')
    profile={'driver':'GTiff','width':data.shape[1],'height':data.shape[0],'count':1,'dtype':'uint16',
             'crs':plan['crs'],'transform':subset_transform,'nodata':plan['nodata'],
             'compress':'deflate','predictor':2,'tiled':True,'blockxsize':128,'blockysize':128}
    with rasterio.open(path,'w',**profile) as dst:
        dst.write(data,1)
        dst.scales=(plan['scale'],);dst.offsets=(plan['offset'],)
        dst.update_tags(SOURCE_URL=plan['source_url'],AOI_BBOX=json.dumps(plan['aoi_bbox']),RECIPE=RECIPE)
    with path.open('r+b') as f:os.fsync(f.fileno())
    if path.stat().st_size>max_output_bytes:raise ValueError('Output exceeds disk file budget')
    on_stage('validate_subset')
    validate_window(path,plan)
    on_stage('before_finalize')
    if fail_before_finalize:
        raise InterruptedError('Controlled interruption after writing the real subset and before finalization')
    return int(np.count_nonzero(inside)),int(np.count_nonzero(data!=plan['nodata']))

def validate_window(path,plan):
    with rasterio.open(path) as r:
        if r.count!=1 or [r.height,r.width]!=plan['shape'] or r.crs.to_string()!=plan['crs']:
            raise ValueError('Subset dimensions/CRS do not match its request')
        if list(r.transform)[:6]!=plan['transform'] or r.dtypes[0]!=plan['dtype'] or r.nodata!=plan['nodata']:
            raise ValueError('Subset grid, type or nodata differs from its recorded request')
        if not np.isclose(r.scales[0],plan['scale']) or not np.isclose(r.offsets[0],plan['offset']):
            raise ValueError('Reflectance scale or offset was lost')
        data=r.read(1)
        inside=geometry_mask([polygon_in_crs(plan['aoi_bbox'],plan['crs'])],out_shape=data.shape,
                             transform=r.transform,invert=True,all_touched=False)
        if np.any(data[~inside]!=plan['nodata']):raise ValueError('Pixels outside AOI were not masked')
        if not np.any(data!=plan['nodata']):raise ValueError('Subset has no valid pixels')
    return True
