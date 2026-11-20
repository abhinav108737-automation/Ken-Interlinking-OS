import fs from 'node:fs/promises';
import {JSDOM} from 'jsdom';

const env = Object.fromEntries((await fs.readFile('.env','utf8')).split(/\r?\n/).filter(x=>x.trim()&&!x.startsWith('#')&&x.includes('=')).map(x=>{const i=x.indexOf('=');return [x.slice(0,i).trim(),x.slice(i+1).trim().replace(/^['"]|['"]$/g,'')];}));
const base=env.KEN_CMS_BASE_URL.replace(/\/$/,'');
if(!['thekenconsulting.com','www.thekenconsulting.com'].includes(new URL(base).hostname))throw Error('Unexpected host');
const discovery=JSON.parse(await fs.readFile('outputs/contextual-v02-shadow/new_candidate_evidence.json','utf8'));
const inventory=new Map();
for(const item of discovery.contextual_evidence_matches)for(const row of [item.source,item.target])if(row.slug.startsWith('kuwait-cloud-based-'))inventory.set(row.slug,row);
// The inventory file is authoritative, including reports with no keyword discovery matches.
const csv=await fs.readFile('data/v02-contextual-review/v02_reports_test_interlinking.csv','utf8');
for(const slug of new Set(csv.match(/kuwait-cloud-based-[a-z0-9-]+-market/g)))if(!inventory.has(slug))inventory.set(slug,{slug,url:`https://www.thekenconsulting.com/industry-reports/${slug}`});
const allowed=new Set(['v02_growth_drivers','v02_market_trajectory','v02_market_segmentation','v02_regional_analysis']);
const norm=x=>String(x).replace(/\s+/g,' ').trim();
const normalizeUrl=value=>{const url=new URL(value);url.hash='';url.search='';return url.href.replace(/\/$/,'');};
const rows=[...inventory.values()].sort((a,b)=>a.slug.localeCompare(b.slug));
const results=[];
for(let i=0;i<rows.length;i+=3){
  await Promise.all(rows.slice(i,i+3).map(async row=>{
    const headers={Accept:'application/json',Authorization:`Bearer ${env.KEN_CMS_READ_TOKEN}`};
    const [cms,publicPage]=await Promise.all([fetch(`${base}/internal/reports/${row.slug}/`,{headers,signal:AbortSignal.timeout(45000)}),fetch(row.url,{signal:AbortSignal.timeout(45000)})]);
    if(!cms.ok)throw Error(`${row.slug}: CMS ${cms.status}`);
    const data=await cms.json();
    const document=new JSDOM(await publicPage.text(),{url:row.url}).window.document;
    const canonicalRaw=document.querySelector('link[rel~="canonical"]')?.getAttribute('href')||'';
    const canonicalUrl=canonicalRaw?new URL(canonicalRaw,row.url).href:'';
    const robotsMeta=[...document.querySelectorAll('meta[name="robots"],meta[name="googlebot"]')].map(x=>norm(x.getAttribute('content')).toLowerCase()).filter(Boolean);
    const xRobots=norm(publicPage.headers.get('x-robots-tag')).toLowerCase();
    const directives=[...robotsMeta,xRobots].join(',');
    const finalUrl=publicPage.url||row.url;
    const canonicalMatches=Boolean(canonicalUrl)&&normalizeUrl(canonicalUrl)===normalizeUrl(row.url);
    const indexability={
      http_status:publicPage.status,
      final_url:finalUrl,
      canonical_url:canonicalUrl||null,
      canonical_matches_destination:canonicalMatches,
      robots_directives:directives||'not specified',
      noindex:/\bnoindex\b/.test(directives),
      nofollow:/\bnofollow\b/.test(directives),
      technically_indexable:publicPage.ok&&!/\bnoindex\b/.test(directives)&&canonicalMatches&&data.protected!==true,
      google_index_status:'unknown_without_search_console',
      ranking_status:'unknown_without_search_console'
    };
    document.querySelectorAll('script,style,noscript,template,[hidden],[aria-hidden="true"]').forEach(x=>x.remove());
    const publicText=norm(document.body.textContent);
    const publicLinks=[...document.querySelectorAll('a[href]')].map(a=>({path:new URL(a.href,row.url).pathname,text:norm(a.textContent)}));
    const fields=(data.fields||[]).filter(f=>allowed.has(f.field)).map(f=>{
      const doc=new JSDOM(f.body_html||'').window.document;
      return {field:f.field,version:f.version,blocks:[...doc.querySelectorAll('p,li')].filter(x=>!x.closest('table')).map(x=>({text:norm(x.textContent),html:x.outerHTML,public_visible:publicPage.ok&&publicText.includes(norm(x.textContent))})).filter(x=>x.text.split(' ').length>=10),links:[...doc.querySelectorAll('a[href]')].map(a=>({path:new URL(a.href,row.url).pathname,text:norm(a.textContent)}))};
    });
    results.push({...row,title:data.title||row.title,protected:data.protected,public_status:publicPage.status,indexability,public_links:publicLinks,fields});
    console.log(`${row.slug}: ${fields.reduce((n,f)=>n+f.blocks.filter(b=>b.public_visible).length,0)} visible blocks`);
  }));
}
results.sort((a,b)=>a.slug.localeCompare(b.slug));
await fs.writeFile('outputs/contextual-v02-shadow/kuwait_topic_evidence.json',JSON.stringify({generated_at:new Date().toISOString(),mode:'read_only',reports:results},null,2)+'\n');
console.log(`Audited ${results.length} reports; no CMS writes.`);
