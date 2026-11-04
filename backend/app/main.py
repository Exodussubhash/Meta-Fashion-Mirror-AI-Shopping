from typing import Optional, Literal
import asyncio
import io
import csv
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from uuid import uuid4
from contextlib import asynccontextmanager

# Support running this file directly as well as importing app.main with Uvicorn.
if not __package__:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'app'

import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Header, Depends, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError
from .catalog import ShopifyCatalog, CatalogError, normalize
from .mock import products as mock_products
from . import tryon
from .evaluation import EvaluationMiddleware, measured_post, measure, record_event, evaluation_context, current_ids, set_evaluation_department
from .photo_assessment import assess_photo
from .preview_validation import validate_try_on, EXCLUDED_GROUPS, classify_missing_products

# app=FastAPI(title='FashionMirror API')
@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.try_on_semaphore = asyncio.Semaphore(3)
    yield

app = FastAPI(title="FashionMirror API", lifespan=lifespan)
app.add_middleware(EvaluationMiddleware)
app.add_middleware(CORSMiddleware,allow_origins=[os.getenv('FRONTEND_ORIGIN','http://localhost:3000')],allow_methods=['GET','POST','DELETE'],allow_headers=['Content-Type','X-Session-ID'])
DATA=Path(os.getenv('DATA_DIR','data')); DATA.mkdir(exist_ok=True)
MODE=os.getenv('CATALOG_MODE','live')
catalog=ShopifyCatalog()
sessions={}
jobs={}
# semaphore=asyncio.Semaphore(3)
logger=logging.getLogger('uvicorn.error')

def session(x_session_id: str=Header(default='')):
    value=sessions.get(x_session_id)
    if not value or value['expires']<time.time():raise HTTPException(401,'Session expired. Please refresh the page.')
    return x_session_id

@app.post('/api/session')
async def create_session():
    # Short-lived bearer capability; no photo is exposed through public URLs.
    for key,value in list(sessions.items()):
        if value['expires']<time.time():
            if value.get('photo'):Path(value['photo']).unlink(missing_ok=True)
            sessions.pop(key,None)
            for jid in [j for j in jobs if jobs[j]['owner']==key]:jobs.pop(jid,None)
    token=secrets.token_urlsafe(32)
    sessions[token]={'expires':time.time()+86400,'photo':None,'history':[],'products':{},'cache':{},'wearer_department':None,'photo_id':None,'photo_assessment':None,'assessment_cache':{},'upload_lock':asyncio.Lock()}
    return {'session_id':token}

@app.get('/api/session')
async def get_session(sid:str=Depends(session)):
    state=sessions[sid]
    return {'has_photo':bool(state['photo']), 'wearer_department':state.get('wearer_department'),
            'photo_assessment':state.get('photo_assessment')}


def invalidate_previews(sid, reason):
    for job in jobs.values():
        if job['owner']==sid and job['status']=='queued':
            job.update(status='blocked',message=reason,code='photo_changed')


class Search(BaseModel):
    query:str=Field(default='everyday fashion',min_length=1,max_length=1000)
    cursor:Optional[str]=Field(default=None,max_length=2048)
    department:Optional[Literal['male','female']]=None
    filters:Optional[dict]=None
    context:Optional[dict]=None

async def search(body, sid):
    state=sessions[sid]
    body=body.model_copy(update={'department':state.get('wearer_department') or body.department})
    set_evaluation_department(body.department)
    try:
        options = {}
        if body.filters: options['filters'] = body.filters
        if body.context: options['context'] = body.context
        result=mock_products(body.query,body.cursor) if MODE=='mock' else await catalog.search_catalog(body.query,body.cursor,department=body.department,**options)
        items=result['products'][:10] if MODE=='mock' else [normalize(p) for p in result.get('products',[])[:10]]
        for product in items:
            product['search_department']=body.department
        if MODE=='live' and state.get('photo_assessment',{}) and state['photo_assessment'].get('status')=='ready' and tryon.provider is not None:
            await classify_missing_products(items,state.setdefault('classification_cache',{}))
        excluded=sum(p.get('garment_group') in EXCLUDED_GROUPS for p in items)
        items=[p for p in items if p.get('garment_group') not in EXCLUDED_GROUPS]
        if body.department:
            items=[p for p in items if not p.get('target_departments') or set(p['target_departments']).issubset({body.department,'unisex'})]
        if excluded:
            result.setdefault('messages',[]).append({'type':'warning','content':'Underwear and lingerie are excluded from this MVP. Some results were removed.'})
        sessions[sid]['products'].update({p['id']:p for p in items})
        return {'products':items,'pagination':result.get('pagination',{}),'mode':MODE,'query':body.query,'department':body.department,'filters':body.filters,'context':body.context,'warnings':[m.get('content','') for m in result.get('messages',[]) if m.get('type')=='warning' or m.get('code')=='price_filter_applied']}
    except CatalogError as exc:raise HTTPException(502,str(exc))
    except (KeyError,TypeError,ValueError):raise HTTPException(502,'The catalogue returned an unsupported product format.')

