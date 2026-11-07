import {chromium} from '/Users/alex/myit/demo/Landing/d2-photo/node_modules/playwright-core/index.mjs';
import {spawn} from 'node:child_process';import {mkdir,writeFile} from 'node:fs/promises';import {dirname,resolve} from 'node:path';import {fileURLToPath} from 'node:url';
const base=dirname(fileURLToPath(import.meta.url)),root=resolve(base,'../..'),out=base+'/r2';await mkdir(out,{recursive:true});
const server=spawn('python3',[root+'/tools/serve.py','--port','4346'],{stdio:'ignore'});let b;const results=[],errors=[];
const check=(name,pass,data)=>{results.push({name,pass,data});if(!pass)console.log('FAIL',name,data);};
try{
 b=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--disable-gpu']});const p=await b.newPage({viewport:{width:390,height:844},reducedMotion:'reduce'});p.on('pageerror',e=>errors.push(e.message));await p.goto('http://127.0.0.1:4346/',{waitUntil:'networkidle'});
 for(const [id,label] of [['sofa','Прямой'],['size','Два места'],['material','Ткань / рогожка']]){
  check('option exists '+id,await p.locator('#'+id).evaluate((e,label)=>[...e.options].some(o=>o.label===label),label));
  await p.locator('#'+id).click();await p.keyboard.press('Home');await p.keyboard.press('ArrowDown');await p.keyboard.press('Enter');const value=await p.locator('#'+id).inputValue();check('keyboard selects '+label,value===label,{value});if(value!==label)await p.locator('#'+id).selectOption({label});
 }
 await p.locator('#description').fill('Независимый тестовый запрос без личных данных.');await p.locator('#description').focus();await p.keyboard.press('Tab');check('photo Tab focus',await p.evaluate(()=>document.activeElement.id==='photos'));
 const fc=p.waitForEvent('filechooser',{timeout:5000});await p.keyboard.press('Enter');const chooser=await fc;check('Enter file chooser',chooser.isMultiple());await chooser.setFiles(root+'/public/assets/sofa-700.jpg');await p.waitForFunction(()=>document.querySelectorAll('#previews li').length===1);
 check('single hidden native picker',await p.locator('input[type=file]').count()===1&&await p.locator('#photos').evaluate(e=>getComputedStyle(e).opacity==='0'));
 await p.locator('#photos').focus();check('upload visible focus',await p.locator('.upload-box').evaluate(e=>getComputedStyle(e).outlineStyle!=='none'));
 await p.screenshot({path:out+'/upload-focus-390.png',fullPage:true,animations:'disabled'});
 await p.locator('#contact').fill('demo@example.test');await p.locator('button[type=submit]').click();check('filled result honest',await p.locator('#result').isVisible()&&(await p.locator('#result').textContent()).includes('Заявка не сохранена'));
 check('no invalid fields',await p.locator('[aria-invalid=true]').count()===0);check('result focus',await p.evaluate(()=>document.activeElement.id==='result'));
 await p.locator('#return').click();check('return keeps selected values',await p.locator('#sofa').inputValue()==='Прямой'&&await p.locator('#size').inputValue()==='Два места'&&await p.locator('#material').inputValue()==='Ткань / рогожка');
 for(const width of [360,390,600,768,1024,1440]){await p.setViewportSize({width,height:900});check('overflow '+width,await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}
 await p.screenshot({path:out+'/form-1440.png',fullPage:true,animations:'disabled'});await p.reload({waitUntil:'networkidle'});await p.locator('button[type=submit]').click();check('empty six errors',await p.locator('#error-summary li').count()===6);check('error summary focus',await p.evaluate(()=>document.activeElement.id==='error-summary'));check('no pageerror',errors.length===0,errors);console.log('Chrome',b.version(),'pass',results.filter(r=>r.pass).length,'/',results.length);
}catch(e){check('runner',false,e.stack);}finally{await b?.close();server.kill();await writeFile(out+'/results.json',JSON.stringify({date:'2026-11-06',results},null,2));}
