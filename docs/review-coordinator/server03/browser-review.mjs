import {chromium} from '/Users/alex/myit/demo/Landing/d2-photo/node_modules/playwright-core/index.mjs';
import{mkdtemp,cp,mkdir,readFile,readdir,writeFile}from'node:fs/promises';import{spawn,spawnSync}from'node:child_process';import{tmpdir}from'node:os';import{join,dirname,resolve}from'node:path';import{fileURLToPath}from'node:url';
const out=dirname(fileURLToPath(import.meta.url)),root=resolve(out,'../../..'),python=root+'/.venv/bin/python',work=await mkdtemp(join(tmpdir(),'d4-server-review-'));
for(const f of ['server','public','tools'])await cp(root+'/'+f,work+'/'+f,{recursive:true,filter:p=>!p.includes('__pycache__')});
let server,b;const results=[],pageErrors=[];const check=(name,pass,data)=>{results.push({name,pass,data});if(!pass)console.log('FAIL',name,data);};
async function start(prefix=''){server=spawn(python,[work+'/tools/run.py','--port','4346',...(prefix?['--prefix',prefix]:[])],{cwd:work,stdio:'ignore'});for(let i=0;i<50;i++){try{if((await fetch('http://127.0.0.1:4346'+prefix+'/api/health')).ok)return;}catch{}await new Promise(r=>setTimeout(r,100));}throw Error('Server startup failed');}
async function stop(){if(!server||server.exitCode!==null||server.signalCode!==null)return;const done=new Promise(r=>server.once('exit',r));server.kill('SIGTERM');await done;}
const counts=()=>{const code="import sqlite3,json; c=sqlite3.connect('data/registry.sqlite3'); print(json.dumps([c.execute('select count(*) from '+t).fetchone()[0] for t in ('submissions','photos','deliveries')]))";const r=spawnSync(python,['-c',code],{cwd:work,encoding:'utf8'});if(r.status)throw Error('counts failed');return JSON.parse(r.stdout);};
try{
 await start();b=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--disable-gpu']});
 const c=await b.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});await c.route('**/*',r=>new URL(r.request().url()).hostname==='127.0.0.1'?r.continue():r.abort());const p=await c.newPage();p.on('pageerror',e=>pageErrors.push(e.message));await p.goto('http://127.0.0.1:4346/',{waitUntil:'networkidle'});
 for(const width of [360,390,600,768,1024,1440]){await p.setViewportSize({width,height:900});check('overflow '+width,await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
 await p.setViewportSize({width:390,height:844});await p.locator('#sample').click();await p.waitForFunction(()=>document.querySelectorAll('#previews li').length===1);
 let savedId;
 await p.route('**/api/submissions',async route=>{const response=await route.fetch();const data=await response.json();savedId=data.submission_id;await route.abort('failed');});
 await p.locator('.submit').click();await p.waitForFunction(()=>document.querySelector('#error-summary').textContent.includes('мог сохраниться'));
 check('lost response did commit',!!savedId&&JSON.stringify(counts())==='[1,1,2]');
 check('uncertain result honest',await p.locator('#result').isHidden()&&(await p.locator('#error-summary').textContent()).includes('мог сохраниться'));
 await stop();await p.unroute('**/api/submissions');await start();
 const response=p.waitForResponse(r=>r.url().endsWith('/api/submissions'));
 await p.locator('.submit').click();const retry=await response;const replay=await retry.json();
 check('restart same id',retry.status()===200&&replay.replayed&&replay.submission_id===savedId);
 await p.waitForFunction(()=>!document.querySelector('#result').hidden);
 check('no duplicate after restart',JSON.stringify(counts())==='[1,1,2]');
 const receiptResponse=p.waitForResponse(r=>r.url().includes('/api/receipts/'));await p.locator('#refresh-receipt').click();const receipt=await receiptResponse;const rjson=await receipt.json();
 check('private receipt correct',receipt.status()===200&&rjson.channels.every(x=>x.status==='not_connected')&&!('receipt_key'in rjson)&&!('payload'in rjson));
 await p.screenshot({path:out+'/receipt-390.png',fullPage:true,animations:'disabled'});
 for(const path of ['/data/registry.sqlite3','/backups/a.sqlite3','/.env','/server/app.py']){const r=await fetch('http://127.0.0.1:4346'+path);check('private path '+path,r.status===404);}
 await stop();
 const backup=spawnSync(python,['-m','server.manage','backup'],{cwd:work,encoding:'utf8'});check('CLI backup',backup.status===0);
 const backupName=(await readdir(work+'/backups')).find(f=>f.endsWith('.sqlite3'));
 await start('/d4');const pp=await c.newPage();await pp.goto('http://127.0.0.1:4346/d4/',{waitUntil:'networkidle'});await pp.locator('#sample').click();await pp.waitForFunction(()=>document.querySelectorAll('#previews li').length===1);await pp.locator('.submit').click();await pp.waitForFunction(()=>!document.querySelector('#result').hidden);
 check('prefix submit works',JSON.stringify(counts())==='[2,2,4]');await stop();
 const restore=spawnSync(python,['-m','server.manage','restore',backupName],{cwd:work,encoding:'utf8'});check('valid CLI restore',restore.status===0&&JSON.stringify(counts())==='[1,1,2]');
 check('no pageerror',pageErrors.length===0,pageErrors);
 console.log('Chrome',b.version(),'pass',results.filter(r=>r.pass).length,'/',results.length);
}catch(e){check('runner',false,e.stack);}finally{await b?.close();await stop();await writeFile(out+'/browser-results.json',JSON.stringify({date:'2026-11-06',work,results},null,2));}
