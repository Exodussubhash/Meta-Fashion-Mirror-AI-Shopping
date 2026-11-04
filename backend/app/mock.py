"""Development fixtures only. Never used as a live-service fallback."""
ITEMS = [
('The everyday cotton tee','ARKET',25,'black','t-shirt','photo-1521572163474-6864f9cf17ab'),
('Relaxed linen shirt','COS',59,'white','shirt','photo-1596755094514-f87e34085b2c'),
('The oversized crewneck','Weekday',35,'black','t-shirt','photo-1503341504253-dff4815485f1'),
('Straight-leg denim','Everlane',88,'blue','jeans','photo-1542272604-787c3835535d'),
('Soft knit cardigan','& Other Stories',69,'cream','knitwear','photo-1434389677669-e08b4cac3105'),
('Everyday leather tote','MATT & NAT',95,'brown','accessories','photo-1553062407-98eeb64c6a62'),
('The essential white tee','Organic Basics',29,'white','t-shirt','photo-1521572163474-6864f9cf17ab'),
('Tailored wool blazer','COS',135,'black','outerwear','photo-1591047139829-d91aecb6caea'),
('Weekend cotton shirt','ARKET',49,'blue','shirt','photo-1596755094514-f87e34085b2c'),
('Relaxed cotton trousers','Everlane',75,'cream','trousers','photo-1473966968600-fa801b869a1a'),
('Heavyweight cotton tee','Weekday',30,'black','t-shirt','photo-1503341504253-dff4815485f1'),
('Merino knit sweater','ARKET',79,'cream','knitwear','photo-1434389677669-e08b4cac3105')]
def products(query, cursor):
    import re
    words=query.lower()
    data=[{'id':f'mock-{i}','title':t,'merchant':m,'price':p,'currency':'GBP','colours':[c],'sizes':['XS','S','M','L','XL'],'target_departments':['unisex'],'garment_group':{'t-shirt':'top','shirt':'top','jeans':'bottom','knitwear':'top','outerwear':'outerwear','trousers':'bottom'}.get(kind,'unknown'),'url':None,'image':f'https://images.unsplash.com/{img}?auto=format&fit=crop&w=700&q=80'} for i,(t,m,p,c,kind,img) in enumerate(ITEMS) if (not any(k in words for k in ['t-shirt','shirt','jeans','knitwear','outerwear','trousers','accessories']) or kind in words)]
    for c in ['black','white','blue','cream','brown']:
        if c in words: data=[p for p in data if c in p['colours']]
    maximum=re.search(r'under\s*[£$]?(\d+)',words)
    if maximum:data=[p for p in data if p['price']<float(maximum[1])]
    start=int(cursor or 0)
    return {'products':data[start:start+10],'pagination':{'has_next_page':start+10<len(data),'cursor':str(start+10),'total_count':len(data)},'mode':'mock'}
