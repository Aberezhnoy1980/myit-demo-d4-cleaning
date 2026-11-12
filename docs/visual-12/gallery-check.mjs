import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';

const endpoint = process.argv[2];
const origin = 'http://127.0.0.1:4347';
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
    if (request.url.startsWith(origin + '/') || request.url === 'about:blank') {
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
  const result = await send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function capture(path, clip) {
  const options = {format: 'jpeg', quality: 90, captureBeyondViewport: true};
  if (clip) options.clip = {...clip, scale: 1};
  const result = await send('Page.captureScreenshot', options);
  await fs.writeFile(path, Buffer.from(result.data, 'base64'));
}
const directory = new URL('./', import.meta.url);
const screenshots = new URL('screenshots/', directory);
const readme = await fs.readFile(new URL('photo-files.json', directory), 'utf8');
const photos = JSON.parse(readme);
const resources = [];
try {
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Fetch.enable', {patterns: [{urlPattern: '*', requestStage: 'Request'}]});
  await send('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
  await send('Page.navigate', {url: origin + '/'});
  await evaluate(`new Promise(resolve => {
    if (document.readyState === 'complete') resolve();
    else window.addEventListener('load',resolve,{once:true});
  })`);
  await evaluate(`document.fonts.ready`);
  await send('Emulation.setEmulatedMedia', {media: 'screen', features: [{name: 'prefers-reduced-motion', value: 'no-preference'}]});
  const ordinaryMotion = await evaluate(`({
    reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
    scrollBehavior: getComputedStyle(document.documentElement).scrollBehavior,
    transition: getComputedStyle(document.querySelector('.button')).transitionDuration
  })`);
  assert.equal(ordinaryMotion.reduce, false);
  assert.equal(ordinaryMotion.scrollBehavior, 'smooth');
  await send('Emulation.setEmulatedMedia', {media: 'screen', features: [{name: 'prefers-reduced-motion', value: 'reduce'}]});
  const reducedMotion = await evaluate(`({
    reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,
    scrollBehavior: getComputedStyle(document.documentElement).scrollBehavior,
    transition: getComputedStyle(document.querySelector('.button')).transitionDuration,
    activeAnimations: document.getAnimations().length
  })`);
  assert.equal(reducedMotion.reduce, true);
  assert.equal(reducedMotion.scrollBehavior, 'auto');
  assert.equal(reducedMotion.transition, '0s');
  assert.equal(reducedMotion.activeAnimations, 0);
  const widths = [];
  const pageImageChecks = [];
  for (const width of [360, 390, 768, 1440]) {
    const height = width < 600 ? 844 : 1000;
    await send('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false});
    await evaluate(`(async () => {
      for (const image of document.querySelectorAll('#examples img')) {
        image.scrollIntoView({behavior:'instant',block:'center'});
        await new Promise(requestAnimationFrame);
        await image.decode();
      }
      document.querySelector('#examples').scrollIntoView({behavior:'instant',block:'start'});
      await new Promise(requestAnimationFrame);
    })()`);
    const state = await evaluate(`(() => {
      const section = document.querySelector('#examples');
      const rect = section.getBoundingClientRect();
      return {
        width: innerWidth, pageWidth: document.documentElement.scrollWidth,
        gallery: {x: rect.x, y: rect.y + scrollY, width: rect.width, height: rect.height},
        pairs: [...section.querySelectorAll('.comparison')].map(article => ({
          title: article.querySelector('h3').textContent,
          captions: [...article.querySelectorAll('figcaption')].map(item => item.textContent),
          figures: [...article.querySelectorAll('figure')].map(figure => {
            const box = figure.getBoundingClientRect();
            const img = figure.querySelector('img');
            return {left: box.left, top: box.top, right: box.right, width: box.width,
              source: img.getAttribute('src'), currentSource: img.currentSrc,
              natural: [img.naturalWidth,img.naturalHeight], complete:img.complete,
              attributes:[img.getAttribute('width'),img.getAttribute('height')],
              loading:img.loading, alt:img.alt};
          })
        })),
        disclaimer: section.querySelector('.examples-disclaimer').innerText,
        reduce: matchMedia('(prefers-reduced-motion: reduce)').matches
      };
    })()`);
    assert.equal(state.width, width);
    assert.equal(state.pageWidth, width);
    assert.equal(state.disclaimer, 'Сгенерированные примеры для демонстрации. Реальный результат зависит от ткани и загрязнения.');
    assert.equal(state.reduce, true);
    assert.equal(state.pairs.length, 2);
    for (const [index, pair] of state.pairs.entries()) {
      assert.deepEqual(pair.captions, ['До', 'После']);
      const name = index === 0 ? 'straight' : 'corner';
      assert.equal(pair.figures[0].source, 'assets/' + name + '-before-800.webp');
      assert.equal(pair.figures[1].source, 'assets/' + name + '-after-800.webp');
      for (const image of pair.figures) {
        assert(image.complete && image.natural[0] > 0);
        assert(image.left >= 0 && image.right <= width);
        assert.deepEqual(image.attributes, ['800', '535']);
        assert.equal(image.loading, 'lazy');
        assert(image.alt.length > 10);
      }
      if (width < 600) assert(pair.figures[1].top > pair.figures[0].top);
      else assert(Math.abs(pair.figures[1].top - pair.figures[0].top) < 1);
    }
    const clip = {x: 0, y: Math.max(0, state.gallery.y - 12), width, height: Math.ceil(state.gallery.height + 24)};
    await capture(new URL('gallery-' + width + '.jpg', screenshots), clip);
    await evaluate(`(async () => {
      for (const image of document.images) {
        if (image.src.startsWith('blob:')) continue;
        image.scrollIntoView({behavior:'instant',block:'center'});
        await new Promise(requestAnimationFrame);
        await image.decode();
      }
      window.scrollTo({top:0,behavior:'instant'});
      await new Promise(requestAnimationFrame);
    })()`);
    const pageImages = await evaluate(`[...document.images].map(image => ({
      source:image.getAttribute('src'), complete:image.complete,
      width:image.naturalWidth,height:image.naturalHeight
    }))`);
    assert(pageImages.every(image => image.complete && image.width > 0 && image.height > 0));
    pageImageChecks.push({width,images:pageImages});
    const fullHeight = await evaluate('document.documentElement.scrollHeight');
    await capture(new URL('page-final-' + width + '.jpg', screenshots), {x: 0, y: 0, width, height: fullHeight});
    widths.push(state);
  }
  for (const image of photos) {
    for (const derivative of image.derivatives) {
      const response = await fetchLocal(origin + '/' + derivative.path.replace(/^public\//, ''));
      const bytes = Buffer.from(await response.arrayBuffer());
      assert.equal(response.status, 200);
      assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'), derivative.sha256);
      resources.push({path: derivative.path, status: response.status, bytes: bytes.length, sha256: derivative.sha256});
    }
  }
  const broken = network.filter(response => response.status >= 400);
  assert.deepEqual(broken, []);
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  const version = await (await fetchLocal(endpoint + '/json/version')).json();
  const report = {
    date: '2026-11-11', browser: version.Browser,
    mode: 'Isolated Chrome profile, loopback preview with synthetic health, no delivery',
    emulation: {method: 'CDP Emulation.setEmulatedMedia', ordinaryMotion, reducedMotion},
    widths, resources, pageImageChecks, browserResponses: network, broken, errors, external
  };
  await fs.writeFile(new URL('gallery-browser-results.json', directory), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify({browser: version.Browser, widths: widths.map(item => item.width),
    resources: resources.length, reducedMotion, errors, external}, null, 2));
} finally {
  socket.close();
  await fetchLocal(endpoint + '/json/close/' + tab.id).catch(() => {});
}
