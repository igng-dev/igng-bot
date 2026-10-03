import test from 'node:test';import assert from 'node:assert/strict';import {readFileSync}from'node:fs';import {createHash}from'node:crypto';import yaml from'js-yaml';
import {RESERVED2_PROMPT}from'../src/runtime.js';import {safeFetch}from'../donor/qq-bridge/safe-fetch.js';
const hash=data=>createHash('sha256').update(data).digest('hex');
test('reserved2 original persona is preserved as evaluated YAML and vendored bytes are pinned',()=>{
 const source=readFileSync(new URL('../donor/qq-bridge/agent.cordis.yml',import.meta.url));const preset=yaml.load(source.toString());
 assert.equal(RESERVED2_PROMPT,preset.find(p=>p.id==='persona').config.prefix);
 const provenance=JSON.parse(readFileSync(new URL('../donor/qq-bridge/hashes.json',import.meta.url)));
 for(const [file,expected]of Object.entries(provenance))assert.equal(hash(readFileSync(new URL('../donor/qq-bridge/'+file,import.meta.url))),expected,file);
});
test('donor SSRF boundary denies local, private, metadata and embedded-credential fetches',async()=>{
 for(const url of ['file:///etc/passwd','http://127.0.0.1','http://[::1]','http://192.168.1.1','http://169.254.169.254','http://100.64.0.1','http://name.local','https://user:secret@example.com'])await assert.rejects(safeFetch(url));
});
