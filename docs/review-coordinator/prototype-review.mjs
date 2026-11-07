// Independent browser check; uses the existing QA environment, not a D4 dependency.
import {chromium} from '/Users/alex/myit/demo/Landing/d2-photo/node_modules/playwright-core/index.mjs';
import {spawn} from 'node:child_process';
import {readFile,writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {dirname,resolve} from 'node:path';
const out=dirname(fileURLToPath(import.meta.url));const root=resolve(out,'../..');
const results=[];const errors=[];
const check=(name,pass,data)=>{results.push({name,pass,data});if(!pass)console.log('FAIL',name,JSON.stringify(data));};
const server=spawn('python3',[root+'/tools/serve.py','--port','4346'],{stdio:'ignore'});
let b;
try{
 b=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--disable-gpu']});
 const p=await b.newPage({viewport:{width:390,height:844},reducedMotion:'reduce'});p.on('pageerror',e=>errors.push(e.message));
 await p.goto('http://127.0.0.1:4346/',{waitUntil:'networkidle'});
 for(const [id,label] of [['sofa','Прямой'],['size','Два места'],['material','Ткань / рогожка']]){
  const options=await p.locator('#'+id).evaluate(e=>[...e.options].map(o=>({label:o.textContent,value:o.value})));
  check('available option '+label,options.some(o=>o.label===label),options);
 }
 for(const width of [360,390,600,768,1024,1440]){await p.setViewportSize({width,height:900});check('overflow '+width,await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
 await p.setViewportSize({width:390,height:844});
 await p.locator('button[type="submit"]').click();
 check('six empty field errors',await p.locator('#error-summary li').count()===6);
 check('error focus',await p.evaluate(()=>document.activeElement.id==='error-summary'));
 await p.locator('#sofa').selectOption({label:'Угловой'});await p.locator('#size').selectOption({label:'Три места'});await p.locator('#material').selectOption({label:'Не знаю'});
 await p.locator('#description').fill('Тестовый след от напитка. Личных данных нет.');
 await p.locator('#photos').setInputFiles(root+'/public/assets/sofa-700.jpg');
 await p.waitForFunction(()=>document.querySelectorAll('#previews li').length===1);
 await p.locator('#contact').fill('real@example.com');await p.locator('button[type="submit"]').click();
 check('real contact rejected',await p.locator('#contact').getAttribute('aria-invalid')==='true');
 await p.locator('#contact').fill('demo@example.test');await p.locator('button[type="submit"]').click();
 check('honest result',await p.locator('#result').isVisible()&&(await p.locator('#result').textContent()).includes('Заявка не сохранена'));
 await p.locator('#return').click();check('fields retained',(await p.locator('#description').inputValue()).includes('Тестовый')&&await p.locator('#previews li').count()===1);
 await p.locator('#previews button').click();check('remove photo',await p.locator('#previews li').count()===0);
 await p.locator('#photos').setInputFiles({name:'broken.png',mimeType:'image/png',buffer:Buffer.from('not a PNG')});
 await p.waitForFunction(()=>document.querySelector('#photos-error').textContent.includes('не удалось'));
 check('broken photo rejected',await p.locator('#previews li').count()===0);
 await p.screenshot({path:out+'/checked-form-390.png',fullPage:true,animations:'disabled'});
 check('no pageerror',errors.length===0,errors);
 console.log('browser',b.version(),'checks',results.length,'pass',results.filter(r=>r.pass).length);
}catch(e){check('runner',false,e.stack);}
finally{await b?.close();server.kill();await writeFile(out+'/results.json',JSON.stringify({date:'2026-11-06',results},null,2));}