@app.get('/api/products')
async def get_products(query:str='everyday fashion',cursor:Optional[str]=None,department:Optional[Literal['male','female']]=None,sid:str=Depends(session)):
    return await search(Search(query=query,cursor=cursor,department=department),sid)

@app.post('/api/search')
async def search_products(body:Search,sid:str=Depends(session)):
    return await search(body,sid)

@app.get('/api/products/{product_id:path}')
async def product_detail(product_id:str,sid:str=Depends(session)):
    if MODE=='mock':return sessions[sid]['products'].get(product_id,{})
    try:return await catalog.get_product(product_id)
    except CatalogError as exc:raise HTTPException(502,str(exc))

class Lookup(BaseModel):
    ids:list[str]=Field(min_length=1,max_length=10)
@app.post('/api/catalog/lookup')
async def lookup(body:Lookup,sid:str=Depends(session)):
    if MODE=='mock':return {'products':[sessions[sid]['products'][i] for i in body.ids if i in sessions[sid]['products']]}
    try:return await catalog.lookup_catalog(body.ids)
    except CatalogError as exc:raise HTTPException(502,str(exc))

class Chat(BaseModel):
    message:str=Field(min_length=1,max_length=2000)
    department:Optional[Literal['male','female']]=None
@app.post('/api/chat')
async def chat(body:Chat,sid:str=Depends(session)):
    state=sessions[sid]
    body=body.model_copy(update={'department':state.get('wearer_department') or body.department})
    set_evaluation_department(body.department)
    if state.get('history_department')!=body.department:
        state['history']=[]
    state['history_department']=body.department
    filters = None
    context = None
    if MODE=='mock':
        query=' '.join([m['content'] for m in state['history'] if m['role']=='user']+[body.message])[-1000:]
        reply='I’ve updated your edit. Development mode uses sample products and a simple context search.'
    else:
        if not os.getenv('MODEL_API_KEY'):raise HTTPException(503,'Muse Spark is not configured. You can still use catalogue search.')
        # Muse supplies optional Shopify filters; the backend does not parse the user's words.
        tool = {'type': 'function', 'function': {
            'name': 'search_catalog',
            'description': 'Search fashion across merchants. Preserve prior requirements unless changed. Put explicit price, size and colour requirements in filters. Keep garment type, material and style in query. Omit filters the user did not request.',
            'parameters': {
                'type': 'object', 'additionalProperties': False,
                'properties': {
                    'query': {'type': 'string', 'description': 'Complete fashion query preserving the shopper wording and current preferences.'},
                    'filters': {
                        'type': 'object', 'additionalProperties': False,
                        'properties': {
                            'price': {'type': 'object', 'additionalProperties': False, 'properties': {
                                'min': {'type': 'integer', 'minimum': 0},
                                'max': {'type': 'integer', 'minimum': 0},
                            }, 'description': 'Inclusive price bounds in context.currency minor units. GBP 30 = 3000; strictly under GBP 30 = max 2999.'},
                            'attributes': {'type': 'array', 'items': {
                                'type': 'object', 'additionalProperties': False,
                                'properties': {
                                    'name': {'type': 'string', 'enum': ['Size', 'Color', 'Target gender']},
                                    'values': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1},
                                }, 'required': ['name', 'values'],
                            }},
                        },
                    },
                    'context': {
                        'type': 'object', 'additionalProperties': False,
                        'properties': {
                            'address_country': {'type': 'string', 'description': 'Two-letter country, default GB. Do not infer country from currency alone.'},
                            'currency': {'type': 'string', 'description': 'ISO currency, default GBP. Always supply when filtering price. Use explicit currency; bare amounts use GBP for this UK MVP.'},
                        },
                    },
                }, 'required': ['query'],
            },
        }}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response=await measured_post(client,'https://api.meta.ai/v1/chat/completions',operation='chat',provider='meta',model='muse-spark-1.3',headers={'Authorization':'Bearer '+os.environ['MODEL_API_KEY']},json={'model':'muse-spark-1.3','messages':[{'role':'system','content':f'You are a concise fashion stylist. Always call search_catalog with a complete query preserving conversational filters. Do not invent inventory. The selected shopping department is {body.department or "all"}. Keep the query consistent with that department.'}]+state['history'][-20:]+[{'role':'user','content':body.message}],'tools':[tool],'tool_choice':'auto'})
                logger.info('Muse Spark chat response: %s', response.text[:1000])
                response.raise_for_status()
                call=response.json()['choices'][0]['message']['tool_calls'][0]
                if call['function']['name']!='search_catalog':raise ValueError()
                arguments=json.loads(call['function']['arguments'])
                parsed=Search(**arguments,department=body.department)
                query=parsed.query
                filters=parsed.filters
                context=parsed.context
                reply='Your edit is ready. I’ve kept your preferences and searched across Shopify merchants.'
                logger.info('Muse Spark chat query=%s filters=%s context=%s department=%s',query,filters,context,body.department)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Muse Spark rejected request: HTTP %s; response=%s",
                exc.response.status_code,
                exc.response.text[:1000],
            )
            raise HTTPException(502, "Muse Spark rejected the request.")

        except (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError) as exc:
            logger.warning(
                "Muse Spark request/parsing failed: %s",
                type(exc).__name__,
            )
            raise HTTPException(502, "Muse Spark request or response failed.")
        # except (httpx.HTTPError,KeyError,ValueError,IndexError,TypeError):raise HTTPException(502,'Muse Spark could not complete this search. Please retry or use the search bar.')
    result=await search(Search(query=query,department=body.department,filters=filters,context=context),sid)
    state['history']=(state['history']+[{'role':'user','content':body.message},{'role':'assistant','content':json.dumps({'query':query,'filters':filters,'context':context})}])[-20:]
    return {**result,'reply':reply}

