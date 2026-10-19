"""Supplied, bounded HTTPS range reader for Rasterio's custom opener.
Rejects servers that ignore Range, so a crop cannot silently download a full tile.
"""
import io
import re
from urllib.request import Request,urlopen
from urllib.parse import urlsplit
from collections import OrderedDict

class RangeFailure(OSError):
    pass

class RangeSession:
    def __init__(self,url,byte_budget=12*1024*1024,simulate=False):
        if urlsplit(url).scheme!='https':
            raise ValueError('Only public HTTPS COG URLs are supported')
        self.url=url;self.byte_budget=byte_budget;self.network_bytes=0;self.requests=0
        self.size=None;self.cache=OrderedDict();self.block_size=262144;self.simulate=simulate
        self.last_error=None;self.last_http_status=None;self.etag=None
    def fetch(self,start,end):
        length=end-start+1
        if self.network_bytes+length>self.byte_budget:
            self.last_error='Range byte budget exceeded'
            raise RangeFailure(self.last_error)
        if self.simulate and self.requests>=2:
            self.last_error='Controlled network interruption after actual range responses'
            raise RangeFailure(self.last_error)
        request=Request(self.url,headers={'Range':f'bytes={start}-{end}','User-Agent':'AGH-Lab03/1.0','Accept-Encoding':'identity'})
        if self.etag:request.add_header('If-Match',self.etag)
        self.requests+=1
        try:
            with urlopen(request,timeout=30) as response:
                self.last_http_status=response.status
                tag=response.headers.get('ETag')
                if self.etag is not None and tag!=self.etag:raise RangeFailure('Remote ETag changed during reading')
                self.etag=tag
                if response.status!=206:
                    raise RangeFailure('Server ignored the byte range; full-tile fallback refused')
                match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',response.headers.get('Content-Range',''))
                if not match or tuple(map(int,match.groups()[:2]))!=(start,end):
                    raise RangeFailure('Unexpected Content-Range')
                size=int(match.group(3))
                if self.size is not None and size!=self.size:
                    raise RangeFailure('Remote file size changed during reading')
                self.size=size
                announced=response.headers.get('Content-Length')
                if announced is not None and int(announced)!=length:
                    raise RangeFailure('Range Content-Length mismatch')
                chunks=[];received=0
                while received<length:
                    chunk=response.read(min(65536,length-received))
                    if not chunk:break
                    chunks.append(chunk);received+=len(chunk);self.network_bytes+=len(chunk)
                if received!=length:
                    raise RangeFailure('Interrupted range response')
                return b''.join(chunks)
        except Exception as exc:
            if hasattr(exc,'code'):self.last_http_status=exc.code
            self.last_error=f'{type(exc).__name__}: {exc}'
            raise
    def get_size(self):
        if self.size is None:self.fetch(0,0)
        return self.size
    def prepare(self,first,last):
        missing=[n for n in range(first,last+1) if n not in self.cache]
        groups=[]
        for number in missing:
            if groups and number==groups[-1][-1]+1:groups[-1].append(number)
            else:groups.append([number])
        for group in groups:
            start=group[0]*self.block_size
            end=min(self.get_size()-1,(group[-1]+1)*self.block_size-1)
            data=self.fetch(start,end)
            for number in group:
                offset=(number-group[0])*self.block_size
                self.cache[number]=data[offset:offset+self.block_size]
        # The complete session is bounded by byte_budget; no whole tile is cached.
    def block(self,number):
        self.prepare(number,number)
        return self.cache[number]
    def opener(self,path,mode='rb'):
        if str(path)!=self.url or mode not in ('r','rb'):
            raise FileNotFoundError(path)
        return RangeFile(self)

class RangeFile(io.RawIOBase):
    def __init__(self,session):
        super().__init__();self.session=session;self.position=0
        self.session.get_size()
    def readable(self):return True
    def seekable(self):return True
    def writable(self):return False
    def tell(self):return self.position
    def seek(self,offset,whence=0):
        target=offset if whence==0 else self.position+offset if whence==1 else self.session.get_size()+offset if whence==2 else -1
        if target<0:raise ValueError('Invalid seek')
        self.position=target;return target
    def _read(self,size=-1):
        remaining=max(0,self.session.get_size()-self.position)
        n=remaining if size is None or size<0 else min(size,remaining)
        if n==0:return b''
        if n>self.session.byte_budget:
            raise RangeFailure('Large read refused before allocating memory')
        self.session.prepare(self.position//self.session.block_size,(self.position+n-1)//self.session.block_size)
        pieces=[]
        while n:
            block_number,offset=divmod(self.position,self.session.block_size)
            block=self.session.block(block_number)
            take=min(n,len(block)-offset)
            if take<=0:raise RangeFailure('Invalid range cache state')
            pieces.append(block[offset:offset+take]);self.position+=take;n-=take
        return b''.join(pieces)
    def read(self,size=-1):
        # GDAL's C callback treats a short read as I/O failure. Preserve the Python
        # cause and re-raise it at the outer service boundary, avoiding unraisable
        # callback tracebacks. No output is accepted after last_error is set.
        try:return self._read(size)
        except (OSError,ValueError) as exc:
            if not self.session.last_error:self.session.last_error=f'{type(exc).__name__}: {exc}'
            return b''
    def readinto(self,b):
        data=self.read(len(b));b[:len(data)]=data;return len(data)
