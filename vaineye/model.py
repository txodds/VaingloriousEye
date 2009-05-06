import time
import urlparse
from datetime import datetime, timedelta
import re
import os
import mimetypes
from sqlalchemy import MetaData, Table
from sqlalchemy import Column, Integer, String, DateTime, Float
from sqlalchemy import create_engine, select, and_
try:
    import GeoIP
except ImportError:
    import sys
    sys.stderr.write('Could not import GeoIP')
    geo_ip = None
else:
    geo_ip = GeoIP.open(os.path.join(os.path.dirname(__file__), 'GeoLiteCity.dat'),
                        GeoIP.GEOIP_STANDARD)
from vaineye.ziptostate import zip_to_state

class RequestTracker(object):

    def __init__(self, db):
        self.engine = create_engine(db)
        self.sql_metadata = MetaData()
        self.table = Table(
            'requests', self.sql_metadata,
            Column('id', Integer, primary_key=True),
            Column('ip', String(15)),
            Column('date', DateTime),
            Column('processing_time', Float),
            Column('request_method', String(15)),
            Column('scheme', String),
            Column('host', String),
            Column('path', String),
            Column('query_string', String),
            Column('user_agent', String),
            Column('referrer', String),
            Column('response_code', Integer),
            Column('response_bytes', Integer),
            Column('content_type', String),
            Column('ip_country_code', String),
            Column('ip_country_code3', String), # ?
            Column('ip_country_name', String), # Redundant?
            Column('ip_region', String),
            Column('ip_city', String),
            Column('ip_postal_code', String),
            Column('ip_latitude', Float), # String?
            Column('ip_longitude', Float), # String?
            Column('ip_dma_code', Integer), # ?
            Column('ip_area_code', Integer),
            Column('ip_state', String(2)),
            )
        self.table_insert = self.table.insert()
        self.sql_metadata.create_all(self.engine)
        self._pending = []

    def add_request(self, environ, start_time, end_time,
                    status, response_headers):
        request = {
            'REMOTE_ADDR': environ['REMOTE_ADDR'],
            'vaineye.date': datetime.now(),
            'vaineye.start_time': time.time(),
            'REQUEST_METHOD': environ['REQUEST_METHOD'],
            'wsgi.url_scheme': environ['wsgi.url_scheme'],
            'HTTP_HOST': environ.get('HTTP_HOST', ''),
            'SCRIPT_NAME': environ.get('SCRIPT_NAME', ''),
            'PATH_INFO': environ.get('PATH_INFO', ''),
            'QUERY_STRING': environ.get('QUERY_STRING', ''),
            'HTTP_USER_AGENT': environ.get('HTTP_USER_AGENT', ''),
            'HTTP_REFERER': environ.get('HTTP_REFERER', ''),
            }
        request['vaineye.response_code'] = int(status.split(None, 1)[0])
        for header_name, header_value in response_headers:
            header_name = header_name.lower()
            if header_name == 'content-length':
                request['vaineye.response_bytes'] = int(header_value)
            elif header_name == 'content-type':
                request['vaineye.content_type'] = header_value
        request['vaineye.end_time'] = time.time()
        ## FIXME: should I just be creating SQLAlchemy bound inserts
        ## that I can execute later?
        self._pending.append(request)

    def write_pending(self, callback=None):
        conn = self.engine.connect()
        total = len(self._pending)
        all_values = []
        for index, request in enumerate(self._pending):
            if callback:
                callback(index, total)
            if request.get('vaineye.end_time'):
                processing_time = request['vaineye.end_time'] - request['vaineye.start_time']
            else:
                processing_time = None
            date = request.get('vaineye.date')
            if not date:
                date = datetime.fromtimestamp(request['vaineye.start_time'])
            self.add_geoip(request)
            values = {
                'ip': request['REMOTE_ADDR'],
                'date': date,
                'processing_time': processing_time,
                'request_method': request['REQUEST_METHOD'],
                'scheme': request['wsgi.url_scheme'],
                'host': request.get('HTTP_HOST'),
                ## urllib.quote?:
                'path': request.get('SCRIPT_NAME', '')+request.get('PATH_INFO', ''),
                'query_string': request.get('QUERY_STRING', ''),
                'user_agent': request.get('HTTP_USER_AGENT', ''),
                'referrer': request.get('HTTP_REFERER', ''),
                'response_code': request['vaineye.response_code'],
                'response_bytes': request.get('vaineye.response_bytes'),
                'content_type': request.get('vaineye.content_type'),
                }
            if request.get('vaineye.ip_location'):
                for name, value in request['vaineye.ip_location'].items():
                    if isinstance(value, str):
                        try:
                            ## FIXME: right encoding?
                            value = value.decode('latin1')
                        except UnicodeDecodeError, e:
                            raise ValueError("Bad item: %r, %s" % (value, e))
                    values['ip_%s' % name] = value
            else:
                values.update(self._empty_ip_location)
            all_values.append(values)
            ins = self.table_insert.values(values)
        if callback:
            callback()
        conn.execute(self.table_insert, all_values)
        self._pending = []

    _empty_ip_location = {
        'ip_country_code': None, 'ip_country_code3': None,
        'ip_country_name': None, 'ip_region': None,
        'ip_city': None, 'ip_postal_code': None,
        'ip_latitude': None, 'ip_longitude': None,
        'ip_dma_code': None, 'ip_area_code': None}

    def requests(self, query, callback=None):
        conn = self.engine.connect()
        q = select([self.table],
                   query)
        result = conn.execute(q)
        total = result.rowcount
        if callback:
            callback(None, total)
        for index, row in enumerate(result):
            row = dict(row)
            url = urlparse.urlunsplit((row['scheme'],
                                       row['host'],
                                       row['path'],
                                       row['query_string'],
                                       ''))
            row['url'] = url
            if callback:
                callback(index, total)
            yield row

    apache_line_re = re.compile(r'''
    (?P<ip>[\d.:a-fA-F]+)          \s+  # IP Address
    (?P<ident>[^\s]+)              \s+  # ident (usually -)
    (?P<user>[^\s]+)               \s+  # logged-in user (usually -)
    \[(?P<date>[^\]]*)\]           \s+  # date
    "(?P<method>[A-Z]+)            \s+  # Start of the request string
    (?P<path>[^ ]+)                \s+  # Requested path
    HTTP/(?P<http_version>[\d.]*)" \s+  # The version
    (?P<status>\d+)                \s+  # Response status version
    (?P<bytes>\d+|-)               \s+  # Bytes in response (- is 0)
    "(?P<referrer>[^"]*)"          \s+  # Referrer
    "(?P<user_agent>[^"]*)"             # User-Agent
    ''', re.VERBOSE)

    apache_date_format = '%d/%b/%Y:%H:%M:%S'

    def import_apache_line(self, line, default_scheme='http', default_host='localhost'):
        match = self.apache_line_re.match(line)
        if not match:
            raise ValueError("Bad line, cannot parse: %r" % line)
        d = match.groupdict()
        date = datetime.fromtimestamp(
            time.mktime(time.strptime(d['date'].split(None, 1)[0], self.apache_date_format)))
        date = date + timedelta(hours=int(d['date'].split(None, 1)[1]))
        if '?' in d['path']:
            path, query_string = d['path'].split('?', 1)
        else:
            path = d['path']
            query_string = ''
        if not d.get('bytes') or d['bytes'] == '-':
            bytes = 0
        else:
            bytes = int(d['bytes'])
        referrer = d['referrer']
        if referrer == '-':
            referrer = ''
        user_agent = d['user_agent']
        if user_agent == '-':
            user_agent = '-'
        content_type, encoding = mimetypes.guess_type(path)
        request = {
            'REMOTE_ADDR': d['ip'],
            'vaineye.date': date,
            'vaineye.start_time': None,
            'vaineye.end_time': None,
            'REQUEST_METHOD': d['method'],
            'wsgi.url_scheme': default_scheme,
            'HTTP_HOST': default_host,
            'SCRIPT_NAME': '',
            'PATH_INFO': path,
            'QUERY_STRING': query_string,
            'HTTP_USER_AGENT': user_agent,
            'HTTP_REFERER': referrer,
            'vaineye.response_code': int(d['status']),
            'vaineye.response_bytes': bytes,
            'vaineye.content_type': content_type,
            'vaineye.ip_location': None,
            }
        self._pending.append(request)

    _geoip_warned = False

    def add_geoip(self, request):
        if (not geo_ip or request.get('vaineye.ip_location')
            or not request.get('REMOTE_ADDR')):
            return
        try:
            rec = geo_ip.record_by_addr(request['REMOTE_ADDR'])
        except SystemError:
            if not self._geoip_warned:
                print >> sys.stderr, 'You must get this:'
                print >> sys.stderr, 'http://geolite.maxmind.com/download/geoip/database/GeoLiteCity.dat.gz'
                print >> sys.stderr, 'Per instructions: http://www.maxmind.com/app/installation?city=1'
                self._geoip_warned = True
            return
        if rec.get('postal_code'):
            state = zip_to_state(rec['postal_code'])
        else:
            state = None
        rec['state'] = state
        request['vaineye.ip_location'] = rec