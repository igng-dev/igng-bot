import { createServer } from 'node:http';
import { MySQLStore } from './store.js';
import { SocialRuntime } from './runtime.js';
import { settings, equalSecret, PolicyError, bounded, integer } from './policy.js';
import { reconstruct } from './transcript.js';
export const name='yunying-social';
export const inject=['agentLoop','agents','sessions','sessionPersistence','tools','systemPrompt','skills','attachments','web'];
async function body(request) {
  let size=0;const chunks=[];
  for await(const chunk of request){size+=chunk.length;if(size>4*1024*1024)throw new PolicyError('request too large');chunks.push(chunk);}
  try{return JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw new PolicyError('invalid JSON');}
}
export async function route(request,runtime,config) {
  if(request.method==='GET'&&request.url==='/health')return {ok:runtime.store.healthy&&!runtime.stopping,version:'4.0.0',harness:'0.2.1-alpha.1'};
  if(request.method!=='POST')throw new PolicyError('method denied');
  const admin=request.url.startsWith('/admin/');
  const secret=admin?config.adminSecret:config.secret;
  if(!secret||!equalSecret(request.headers.authorization,'Bearer '+secret))throw new PolicyError('authentication denied');
  const data=await body(request);
  if(!admin) {
    if(request.url!=='/events')throw new PolicyError('capability denied');
    return runtime.accept(data);
  }
  // This route is intentionally absent from the model tool surface. Tokens never confer QQ membership authority.
  const store=runtime.store,actor={admin:true,key:'owner'};
  if(request.url==='/admin/memory/search')return store.memorySearch(actor,data);
  if(request.url==='/admin/memory/read')return store.memoryRead(actor,data.id,true);
  if(request.url==='/admin/memory/versions')return {ok:true,versions:await store.adminVersions(data.id)};
  if(request.url==='/admin/memory/update') {
    // Website edits retain immutable scope/visibility; CAS and version history are mandatory.
    const old=(await store.memoryRead(actor,data.id,true)).document;
    return store.transaction(async conn=>{
      const [rows]=await conn.execute('SELECT * FROM memory_documents WHERE id=? FOR UPDATE',[data.id]);
      const doc=rows[0];if(Number(doc.current_version)!==integer(data.expectedVersion,1,1e9))throw new PolicyError('version conflict');
      doc.title=bounded(data.title||doc.title,240);doc.markdown=bounded(data.markdown,24000,false);doc.current_version=Number(doc.current_version)+1;
      await conn.execute('UPDATE memory_documents SET title=?,markdown=?,current_version=? WHERE id=?',[doc.title,doc.markdown,doc.current_version,doc.id]);
      await store.revision(conn,actor,doc,[],'owner-edit',bounded(data.reason,500));return {ok:true,document:doc};
    });
  }
  if(request.url==='/admin/memory/rollback')return store.adminRollback(data.id,data.version,data.expectedVersion,data.reason);
  if(request.url==='/admin/memory/forget')return store.memoryUpdate(actor,data,true);
  if(request.url==='/admin/session/events') {
    const sessionId=bounded(data.sessionId,80);
    const events=await store.sessionEvents(sessionId,data.after,integer(data.limit,1,5000,1000));
    return {ok:true,sessionId,events};
  }
  if(request.url==='/admin/session/transcript') {
    // Ordered, model-visible flow reconstructed from the MySQL projection.
    const sessionId=bounded(data.sessionId,80);
    const events=await store.sessionEvents(sessionId,data.after,integer(data.limit,1,5000,5000));
    return {ok:true,sessionId,transcript:reconstruct(events)};
  }
  if(request.url==='/admin/session/rebuild') {
    // Operator repair: replay the authoritative official log into the projection.
    return runtime.rebuildTranscript(bounded(data.sessionId,80));
  }
  throw new PolicyError('capability denied');
}
export async function apply(ctx) {
  const config=settings();
  if(config.adminSecret&&(config.adminSecret.length<32||equalSecret(config.adminSecret,config.secret)))throw new Error('owner secret must be strong and separate from infrastructure secret');
  const store=await MySQLStore.open();
  const runtime=new SocialRuntime(ctx,store,config);
  let server,heartbeat,mirror,closed=false;
  const close=async()=>{
    if(closed)return;closed=true;
    clearInterval(heartbeat);clearInterval(mirror);
    if(server)await new Promise(resolve=>server.close(resolve));
    try{await runtime.close();}finally{await store.close();}
  };
  ctx.effect(()=>close);
  try {
    await runtime.restore();
    server=createServer(async(request,response)=>{
      try{const result=await route(request,runtime,config);response.writeHead(result.ok===false?503:200,{'content-type':'application/json'});response.end(JSON.stringify(result));}
      catch(error){response.writeHead(error instanceof PolicyError?403:503,{'content-type':'application/json'});response.end(JSON.stringify({ok:false,error:error instanceof PolicyError?error.message:'runtime unavailable'}));runtime.report(error);}
    });
    server.requestTimeout=30000;server.headersTimeout=10000;
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(config.port,config.host,resolve);});
    heartbeat=setInterval(()=>void store.heartbeat().then(()=>runtime.refreshPolicies()).catch(error=>{
      runtime.report(error);
      // A health failure alone does not make Docker restart a container. Exit this dedicated Profile.
      void close().finally(()=>process.kill(process.pid,'SIGTERM')).catch(error=>runtime.report(error));
    }),5000);heartbeat.unref();
    let mirroring=false;
    mirror=setInterval(()=>{if(mirroring)return;mirroring=true;void Promise.all([runtime.mirrorCalls(),runtime.flushTranscripts()]).catch(error=>runtime.report(error)).finally(()=>{mirroring=false;});},5000);mirror.unref();
    ctx.logger.info('YunYing native DSH Profile ready; official Session persistence, MySQL transcript projection and scoped social tools');
  }catch(error){await close();throw error;}
}