@app.post('/api/upload')
async def upload(file:UploadFile=File(...),department:Literal['male','female']=Form(...),sid:str=Depends(session)):
    set_evaluation_department(department)
    raw=await file.read(10*1024*1024+1)
    if len(raw)>10*1024*1024:raise HTTPException(413,'Choose a photo smaller than 10 MB.')
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.width*image.height>25_000_000:raise HTTPException(422,'Choose a photo under 25 megapixels.')
            from PIL import ImageOps
            image=ImageOps.exif_transpose(image).convert('RGB')
            image.thumbnail((2048,2048))
            normalized=io.BytesIO();image.save(normalized,format='JPEG',quality=90)
            photo_bytes=normalized.getvalue()
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError):raise HTTPException(422,'Upload a valid JPEG, PNG or WebP photo.')
    state=sessions[sid]
    async with state['upload_lock']:
        path=DATA/(uuid4().hex+'.jpg');path.write_bytes(photo_bytes)
        photo_id=hashlib.sha256(photo_bytes).hexdigest()
        old=state['photo']
        invalidate_previews(sid,'The photo changed. This queued preview was cancelled.')
        state.update(photo=str(path),photo_id=photo_id,wearer_department=department,cache={},
                     photo_assessment={'status':'checking','photo_id':photo_id,'reason':'Checking photo suitability.'})
        try:
            assessment=state['assessment_cache'].get(photo_id)
            record_event('photo.assessment_cache','hit' if assessment is not None else 'miss',kind='cache',cache_hit=assessment is not None,item_count=1)
            if assessment is None:
                assessment=await assess_photo(path)
                if assessment['status'] in ('ready','needs_new_photo'):
                    if len(state['assessment_cache'])>=8:state['assessment_cache'].pop(next(iter(state['assessment_cache'])))
                    state['assessment_cache'][photo_id]=assessment
            assessment={**assessment,'photo_id':photo_id}
            state['photo_assessment']=assessment
        finally:
            if old:Path(old).unlink(missing_ok=True)
    logger.info('Photo uploaded department=%s assessment=%s',department,assessment['status'])
    message=(
        'Photo checked. Previews will start automatically for compatible products with confirmed categories.'
        if assessment['status']=='ready' and tryon.provider is not None else
        'Photo saved successfully. Preview unavailable: no image-generation provider is connected.'
        if assessment['status']=='ready' else assessment.get('reason','Photo suitability could not be confirmed.')
    )
    return {'uploaded':True,'expires_in':86400,'try_on_configured':tryon.provider is not None,
            'wearer_department':department,'photo_assessment':assessment,'message':message}

