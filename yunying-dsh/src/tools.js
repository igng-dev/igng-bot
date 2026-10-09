import { readFileSync } from 'node:fs';
import { setTimeout as sleep } from 'node:timers/promises';
import { serializeModelData } from '../donor/qq-bridge/qq-model-view.js';
import { sanitizeQuery, decodeHtml } from '../donor/qq-bridge/web-functions.js';
import { bingSearchWithFallback } from './search.js';
import { safeFetch } from '../donor/qq-bridge/safe-fetch.js';
import { authorize, PolicyError, bounded, integer, safeNetworkQuery } from './policy.js';
const descriptions = JSON.parse(readFileSync(new URL('../donor/qq-bridge/tool-descriptions.json', import.meta.url)));
const str = description => ({ type: 'string', description });
const num = description => ({ type: 'integer', description });
const any = {};
function supportedSchema(value) {
  if(Array.isArray(value))return value.map(supportedSchema);
  if(!value||typeof value!=='object')return value;
  return Object.fromEntries(Object.entries(value).filter(([key])=>!['minimum','maximum','minItems','maxItems'].includes(key)).map(([key,child])=>[key,supportedSchema(child)]));
}
const object = (properties, required = []) => supportedSchema({ type: 'object', properties, required, additionalProperties: false });
const numberOrString = { oneOf: [{type:'string'}, {type:'integer'}] };
const props = { key: str('会话 key：group:群号 或 private:QQ号'), token: str('会话令牌，见唤醒提示'),
  throughSeq: { type:'integer', minimum:0, description:'已查看结果里的连续安全 readThroughSeq' } };
const memorySources = { type:'array', items:str('当前会话已查看消息的 eventId'), minItems:1, maxItems:12 };
const memoryProps = { ...props, id:str('记忆文档 UUID'), title:str('标题'), markdown:str('Markdown 语义内容'),
  sources:memorySources, reason:str('值得记忆或更新的原因'), expectedVersion:num('读取结果里的 current_version'),
  personQQ:str('个人 QQ 号：写入其个人跨群记忆，必须有该人本人消息来源') };
