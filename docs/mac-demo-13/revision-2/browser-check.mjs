import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';

const endpoint = process.argv[2];
let origin = 'http://127.0.0.1:4351';
assert.match(endpoint, /^http:\/\/127\.0\.0\.1:\d+$/);
async function fetchLocal(url, options = {}) {
  return fetch(url, {...options, signal: AbortSignal.timeout(8000)});
}
const protocol = await (await fetchLocal(endpoint + '/json/protocol')).json();
const emulation = protocol.domains.find(domain => domain.domain === 'Emulation');
assert(emulation.commands.some(command => command.name === 'setEmulatedMedia'));
const tab = await (await fetchLocal(endpoint + '/json/new?about:blank', {method: 'PUT'})).json();
const socket = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, {once: true});
  socket.addEventListener('error', reject, {once: true});
});
let sequence = 0;
const pending = new Map();
const network = [];
const external = [];
const errors = [];
let healthOverride = null;
function send(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error('CDP timeout: ' + method));
    }, 30000);
    pending.set(id, {resolve, reject, timer});
    socket.send(JSON.stringify({id, method, params}));
  });
}
socket.addEventListener('message', event => {
  const message = JSON.parse(event.data);
  if (message.id) {
    const request = pending.get(message.id);
    if (!request) return;
    clearTimeout(request.timer);
    pending.delete(message.id);
    if (message.error) request.reject(new Error(JSON.stringify(message.error)));
    else request.resolve(message.result);
  } else if (message.method === 'Fetch.requestPaused') {
    const {requestId, request} = message.params;
    if (healthOverride && request.url === origin + '/api/health') {
      send('Fetch.fulfillRequest', {requestId, responseCode: 200,
        responseHeaders: [{name:'Content-Type',value:'application/json'}],
        body:Buffer.from(JSON.stringify({delivery:healthOverride})).toString('base64')}).catch(error=>errors.push(error.message));
    } else if (request.url.startsWith(origin + '/') || request.url === 'about:blank') {
      send('Fetch.continueRequest', {requestId}).catch(error => errors.push(error.message));
    } else {
      external.push(request.url);
      send('Fetch.failRequest', {requestId, errorReason: 'BlockedByClient'}).catch(error => errors.push(error.message));
    }
  } else if (message.method === 'Network.responseReceived') {
    network.push({url: message.params.response.url, status: message.params.response.status});
  } else if (message.method === 'Runtime.exceptionThrown') {
    errors.push(message.params.exceptionDetails.text);
  }
});
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true, userGesture: true});
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function capture(path, clip) {
  const options = {format: 'jpeg', quality: 90, captureBeyondViewport: false};
  if (clip) options.clip = {...clip, scale: 1};
  const result = await send('Page.captureScreenshot', options);
  await fs.writeFile(path, Buffer.from(result.data, 'base64'));
}
const directory = new URL('./', import.meta.url);
const screenshots = new URL('screenshots/',directory);
await fs.mkdir(screenshots,{recursive:true});
const checks=[];
const visibleCopies=[];
async function checkVisibleCopy(mode, scripts) {
  const copy=await evaluate(`({
    request:document.querySelector('#request').innerText,
    title:document.querySelector('#demo-mode-title').innerText,
    storage:document.querySelector('#storage-note').innerText,
    delivery:document.querySelector('#aside-delivery-note').innerText,
    submit:document.querySelector('.submit').innerText,
    disabled:document.querySelector('.submit').disabled,
    jsNoticeVisible:!document.querySelector('#js-notice').hidden
      && getComputedStyle(document.querySelector('#js-notice')).display!=='none'
  })`);
  if(mode==='public') {
    assert.doesNotMatch(copy.request,/24 часов|Это локальный демонстрационный стенд/);
    assert.match(copy.storage,/только в памяти открытой страницы.*теряются при её перезагрузке.*Сохранения на сервере нет/);
    assert.match(copy.submit,/Подготовить демо-запрос/);
    assert.match(copy.request,/Отправок в Telegram и Google-таблицу нет/);
  } else {
    assert.match(copy.title,/локальный демонстрационный стенд/);
    assert.match(copy.storage,/После сохранения заявка и обработанные фото хранятся локально.*до 24 часов/);
    assert.doesNotMatch(copy.request,/Сохранения на сервере нет|Это публичная демонстрационная форма/);
    assert.match(copy.submit,/Сохранить тестовый запрос/);
    assert.match(copy.delivery,mode==='mac_demo_live' ? /Доставка в тестовый Telegram и тестовую Google-таблицу/ : /Внешняя доставка не подключена/);
  }
  assert.equal(copy.disabled,!scripts);
  assert.equal(copy.jsNoticeVisible,!scripts);
  visibleCopies.push({mode,scripts,...copy});
}
async function until(expression) {
  for(let n=0;n<80;n++) {
    if(await evaluate(expression))return;
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  throw Error('Condition timed out: '+expression);
}
async function open(port) {
  origin='http://127.0.0.1:'+port;
  await send('Page.navigate',{url:origin+'/'});
  await send('Page.bringToFront');
  await until(`document.readyState==='complete' && document.querySelector('.submit') && !document.querySelector('.submit').disabled`);
}
async function click(selector) {await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`)}
async function sample() {
  await click('#sample');
  await until(`document.querySelectorAll('#previews img').length===1 && !document.querySelector('#sample').disabled`);
  assert(await evaluate(`document.querySelector('#previews img').complete && document.querySelector('#previews img').naturalWidth>0`));
}
async function screenshot(name,width) {
  await send('Emulation.setDeviceMetricsOverride',{width,height:width<600?844:1000,deviceScaleFactor:1,mobile:false});
  await evaluate(`document.querySelector('#result').scrollIntoView({behavior:'instant',block:'end'})`);
  await capture(new URL(name+'-'+width+'.jpg',screenshots));
  assert.equal(await evaluate('document.documentElement.scrollWidth'),width);
}
try {
  for(const domain of ['Page','Runtime','Network'])await send(domain+'.enable');
  await send('Fetch.enable',{patterns:[{urlPattern:'*',requestStage:'Request'}]});
  await send('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  await send('Emulation.setEmulatedMedia',{media:'screen',features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  for(const [port,mode] of [[4351,'public'],[4352,'ordinary'],[4353,'mac_demo_live']]) {
    await send('Emulation.setScriptExecutionDisabled',{value:true});
    origin='http://127.0.0.1:'+port;
    await send('Page.navigate',{url:origin+'/'});
    await until(`document.readyState==='complete' && document.querySelector('#storage-note')`);
    await checkVisibleCopy(mode,false);
    await evaluate(`document.querySelector('.demo-note').scrollIntoView({behavior:'instant',block:'start'})`);
    await capture(new URL(mode+'-no-js-390.jpg',screenshots));
  }
  await send('Emulation.setScriptExecutionDisabled',{value:false});
  await open(4351);
  await checkVisibleCopy('public',true);
  assert.match(await evaluate(`document.querySelector('#submit-note').innerText`),/сохранения на сервере нет/);
  await click('.submit');
  await until(`!document.querySelector('#error-summary').hidden`);
  assert.equal(await evaluate('document.activeElement.id'),'error-summary');
  await sample();
  // Exercise keyboard focus on the actual file control and remove/re-add photo.
  await evaluate(`document.querySelector('#photos').focus()`);
  assert.equal(await evaluate('document.activeElement.id'),'photos');
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
  assert.equal(await evaluate(`document.activeElement.getAttribute('aria-label')`),'Удалить фото 1');
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r'});
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
  await until(`document.querySelectorAll('#previews img').length===0`);
  await sample();
  await click('.submit');
  await until(`!document.querySelector('#result').hidden`);
  assert.equal(await evaluate('document.activeElement.id'),'result');
  assert.equal(await evaluate(`document.querySelector('#result-title').innerText`),'Демо-запрос подготовлен');
  assert.match(await evaluate(`document.querySelector('#receipt-details').innerText`),/не отправлялось/);
  await send('Browser.grantPermissions',{origin,permissions:['clipboardReadWrite','clipboardSanitizedWrite']});
  await click('#copy-request');
  await until(`!document.querySelector('#copy-status').hidden`);
  assert.match(await evaluate(`document.querySelector('#copy-status').innerText`),/Текст скопирован/);
  assert.match(await evaluate('navigator.clipboard.readText()'),/demo@example.test/);
  for(const width of [390,1440])await screenshot('public-short-result',width);
  await evaluate(`document.querySelector('#description').value='Длинное тестовое описание. '.repeat(32);document.querySelector('#description').dispatchEvent(new Event('input',{bubbles:true}))`);
  await click('.submit');
  await until(`!document.querySelector('#result').hidden && document.querySelector('#error-summary').hidden`);
  for(const width of [360,390,768,1440])await screenshot('public-result',width);
  assert.equal(network.filter(r=>r.url.includes('/api/')).length,0);
  checks.push({mode:'public',validation:true,photoPreviewRemove:true,keyboardPhotoFocus:true,copy:true,widths:[360,390,768,1440],apiCalls:0});
  await open(4352);
  await checkVisibleCopy('ordinary',true);
  assert.match(await evaluate(`document.querySelector('#submit-note').innerText`),/сохранение только на этом стенде/);
  await sample();await click('.submit');
  await until(`!document.querySelector('#result').hidden`);
  await click('#refresh-receipt');
  await until(`!document.querySelector('#refresh-receipt').disabled`);
  assert.match(await evaluate(`document.querySelector('#receipt-details').innerText`),/Канал выключен/);
  await screenshot('ordinary-receipt',390);
  checks.push({mode:'ordinary',localReceipt:true,delivery:false});
  await open(4353);
  await checkVisibleCopy('mac_demo_live',true);
  assert.match(await evaluate(`document.querySelector('#submit-note').innerText`),/обработанное фото, номер заявки и параметры дивана уйдут в тестовый Telegram/);
  await sample();
  await evaluate(`document.querySelector('.submit').scrollIntoView({behavior:'instant',block:'end'})`);
  await capture(new URL('mac-before-action-390.jpg',screenshots));
  const postsBefore=network.filter(r=>r.url===origin+'/api/submissions').length;
  healthOverride='not_connected';
  await click('.submit');await until(`!document.querySelector('#error-summary').hidden`);
  assert.match(await evaluate(`document.querySelector('#submit-note').innerText`),/сохранение только на этом стенде/);
  await checkVisibleCopy('ordinary',true);
  healthOverride=null;
  await click('.submit');await until(`document.querySelector('#submit-note').innerText.includes('обработанное фото')`);
  await checkVisibleCopy('mac_demo_live',true);
  assert.equal(network.filter(r=>r.url===origin+'/api/submissions').length,postsBefore);
  await click('.submit');
  await until(`!document.querySelector('#result').hidden`);
  let receipt='';
  for(let n=0;n<40;n++) {
    await click('#refresh-receipt');
    await until(`!document.querySelector('#refresh-receipt').disabled`);
    receipt=await evaluate(`document.querySelector('#receipt-details').innerText`);
    if(receipt.includes('девяти полей'))break;
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  assert.match(receipt,/Telegram: Создание сообщения подтверждено/);
  assert.match(receipt,/Google Sheets:.*девяти полей/);
  const id=receipt.match(/[a-f0-9-]{36}/)[0];
  await click('.submit');
  await until(`!document.querySelector('#result').hidden`);
  assert.match(await evaluate(`document.querySelector('#receipt-details').innerText`),new RegExp(id));
  await click('#refresh-receipt');await until(`!document.querySelector('#refresh-receipt').disabled`);
  for(const width of [390,1440])await screenshot('mac-offline-receipt',width);
  await evaluate(`document.querySelector('#description').value+=' Новый';document.querySelector('#description').dispatchEvent(new Event('input',{bubbles:true}))`);
  await click('.submit');await until(`!document.querySelector('#error-summary').hidden`);
  assert.match(await evaluate(`document.querySelector('#error-summary').innerText`),/Лимит/);
  checks.push({mode:'mac_demo_live with fake transports',preActionNotice:true,modeChangeWaitsForNewClick:true,bothConfirmed:true,repeatSameId:true,quotaRejected:true});
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
  const reduced=await evaluate(`({matches:matchMedia('(prefers-reduced-motion: reduce)').matches,scroll:getComputedStyle(document.documentElement).scrollBehavior,transition:getComputedStyle(document.querySelector('.button')).transitionDuration})`);
  assert.deepEqual(reduced,{matches:true,scroll:'auto',transition:'0s'});
  const browser=(await(await fetchLocal(endpoint+'/json/version')).json()).Browser;
  await fs.writeFile(new URL('browser-results.json',directory),JSON.stringify({date:'2026-11-11',browser,checks,visibleCopies,reduced,errors,external,responses:network.map(r=>({...r,url:r.url.replace(/\/receipts\/[a-f0-9-]+/,'/receipts/<submission-id>')}))},null,2)+'\n');
  console.log(JSON.stringify({browser,checks,reduced,errors,external},null,2));
} finally {
  socket.close();await fetchLocal(endpoint+'/json/close/'+tab.id).catch(()=>{});
}
