import test from 'node:test';
import assert from 'node:assert/strict';
import {bingSearchWithFallback,parseBingRss} from '../src/search.js';
test('Bing retains a successful donor HTML result unchanged',async()=>{
 const original={query:'原查询',results:[{title:'文档',url:'https://example.org',snippet:'说明'}]};
 assert.equal(await bingSearchWithFallback('原查询',{htmlSearch:async()=>original,fetcher:async()=>{throw new Error('unnecessary fetch');}}),original);
});
test('Bing RSS fallback preserves the tool result contract and uses the bounded safe transport',async()=>{
 let requested;
 const result=await bingSearchWithFallback('Docker [CQ:at,qq=all]  Compose',{htmlSearch:async()=>({results:[]}),fetcher:async(url,limit)=>{
  requested={url:new URL(url),limit};
  return {statusCode:200,body:'<rss><channel><item><title>Docker &amp; Compose</title><link>https://docs.docker.com/compose/</link><description><![CDATA[<b>Official</b> documentation]]></description></item></channel></rss>'};
 }});
 assert.equal(requested.url.origin,'https://cn.bing.com');assert.equal(requested.url.searchParams.get('format'),'rss');assert.equal(requested.limit,512000);
 assert.deepEqual(result,{query:'Docker Compose',results:[{title:'Docker & Compose',url:'https://docs.docker.com/compose/',snippet:'Official documentation'}]});
 await assert.rejects(bingSearchWithFallback('q',{htmlSearch:async()=>({results:[]}),fetcher:async()=>({statusCode:503})}),/HTTP 503/);
});
test('Bing RSS bounds results and ignores unusable or embedded-credential links',()=>{
 const xml='<item><title>拒绝</title><link>https://secret@example.org/</link></item><item><title>拒绝</title><link>file:///etc/passwd</link></item>'+
  Array.from({length:12},(_,i)=>`<item><title>文档${i}</title><link>https://example.org/${i}</link></item>`).join('');
 const result=parseBingRss(xml);assert.equal(result.length,8);assert.equal(result[0].title,'文档0');
});
