"""Local operational evaluation. Records metrics only; never makes API calls itself.

Report: python -m app.evaluation --output data/evaluation-summary.csv
"""
import argparse
import csv
import json
import logging
import math
import os
import statistics
import threading
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import config  # Load the same .env configuration as the application.

logger = logging.getLogger('uvicorn.error')
_context = ContextVar('evaluation_context', default=None)
_lock = threading.Lock()
# Deliberately excludes prompts, responses, photo hashes, URLs, session IDs and keys.
_FIELDS = {'request_id','batch_id','job_id','retry_of','department','provider','model','kind','operation',
           'outcome','duration_ms','http_status','error_type','error_code','cache_hit',
           'item_count','requested','new_generations','reused','blocked','unavailable',
           'retry','queue_wait_ms','first_preview_ms','completed','failed','pending','images_returned','usage','tokens','route','method'}
_USAGE_KEYS = {'input_tokens','output_tokens','prompt_tokens','completion_tokens','total_tokens',
               'input_tokens_details','output_tokens_details','prompt_tokens_details',
               'completion_tokens_details','cached_tokens','reasoning_tokens','text_tokens',
               'image_tokens','audio_tokens','input_images','output_images','image_count'}


_EVENT_NUMBERS = {'schema_version','duration_ms','http_status','item_count','requested',
                  'new_generations','reused','blocked','unavailable','queue_wait_ms',
                  'first_preview_ms','completed','failed','pending','images_returned'}
_EVENT_BOOLEANS = {'cache_hit','retry'}
EVENT_COLUMNS = ['schema_version','timestamp','event_id','run','mode'] + sorted(_FIELDS-{'usage','tokens'}) + ['tokens_input','tokens_output','tokens_total','usage_json']


def event_to_csv(record):
    row={key:record.get(key) for key in EVENT_COLUMNS if key not in ('usage_json','tokens_input','tokens_output','tokens_total')}
    for key in _EVENT_BOOLEANS:
        row[key]='true' if record.get(key) is True else 'false' if record.get(key) is False else ''
    tokens=record.get('tokens') or {}
    for name in ('input','output','total'):row['tokens_'+name]=tokens.get(name)
    row['usage_json']=json.dumps(record['usage'],ensure_ascii=True,allow_nan=False) if record.get('usage') is not None else ''
    return row


def event_from_csv(row):
    record={}
    for key in EVENT_COLUMNS:
        if key.startswith('tokens_') or key=='usage_json':continue
        value=row.get(key,'')
        if value=='':continue
        elif key in _EVENT_BOOLEANS:
            if value not in ('true','false'):raise ValueError('Invalid CSV boolean')
            record[key]=value=='true'
        elif key in _EVENT_NUMBERS:
            record[key]=json.loads(value)
            if not isinstance(record[key],(int,float)) or isinstance(record[key],bool) or not math.isfinite(record[key]):raise ValueError('Invalid CSV number')
        else:record[key]=value
    tokens={name:json.loads(row['tokens_'+name]) if row.get('tokens_'+name) else None for name in ('input','output','total')}
    record['tokens']=tokens if any(v is not None for v in tokens.values()) else None
    record['usage']=json.loads(row['usage_json']) if row.get('usage_json') else None
    record.setdefault('department',None)
    return record


def read_events(path):
    records=[];invalid=0
    if not path.exists():return records,invalid
    with path.open(newline='',encoding='utf-8') as stream:
        rows=csv.DictReader(stream) if path.suffix.lower()=='.csv' else stream
        for row in rows:
            try:
                record=event_from_csv(row) if isinstance(row,dict) else json.loads(row)
                if not isinstance(record,dict) or not record.get('operation') or not record.get('outcome'):raise ValueError()
                records.append(record)
            except (ValueError,TypeError,KeyError):invalid+=1
    return records,invalid


def write_event_csv(path,records):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=EVENT_COLUMNS);writer.writeheader()
        writer.writerows(event_to_csv(record) for record in records)


