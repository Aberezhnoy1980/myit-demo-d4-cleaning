'use strict';
const form = document.querySelector('#lead-form');
const summary = document.querySelector('#error-summary');
const result = document.querySelector('#result');
const photos = document.querySelector('#photos');
const previews = document.querySelector('#previews');
const submitButton = form.querySelector('.submit');
const publicForm = document.querySelector('html')?.dataset?.formMode === 'public';
let selected = [];
let urls = [];
let pending = null;
let receipt = null;
let deliveryMode = null;
let publicDraft = '';
const api = path => new URL('api/' + path, document.baseURI);
const labels = {sofa:'Выберите форму дивана.',size:'Укажите размер дивана.',material:'Выберите материал или «Не знаю».',description:'Кратко опишите задачу.',photos:'Добавьте хотя бы одно тестовое фото.',contact:'Укажите тестовый контакт.'};
const isDeliveryMode = mode => ['not_connected', 'test', 'closed_live', 'mac_demo_live'].includes(mode);
async function readDeliveryMode() {
  try {
    const response = await fetch(api('health'), {cache:'no-store', signal:AbortSignal.timeout(5000)});
    if (response.ok) {
      const {delivery} = await response.json();
      if (isDeliveryMode(delivery)) return delivery;
    }
  } catch {}
  return null;
}
function showDeliveryNotice(mode) {
  deliveryMode = mode;
  document.querySelector('#demo-mode-title').textContent = 'Это локальный демонстрационный стенд.';
  document.querySelector('#storage-note').textContent = 'После сохранения заявка и обработанные фото хранятся локально на этом стенде до 24 часов.';
  document.querySelector('#submit-label').textContent = 'Сохранить тестовый запрос';
  const live = mode === 'closed_live' || mode === 'mac_demo_live';
  const summary = live ? 'Доставка в тестовый Telegram и тестовую Google-таблицу.'
    : mode ? 'Сохранение локально, без внешних отправок.' : '';
  document.querySelector('#hero-delivery-note').textContent = ('Демо: только тестовые данные. ' + summary).trim();
  document.querySelector('#aside-delivery-note').textContent = live ? summary : mode ? 'Внешняя доставка не подключена.' : '';
  document.querySelector('#submit-note').textContent = live
    ? 'После сохранения обработанное фото, номер заявки и параметры дивана уйдут в тестовый Telegram. Данные заявки с синтетическим контактом уйдут в тестовую Google-таблицу.'
    : mode ? 'Демо: сохранение только на этом стенде. Уведомления не отправляются автоматически.'
      : 'Не удалось определить режим сохранения. Перезагрузите страницу.';
}
function showPublicNotice() {
  document.querySelector('#demo-mode-title').textContent = 'Это публичная демонстрационная форма.';
  document.querySelector('#storage-note').textContent = 'Данные и фото находятся только в памяти открытой страницы и теряются при её перезагрузке. Сохранения на сервере нет.';
  document.querySelector('#hero-delivery-note').textContent = 'Демо: запрос можно подготовить в браузере. Отправок в Telegram и Google-таблицу нет.';
  document.querySelector('#aside-delivery-note').textContent = 'Отправок в Telegram и Google-таблицу нет. Текст обращения можно скопировать.';
  document.querySelector('#submit-note').textContent = 'Публичное демо. Нажатие подготовит текст обращения только в браузере. Отправок в Telegram или Google-таблицу и сохранения на сервере нет. Фото остаются в открытой странице.';
  document.querySelector('#submit-label').textContent = 'Подготовить демо-запрос';
  document.getElementById('refresh-receipt').hidden = true;
  document.querySelector('#copy-request').hidden = false;
}
function renderPublicRequest(values) {
  publicDraft = ['Демо-запрос — копия для просмотра',
    'Форма дивана: ' + values.sofa, 'Число мест: ' + values.size,
    'Обивка: ' + values.material, 'Описание: ' + values.description,
    'Способ связи: ' + (values.channel === 'telegram' ? 'Telegram' : 'Электронная почта'),
    'Вымышленный контакт: ' + values.contact,
    'Тестовых фотографий: ' + selected.length,
    'Обращение не отправлялось. Фотографии остаются в браузере и не входят в текстовую копию.'].join('\n');
  document.querySelector('#result-title').textContent = 'Демо-запрос подготовлен';
  const details = document.querySelector('#receipt-details');
  details.replaceChildren();
  for (const line of publicDraft.split('\n')) {
    const paragraph = document.createElement('p');
    paragraph.textContent = line;
    details.append(paragraph);
  }
  document.querySelector('#copy-status').hidden = true;
  result.hidden = false;
  result.focus();
}
function showError(id, message) {
  const field = document.getElementById(id);
  const error = document.getElementById(id+'-error');
  field.setAttribute('aria-invalid', message ? 'true' : 'false');
  error.textContent = message || '';
  error.hidden = !message;
}
function invalidateResult() { result.hidden = true; }
function renderPhotos() {
  urls.forEach(URL.revokeObjectURL); urls = []; previews.replaceChildren();
  selected.forEach((file, index) => {
    const li = document.createElement('li');
    const img = document.createElement('img');
    const url = URL.createObjectURL(file); urls.push(url); img.src = url; img.alt = 'Тестовое фото '+(index+1);
    const caption = document.createElement('p'); caption.textContent = file.name;
    const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Удалить'; button.setAttribute('aria-label','Удалить фото '+(index+1));
    button.addEventListener('click', () => {selected.splice(index,1); renderPhotos(); invalidateResult(); photos.focus();});
    li.append(img,caption,button); previews.append(li);
  });
}
photos.addEventListener('change', async () => {
  invalidateResult();
  const batch = Array.from(photos.files); photos.value = '';
  const next = selected.concat(batch);
  let error = '';
  if (next.length > 3) error = 'Можно добавить не больше 3 фото. Удалите лишнее и попробуйте снова.';
  else if (next.some(f => f.size > 5*1024*1024)) error = 'Каждое фото должно быть не больше 5 МБ.';
  else if (next.reduce((n,f)=>n+f.size,0) > 12*1024*1024) error = 'Общий размер фотографий должен быть не больше 12 МБ.';
  else if (next.some(f => !['image/jpeg','image/png','image/webp'].includes(f.type))) error = 'Выберите JPEG, PNG или WebP. Другие форматы не подходят.';
  if (error) { showError('photos',error); return; }
  photos.disabled = true;
  try {
    for (const file of batch) {
      const bitmap = await createImageBitmap(file);
      const pixels = bitmap.width * bitmap.height; bitmap.close();
      if (pixels > 20000000) throw new Error('pixels');
    }
    selected = next; renderPhotos(); showError('photos','');
  } catch(e) {showError('photos',e.message === 'pixels' ? 'Фото больше 20 мегапикселей. Выберите уменьшенную копию.' : 'Фото не удалось прочитать. Выберите исправный JPEG, PNG или WebP.');}
  finally {photos.disabled = false;}
});
form.addEventListener('input', () => {invalidateResult();document.querySelector('#counter').textContent = document.querySelector('#description').value.length+' / 1000';});
document.querySelector('#channel').addEventListener('change', e => {
  const telegram = e.target.value === 'telegram';
  const contact = document.querySelector('#contact');
  contact.value = ''; contact.placeholder = telegram ? '@demo_test' : 'demo@example.test'; contact.inputMode = telegram ? 'text' : 'email';
  document.querySelector('#contact-label').textContent = telegram ? 'Тестовый Telegram' : 'Тестовый email'; showError('contact',''); invalidateResult();
});
form.addEventListener('submit', async e => {
  e.preventDefault();
  if (submitButton.disabled) return;
  const shownMode = deliveryMode;
  invalidateResult();
  const errors = {};
  ['sofa','size','material','description'].forEach(id => { const value = document.getElementById(id).value.trim(); if(!value) errors[id] = labels[id]; });
  if (document.querySelector('#description').value.trim().length > 1000) errors.description = 'Описание должно быть не длиннее 1000 символов.';
  if (!selected.length) errors.photos = labels.photos;
  if (photos.disabled) errors.photos = 'Подождите, пока закончится проверка фото.';
  const contact = document.querySelector('#contact').value.trim();
  const telegram = document.querySelector('#channel').value === 'telegram';
  if (!contact) errors.contact = labels.contact;
  else if (telegram ? !/^@demo_[a-z0-9_]{1,30}$/i.test(contact) : !/^[a-z0-9._+-]+@example\.test$/i.test(contact)) errors.contact = telegram ? 'Для демо используйте @demo_test (имя начинается с @demo_).' : 'Для демо используйте адрес на @example.test, например demo@example.test.';
  Object.keys(labels).forEach(id => showError(id,errors[id]));
  const list = summary.querySelector('ul'); list.replaceChildren();
  Object.entries(errors).forEach(([id,message]) => {const li=document.createElement('li');const a=document.createElement('a');a.href='#'+id;a.textContent=message;a.addEventListener('click',()=>document.getElementById(id).focus());li.append(a);list.append(li);});
  summary.hidden = !Object.keys(errors).length;
  if (!summary.hidden) {summary.focus();return;}
  const values = Object.fromEntries(['sofa','size','material','description','channel','contact'].map(id=>[id,document.getElementById(id).value.trim()]));
  if (publicForm) {
    renderPublicRequest(values);
    return;
  }
  const signature = JSON.stringify(values);
  if (!pending || pending.signature !== signature || selected.length !== pending.files.length || selected.some((f,i)=>f !== pending.files[i])) {
    pending = {signature, files:[...selected], key:crypto.randomUUID()};
  }
  const body = new FormData();
  Object.entries(values).forEach(([name,value])=>body.append(name,value));
  selected.forEach(file=>body.append('photos',file));
  const sending = document.querySelector('#sending');
  const controls = [...form.querySelectorAll('fieldset, .submit, #sample')];
  controls.forEach(el=>el.disabled=true); form.setAttribute('aria-busy','true'); sending.hidden=false;
  const controller = new AbortController();
  let timeout;
  try {
    const mode = await readDeliveryMode();
    if (mode !== shownMode) {
      showDeliveryNotice(mode);
      requestError(mode ? 'Режим сохранения изменился. Прочитайте пояснение перед кнопкой и нажмите её ещё раз.'
        : 'Не удалось определить режим сохранения. Запрос не отправлен.');
      return;
    }
    timeout = setTimeout(()=>controller.abort(),30000);
    const response = await fetch(api('submissions'), {method:'POST', body, signal:controller.signal,
      headers:{'Idempotency-Key':pending.key, 'X-D4-Delivery-Mode':shownMode}});
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 409 && isDeliveryMode(data.delivery_mode)) showDeliveryNotice(data.delivery_mode);
      Object.entries(data.fields || {}).forEach(([id,message])=>{if (Object.hasOwn(labels,id)) showError(id,message);});
      requestError(data.message || 'Не удалось подтвердить сохранение. Повторите запрос.');
      return;
    }
    receipt = data;
    renderReceipt(data);
  } catch {
    requestError('Не удалось получить ответ. Запрос мог сохраниться. Поля и фото остались: повторите тот же запрос — сохранённая заявка не продублируется.');
  } finally {
    clearTimeout(timeout);controls.forEach(el=>el.disabled=false);submitButton.disabled=!deliveryMode;form.removeAttribute('aria-busy');sending.hidden=true;
  }
});
function requestError(message) {
  summary.querySelector('ul').replaceChildren();
  const item=document.createElement('li');item.textContent=message;summary.querySelector('ul').append(item);
  summary.hidden=false;summary.focus();
}
function renderReceipt(data) {
  summary.hidden=true;
  document.querySelector('#result-title').textContent='Запрос сохранён';
  const details=document.querySelector('#receipt-details');details.replaceChildren();
  const number=document.createElement('p');number.textContent='Номер запроса: '+data.submission_id;
  const note=document.createElement('p');note.textContent='Цена и время выезда требуют согласования.';
  const expiry=document.createElement('p');expiry.textContent='Квитанция доступна до '+new Date(data.receipt_expires*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})+' (1 час после сохранения). После перезагрузки страницы доступ к ней теряется.';
  details.append(number);
  for (const [id, name] of [['telegram', 'Telegram'], ['sheets', 'Google Sheets']]) {
    const channel = (data.channels || []).find(item => item.channel === id);
    const line = document.createElement('p');
    const mode = channel?.simulated ? 'Тестовый режим. ' : '';
    line.textContent = name + ': ' + mode + (channel?.message || 'Канал выключен.');
    details.append(line);
  }
  details.append(note,expiry);
  if (data.photos) {const photosNote=document.createElement('p');photosNote.textContent='Проверенных фотографий: '+data.photos;details.append(photosNote);}
  result.hidden=false;result.focus();
}
document.querySelector('#refresh-receipt').addEventListener('click',async e=>{
  if(!receipt)return;e.target.disabled=true;
  try {
    const response=await fetch(api('receipts/'+receipt.submission_id),{headers:{'X-Receipt-Key':receipt.receipt_key},signal:AbortSignal.timeout(10000)});
    const data=await response.json();
    if(!response.ok){result.hidden=true;requestError(data.message || 'Квитанция временно недоступна.');return;}
    renderReceipt(data);
  } catch {requestError('Квитанция временно недоступна. Это не подтверждение доставки. Повторите проверку позже.');}
  finally{e.target.disabled=false;}
});
document.querySelector('#copy-request').addEventListener('click', async () => {
  const status = document.querySelector('#copy-status');
  try {
    await navigator.clipboard.writeText(publicDraft);
    status.textContent = 'Текст скопирован. Фотографии остаются в браузере.';
  } catch {
    status.textContent = 'Копирование недоступно. Выделите текст результата и скопируйте его вручную.';
  }
  status.hidden = false;
});
document.querySelector('#sample').addEventListener('click',async e=>{
  e.target.disabled=true;invalidateResult();
  try {
    const response=await fetch(new URL('assets/sofa-700.jpg',document.baseURI),{signal:AbortSignal.timeout(10000)});
    if(!response.ok)throw new Error('sample');
    const blob=await response.blob();
    const values={sofa:'Прямой',size:'Два места',material:'Ткань / рогожка',description:'Тест: след от чая на сиденье. Стоковое фото для демонстрации.',channel:'email',contact:'demo@example.test'};
    Object.entries(values).forEach(([id,value])=>document.getElementById(id).value=value);
    document.querySelector('#contact-label').textContent='Тестовый email';document.querySelector('#contact').placeholder='demo@example.test';document.querySelector('#contact').inputMode='email';
    selected=[new File([blob],'demo-sofa.jpg',{type:'image/jpeg'})];renderPhotos();
    Object.keys(labels).forEach(id=>showError(id,''));summary.hidden=true;
    document.querySelector('#counter').textContent=values.description.length+' / 1000';document.querySelector('#sofa').focus();
  } catch {requestError('Тестовое фото не загрузилось. Попробуйте ещё раз или выберите безопасное тестовое фото.');}
  finally{e.target.disabled=false;}
});
document.querySelector('#return').addEventListener('click', () => {result.hidden = true;document.querySelector('#sofa').focus();});
window.addEventListener('pagehide', () => urls.forEach(URL.revokeObjectURL));
document.querySelector('#sample').disabled=false;
document.querySelector('#js-notice').hidden=true;
if (publicForm) {
  showPublicNotice();
  submitButton.disabled = false;
} else {
  readDeliveryMode().then(mode => {
    showDeliveryNotice(mode);
    submitButton.disabled = !mode;
  });
}
