import {chromium} from '/Users/alex/myit/demo/Landing/d2-photo/node_modules/playwright-core/index.mjs';
import fs from 'node:fs/promises';
import path from 'node:path';
const out=path.dirname(new URL(import.meta.url).pathname),base='http://127.0.0.1:4346/';
const browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--disable-gpu']});
const r={browser:browser.version(),checks:[],errors:[],requests:[]};const check=(name,pass,detail)=>r.checks.push({name,pass,detail});
try{
 for(const width of [360,390,768,1440]){
  const ctx=await browser.newContext({viewport:{width,height:900}});await ctx.route('**/*',route=>route.request().url().startsWith(base)?route.continue():route.abort());
  const p=await ctx.newPage();p.on('pageerror',e=>r.errors.push(e.message));p.on('request',req=>r.requests.push({url:req.url(),method:req.method()}));await p.goto(base);
  check(`title-${width}`,await p.title()==='Диван в порядке — бережно к вашему дивану');
  check(`accessible-name-${width}`,await p.getByRole('link',{name:'Диван в порядке — в начало',exact:true}).count()===1);
  const layout=await p.evaluate(()=>{const brand=document.querySelector('.brand'),nav=document.querySelector('nav');const name=brand.querySelector('span').firstChild;const range=document.createRange();range.selectNode(name);const b=brand.getBoundingClientRect(),n=nav.getBoundingClientRect();return {lines:range.getClientRects().length,gap:n.left-b.right,client:document.documentElement.clientWidth,scroll:document.documentElement.scrollWidth}});
  check(`brand-layout-${width}`,layout.lines===1&&layout.gap>=0&&layout.scroll<=layout.client,layout);
  check(`no-old-name-${width}`,!/тихий\s+дом/i.test(await p.locator('body').textContent()));
  await p.getByRole('link',{name:'Диван в порядке — в начало',exact:true}).focus();await p.keyboard.press('Tab');
  check(`keyboard-nav-${width}`,await p.evaluate(()=>document.activeElement.getAttribute('href'))===(width<=700?'#request':'#approach'));
  const outline=await p.evaluate(()=>({style:getComputedStyle(document.activeElement).outlineStyle,width:getComputedStyle(document.activeElement).outlineWidth}));
  check(`keyboard-outline-${width}`,outline.style==='solid'&&parseFloat(outline.width)>=2,outline);
  await p.locator('.hero a.button').click();await p.waitForTimeout(400);
  check(`cta-${width}`,await p.evaluate(()=>location.hash==='#request'&&document.querySelector('#request').getBoundingClientRect().top>=0&&document.querySelector('#request').getBoundingClientRect().top<100));
  if(width===390||width===1440){await p.goto(base);await p.screenshot({path:path.join(out,`hero-${width}.png`),fullPage:false})}
  await ctx.close();
 }
 check('no-api-calls',r.requests.every(req=>req.method==='GET'&&req.url.startsWith(base)&&!req.url.includes('/api/')));
}finally{await browser.close();await fs.writeFile(path.join(out,'browser-results.json'),JSON.stringify(r,null,2)+'\n')}
console.log(JSON.stringify({checks:r.checks.length,passed:r.checks.filter(x=>x.pass).length,failed:r.checks.filter(x=>!x.pass),errors:r.errors}));