@app.delete('/api/upload')
async def delete_photo(sid:str=Depends(session)):
    state=sessions[sid]
    set_evaluation_department(state.get('wearer_department'))
    async with state['upload_lock']:
        invalidate_previews(sid,'The photo was removed. This queued preview was cancelled.')
        if state['photo']:Path(state['photo']).unlink(missing_ok=True)
        state.update(photo=None,photo_id=None,photo_assessment=None,wearer_department=None,cache={},assessment_cache={})
    return {'deleted':True}

class Batch(BaseModel):
    product_ids:list[str]=Field(min_length=1,max_length=10)
    retry_failed:bool=False

async def run_job(jid,photo,image,image_attributes):
    job=jobs.get(jid)
    if not job or job['status']!='queued':return
    with evaluation_context(request_id=job.get('_request_id'),batch_id=job.get('_batch_id'),job_id=jid,department=job.get('_department')):
        with measure('preview.job',kind='job',retry=job.get('_retry',False),retry_of=job.get('_retry_of')) as metric:
            try:
                await _run_job(jid,photo,image,image_attributes)
            finally:
                metric['outcome']=job['status']
                if job.get('_started_at') is not None:
                    metric['queue_wait_ms']=round((job['_started_at']-job['_queued_at'])*1000,3)
                if job['status']=='completed':job['_completed_at']=time.perf_counter()
                if job.get('code'):metric['error_code']=job['code']


async def _run_job(jid,photo,image,image_attributes):
    # async with semaphore:
    async with app.state.try_on_semaphore:
        job=jobs.get(jid)
        if not job or job['status']!='queued':return
        state=sessions.get(job['owner'],{})
        if state.get('photo_id')!=job.get('photo_id'):
            job.update(status='blocked',message='The photo changed. Preview cancelled.',code='photo_changed')
            return
        decision=validate_try_on(state,state.get('products',{}).get(job['product_id'],{}))
        if not decision['allowed']:
            job.update(status='blocked',message=decision['reason'],code=decision['code'])
            return
        job['status']='processing'
        job['_started_at']=time.perf_counter()
        started=time.monotonic()
        logger.info('Try-on started job=%s product=%s provider=%s',jid,job['product_id'],type(tryon.provider).__name__)
        try:
            url=await asyncio.wait_for(tryon.provider.generate(Path(photo),image,image_attributes),timeout=120)
            if not url.startswith('https://'):raise ValueError('Invalid provider URL')
            if state.get('photo_id')!=job.get('photo_id'):
                job.update(status='blocked',message='The photo changed while generation was running.',code='photo_changed')
                return
            job.update(status='completed',image=url)
            logger.info('Try-on completed job=%s duration=%.1fs',jid,time.monotonic()-started)
        except tryon.TryOnError as exc:
            job.update(status='failed',message=str(exc))
            logger.warning('Try-on failed job=%s duration=%.1fs reason=%s',jid,time.monotonic()-started,str(exc))
        except Exception as exc:
            job.update(status='failed',message='Preview could not be generated. Please try again later.')
            logger.warning('Try-on failed job=%s duration=%.1fs error_type=%s',jid,time.monotonic()-started,type(exc).__name__)