def write_summary_csv(path,report):
    def flatten(value,prefix=''):
        result={}
        for key,item in value.items():
            name=prefix+key
            if isinstance(item,dict):result.update(flatten(item,name+'.'))
            else:result[name]=item
        return result
    rows=[{**flatten(operation),'report_records':report['records'],
           'invalid_lines_skipped':report['invalid_lines_skipped']} for operation in report['operations']]
    leading=['kind','operation','provider','model','mode','department','count']
    columns=leading+sorted(set().union(*(row.keys() for row in rows))-set(leading))
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(rows)


def metrics_path():
    default = Path(__file__).resolve().parents[1] / 'data' / 'evaluation.csv'
    return Path(os.getenv('EVALUATION_FILE') or default)


def current_ids():
    return dict(_context.get() or {})


def set_evaluation_department(department):
    """Attach the explicit shopping preference to this request, not a photo inference."""
    values = _context.get()
    if values is None:
        values = {}
        _context.set(values)
    values['department'] = department if department in ('male','female') else None


@contextmanager
def evaluation_context(**identifiers):
    values = current_ids()
    values.update({k:v for k,v in identifiers.items() if k in {'request_id','batch_id','job_id','retry_of'} and v is not None})
    if 'department' in identifiers:
        department=identifiers['department']
        values['department']=department if department in ('male','female') else None
    token = _context.set(values)
    try:
        yield
    finally:
        _context.reset(token)


def record_event(operation, outcome, **fields):
    if os.getenv('EVALUATION_ENABLED','1').lower() in {'0','false','off'}:
        return
    record = {**current_ids(), **{k:v for k,v in fields.items() if k in _FIELDS}}
    record['department']=record.get('department') if record.get('department') in ('male','female') else None
    record.update(schema_version=1, timestamp=datetime.now(timezone.utc).isoformat(),
                  event_id=uuid4().hex, operation=operation, outcome=outcome,
                  run=os.getenv('EVALUATION_RUN','default'), mode=os.getenv('CATALOG_MODE','live'))
    try:
        # Append-only local telemetry. Filesystem failures must never break shopping.
        with _lock:
            path=metrics_path();path.parent.mkdir(parents=True,exist_ok=True)
            if path.suffix.lower()=='.csv':
                has_header=path.exists() and path.stat().st_size>0
                if has_header:
                    with path.open(newline='',encoding='utf-8') as stream:
                        if next(csv.reader(stream),[])!=EVENT_COLUMNS:
                            raise ValueError('Unexpected event CSV schema')
                with path.open('a',newline='',encoding='utf-8') as stream:
                    writer=csv.DictWriter(stream,fieldnames=EVENT_COLUMNS)
                    if not has_header:writer.writeheader()
                    writer.writerow(event_to_csv(record))
            else:
                encoded=json.dumps(record,ensure_ascii=True,allow_nan=False)+'\n'
                with path.open('a',encoding='utf-8') as stream:stream.write(encoded)
    except (OSError,TypeError,ValueError):
        logger.warning('Evaluation metrics could not be written; request continues.')


@contextmanager
def measure(operation, **fields):
    started=time.perf_counter()
    result={'kind':'operation','outcome':'success',**fields}
    try:
        yield result
    except BaseException as exc:
        name=type(exc).__name__
        result.update(outcome='cancelled' if name=='CancelledError' else 'timeout' if 'timeout' in name.lower() else 'failed', error_type=name)
        raise
    finally:
        outcome=result.pop('outcome')
        result['duration_ms']=round((time.perf_counter()-started)*1000,3)
        record_event(operation,outcome,**result)


def _numeric_usage(value):
    if not isinstance(value,dict):return {}
    result={}
    for key,item in value.items():
        if key not in _USAGE_KEYS:continue
        if isinstance(item,dict):
            nested=_numeric_usage(item)
            if nested:result[key]=nested
        elif isinstance(item,(int,float)) and not isinstance(item,bool) and math.isfinite(item) and item>=0:
            result[key]=item
    return result


