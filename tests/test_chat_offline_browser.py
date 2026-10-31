"""Real Chromium DOM checks with in-page API fixtures; no network or model."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import chat
from harness import ROOT, start_process, stop_process
from tests.test_chat_browser import BROWSER


@unittest.skipUnless(BROWSER,'Chromium required for offline DOM checks')
class ChatOfflineBrowserTests(unittest.TestCase):
    def test_real_dom_theme_metadata_errors_management_and_backward_scroll(self):
        (ROOT/'sessions').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'sessions',prefix='Offline Chat ') as folder:
            root=Path(folder)
            prelude=r'''
const fixtureTurns=Array.from({length:6000},(_,i)=>({role:i%2?'assistant':'user',content:'Message '+i+'\n'+'Retained text. '.repeat(25)}));
fixtureTurns[5999].content='## Safe heading\n<img src=x onerror="window.__xss=1">\n```python\r\n    print("exact")\r\n\r\n```\r\n';
const fixtures=new Map([['A',{id:'A',title:'A',created_at:1793366520000,revision:'a1',rollovers:2,turns:fixtureTurns}],['B',{id:'B',title:'B',created_at:null,revision:'b1',rollovers:0,turns:[{role:'user',content:'Only B'},{role:'assistant',content:'B answer'}]}]]);
let fixtureFailure='',fixtureRequests=[],fixtureSendRelease,fixtureStatsUnavailable=false;
Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>window.__copied=text}});
window.fetch=async(path,options)=>{
  const data=JSON.parse(options.body);fixtureRequests.push({path,...data});
  if(path===fixtureFailure)throw Error('network fixture');
  const c=fixtures.get(data.id);let result;
  if(path==='/heartbeat')result={connected:true};
  else if(path==='/conversations')result={conversations:[...fixtures.values()].map(({turns,...row})=>row)};
  else if(path==='/history'||path==='/send'){
    if(path==='/send'){
      await new Promise(resolve=>fixtureSendRelease=resolve);
      c.turns.push({role:'user',content:data.message},{role:'assistant',content:data.message+' answer'});c.revision+='x';
    }
    const end=data.before??c.turns.length,start=Math.max(0,end-data.limit);
    result={...c,turns:c.turns.slice(start,end),start,end,total:c.turns.length,before:start||null};
  }else if(path==='/stats')result={id:c.id,revision:c.revision,context_percent:fixtureStatsUnavailable?null:c.id==='A'?25:12.5,rollovers:c.rollovers};
  else if(path==='/rename'){c.title=data.title.trim();result={id:c.id};}
  else if(path==='/delete'){if(!data.confirm)throw Error('confirmation missing');fixtures.delete(data.id);result={deleted:true};}
  else throw Error('unexpected route');
  return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(result))};
};
'''
            checks=r'''
async function verifyOffline(){
  const assert=(ok,message)=>{if(!ok)throw Error(message);};
  const wait=async condition=>{for(let i=0;i<100;i++){if(condition())return;await new Promise(r=>setTimeout(r,10));}throw Error('timed out');};
  await wait(()=>loaded);
  if(!sessionStorage.getItem('offline-refreshed')){await select('B');sessionStorage.setItem('offline-refreshed','yes');location.reload();return;}
  assert(active==='B'&&location.hash===''&&token==='offline-token','real page refresh restores tab token and active chat');
  await select('A');await wait(()=>document.querySelector('#context').textContent==='Context 25%');
  assert(document.documentElement.dataset.theme==='dark','default dark');
  applyTheme('light',true);assert(localStorage.getItem(themeKey)==='light','save explicit light');applyTheme(stored('localStorage',themeKey));assert(themeMode==='light','restore light');applyTheme('dark',true);
  assert(!document.querySelector('#theme').textContent.includes('System'),'no System choice');
  assert(document.querySelector('#started').textContent.startsWith('Started '),'known timestamp');
  assert(view.querySelectorAll('.message').length===40,'bounded initial DOM');
  assert(view.querySelector('.message').dataset.index==='5960','latest page first');
  assert(timeline.scrollHeight-timeline.scrollTop-timeline.clientHeight<3,'initial viewport at bottom');
  assert(!view.querySelector('img,script'),'untrusted HTML is text');
  document.querySelector('.copy').click();await wait(()=>window.__copied!==undefined);assert(window.__copied==='    print("exact")\r\n\r\n','exact code copy');
  const initialNodes=view.querySelectorAll('*').length;
  for(const width of [1280,760,390,320]){
    const frame=document.createElement('iframe');frame.style.cssText='position:fixed;left:0;top:0;border:0;width:'+width+'px;height:640px';frame.srcdoc=window.__layoutPage;document.body.append(frame);
    await wait(()=>frame.contentDocument?.querySelector('#status')?.textContent.startsWith('History loaded'));const d=frame.contentDocument,r=s=>d.querySelector(s).getBoundingClientRect();
    assert(d.documentElement.scrollWidth<=width+1,'no horizontal overflow at '+width);
    assert(r('form').bottom<=640+1&&r('#timeline').bottom<=r('.composer-area').top+1,'composer remains separate at '+width);
    frame.remove();
  }
  timeline.scrollTop=5;const anchor=view.querySelector('.message').getBoundingClientRect().top;
  await loadOlder();assert(shownStart===5920,'previous page');
  assert(Math.abs(view.querySelector('[data-index="5960"]').getBoundingClientRect().top-anchor)<2,'prepend anchor stable');
  assert(new Set([...view.querySelectorAll('.message')].map(n=>n.dataset.index)).size===80,'no duplicates');
  input.value='Pending new request';input.dispatchEvent(new Event('input'));const sending=form.onsubmit({preventDefault(){}});
  await wait(()=>Boolean(fixtureSendRelease));timeline.scrollTop=timeline.scrollHeight/2;const top=timeline.scrollTop;fixtureSendRelease();await sending;
  assert(Math.abs(timeline.scrollTop-top)<2,'response does not interrupt reading');assert(shownEnd===6002,'new pair appended');
  fixtureFailure='/rename';openManage(conversations.find(r=>r.id==='A'),'Rename');document.querySelector('#manage-confirm').click();
  await wait(()=>!document.querySelector('#error-notice').hidden);dialog.close();await select('B');
  assert(!document.querySelector('#error-notice').hidden,'error persists across success/switch');
  await wait(()=>document.querySelector('#context').textContent==='Context 12.5%');
  assert(document.querySelector('#rollovers').textContent==='Rollovers 0','independent rollovers');
  assert(document.querySelector('#started').textContent==='Start time unavailable','unknown legacy timestamp');
  assert(!view.textContent.includes('Message 5999'),'conversation isolated');
  fixtureStatsUnavailable=true;await measure('B',selection);assert(document.querySelector('#context').textContent==='Context —','neutral unavailable measurement');fixtureStatsUnavailable=false;
  document.querySelector('#error-close').click();assert(document.querySelector('#error-notice').hidden,'dismiss error');fixtureFailure='';
  openManage(conversations.find(r=>r.id==='B'),'Rename');renameInput.value='<script>Renamed</script>';document.querySelector('#manage-confirm').click();
  await wait(()=>!dialog.open);assert(title.textContent==='<script>Renamed</script>'&&!title.querySelector('script'),'safe cosmetic title');
  fixtureFailure='/delete';openManage(conversations.find(r=>r.id==='B'),'Delete');document.querySelector('#manage-confirm').click();await wait(()=>!document.querySelector('#error-notice').hidden);
  assert(fixtures.size===2&&dialog.open,'failed deletion preserves conversation');dialog.close();document.querySelector('#error-close').click();
  fixtureFailure='/send';input.value='Keep failed send';await form.onsubmit({preventDefault(){}});
  assert(input.value==='Keep failed send'&&!document.querySelector('#error-notice').hidden,'failed send preserves input and shows error');
  document.querySelector('#error-close').click();fixtureFailure='';
  openManage(conversations.find(r=>r.id==='B'),'Delete');assert(fixtures.size===2,'confirmation required');dialog.close();
  document.querySelector('#app').style.gridTemplateColumns='minmax(0,1fr)';
  for(const id of ['B','A']){await select(id);openManage(conversations.find(r=>r.id===id),'Delete');document.querySelector('#manage-confirm').click();await wait(()=>!dialog.open);}
  assert(active===null&&send.disabled&&!fresh.disabled,'last deletion leaves usable empty state');
  assert(!document.querySelector('aside').textContent.includes('Saved on this device.'),'obsolete footer removed');
  const report={initialMessages:40,initialNodes,totalMessages:6000,historyRequests:fixtureRequests.filter(r=>r.path==='/history').map(r=>({limit:r.limit,before:r.before}))};
  document.body.dataset.test='PASS';const pre=document.createElement('pre');pre.id='test-result';pre.textContent=JSON.stringify(report);document.body.append(pre);
}
verifyOffline().catch(error=>{document.body.dataset.test='FAIL';const pre=document.createElement('pre');pre.id='test-result';pre.textContent=error.stack;document.body.append(pre);});
'''
            layout=chat.PAGE.replace('<script>','<script>'+prelude.replace('</script','<\\/script'),1)
            fixtures=prelude+'\nwindow.__layoutPage='+json.dumps(layout)+';\n'
            page=chat.PAGE.replace('initialize();','initialize();\n'+checks.replace('</script','<\\/script'),1).replace('<script>','<script>'+fixtures.replace('</script','<\\/script'),1)
            path=root/'fixture.html';path.write_text(page,encoding='utf-8')
            process=start_process([BROWSER,'--headless=new','--disable-gpu','--no-sandbox','--no-first-run','--no-default-browser-check',
                '--disable-background-networking','--disable-extensions','--disable-component-update','--disable-sync','--disable-breakpad',
                '--user-data-dir='+str(root/'profile'),'--dump-dom','--virtual-time-budget=15000',path.as_uri()+'#offline-token'],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace')
            try:output,error=process.communicate(timeout=35)
            finally:stop_process(process)
            destination=ROOT/'logs/chat-management-audit/offline-browser.html'
            destination.parent.mkdir(parents=True,exist_ok=True);destination.write_text(output,encoding='utf-8')
            self.assertEqual(process.returncode,0,error[-2000:]);self.assertIn('data-test="PASS"',output,output[-5000:])


if __name__=='__main__':unittest.main()
