"""Read-only integration probe using Shopify's documented development profile."""
import asyncio, os, json
from app.catalog import ShopifyCatalog, normalize
os.environ.setdefault('UCP_AGENT_PROFILE_URL','https://shopify.dev/ucp/agent-profiles/examples/2026-08-25/valid-with-capabilities.json')
async def main():
    c=ShopifyCatalog()
    result=await c.search_catalog('black cotton t-shirt under £40')
    products=result.get('products',[])
    assert 0 < len(products) <= 10
    assert all(normalize(p)['image'] for p in products), 'Missing product images'
    assert len({normalize(p)['merchant'] for p in products}) > 1, 'Expected multiple merchants'
    print(json.dumps({'count':len(products),'pagination':result.get('pagination'),'merchants':list({normalize(p)['merchant'] for p in products}),'first':normalize(products[0]) if products else None},indent=2))
    if products:
        pid=products[0]['id']
        for name,data in [('lookup',await c.lookup_catalog([pid])),('detail',await c.get_product(pid))]:
            print(name,json.dumps(data)[:500])
        print('normalized',json.dumps(normalize(products[0])))
asyncio.run(main())