export function registerTools(ctx, runtime) {
  const { state, store, config, infra } = runtime;
  const names = new Set(['skill']);
  const actor = () => ({ key:state.key, sessionId:state.sessionId, seenSeqs:state.modelSeenSeqs, qqs:state.actorQqs() });
  const register = (name, properties, required, execute, extras = {}, description) => {
    const definition = {
      name, description: description || descriptions[name] || name,
      parameters: object(properties, required),
      output: { schema:{type:'object',additionalProperties:true}, render:(_args,value)=>[{type:'text',text:serializeModelData(value)}] },
      timeoutMs: name.includes('wait_for_messages') ? 735000 : name.includes('send') ? 120000 : name.includes('web_') ? 45000 : 30000,
      // DSH's tool scheduler supplies exclusive mutation ordering and cooperative cancellation.
      async execute(args, exec) {
        authorize(state, args, exec); exec.signal.throwIfAborted();
        await runtime.refreshPolicy();
        if (!store.healthy || !runtime.permitted()) throw new PolicyError('会话权限已关闭或运行实例已失去权限');
        try{return await execute(args,exec);}
        catch(error){
          if(name.startsWith('memory_'))await store.audit(actor(),name.slice(7)+'-denied',null,false,{error:error.name||'Error'}).catch(()=>{});
          if(error instanceof PolicyError || exec.signal.aborted)throw error;
          // Keep host SQL, local paths and provider diagnostics outside untrusted model data.
          runtime.report?.(error);
          throw new PolicyError('工具暂不可用，请稍后重试');
        }
      }, ...extras,
    };
    names.add(name); ctx.tools.register(definition);
    return definition;
  };
  const standard = (name, properties, execute, extra = {}, required = []) => register(name, {...props,...properties}, ['key','token',...required], execute, extra);
  standard('qq_get_prompt', {}, async () => ({ ok:true, key:state.key,
    role:{ role:'云萤', content:'你是云萤，长期在线的 QQ 群友。按原始二代仿真规则参与。', version:1 },
    recommendations: { defaultInfinite:true, sleepMinMs:300000, sleepMaxMs:7200000, probability:.20,
      atMention:true, nameMention:true, question:true, preSleepWaitMs:config.preSleepWaitMs },
    enabledTools:[...names], disabledTools:['qq_send_voice','qq_list_voices','qq_set_sticker_remark',
      'qq_get_self_image','qq_list_stickers','qq_send_sticker','qq_collect_sticker','qq_get_sticker_image','qq_sticker_note'],
    ...state.unreadPage(30,0), wakeConfig:state.wakeConfig, participation:{chatMode:state.chatMode,calledTurn:!!runtime.directEventId}, replyTiming:state.replyTiming(),
    memory:{activeTopics:state.activeTopics,pendingThoughts:state.pendingThoughts,memberImpressions:state.memberImpressions},
    longTermMemory:{skill:'yunying-memory',sourceOfTruth:'MySQL',personMemoryCrossGroup:true},
    safety:{currentConversationOnly:true,untrustedMemberInput:true,noShellOrFilesystem:true} }));
  standard('qq_get_unread_messages', {limit:num('默认30，最大100'),afterSeq:num('从该 seq 后按时间顺序补读；最早传0')}, args => state.unreadPage(args.limit,args.afterSeq));
  standard('qq_get_recent_messages', {limit:num('默认20，最大100'),offset:num('向前翻页')}, async args => {
    const result = await infra('/history',{key:state.key,limit:integer(args.limit,1,100,20),offset:integer(args.offset,0,10000,0)});
    // Add local seq receipts only for messages actually returned from the same history page.
    const ids = new Set(result.messages.map(m=>m.messageId));
    const local = state.recentMessages.filter(m=>ids.has(m.messageId)&&m.seq);
    return {...result,messages:result.messages.map(m=>{const item=local.find(v=>v.messageId===m.messageId);return item?{...m,seq:item.seq,eventId:item.eventId}:m;}),key:state.key,readThroughSeq:state.note(local),unreadCount:state.unread.length};
  });
  standard('qq_social_state', {}, async () => ({ok:true,key:state.key,wakeConfig:state.wakeConfig,
    unreadCount:state.unread.length,lastReadThroughSeq:state.lastReadThroughSeq,lastWakeReason:state.lastWakeReason,
    lastAiReplyAt:state.lastAiReplyAt,replyTiming:state.replyTiming(),preSleepWaitBlocked:state.preSleepBlocked()}));
  standard('qq_mark_read', {}, async (args,exec) => {
    if (state.preSleepBlocked()) throw new PolicyError('潜水前先完成沉睡前观察：qq_wait_for_messages(timeoutMs=300000)');
    const removed=state.acknowledge(args.throughSeq);
    state.lastActionAt=Date.now();state.wakeConfig.confirmedAt=Date.now();state.wakeConfig.confirmedBy='agent';state.ensureWakeable();
    await store.saveState(state); runtime.closedThrough=state.lastReadThroughSeq;
    exec.concludeTurn(); runtime.schedule();
    return {ok:true,removed,unreadCount:state.unread.length,readThroughSeq:state.readThrough(),wakeConfig:state.wakeConfig};
  });
  standard('qq_set_wake_config', { config:object({mode:{type:'string',enum:['active','diving']},infinite:{type:'boolean'},
    sleepMs:{type:'number'},sleepUntil:str('ISO 时间'),triggers:object({atMention:{type:'boolean'},nameMention:{type:'boolean'},
    question:{type:'boolean'},poke:{type:'boolean'},anyMessage:{type:'boolean'},probability:{type:'number',minimum:0,maximum:1},
    speakerIds:{type:'array',items:numberOrString,maxItems:20},keywords:{type:'array',items:str('关键词'),maxItems:20}}),batchWindowMs:num('合批窗口毫秒')}) }, async (args,exec) => {
    const result=state.setWake(args.config,args.throughSeq);await store.saveState(state);
    runtime.closedThrough=state.lastReadThroughSeq; exec.concludeTurn(); runtime.schedule();return result;
  },{},['config']);
  standard('qq_wait_for_messages', { purpose:{type:'string',enum:['messages','reply']}, timeoutMs:{type:'number'},
    minNewMessages:{type:'number'},quietMs:{type:'number'} }, async (args,exec)=>{
    runtime.cancelReplyCheck();
    const started=Date.now();
    let result=await state.wait(args,exec.signal);
    // The donor prompt tells the model to continue waiting while the pre-sleep
    // observation still owes credit ("按剩余时间继续等待"). Each continuation
    // would otherwise be another full model step resending the whole Session.
    // Finish the same observation inside this one tool call. The donor only
    // grants credit inside one continuous call, so a continuation waits a full
    // pre-sleep window, bounded by the chain budget and the tool timeout.
    const chainBudget=Math.max(0,Number(runtime.config.waitChainMs)||0);
    if(args.purpose!=='reply'&&chainBudget>0){
      const deadline=started+Math.min(chainBudget,700000);
      const preSleepMs=Math.max(0,Number(runtime.config.preSleepWaitMs)||0);
      while(!result.arrived&&!result.preSleepWaitSatisfied&&Date.now()<deadline){
        const remaining=Math.max(0,Number(result.preSleepWaitRemainingMs)||0);
        const timeoutMs=Math.min(600000,Math.max(preSleepMs,remaining),Math.max(1,deadline-Date.now()));
        result=await state.wait({...args,timeoutMs},exec.signal);
      }
    }
    await store.saveState(state);return result;
  });
  const sendProperties = { messages:{oneOf:[{type:'string'},{type:'array',items:{type:'string'},minItems:1,maxItems:8}]},
    replyToMessageId:numberOrString, atUserId:numberOrString, gapMode:{type:'string',enum:['auto','fixed','byLength']},
    gapMs:{type:'number'},gaps:{type:'array',items:{type:'number'},maxItems:7} };
  const sendMessages = async (args,exec) => {
    let messages=args.messages;
    if(typeof messages==='string' && /^[\[\"]/.test(messages.trim())) {try {const parsed=JSON.parse(messages);if(typeof parsed==='string'||Array.isArray(parsed))messages=parsed;}catch {}}
    messages=Array.isArray(messages)?messages:[messages];
    if(!messages.length||messages.length>8)throw new PolicyError('一轮最多发送8条');
    for(const text of messages)bounded(text,config.maxMessageChars);
    const now=Date.now(),minute=state.sendTimes.filter(t=>now-t<60000),hour=state.sendTimes.filter(t=>now-t<3600000);
    if(minute.length+messages.length>config.maxSendMinute||hour.length+messages.length>config.maxSendHour)throw new PolicyError('发送频率超限，请等待');
    const results=[];
    for(let index=0;index<messages.length;index++) {
      exec.signal.throwIfAborted();
      if(index>0) {
        let gap=args.gapMode==='fixed'?Number(args.gaps?.[index-1]??args.gapMs??1000):args.gapMode==='byLength'?800+messages[index].length*20:config.sendMinMs+Math.random()*(config.sendMaxMs-config.sendMinMs);
        if(args.gapMode==='auto'&&Math.random()<.2)gap=5000+Math.random()*5000;
        await sleep(Math.max(0,Math.min(10000,gap||0)),undefined,{signal:exec.signal});
      }
      const result=await infra('/send',{key:state.key,requestId:`${state.sessionId}:${exec.callId}:${index}`,
        triggerEventId:runtime.directEventId,message:messages[index],replyToMessageId:args.replyToMessageId,atUserId:args.atUserId},exec.signal);
      results.push(result);if(!result.ok)break;
      state.sent(result.message_id,messages[index]);await store.saveState(state);runtime.scheduleReplyCheck();
    }
    return {ok:results.length===messages.length&&results.every(r=>r.ok),key:state.key,results};
  };
  const send=standard('qq_send_message',sendProperties,sendMessages,{},['messages']);
  register('mcp__snowluma__qq_send_message', {...props,...sendProperties},['key','token','messages'],sendMessages,{},send.description);
  standard('qq_send_burst',{...sendProperties},sendMessages,{},['messages']);
  // qq_reply preserves donor's groupId/message/replyToMessageId shape while remaining bound to exec.agent.
  const replyProps={...props,groupId:numberOrString,message:str('纯文本'),replyToMessageId:numberOrString};
  const replyExecute=async(args,exec)=>{
    if(args.groupId!==undefined&&state.key!==`group:${args.groupId}`)throw new PolicyError('回复目标不是当前会话');
    return sendMessages({...args,messages:args.message},exec);
  };
  register('qq_reply',replyProps,['key','token','message','replyToMessageId'],replyExecute);
  register('mcp__snowluma__qq_reply',replyProps,['key','token','message','replyToMessageId'],replyExecute,{},descriptions.qq_reply);
  standard('qq_send_poke',{targetUserId:numberOrString},async(args,exec)=>{
    const now=Date.now();if(state.sendTimes.filter(t=>now-t<60000).length>=config.maxSendMinute)throw new PolicyError('发送频率超限');
    const result=await infra('/poke',{key:state.key,triggerEventId:runtime.directEventId,userId:String(args.targetUserId||state.key.split(':')[1]),requestId:`${state.sessionId}:${exec.callId}:poke`},exec.signal);
    if(result.ok){state.sent(null,'[拍一拍]');await store.saveState(state);}return result;
  });
  const resolveMessageId = supplied => {
    const exact=state.recentMessages.find(m=>m.messageId===String(supplied));if(exact)return exact.messageId;
    const local=state.recentMessages.find(m=>m.seq===Number(supplied));return local?.messageId||String(supplied);
  };
  standard('qq_get_message_detail',{messageId:numberOrString},async args=>{
    const result=await infra('/detail',{key:state.key,messageId:resolveMessageId(args.messageId)});
    const local=state.unread.filter(m=>m.messageId===result.message.messageId);return {...result,readThroughSeq:state.note(local)};
  },{},['messageId']);
  standard('qq_get_message_images',{messageId:numberOrString},async(args,exec)=>{
    const result=await infra('/images',{key:state.key,messageId:resolveMessageId(args.messageId)},exec.signal);
    const content=await ctx.attachments.admitPromptContent(result.images.map(img=>({type:'image',mediaType:img.mediaType,data:img.data})));
    // Persist DSH attachment references, never transient paths/base64 in a tool result.
    return {ok:true,messageId:String(args.messageId),images:content.map(c=>c.attachment),isRecalled:result.isRecalled};
  },{output:{schema:{type:'object',additionalProperties:true},render:(_args,value)=>[
    {type:'text',text:serializeModelData({...value,images:undefined,count:value.images.length})},
    ...value.images.map(attachment=>({type:'image',attachment}))]}},['messageId']);
  standard('qq_get_my_recent_messages',{limit:num('默认20，最多100')},async args=>{
    const result=await infra('/history',{key:state.key,limit:integer(args.limit,1,100,20)});
    return {ok:true,messages:result.messages.filter(m=>m.isSelf)};
  });
  standard('qq_get_active_members',{limit:num('最多20')},async args=>{
    const members=new Map();for(const m of state.recentMessages)if(m.userId&&!m.isSelf)members.set(m.userId,{userId:m.userId,sender:m.sender||m.userId});
    return {ok:true,members:[...members.values()].slice(-integer(args.limit,1,20,20))};
  });
  standard('qq_get_forward_msg',{id:str('当前会话收到的转发 id')},async args=>{
    const history=await infra('/history',{key:state.key,limit:100});
    const found=[];
    const walk=(value,depth=0)=>{if(depth>12||!value)return;if(Array.isArray(value)){for(const v of value)walk(v,depth+1);}
      else if(typeof value==='object'){if(value.type==='forward'&&(String(value.id||value.forward_id||'')===args.id))found.push(value);for(const v of Object.values(value))if(v&&typeof v==='object')walk(v,depth+1);}};
    for(const msg of history.messages)if(!msg.isRecalled)walk(msg.structure);
    if(!found.length)throw new PolicyError('该转发不在当前会话已收到的历史中');return {ok:true,id:args.id,messages:found};
  },{},['id']);
  const shortCategories={activeTopic:'activeTopics',pendingThought:'pendingThoughts',memberImpression:'memberImpressions'};
  standard('qq_memory_query',{category:{type:'string',enum:Object.keys(shortCategories)}},async args=>({ok:true,
    memory:args.category?state[shortCategories[args.category]]:{activeTopics:state.activeTopics,pendingThoughts:state.pendingThoughts,memberImpressions:state.memberImpressions}}));
  standard('qq_memory_append',{category:{type:'string',enum:Object.keys(shortCategories)},content:str('轻量记忆'),extra:object({target:str('群友昵称'),participants:{type:'array',items:str('参与者')},pendingQuestion:str('问题'),motivation:str('动机'),expiresAtMs:{type:'number'}})},async args=>{
    const field=shortCategories[args.category];bounded(args.content,1000);const entry={content:args.content,...args.extra,createdAt:Date.now()};
    if(args.category==='memberImpression'){bounded(args.extra?.target,120);state[field][args.extra.target]=entry;if(Object.keys(state[field]).length>50)delete state[field][Object.keys(state[field])[0]];}
    else {if(args.category==='pendingThought')entry.expiresAt=Date.now()+Math.min(7200000,Number(args.extra?.expiresAtMs)||7200000);state[field].push(entry);state[field]=state[field].slice(-30);}
    await store.saveState(state);return {ok:true};
  },{},['category','content']);
  standard('qq_memory_remove',{category:{type:'string',enum:Object.keys(shortCategories)},content:str('原文'),target:str('群友昵称')},async args=>{
    const field=shortCategories[args.category];if(args.category==='memberImpression')delete state[field][args.target];else state[field]=state[field].filter(e=>e.content!==args.content);
    await store.saveState(state);return {ok:true};
  },{},['category']);
  standard('qq_memory_clear',{category:{type:'string',enum:Object.keys(shortCategories)}},async args=>{
    for(const [category,field]of Object.entries(shortCategories))if(!args.category||args.category===category)state[field]=category==='memberImpression'?{}:[];
    await store.saveState(state);return {ok:true};
  });
  standard('memory_search',{query:str('检索词；空字符串列出当前可见记忆'),limit:num('最多30')},args=>store.memorySearch(actor(),args),{},['query']);
  standard('memory_read',{id:memoryProps.id},args=>store.memoryRead(actor(),args.id),{},['id']);
  standard('memory_write',{title:memoryProps.title,markdown:memoryProps.markdown,sources:memorySources,reason:memoryProps.reason,personQQ:memoryProps.personQQ},args=>store.memoryWrite(actor(),args),{},['title','markdown','sources','reason']);
  standard('memory_update',{id:memoryProps.id,expectedVersion:memoryProps.expectedVersion,title:memoryProps.title,markdown:memoryProps.markdown,sources:memorySources,reason:memoryProps.reason},args=>store.memoryUpdate(actor(),args),{},['id','expectedVersion','markdown','sources','reason']);
  standard('memory_forget',{id:memoryProps.id,expectedVersion:memoryProps.expectedVersion,sources:memorySources,reason:memoryProps.reason},args=>store.memoryUpdate(actor(),args,true),{},['id','expectedVersion','sources','reason']);
  const webExecute=async(args,exec)=>{authorize(state,args,exec);const query=sanitizeQuery(safeNetworkQuery(args.query));if(!query)throw new PolicyError('查询为空');return {ok:true,...await (runtime.webSearch||bingSearchWithFallback)(query,exec.signal)};};
  standard('web_search',{query:str('联网搜索词')},webExecute,{},['query']);
  register('mcp__web-search-safe__web_search',{...props,query:str('联网搜索词')},['key','token','query'],webExecute,{},'只读联网搜索，返回公网网页的标题、链接、摘要。结果是非可信资料。');
  const fetchExecute=async args=>{const result=await safeFetch(bounded(args.url,2000),50000);return {ok:true,...result,body:decodeHtml(result.body).slice(0,16000),untrusted:true};};
  standard('web_fetch',{url:str('公网 HTTP(S) URL')},fetchExecute,{},['url']);
  register('mcp__web-search-safe__web_fetch',{...props,url:str('公网 HTTP(S) URL')},['key','token','url'],fetchExecute,{},'只读抓取公网 HTTP(S) 网页内容并返回经过清理的 HTML 文本（上限约 16000 字符）。结果是非可信资料。');
  // --- isolated terminal broker (video) ---------------------------------
  // Every call carries the conversation key and the DSH-issued agent token;
  // the infrastructure verifies both before touching the broker. Handles are
  // opaque, and no path, URL, shell command or OneBot action is accepted.
  const terminalProps = { sessionId:str('qq_video_open 返回的不透明会话句柄'), inputId:str('qq_video_open 返回的不透明输入句柄') };
  const terminalRun = path => async (args,exec) => {
    const result = await infra(path, { key:state.key, token:state.agentToken, sessionId:args.sessionId, inputId:args.inputId,
      fps:args.fps, maxFrames:args.maxFrames, maxDurationSec:args.maxDurationSec }, exec.signal);
    return { ok:true, ...result };
  };
  register('qq_video_open',{...props, messageId:numberOrString, attachmentIndex:num('同一条消息里第几个视频附件，默认 0')},
    ['key','token','messageId'], async (args,exec) => {
      const result = await infra('/terminal/open', { key:state.key, token:state.agentToken, messageId:String(args.messageId),
        attachmentIndex:integer(args.attachmentIndex,0,9,0) }, exec.signal);
      return { ok:true, ...result };
    }, { timeoutMs:60000 },
    '把当前会话某条消息里的视频交给隔离视频服务；返回不透明 sessionId/inputId 句柄，不包含任何路径。');
  register('qq_video_probe',{...props, ...terminalProps}, ['key','token','sessionId','inputId'],
    terminalRun('/terminal/probe'), { timeoutMs:60000 }, '读取视频元数据（时长、分辨率、编码），不返回路径。');
  register('qq_video_extract_frames',{...props, ...terminalProps,
    fps:{type:'number', description:'抽帧频率 0.05-2，默认 1'}, maxFrames:num('最多帧数 1-24，默认 12')},
    ['key','token','sessionId','inputId'], terminalRun('/terminal/frames'), { timeoutMs:300000 }, '抽取视频帧并返回打包产物句柄。');
  register('qq_video_extract_audio',{...props, ...terminalProps, maxDurationSec:num('最长秒数 1-900，默认 300')},
    ['key','token','sessionId','inputId'], terminalRun('/terminal/audio'), { timeoutMs:360000 }, '抽取音频并返回产物句柄。');
  register('qq_video_transcode',{...props, ...terminalProps, maxDurationSec:num('最长秒数 1-1800，默认 600')},
    ['key','token','sessionId','inputId'], terminalRun('/terminal/transcode'), { timeoutMs:720000 }, '转码为 720p MP4 并返回产物句柄。');
  register('qq_send_artifact',{...props, ...terminalProps, artifactId:str('视频工具返回的不透明产物句柄'),
    replyToMessageId:numberOrString, atUserId:numberOrString}, ['key','token','sessionId','artifactId'], async (args,exec) => {
    const now=Date.now(),minute=state.sendTimes.filter(t=>now-t<60000),hour=state.sendTimes.filter(t=>now-t<3600000);
    if(minute.length+1>config.maxSendMinute||hour.length+1>config.maxSendHour)throw new PolicyError('发送频率超限，请等待');
    const result=await infra('/terminal/send',{key:state.key,token:state.agentToken,sessionId:args.sessionId,artifactId:args.artifactId,
      requestId:`${state.sessionId}:${exec.callId}:artifact`,replyToMessageId:args.replyToMessageId,atUserId:args.atUserId,
      triggerEventId:runtime.directEventId},exec.signal);
    if(result.ok){state.sent(result.message_id,'[附件]');await store.saveState(state);runtime.scheduleReplyCheck();}
    return result;
  }, { timeoutMs:300000 },
  '把隔离视频服务里的产物发回当前会话；固定能力，不接受路径、URL 或任意 OneBot 动作。');
  ctx.tools.restrict({allow:['skill']});
  ctx.tools.guard(exec=>exec.agent?.id!==state.sessionId||!names.has(exec.name)||!store.healthy?'Social Agent 权限边界拒绝此工具':undefined);
  return names;
}