async def run_batch(pending,observed_jobs=None,batch_id=None,submitted_at=None):
    outcomes=await asyncio.gather(*(run_job(*args) for args in pending),return_exceptions=True)
    for args,outcome in zip(pending,outcomes):
        if isinstance(outcome,BaseException):
            job=jobs.get(args[0])
            if job and job['status'] in ('queued','processing'):
                job.update(status='failed',message='The preview worker failed. Please retry.')
            logger.error('Try-on worker failed job=%s error_type=%s',args[0],type(outcome).__name__)
    if observed_jobs is not None:
        record_batch_timing(observed_jobs,batch_id,submitted_at)


def record_batch_timing(observed_jobs,batch_id,submitted_at):
    counts={status:sum(job['status']==status for job in observed_jobs) for status in ('completed','failed','blocked','unavailable','queued','processing')}
    completed=[max(0,(job.get('_completed_at',submitted_at)-submitted_at)*1000) for job in observed_jobs if job['status']=='completed']
    pending=counts['queued']+counts['processing']
    record_event('preview.batch','pending' if pending else 'finished',kind='batch_timing',batch_id=batch_id,
                 duration_ms=None if pending else round((time.perf_counter()-submitted_at)*1000,3),
                 first_preview_ms=round(min(completed),3) if completed else None,
                 completed=counts['completed'],failed=counts['failed'],blocked=counts['blocked'],
                 unavailable=counts['unavailable'],pending=pending)


@app.post('/api/try-on/batch')
async def batch(body:Batch,background:BackgroundTasks,sid:str=Depends(session)):
    state=sessions[sid]
    set_evaluation_department(state.get('wearer_department'))
    if not state['photo']:raise HTTPException(422,'Upload your photo first.')
    if any(pid not in state['products'] for pid in body.product_ids):raise HTTPException(422,'Search for these products first.')
    result=[]
    pending=[]
    reused=0
    batch_id=uuid4().hex
    submitted_at=time.perf_counter()
    for pid in dict.fromkeys(body.product_ids):
        product=state['products'][pid]
        cached_job=jobs.get(state['cache'].get(pid,''))
        decision=validate_try_on(state,product)
        logger.info(
            'Preview validation product=%s allowed=%s reason=%s '
            'selected_department=%s product_departments=%s garment_group=%s',
            pid,
            decision['allowed'],
            decision['code'],
            state.get('wearer_department'),
            product.get('target_departments'),
            product.get('garment_group'),
        )
        if not decision['allowed']:
            cached=jobs.get(state['cache'].get(pid,''))
            if cached and cached['status']=='blocked':
                cached.update(message=decision['reason'],code=decision['code']);result.append(cached);reused+=1
                continue
            jid=uuid4().hex
            job={'id':jid,'product_id':pid,'owner':sid,'photo_id':state.get('photo_id'),
                 'status':'blocked','message':decision['reason'],'code':decision['code']}
            jobs[jid]=job;state['cache'][pid]=jid;result.append(job)
            logger.info('Try-on blocked product=%s reason=%s selected_department=%s product_departments=%s search_department=%s garment_group=%s',pid,decision['code'],state.get('wearer_department'),product.get('target_departments'),product.get('search_department'),product.get('garment_group'))
            continue
        if pid in state['cache']:
            cached=jobs[state['cache'][pid]]
            retry_failure=cached['status']=='failed' and body.retry_failed
            newly_available=cached['status'] in ('unavailable','blocked') and tryon.provider is not None
            if not retry_failure and not newly_available:
                reused+=1
                result.append(cached);continue
        jid=uuid4().hex
        available=tryon.provider is not None and bool(state['products'][pid].get('image'))
        job={'id':jid,'product_id':pid,'owner':sid,'_product_url':product.get('url'),'photo_id':state.get('photo_id'),'_queued_at':time.perf_counter(),'_department':state.get('wearer_department'),'_batch_id':batch_id,'_request_id':current_ids().get('request_id'),'_retry':bool(body.retry_failed and cached_job and cached_job['status']=='failed'),'_retry_of':cached_job['id'] if body.retry_failed and cached_job and cached_job['status']=='failed' else None,'status':'queued' if available else 'unavailable','message':None if available else 'Preview unavailable — image-generation provider not connected.'}
        jobs[jid]=job;state['cache'][pid]=jid;result.append(job)
        logger.info('Try-on %s job=%s product=%s',job['status'],jid,pid)
        if available:
            pending.append((jid,state['photo'],state['products'][pid]['image'],{'colours': state['products'][pid].get('colours', [])}))
    record_event('preview.batch','submitted',kind='batch',batch_id=batch_id,requested=len(result),
                 new_generations=len(pending),reused=reused,blocked=sum(j['status']=='blocked' for j in result),
                 unavailable=sum(j['status']=='unavailable' for j in result))
    if reused:
        record_event('preview.job_cache','hit',kind='cache',batch_id=batch_id,cache_hit=True,item_count=reused)
    if len(result)>reused:
        record_event('preview.job_cache','miss',kind='cache',batch_id=batch_id,cache_hit=False,item_count=len(result)-reused)
    if pending:background.add_task(run_batch,pending,result,batch_id,submitted_at)
    else:record_batch_timing(result,batch_id,submitted_at)
    logger.info('Try-on batch requested=%d new_generations=%d reused=%d concurrency_limit=3',len(result),len(pending),reused)
    return {'jobs':[{k:v for k,v in j.items() if k!='owner' and not k.startswith('_')} for j in result]}

