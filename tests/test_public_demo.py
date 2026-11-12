"""Публичная статическая форма готовит копию без backend и доставки."""
import json
import re
import subprocess

from fastapi.testclient import TestClient
import pytest

from server.app import create_app
from server.model import ROOT, Settings


def test_static_entry_is_public_and_backend_entry_is_explicit(tmp_path):
    static = (ROOT / 'public/index.html').read_text()
    assert 'data-form-mode="public"' in static
    assert '24 часов' not in static and 'Это локальный демонстрационный стенд.' not in static
    assert 'только в памяти открытой страницы' in static and 'Сохранения на сервере нет.' in static
    assert 'Для работы формы нужен JavaScript.' in static
    assert 'id="submit-label">Подготовить демо-запрос</span>' in static
    with TestClient(create_app(Settings(data_dir=tmp_path / 'registry', prefix='/d4')),
                    base_url='http://127.0.0.1') as client:
        for path in ['/d4/', '/d4/index.html']:
            result = client.get(path)
            assert result.status_code == 200 and 'data-form-mode="server"' in result.text
            assert 'data-form-mode="public"' not in result.text
            assert 'После сохранения заявка и обработанные фото хранятся локально на этом стенде до 24 часов.' in result.text
            assert 'Это локальный демонстрационный стенд.' in result.text
            assert 'Внешняя доставка не подключена.' in result.text
            assert 'Сохранения на сервере нет.' not in result.text
            assert 'id="submit-label">Сохранить тестовый запрос</span>' in result.text


@pytest.mark.parametrize('clipboard', ['success', 'denied'])
def test_public_form_never_calls_api_or_persistent_storage(clipboard):
    script = r'''
const fs=require('node:fs'), vm=require('node:vm');
const nodes=new Map();
function node(id) {
  if(!nodes.has(id)) nodes.set(id,{
    value:'',textContent:'',hidden:true,disabled:id==='.submit',children:[],handlers:{},
    dataset:id==='html'?{formMode:'public'}:{},
    addEventListener(type,fn){this.handlers[type]=fn},
    querySelector:node,querySelectorAll(){return [node('.submit')]},
    setAttribute(){},removeAttribute(){},focus(){focused=id},
    replaceChildren(){this.children=[]},append(...items){this.children.push(...items)},
  });
  return nodes.get(id);
}
let focused='', copied='';
const calls=[];
class TestURL extends URL {static createObjectURL(){return 'blob:synthetic-photo'} static revokeObjectURL(){}}
const context=vm.createContext({
  document:{baseURI:'https://example.test/portfolio/d4/',querySelector:node,getElementById:id=>node('#'+id),
            createElement:tag=>({tag,textContent:'',children:[],setAttribute(){},addEventListener(){},append(...items){this.children.push(...items)}})},
  window:{addEventListener(){}}, URL:TestURL,
  navigator:{clipboard:{async writeText(text){if(process.argv[2]==='denied')throw Error('Synthetic denial');copied=text}}},
  async fetch(url){calls.push(String(url));throw Error('Any network access forbidden')},
  localStorage:new Proxy({},{get(){throw Error('Persistent storage forbidden')}}),
  createImageBitmap:async file=>({width:file.width||40,height:30,close(){}}),
});
(async()=>{
  vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
  const before={enabled:!node('.submit').disabled,note:node('#submit-note').textContent,
    title:node('#demo-mode-title').textContent,storage:node('#storage-note').textContent,
    aside:node('#aside-delivery-note').textContent,label:node('#submit-label').textContent,calls:[...calls]};
  const submit=()=>node('#lead-form').handlers.submit({preventDefault(){}});
  await submit();
  const invalid={focused, errors:node('ul').children.length};
  const values={sofa:'Прямой',size:'Два места',material:'Ткань / рогожка',description:'<img src=x onerror=alert(1)> Тестовое пятно',channel:'email',contact:'demo@example.test'};
  for(const [id,value] of Object.entries(values))node('#'+id).value=value;
  node('#photos').files=[{name:'synthetic.jpg',size:200,type:'image/jpeg'}];
  await node('#photos').handlers.change();
  await submit();
  const rendered=node('#receipt-details').children.map(item=>item.textContent);
  await node('#copy-request').handlers.click();
  await submit();
  const repeated=node('#receipt-details').children.map(item=>item.textContent);
  node('#photos').files=[{name:'too-wide.jpg',size:100,type:'image/jpeg',width:1000000}];
  await node('#photos').handlers.change();
  const badPhoto=node('#photos-error').textContent;
  process.stdout.write(JSON.stringify({before,invalid,title:node('#result-title').textContent,
    rendered,repeated,copied,copyStatus:node('#copy-status').textContent,
    refreshHidden:node('#refresh-receipt').hidden,copyHidden:node('#copy-request').hidden,badPhoto,calls}));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run(['node', '-e', script, str(ROOT / 'public/app.js'), clipboard],
                            capture_output=True, text=True, check=True, timeout=10)
    ui = json.loads(result.stdout)
    assert ui['before']['enabled'] and not ui['before']['calls']
    assert 'Публичное демо' in ui['before']['note'] and 'сохранения на сервере нет' in ui['before']['note']
    assert '24 часов' not in ' '.join(value for value in ui['before'].values() if isinstance(value, str))
    assert ui['before']['title'] == 'Это публичная демонстрационная форма.'
    static = (ROOT / 'public/index.html').read_text()
    assert ui['before']['storage'] == re.search(r'id="storage-note">([^<]+)</span>', static)[1]
    assert ui['before']['label'] == 'Подготовить демо-запрос'
    assert ui['invalid'] == {'focused': '#error-summary', 'errors': 6}
    assert ui['title'] == 'Демо-запрос подготовлен'
    assert ui['rendered'] == ui['repeated']
    assert any('demo@example.test' in line for line in ui['rendered'])
    assert any('<img src=x onerror=alert(1)>' in line for line in ui['rendered'])
    assert any('не отправлялось' in line for line in ui['rendered'])
    assert all('Номер запроса' not in line for line in ui['rendered'])
    assert ui['refreshHidden'] and not ui['copyHidden'] and not ui['calls']
    assert '20 мегапикселей' in ui['badPhoto']
    if clipboard == 'success':
        assert 'demo@example.test' in ui['copied'] and 'Текст скопирован' in ui['copyStatus']
    else:
        assert not ui['copied'] and 'вручную' in ui['copyStatus']
