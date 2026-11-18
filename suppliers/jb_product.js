// Julian Bowen (GOb2b shop): read ONE product with everything Shopify needs.
// Runs inside the logged-in browser (any page of trade.julian-bowen.co.uk). arguments[0] = product path.
const done = arguments[arguments.length - 1];
const path = arguments[0];
(async () => {
  const S = Aspidistra.Ecommerce.Web.ScriptServices.ProductScriptService;
  const pd = aspidistra.utilities.getPageDetails();
  const call = (fn, ...a) => new Promise(r => fn.call(S, ...a, x => r(x), e => r({err: e && e.get_message ? e.get_message() : String(e)})));
  const parse = h => new DOMParser().parseFromString(h, 'text/html');
  const txt = e => (e ? e.textContent.replace(/\s+/g, ' ').trim() : '');

  const res = await fetch(path, {credentials: 'include'});
  if (!res.ok) { done({status: res.status}); return; }
  const html = await res.text();
  const doc = parse(html);
  const node = (html.match(/product\.aspx\?node_id=([0-9a-f-]{36})/i) || [])[1];
  if (!node) { done({notProduct: true, loggedIn: pd.LoggedIn}); return; }
  const crumbs = [...doc.querySelectorAll('.Breadcrumb a, [class*=readcrumb] a')].map(txt).filter(Boolean);
  const title = txt(doc.querySelector('h1')) || (doc.title || '').replace(/\s*-\s*Julian Bowen Limited\s*$/i, '').trim();

  let skus = await call(S.GetSkusByMatrixProduct, node, {imageWidth: 1200, imageHeight: 1200, noImageAlt: ''});
  if (!Array.isArray(skus) || !skus.length) skus = [{SKU: node, Axes: [], StockCode: '', Name: title, ImageHref: ''}];
  const info = await call(S.GetFastPageInfo, Object.assign({}, pd, {ProductIds: skus.map(s => s.SKU)}));
  const byId = {};
  ((info && info.ProductInfo) || []).forEach(p => { byId[p.NodeId] = p; });

  const variants = [];
  for (const s of skus) {
    const p = byId[s.SKU] || {};
    const imgsHtml = await fetch('/ajax/product-images.aspx?node_id=' + s.SKU, {credentials: 'include'}).then(r => r.ok ? r.text() : '').catch(() => '');
    const images = [...new Set([...parse(imgsHtml).querySelectorAll('a.LightboxHref, a[data-lightbox-gallery]')]
      .map(a => a.getAttribute('href')).filter(u => u && /^https?:/.test(u)))];
    const cf = {};
    (p.ProductCustomFields || []).forEach(f => { cf[f.Name] = f.Value; });
    const due = (String(p.DueInDate || '').match(/\d{10,}/) || [])[0];
    variants.push({
      id: s.SKU, sku: p.StockCode || s.StockCode || '', name: p.Name || s.Name || '',
      options: (s.Axes || []).map(a => [a.Axis.Axis, a.Value.Name]),
      image: (s.ImageHref || '').replace(/_\d+x\d+(\.\w+)$/, '_1200x1200$1'), images,
      price: p.Price ? p.Price.Net : null, rrp: p.Price ? p.Price.RRPNet : null,
      freeStock: typeof p.FreeStock === 'number' ? p.FreeStock : null, stockLevel: p.StockLevel || '',
      stockMessage: txt(parse(p.StockMessage || '').body), purchasable: p.Purchasable !== false,
      nextQty: cf['Next Available Quantity'] || '', dueIn: due ? new Date(+due).toISOString().slice(0, 10) : '',
      gtin: p.GTIN || p.Barcode || '', brand: (p.Brand && typeof p.Brand === 'object') ? (p.Brand.Name || p.Brand.name || '') : (p.Brand || ''),
    });
  }

  // description / dimensions tabs of the first variant (shared by the product line)
  const tabsHtml = await fetch('/ajax/product-tabs.aspx?node_id=' + skus[0].SKU, {credentials: 'include'}).then(r => r.ok ? r.text() : '').catch(() => '');
  const td = parse(tabsHtml);
  const titles = [...td.querySelectorAll('.TabControls li a, ul.InfoTab a')].map(txt);
  const panels = [...td.querySelectorAll('.TabContentsContainer > .TabContent')];
  const tabs = {};
  panels.forEach((el, i) => {
    const t = titles[i] || ('Tab ' + i);
    if (/download/i.test(t)) return;
    tabs[t] = {html: el.innerHTML.trim(), lines: [...el.querySelectorAll('p, li, tr')].map(txt).filter(Boolean)};
  });
  done({node, title, crumbs, variants, tabs, loggedIn: pd.LoggedIn});
})().catch(e => done({error: String(e)}));