@app.get('/api/try-on/{job_id}')
async def get_job(job_id:str,sid:str=Depends(session)):
    job=jobs.get(job_id)
    if not job or job['owner']!=sid:raise HTTPException(404,'Preview not found.')
    return {k:v for k,v in job.items() if k!='owner' and not k.startswith('_')}

class PreviewFeedback(BaseModel):
    rating: Literal['like', 'dislike']
    reason: Optional[Literal['matches_expectations', 'wrong_garment_or_colour',
                            'identity_changed', 'anatomy_incorrect', 'unrealistic_fit']] = None

@app.post('/api/try-on/{job_id}/feedback')
async def save_preview_feedback(job_id: str, feedback: PreviewFeedback, sid: str = Depends(session)):
    job = jobs.get(job_id)
    if not job or job['owner'] != sid:
        raise HTTPException(404, 'Preview not found.')
    if job['status'] != 'completed':
        raise HTTPException(409, 'Feedback requires a completed preview.')
    if feedback.reason and ((feedback.reason == 'matches_expectations') != (feedback.rating == 'like')):
        raise HTTPException(422, 'Choose a reason that matches your rating.')
    row = {
        'timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
        'session_ref': hashlib.sha256(sid.encode()).hexdigest()[:12],
        'job_id': job_id,
        'product_id': job['product_id'],
        'rating': feedback.rating,
        'reason': feedback.reason or '',
        'generated_image_url': job.get('image') or '',
        'product_url': job.get('_product_url') or sessions[sid].get('products', {}).get(job['product_id'], {}).get('url') or '',
    }
    # Append each submission; no database, image data or bearer tokens are stored.
    path = DATA / 'feedback.csv'
    try:
        needs_header = not path.exists() or path.stat().st_size == 0
        # Upgrade the earlier CSV header once, preserving existing feedback rows.
        if not needs_header:
            with path.open(newline='', encoding='utf-8') as existing:
                reader = csv.DictReader(existing)
                old_fields = reader.fieldnames or []
                old_rows = list(reader) if old_fields != list(row) else None
            if old_rows is not None:
                temporary = path.with_suffix('.csv.tmp')
                with temporary.open('w', newline='', encoding='utf-8') as output:
                    writer = csv.DictWriter(output, fieldnames=list(row))
                    writer.writeheader()
                    writer.writerows(old_rows)
                temporary.replace(path)
        with path.open('a', newline='', encoding='utf-8') as output:
            writer = csv.DictWriter(output, fieldnames=list(row))
            if needs_header:
                writer.writeheader()
            writer.writerow(row)
    except OSError:
        logger.exception('Could not save preview feedback')
        raise HTTPException(503, 'Feedback could not be saved. Please retry.')
    return {'saved': True, **row}

@app.get('/api/health')
async def health():return {'status':'ok','mode':MODE,'muse_configured':bool(os.getenv('MODEL_API_KEY')),'try_on_configured':tryon.provider is not None}

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='0.0.0.0',port=8000)
