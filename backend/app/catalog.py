import os
import json
import logging
import httpx
from .evaluation import measured_post, evaluation_context, current_ids
from .config import agent_profile_url
from .preview_validation import classify_product

ENDPOINT = 'https://catalog.shopify.com/api/ucp/mcp'
logger = logging.getLogger('uvicorn.error')

class CatalogError(Exception):
    pass

class ShopifyCatalog:
    async def call(self, name, catalog):
        profile = agent_profile_url()
        if not profile:
            raise CatalogError('Publish your UCP agent profile and set UCP_AGENT_PROFILE_URL to enable Shopify search.')
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if os.getenv('SHOPIFY_CATALOG_TOKEN'):
            headers['Authorization'] = 'Bearer ' + os.environ['SHOPIFY_CATALOG_TOKEN']
        request_id = current_ids().get('request_id', 'standalone')
        logger.info(
            'Shopify MCP request request_id=%s tool=%s catalog=%s',
            request_id, name, json.dumps(catalog, ensure_ascii=True),
        )
        try:
            async with httpx.AsyncClient(timeout=40) as client:
                response = await measured_post(client, ENDPOINT, operation='shopify.'+name, provider='shopify', headers=headers, json={
                    'jsonrpc':'2.0','id':1,'method':'tools/call',
                    'params':{'name':name,'arguments':{'meta':{'ucp-agent':{'profile':profile}},'catalog':catalog}}})
                response.raise_for_status()
                body = response.json()
            # logger.info(
            #     'Shopify MCP response request_id=%s tool=%s data=%s',
            #     request_id, name, json.dumps(body, ensure_ascii=True),
            # )
            if body.get('error'):
                raise CatalogError(body['error'].get('message','Shopify rejected the request.'))
            result = body.get('result',{})
            data = result.get('structuredContent')
            if not data or result.get('isError') or data.get('ucp',{}).get('status') == 'error':
                raise CatalogError('; '.join(m.get('content','') for m in (data or {}).get('messages',[])) or 'Invalid Shopify response.')
            return data
        except (httpx.HTTPError, ValueError) as exc:
            raise CatalogError('Shopify is currently unavailable. Please retry.') from exc

    async def search_catalog(self, query, cursor=None, department=None, filters=None, context=None):
        payload = {
            'query': query,
            'context': {'address_country': 'GB', 'currency': 'GBP', **(context or {})},
            'pagination': {'limit': 10, **({'cursor': cursor} if cursor else {})},
        }
        if filters:
            payload['filters'] = {**filters, 'attributes': list(filters.get('attributes', []))}
        target_gender = {'male': 'Male', 'female': 'Female'}.get(department)
        if target_gender:
            chosen = payload.setdefault('filters', {})
            chosen['attributes'] = [a for a in chosen.get('attributes', []) if a.get('name') != 'Target gender'] + [{
                'name': 'Target gender',
                'values': [target_gender, 'Unisex'],
            }]
        with evaluation_context(department=department):
            return await self.call('search_catalog', payload)

    async def lookup_catalog(self, ids):
        return await self.call('lookup_catalog', {'ids':ids,'context':{'address_country':'GB','currency':'GBP'}})

    async def get_product(self, product_id):
        return await self.call('get_product', {'id':product_id,'context':{'address_country':'GB','currency':'GBP'}})


def normalize(p):
    variants = p.get('variants') or []
    v = variants[0] if variants else {}
    price = (p.get('price_range') or {}).get('min') or v.get('price') or {}
    images = [m for m in (p.get('media') or v.get('media') or []) if m.get('type') == 'image'] or p.get('images') or []
    image = images[0] if images else p.get('featured_image') or {}
    seller = v.get('seller') or p.get('seller') or {}
    options = p.get('options') or []
    def values(name):
        return [x.get('label',str(x)) if isinstance(x,dict) else str(x) for o in options if o.get('name','').lower() in name for x in o.get('values',[])]
    category = p.get('category') or p.get('categories') or p.get('taxonomy_category') or (p.get('metadata') or {}).get('category')
    product_type = p.get('product_type') or p.get('productType')
    gender_labels = values(['target gender', 'gender'])
    gender_labels += [o.get('label', '') for o in v.get('options', []) if o.get('name', '').lower() in ('target gender', 'gender')]
    aliases = {'male': 'male', 'men': 'male', 'mens': 'male', 'female': 'female', 'women': 'female', 'womens': 'female', 'unisex': 'unisex'}
    departments = sorted({aliases[x.strip().lower()] for x in gender_labels if x.strip().lower() in aliases})
    description=p.get('description') or v.get('description') or {}
    description=(description.get('plain') or description.get('html') or '') if isinstance(description,dict) else str(description)
    result = {'id':p['id'],'description':description,'title':p.get('title','Untitled product'),'image':image.get('url') if isinstance(image,dict) else image,
            'merchant':seller.get('name') or p.get('vendor') or 'Shopify merchant',
            'price':price.get('amount',0)/(1 if price.get('currency') in ['JPY','KRW'] else 1000 if price.get('currency') in ['BHD','KWD','OMR'] else 100),'currency':price.get('currency','GBP'),
            'url':p.get('url') or v.get('url') or v.get('checkout_url'),
            'colours':values(['color','colour']),'sizes':values(['size']),
            'category':category,'product_type':product_type,'target_departments':departments}
    result['garment_group'] = classify_product(result)
    return result