def response_metrics(response, image_response=False):
    """Extract allowlisted numeric usage only, including usage on failed responses."""
    result={'http_status':response.status_code,'usage':None,'tokens':None}
    try:body=response.json()
    except (ValueError,TypeError):return result
    if not isinstance(body,dict):return result
    usage=_numeric_usage(body.get('usage'))
    if usage:
        result['usage']=usage
        inputs=usage.get('input_tokens',usage.get('prompt_tokens'))
        outputs=usage.get('output_tokens',usage.get('completion_tokens'))
        total=usage.get('total_tokens')
        if total is None and inputs is not None and outputs is not None:total=inputs+outputs
        result['tokens']={'input':inputs,'output':outputs,'total':total}
    if image_response and isinstance(body.get('data'),list):
        result['images_returned']=sum(isinstance(x,dict) and bool(x.get('url') or x.get('b64_json')) for x in body['data'])
    # An HTTP 200 JSON-RPC/UCP error still represents a failed catalogue request.
    document=body.get('result') or {}
    structured=document.get('structuredContent') if isinstance(document,dict) else None
    semantic_error=bool(body.get('error')) or (isinstance(document,dict) and bool(document.get('isError')))
    if isinstance(structured,dict) and isinstance(structured.get('ucp'),dict):semantic_error=semantic_error or structured['ucp'].get('status')=='error'
    if response.status_code>=400 or semantic_error:result['outcome']='failed'
    return result


async def measured_post(client, url, *, operation, provider, model=None, **kwargs):
    """Instrument one existing HTTP attempt, without retrying or changing its response."""
    with measure(operation,kind='provider_request',provider=provider,model=model) as metric:
        response=await client.post(url,**kwargs)
        try:metric.update(response_metrics(response,image_response=operation=='image_generation'))
        except Exception:
            metric.update(http_status=response.status_code,outcome='failed' if response.status_code>=400 else 'success')
        return response


class EvaluationMiddleware:
    """Measure API response time, excluding subsequent background work and polling."""
    ROUTES={'/api/search','/api/chat','/api/products','/api/upload','/api/try-on/batch'}
    def __init__(self,app):self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('path') not in self.ROUTES or scope.get('method')=='OPTIONS':
            return await self.app(scope,receive,send)
        request_id=uuid4().hex
        started=time.perf_counter();status=None;recorded=False
        def finish(outcome,error_type=None):
            nonlocal recorded
            if recorded:return
            recorded=True
            record_event('api.request',outcome,kind='api_request',route=scope['path'],method=scope['method'],
                         http_status=status,error_type=error_type,duration_ms=round((time.perf_counter()-started)*1000,3))
        async def wrapped_send(message):
            nonlocal status
            if message['type']=='http.response.start':
                status=message['status']
                message={**message,'headers':[*message.get('headers',[]),(b'x-request-id',request_id.encode())]}
            await send(message)
            if message['type']=='http.response.body' and not message.get('more_body',False):
                finish('success' if status is not None and status<400 else 'failed')
        with evaluation_context(request_id=request_id):
            try:await self.app(scope,receive,wrapped_send)
            except BaseException as exc:
                finish('failed',type(exc).__name__)
                raise


def percentile(values, percent):
    if not values:return None
    values=sorted(values);position=(len(values)-1)*percent/100
    lower=math.floor(position);upper=math.ceil(position)
    return round(values[lower]+(values[upper]-values[lower])*(position-lower),3)


