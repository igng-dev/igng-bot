// Runtime compatibility only: donor's Prompt and HTML search stay untouched.
import {bingSearch,decodeHtml,sanitizeQuery} from '../donor/qq-bridge/web-functions.js';
import {safeFetch} from '../donor/qq-bridge/safe-fetch.js';
export function parseBingRss(body) {
  const results=[];
  const field=(item,name)=>decodeHtml((item.match(new RegExp(`<${name}\\b[^>]*>([\\s\\S]*?)<\\/${name}>`,'i'))?.[1]||'').replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g,'$1'));
  for(const match of body.matchAll(/<item\b[^>]*>([\s\S]*?)<\/item>/gi)) {
    const item=match[1],title=field(item,'title').slice(0,240),url=field(item,'link'),snippet=field(item,'description').slice(0,2000);
    let target;try{target=new URL(url);}catch{continue;}
    if(!title||!['http:','https:'].includes(target.protocol)||target.username||target.password)continue;
    results.push({title,url,snippet});if(results.length>=8)break;
  }
  return results;
}
export async function bingSearchWithFallback(query,dependencies={}) {
  query=sanitizeQuery(query);
  const original=await (dependencies.htmlSearch||bingSearch)(query);
  if(original.results.length)return original;
  // cn.bing.com's NAS response has no b_algo elements. Its public RSS endpoint
  // supplies the same read-only results without credentials or a broader URL surface.
  const url=new URL('https://cn.bing.com/search');url.searchParams.set('q',query);url.searchParams.set('format','rss');
  const response=await (dependencies.fetcher||safeFetch)(url.toString(),512000);
  if(response.statusCode<200||response.statusCode>=300)throw new Error(`搜索服务 HTTP ${response.statusCode}`);
  return {query,results:parseBingRss(response.body)};
}
