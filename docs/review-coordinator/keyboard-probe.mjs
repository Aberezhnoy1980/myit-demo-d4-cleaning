import {chromium} from '/Users/alex/myit/demo/Landing/d2-photo/node_modules/playwright-core/index.mjs';
import {spawn} from 'node:child_process';import{writeFile}from'node:fs/promises';import{dirname,resolve}from'node:path';import{fileURLToPath}from'node:url';
const out=dirname(fileURLToPath(import.meta.url)),root=resolve(out,'../..');const server=spawn('python3',[root+'/tools/serve.py','--port','4346'],{stdio:'ignore'});let b;const rows=[];
try{b=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--disable-gpu']});const p=await b.newPage();await p.goto('http://127.0.0.1:4346/');
for(const id of ['sofa','size','material']){await p.locator('#'+id).focus();const events=[];for(const key of ['ArrowDown','ArrowDown','Enter','Tab']){await p.keyboard.press(key);await p.waitForTimeout(200);events.push({key,value:await p.locator('#'+id).inputValue()});}rows.push({id,events});}
console.log(JSON.stringify(rows));}finally{await b?.close();server.kill();await writeFile(out+'/r2/keyboard-probe.json',JSON.stringify(rows,null,2));}