def summarize(records):
    """Provider, API and job populations stay separate to avoid double counting."""
    groups=defaultdict(list)
    for record in records:
        groups[(record.get('kind','event'),record.get('operation'),record.get('provider'),record.get('model'),record.get('mode'),record.get('department'))].append(record)
    operations=[]
    for (kind,operation,provider,model,mode,department),rows in sorted(groups.items(),key=lambda x:str(x[0])):
        durations=[r['duration_ms'] for r in rows if isinstance(r.get('duration_ms'),(int,float))]
        tokens={}
        for name in ('input','output','total'):
            supplied=[r['tokens'][name] for r in rows if isinstance(r.get('tokens'),dict) and r['tokens'].get(name) is not None]
            tokens[name]={'sum':sum(supplied) if supplied else None,'records_reporting':len(supplied)}
        success_outcome='success' if kind in ('provider_request','api_request') else 'completed' if kind=='job' else None
        operations.append({'kind':kind,'operation':operation,'provider':provider,'model':model,'mode':mode,'department':department,
            'count':len(rows),'outcomes':dict(Counter(r['outcome'] for r in rows)),
            'success_rate':sum(r['outcome']==success_outcome for r in rows)/len(rows) if success_outcome else None,
            'counts':{key:sum(r.get(key,0) for r in rows) for key in ('requested','new_generations','reused','blocked','unavailable','completed','failed','pending')},
            'latency_ms':{'samples':len(durations),'mean':round(statistics.mean(durations),3) if durations else None,
                          'p50':percentile(durations,50),'p95':percentile(durations,95)},
            'usage_records_reported':sum(bool(r.get('usage')) for r in rows),'tokens':tokens,
            'cache_hits':sum(r.get('item_count',1) for r in rows if r.get('cache_hit') is True),
            'cache_misses':sum(r.get('item_count',1) for r in rows if r.get('cache_hit') is False),
            'manual_retries':sum(r.get('retry') is True for r in rows),
            'images_returned':sum(r.get('images_returned',0) for r in rows),
            'first_preview_ms':{'p50':percentile([r['first_preview_ms'] for r in rows if r.get('first_preview_ms') is not None],50),
                                'p95':percentile([r['first_preview_ms'] for r in rows if r.get('first_preview_ms') is not None],95)},
            'queue_wait_ms':{'p50':percentile([r['queue_wait_ms'] for r in rows if r.get('queue_wait_ms') is not None],50),
                             'p95':percentile([r['queue_wait_ms'] for r in rows if r.get('queue_wait_ms') is not None],95)}})
    return {'records':len(records),'operations':operations,
            'notes':['Operational measurements only; no quality/accuracy score or cost estimate is inferred.',
                     'Provider-request success describes HTTP/protocol success, not model-output correctness.',
                     'Token sums include reported usage only; missing usage is unknown, not zero. Nested details are subsets.',
                     'API latency ends at the response body; preview.job latency includes queue wait. No browser render timing is measured.',
                     'Department records the explicit shopping preference. Older or unscoped records remain null/unknown.',
                     'Stub/mock traffic must use a separate EVALUATION_RUN or output file before publishing results.']}


def main():
    parser=argparse.ArgumentParser(description='Summarize local FashionMirror evaluation events; makes no API calls.')
    parser.add_argument('--input',type=Path,help='Read one CSV or JSONL file; default combines current and legacy recordings')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--export-events',type=Path,help='Export filtered individual event records as CSV')
    parser.add_argument('--run',help='Only include records with this EVALUATION_RUN label')
    parser.add_argument('--department',choices=['male','female','unknown'],help='Filter by selected shopping department; unknown includes older records')
    args=parser.parse_args();records=[];invalid=0;seen=set()
    inputs=[args.input] if args.input else [metrics_path()]
    legacy=Path(__file__).resolve().parents[1]/'data'/'evaluation.jsonl'
    if not args.input and not os.getenv('EVALUATION_FILE') and legacy not in inputs:
        inputs.append(legacy)
    for path in inputs:
        loaded,bad=read_events(path);invalid+=bad
        for record in loaded:
            if args.run is not None and record.get('run')!=args.run:continue
            if args.department is not None and record.get('department')!=({'unknown':None}.get(args.department,args.department)):continue
            identifier=record.get('event_id')
            if identifier and identifier in seen:continue
            if identifier:seen.add(identifier)
            records.append(record)
    if args.export_events:
        write_event_csv(args.export_events,records)
        print('Evaluation events exported to '+str(args.export_events))
    report=summarize(records);report['invalid_lines_skipped']=invalid
    encoded=json.dumps(report,indent=2,allow_nan=False)
    if args.output:
        if args.output.suffix.lower()=='.csv':write_summary_csv(args.output,report)
        else:
            args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(encoded+'\n',encoding='utf-8')
        print('Evaluation report saved to '+str(args.output))
    else:print(encoded)


if __name__=='__main__':main()
