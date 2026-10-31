// Real Chromium, built-in Node WebSocket, controlled local Chat API; no model.
const assert=require('node:assert/strict'),fs=require('node:fs');
const [port,scenario,fixturePath]=process.argv.slice(2),fixture=JSON.parse(fs.readFileSync(fixturePath,'utf8'));
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
let serial=0,socket;const replies=new Map(),listeners=new Map();
async function command(method,params={}){
  const id=++serial;
  return new Promise((resolve,reject)=>{const timer=setTimeout(()=>{replies.delete(id);reject(Error('CDP timeout: '+method));},10000);replies.set(id,{resolve,reject,timer});socket.send(JSON.stringify({id,method,params}));});
}
function once(name){return new Promise(resolve=>{const queue=listeners.get(name)||[];queue.push(resolve);listeners.set(name,queue);});}
async function evaluate(expression){
  const result=await command('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true,userGesture:true});
  if(result.exceptionDetails)throw Error(result.exceptionDetails.exception?.description||result.exceptionDetails.text);
  return result.result.value;
}
async function wait(expression){
  const until=Date.now()+8000;
  while(Date.now()<until){try{if(await evaluate(expression))return;}catch{}await sleep(35);}
  throw Error('Browser condition timed out: '+expression);
}
const node=selector=>`document.querySelector(${JSON.stringify(selector)})`;
async function click(selector){await evaluate(`${node(selector)}.click()`);}
async function input(value){await evaluate(`document.querySelector('textarea').value=${JSON.stringify(value)};document.querySelector('textarea').dispatchEvent(new Event('input'))`);}
async function go(url){await command('Page.navigate',{url});await wait(`document.readyState==='complete' && Boolean(document.querySelector('#status'))`);}
async function ready(){await wait(`document.querySelectorAll('.conversation-select').length===2 && document.querySelector('#status').textContent.startsWith('History loaded')`);}
async function select(id){await click(`nav button[data-id="${id}"]`);await wait(`active===${JSON.stringify(id)} && loaded`);}
async function reload(){const previous=await evaluate('performance.timeOrigin');await command('Page.reload');await wait(`performance.timeOrigin!==${previous} && document.readyState==='complete'`);await ready();}
async function state(){return evaluate(`({active,theme:document.documentElement.dataset.theme,status:label.textContent,connection:connection.textContent,input:input.value,text:view.textContent,count:document.querySelectorAll('.conversation-select').length})`);}

async function run(){
  const target=await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,{method:'PUT'}).then(r=>r.json());
  socket=new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
  socket.onmessage=event=>{
    const message=JSON.parse(event.data);
    if(message.id){const pending=replies.get(message.id);if(!pending)return;clearTimeout(pending.timer);replies.delete(message.id);message.error?pending.reject(Error(message.error.message)):pending.resolve(message.result);}
    else{const queue=listeners.get(message.method);if(queue?.length)queue.shift()(message.params);}
  };
  await command('Page.enable');await command('Runtime.enable');
  await command('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:'light'}]});
  // Capture Copy's exact argument without changing the user's OS clipboard.
  await command('Page.addScriptToEvaluateOnNewDocument',{source:`Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.__copied=text;}}});`});
  if(scenario==='storage')await command('Page.addScriptToEvaluateOnNewDocument',{source:`for(const key of ['sessionStorage','localStorage'])Object.defineProperty(window,key,{get(){throw new DOMException('Storage blocked','SecurityError')}});`});
  if(scenario==='auth'){
    await go(fixture.base+'/');await wait(`label.dataset.state==='error'`);
    assert.match((await state()).status,/Authentication required/);assert.equal((await state()).count,0);
    await evaluate(`sessionStorage.setItem(tokenKey,'stale-token')`);
    await go(fixture.url);await ready();
    assert.equal(await evaluate(`sessionStorage.getItem(tokenKey)===${JSON.stringify(fixture.token)}`),true);
    await go(fixture.base+'/#incorrect-new-token');await wait(`label.dataset.state==='error'`);
    assert.match((await state()).status,/expired or is invalid/);assert.equal((await state()).count,0);
    assert.equal(await evaluate(`sessionStorage.getItem(tokenKey)==='incorrect-new-token'`),true);
    assert.equal(await evaluate(`document.body.textContent.includes('incorrect-new-token')`),false);
    assert.equal(await evaluate(`Object.values(localStorage).includes('incorrect-new-token')`),false);
    await go(fixture.url);await ready();
  }else{
    await go(fixture.url);await ready();
    if(scenario==='stale-stats'){
      await evaluate(`window.__fetch=fetch;window.fetch=(path,options)=>{const data=JSON.parse(options.body);if(path==='/stats'&&data.id===${JSON.stringify(fixture.a)})return new Promise(resolve=>window.__lateStats=()=>resolve({ok:false,status:400,json:async()=>({error:'Old conversation statistics failed'})}));return window.__fetch(path,options);};`);
      await select(fixture.a);await wait(`Boolean(window.__lateStats)`);await select(fixture.b);
      await wait(`document.querySelector('#context').textContent==='Context 12.5%'`);await evaluate(`window.__lateStats()`);await sleep(80);
      assert.equal((await state()).active,fixture.b);assert.match((await state()).status,/History loaded/);
      assert.equal(await evaluate(`document.querySelector('#error-notice').hidden`),true);assert.equal((await state()).connection,'Connected');
    }else if(scenario==='legacy'){
      await click(`nav button[data-id="${fixture.b}"]`);await wait(`active===${JSON.stringify(fixture.b)} && label.textContent.startsWith('Legacy v2')`);
      assert.equal(await evaluate('connection.textContent'),'Connected');assert.equal(await evaluate(`document.querySelector('#error-notice').hidden`),true);
      assert.match((await state()).text,/legacy v2 conversation/);assert.equal(await evaluate('send.disabled'),true);
      await evaluate(`nav.querySelector('button[data-id="${fixture.b}"]').parentElement.querySelector('.menu-toggle').click()`);
      assert.equal(await evaluate(`document.querySelector('#menu-rename').disabled`),true);assert.equal(await evaluate(`document.querySelector('#menu-delete').disabled`),false);
      await click('#menu-delete');await click('#manage-cancel');assert.equal(await evaluate('conversations.length'),2);
      await evaluate(`nav.querySelector('button[data-id="${fixture.b}"]').parentElement.querySelector('.menu-toggle').click()`);await click('#menu-delete');await click('#manage-confirm');
      await wait(`conversations.length===1 && !dialog.open && loaded`);assert.equal(await evaluate('active'),fixture.a);
      assert.equal(await evaluate('connection.textContent'),'Connected');assert.equal(await evaluate(`document.querySelector('#error-notice').hidden`),true);
    }else if(scenario==='drop-area'){
      await command('Emulation.setDeviceMetricsOverride',{width:1200,height:900,deviceScaleFactor:1,mobile:false});
      await select(fixture.b);
      const point=await evaluate(`(()=>{const r=timeline.getBoundingClientRect(),c=document.querySelector('.composer-area').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+80,bottom:c.top};})()`);
      assert.ok(point.y+100<point.bottom,'Drop must be substantially above the composer');
      await evaluate(`(()=>{const transfer=new DataTransfer();transfer.items.add(new File([Uint8Array.from(atob(${JSON.stringify(fixture.imageData)}),c=>c.charCodeAt(0))],'image.png',{type:'image/png'}));window.__drag=transfer;const target=document.elementFromPoint(${point.x},${point.y});target.dispatchEvent(new DragEvent('dragenter',{bubbles:true,cancelable:true,dataTransfer:transfer}));})();`);
      assert.equal(await evaluate(`dropZone.dataset.drag`),'true');
      await evaluate(`view.dispatchEvent(new DragEvent('dragenter',{bubbles:true,dataTransfer:window.__drag}));view.dispatchEvent(new DragEvent('dragleave',{bubbles:true,dataTransfer:window.__drag}));`);
      assert.equal(await evaluate('dropZone.dataset.drag'),'true');
      await evaluate(`document.elementFromPoint(${point.x},${point.y}).dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:window.__drag}))`);
      await wait(`document.querySelectorAll('.image-draft').length===1`);assert.equal(await evaluate('dropZone.dataset.drag'),'');
      await evaluate(`dropZone.dispatchEvent(new DragEvent('dragenter',{bubbles:true,dataTransfer:window.__drag}));document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));`);
      assert.equal(await evaluate('dropZone.dataset.drag'),'');
      await evaluate(`(()=>{const t=new DataTransfer();t.items.add(new File(['no'],'bad.txt',{type:'text/plain'}));document.querySelector('aside').dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:t}));})()`);
      assert.equal(await evaluate(`document.querySelectorAll('.image-draft').length`),1);
      await input('Image dropped above composer');await click('#send');await wait(`!pending && view.textContent.includes('Image dropped above composer answer')`);
      await wait(`view.querySelector('.message-image img')?.naturalWidth>0`);
    }else if(scenario==='images'){
      await select(fixture.b);
      await command('DOM.enable');const dom=await command('DOM.getDocument');const picker=await command('DOM.querySelector',{nodeId:dom.root.nodeId,selector:'#image-files'});
      await command('DOM.setFileInputFiles',{nodeId:picker.nodeId,files:[fixture.imagePath]});
      await wait(`document.querySelectorAll('.image-draft').length===1`);
      const drop=`(()=>{const bytes=Uint8Array.from(atob(${JSON.stringify(fixture.imageData)}),c=>c.charCodeAt(0)),transfer=new DataTransfer();transfer.items.add(new File([bytes],'untrusted.png',{type:'image/png'}));form.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer}));})()`;
      await evaluate(drop);await wait(`document.querySelectorAll('.image-draft').length===2`);
      await evaluate(drop);await wait(`!document.querySelector('#error-notice').hidden`);
      assert.equal(await evaluate(`document.querySelectorAll('.image-draft').length`),2);
      await click('#error-close');await click('.image-draft button');await wait(`document.querySelectorAll('.image-draft').length===1`);
      await command('DOM.setFileInputFiles',{nodeId:picker.nodeId,files:[fixture.imagePath]});await wait(`document.querySelectorAll('.image-draft').length===2`);
      await input('Inspect both images');await click('#send');await wait(`!pending && view.textContent.includes('Inspect both images answer')`);
      await wait(`view.querySelectorAll('.message-image img').length===2 && [...view.querySelectorAll('.message-image img')].every(i=>i.complete&&i.naturalWidth>0)`);
      await click('.message-image button');await wait(`document.querySelector('#image-view').open && document.querySelector('#image-view img').naturalWidth>0`);await click('#image-view button');
      await reload();assert.equal(await evaluate(`view.querySelectorAll('.message-image').length`),2);
      await input('Continue as text');await click('#send');await wait(`!pending && view.textContent.includes('Continue as text answer')`);
      await evaluate(`view.querySelector('.message-image').querySelectorAll('button')[1].click()`);await wait(`document.querySelectorAll('.image-draft').length===1`);
      await input('Examine again');await click('#send');await wait(`!pending && view.textContent.includes('Examine again answer')`);
      await select(fixture.a);assert.equal(await evaluate(`view.querySelectorAll('.message-image').length`),0);
      await select(fixture.b);assert.equal(await evaluate(`view.querySelectorAll('.message-image').length`),3);
    }else if(scenario==='refresh'){
      await select(fixture.a);
      const before=await state();assert.match(before.text,/Archived A/);assert.match(before.text,/Current A/);assert.doesNotMatch(before.text,/Only B/);
      assert.equal(await evaluate('location.hash'),'');await reload();assert.deepEqual(await state(),before);
      await select(fixture.b);assert.match((await state()).text,/Only B/);assert.doesNotMatch((await state()).text,/Archived A/);
      await reload();assert.equal((await state()).active,fixture.b);
    }else if(scenario==='markdown'){
      await select(fixture.a);
      assert.equal(await evaluate(`view.querySelectorAll('.message.user').length`),2);
      assert.equal(await evaluate(`view.querySelectorAll('.message.assistant').length`),2);
      assert.equal(await evaluate(`view.querySelectorAll('.code-frame').length`),2);
      assert.equal(await evaluate(`view.querySelector('.code-bar span').textContent`),'python');
      assert.equal(await evaluate(`view.querySelector('pre code').textContent`),fixture.code);
      assert.equal(await evaluate(`Boolean(view.querySelector('p code'))`),true);
      assert.equal(await evaluate(`view.querySelectorAll('li').length`),2);
      assert.equal(await evaluate(`view.querySelectorAll('img,script,iframe,svg').length`),0);
      assert.equal(await evaluate(`window.__xss===undefined`),true);
      assert.match((await state()).text,/<img src=x onerror=/);
      await click('.copy');await wait(`document.querySelector('.copy').textContent==='Copied'`);
      assert.equal(await evaluate('window.__copied'),fixture.code);
    }else if(scenario==='theme'){
      assert.equal((await state()).theme,'dark');await click('#theme');assert.equal((await state()).theme,'light');
      await reload();assert.equal((await state()).theme,'light');
      assert.deepEqual(await evaluate('Object.keys(localStorage)'),['backtrackify.chat.theme']);
      await click('#theme');assert.equal((await state()).theme,'dark');await reload();assert.equal((await state()).theme,'dark');
      assert.equal(await evaluate(`document.querySelector('#theme').textContent.includes('System')`),false);
    }else if(scenario==='send'){
      await select(fixture.b);await input('Useful new request');
      await command('Fetch.enable',{patterns:[{urlPattern:'*/send',requestStage:'Request'}]});
      let paused=once('Fetch.requestPaused');await click('#send');const request=await paused;
      assert.equal(await evaluate('send.disabled'),true);assert.equal((await state()).status,'Generating response…');
      assert.equal((await state()).input,'Useful new request');
      await command('Fetch.continueRequest',{requestId:request.requestId});await command('Fetch.disable');
      await wait(`label.textContent.startsWith('Saved locally') && !pending`);
      assert.equal((await state()).input,'');assert.match((await state()).text,/Useful new request answer/);
      assert.equal(await evaluate(`label.dataset.state`),'good');
      await input('Keep this draft');
      await command('Fetch.enable',{patterns:[{urlPattern:'*/send',requestStage:'Request'}]});
      paused=once('Fetch.requestPaused');await click('#send');const failed=await paused;
      await command('Fetch.failRequest',{requestId:failed.requestId,errorReason:'ConnectionFailed'});await command('Fetch.disable');
      await wait(`label.dataset.state==='error' && !pending`);assert.equal((await state()).input,'Keep this draft');
      assert.match((await state()).status,/Cannot reach Local Chat/);
      await input('invalid-completion');await click('#send');await wait(`label.dataset.state==='error' && !pending`);
      assert.match((await state()).status,/incomplete/);assert.equal((await state()).input,'invalid-completion');
      assert.doesNotMatch((await state()).text,/invalid-completion/);
    }else if(scenario==='switch'){
      await select(fixture.a);await input('A pending request');
      await command('Fetch.enable',{patterns:[{urlPattern:'*/send',requestStage:'Request'}]});
      const paused=once('Fetch.requestPaused');await click('#send');const request=await paused;
      await select(fixture.b);const before=(await state()).text;await input('B unsent draft');
      await command('Fetch.continueRequest',{requestId:request.requestId});await command('Fetch.disable');
      await wait('pending===null');assert.equal((await state()).active,fixture.b);assert.equal((await state()).text,before);assert.equal((await state()).input,'B unsent draft');
      await select(fixture.a);assert.match((await state()).text,/A pending request answer/);assert.equal((await state()).input,'');
      await select(fixture.b);assert.equal((await state()).input,'B unsent draft');
    }else if(scenario==='new'){
      await click('#new');await wait(`conversations.length===3 && loaded && active!==${JSON.stringify(fixture.a)} && active!==${JSON.stringify(fixture.b)}`);
      assert.equal((await state()).text.includes('Start a conversation'),true);await input('A third independent request');await click('#send');
      await wait(`label.textContent.startsWith('Saved locally') && !pending`);const id=(await state()).active;
      await command('Page.reload');await wait(`loaded && active===${JSON.stringify(id)}`);assert.match((await state()).text,/A third independent request answer/);
      await select(fixture.a);assert.doesNotMatch((await state()).text,/third independent/);
    }else if(scenario==='layout'){
      await select(fixture.a);
      for(const [width,height] of [[1280,900],[760,800],[390,844],[320,640]]){
        await command('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<621});
        const dimensions=await evaluate(`(()=>{const r=s=>document.querySelector(s).getBoundingClientRect();return {page:document.documentElement.scrollWidth,viewport:innerWidth,form:r('form').bottom,history:r('#timeline').bottom,composer:r('.composer-area').top,height:innerHeight};})()`);
        assert.ok(dimensions.page<=dimensions.viewport+1,JSON.stringify(dimensions));assert.ok(dimensions.form<=dimensions.height+1,JSON.stringify(dimensions));assert.ok(dimensions.history<=dimensions.composer+1,JSON.stringify(dimensions));
        assert.equal(await evaluate(`view.querySelector('pre').scrollWidth>view.querySelector('pre').clientWidth`),true);
        if(width<621){await click('#sidebar-toggle');assert.equal(await evaluate(`getComputedStyle(document.querySelector('aside')).display`),'flex');await select(fixture.a);assert.equal(await evaluate(`getComputedStyle(document.querySelector('aside')).display`),'none');}
      }
    }else if(scenario==='restart'){
      await select(fixture.a);console.log('RESTART_REQUEST');
      await new Promise(resolve=>process.stdin.once('data',()=>{process.stdin.pause();resolve();}));
      await command('Page.reload');await wait(`label.dataset.state==='error'`);assert.match((await state()).status,/expired/);
      await go(fixture.base+'/#'+fixture.nextToken);await ready();assert.equal((await state()).active,fixture.a);assert.match((await state()).text,/Archived A/);
      assert.equal(await evaluate(`sessionStorage.getItem(tokenKey)===${JSON.stringify(fixture.nextToken)}`),true);
    }else if(scenario==='storage'){
      assert.equal(await evaluate('location.hash.length>0'),true);await reload();assert.equal((await state()).count,2);
    }else if(scenario==='management'){
      await select(fixture.a);await wait(`document.querySelector('#context').textContent==='Context 25%'`);
      const started=await evaluate(`document.querySelector('#started').textContent`);assert.match(started,/Started /);
      assert.equal(await evaluate(`document.querySelector('#rollovers').textContent`),'Rollovers 1');
      const order=await evaluate(`conversations.map(r=>r.id)`);
      await evaluate(`openManage(conversations.find(r=>r.id===${JSON.stringify(fixture.a)}),'Rename')`);
      await evaluate(`renameInput.value='<script>A renamed</script>'`);await click('#manage-confirm');await wait(`!dialog.open && title.textContent.includes('A renamed')`);
      assert.deepEqual(await evaluate(`conversations.map(r=>r.id)`),order);
      assert.equal(await evaluate(`title.querySelector('script')`),null);
      await reload();assert.equal(await evaluate(`document.querySelector('#started').textContent`),started);
      assert.equal(await evaluate(`title.textContent`),'<script>A renamed</script>');
      await select(fixture.b);await wait(`document.querySelector('#context').textContent==='Context 12.5%'`);
      assert.equal(await evaluate(`document.querySelector('#rollovers').textContent`),'Rollovers 0');
      assert.doesNotMatch((await state()).text,/Archived A/);
    }else if(scenario==='notifications'){
      await select(fixture.a);
      await command('Fetch.enable',{patterns:[{urlPattern:'*/rename',requestStage:'Request'}]});
      await evaluate(`openManage(conversations.find(r=>r.id===active),'Rename')`);
      let paused=once('Fetch.requestPaused');await click('#manage-confirm');let request=await paused;
      await command('Fetch.failRequest',{requestId:request.requestId,errorReason:'ConnectionFailed'});await command('Fetch.disable');
      await wait(`!document.querySelector('#error-notice').hidden`);const text=await evaluate(`document.querySelector('#error-text').textContent`);
      await click('#manage-cancel');await select(fixture.b);assert.equal(await evaluate(`document.querySelector('#error-notice').hidden`),false);
      assert.equal(await evaluate(`document.querySelector('#error-text').textContent`),text);
      await evaluate(`openManage(conversations.find(r=>r.id===active),'Rename');renameInput.value=''`);await click('#manage-confirm');
      await wait(`document.querySelector('#error-text').textContent.includes('1..120')`);await click('#manage-cancel');
      await click('#error-close');assert.equal(await evaluate(`document.querySelector('#error-notice').hidden`),true);
    }else if(scenario==='deletion'){
      await select(fixture.a);await evaluate(`openManage(conversations.find(r=>r.id===active),'Delete')`);
      assert.equal(await evaluate(`dialog.open`),true);assert.equal((await state()).count,2);await click('#manage-cancel');assert.equal((await state()).count,2);
      for(let i=0;i<2;i++){
        await evaluate(`openManage(conversations.find(r=>r.id===active),'Delete')`);await click('#manage-confirm');
        await wait(`!dialog.open && conversations.length===${1-i}`);
      }
      assert.equal(await evaluate(`active===null && send.disabled && !fresh.disabled`),true);
      await command('Page.reload');await wait(`conversations.length===0 && label.textContent.includes('Create a new')`);
      assert.equal(await evaluate(`document.querySelectorAll('.message').length`),0);
    }else if(scenario==='long'){
      await select(fixture.a);
      assert.equal(await evaluate(`view.querySelectorAll('.message').length`),40);
      assert.equal(await evaluate(`view.querySelector('.message').dataset.index`),'5960');
      assert.equal(await evaluate(`timeline.scrollHeight-timeline.scrollTop-timeline.clientHeight<3`),true);
      // Pause the older page and record a visible anchor; prepend must retain its position.
      await command('Fetch.enable',{patterns:[{urlPattern:'*/history',requestStage:'Request'}]});
      let paused=once('Fetch.requestPaused');await evaluate(`timeline.scrollTop=5`);let request=await paused;
      const anchor=await evaluate(`view.querySelector('.message').getBoundingClientRect().top`);
      await command('Fetch.continueRequest',{requestId:request.requestId});await command('Fetch.disable');await wait(`shownStart===5920 && !olderBusy`);
      assert.ok(Math.abs(await evaluate(`view.querySelector('[data-index="5960"]').getBoundingClientRect().top`)-anchor)<2);
      assert.equal(await evaluate(`view.querySelectorAll('.message').length`),80);
      assert.equal(await evaluate(`new Set([...view.querySelectorAll('.message')].map(n=>n.dataset.index)).size`),80);
      await evaluate(`timeline.scrollTop=timeline.scrollHeight`);await input('New pair after long history');
      await command('Fetch.enable',{patterns:[{urlPattern:'*/send',requestStage:'Request'}]});paused=once('Fetch.requestPaused');await click('#send');request=await paused;
      await evaluate(`timeline.scrollTop=timeline.scrollHeight/2`);const top=await evaluate(`timeline.scrollTop`);
      await command('Fetch.continueRequest',{requestId:request.requestId});await command('Fetch.disable');await wait(`!pending && label.textContent.startsWith('Saved')`);
      assert.ok(Math.abs(await evaluate(`timeline.scrollTop`)-top)<2);assert.equal(await evaluate(`view.querySelectorAll('.message').length`),82);
      await select(fixture.b);assert.doesNotMatch((await state()).text,/Long message/);
      await select(fixture.a);assert.equal(await evaluate(`view.querySelectorAll('.message').length`),40);assert.match((await state()).text,/New pair after long history answer/);
      await reload();assert.match((await state()).text,/New pair after long history answer/);
    }else if(scenario==='menu'){
      await evaluate(`(async()=>{await api('/rename',{id:${JSON.stringify(fixture.a)},title:'Long title '.repeat(10)});await refresh();})()`);
      await select(fixture.a);
      for(const [width,height,mode] of [[1280,800,'dark'],[1280,800,'light'],[390,844,'dark']]){
        await command('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<621});await evaluate(`new Promise(requestAnimationFrame)`);await evaluate(`applyTheme(${JSON.stringify(mode)})`);
        if(width<621)await click('#sidebar-toggle');
        const selector=`.conversation-select[data-id="${fixture.a}"]`;
        await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView()`);
        await evaluate(`new Promise(requestAnimationFrame)`);
        const before=await evaluate(`nav.getBoundingClientRect().height`);
        await evaluate(`document.querySelector(${JSON.stringify(selector)}).nextElementSibling.click()`);
        assert.equal(await evaluate(`menu.matches(':popover-open')`),true);
        assert.deepEqual(await evaluate(`[...menu.querySelectorAll('button')].map(b=>b.textContent)`),['Rename','Delete']);
        const dimensions=await evaluate(`(()=>{const r=menu.getBoundingClientRect(),t=menuTrigger,s=getComputedStyle(t);return {left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:innerWidth,height:innerHeight,dots:t.querySelectorAll('i').length,overflow:s.textOverflow,padding:s.padding,clip:t.scrollWidth>t.clientWidth,position:getComputedStyle(menu).position,nav:nav.getBoundingClientRect().height};})()`);
        assert.equal(dimensions.dots,3);assert.equal(dimensions.overflow,'clip');assert.equal(dimensions.padding,'0px');assert.equal(dimensions.clip,false);assert.equal(dimensions.position,'fixed');assert.equal(dimensions.nav,before);
        assert.ok(dimensions.left>=0&&dimensions.right<=dimensions.width&&dimensions.top>=0&&dimensions.bottom<=dimensions.height,JSON.stringify(dimensions));
        if(fixture.output){const shot=await command('Page.captureScreenshot',{format:'png'});fs.writeFileSync(fixture.output+`/menu-${mode}-${width}.png`,Buffer.from(shot.data,'base64'));assert.equal(await evaluate(`menu.matches(':popover-open')`),true);}
        await evaluate(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))`);assert.equal(await evaluate(`menu.matches(':popover-open')`),false);
        await evaluate(`document.querySelector(${JSON.stringify(selector)}).nextElementSibling.click()`);
        await command('Input.dispatchMouseEvent',{type:'mousePressed',x:width-15,y:height-15,button:'left',clickCount:1});await command('Input.dispatchMouseEvent',{type:'mouseReleased',x:width-15,y:height-15,button:'left',clickCount:1});
        assert.equal(await evaluate(`menu.matches(':popover-open')`),false);
      }
      await evaluate(`renderList([...conversations,...Array.from({length:30},(_,i)=>({id:'layout-'+i,title:'Sidebar layout fixture '+i}))])`);
      for(const width of [1280,390]){
        await command('Emulation.setDeviceMetricsOverride',{width,height:500,deviceScaleFactor:1,mobile:width<621});
        await evaluate(`new Promise(requestAnimationFrame)`);
        await evaluate(`(()=>{document.querySelector('#app').dataset.sidebar='open';const t=document.querySelectorAll('.menu-toggle');t[t.length-1].scrollIntoView({block:'end'});})()`);
        await evaluate(`new Promise(requestAnimationFrame)`);
        await evaluate(`(()=>{const t=document.querySelectorAll('.menu-toggle');t[t.length-1].click();})()`);
        await evaluate(`new Promise(requestAnimationFrame)`);assert.equal(await evaluate(`menu.matches(':popover-open')`),true);
        const fit=await evaluate(`(()=>{const m=menu.getBoundingClientRect(),f=document.querySelector('#delete-all').getBoundingClientRect();return {left:m.left,right:m.right,top:m.top,bottom:m.bottom,footer:f.bottom,width:innerWidth,height:innerHeight};})()`);
        assert.ok(fit.left>=0&&fit.right<=fit.width&&fit.top>=0&&fit.bottom<=fit.height&&fit.footer<=fit.height,JSON.stringify(fit));
        await command('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});await command('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape'});
        assert.equal(await evaluate(`menu.matches(':popover-open')`),false);
      }
      // A second trigger replaces the first; the menu never becomes a row child.
      await evaluate(`nav.scrollTop=0`);await evaluate(`new Promise(requestAnimationFrame)`);
      await evaluate(`document.querySelectorAll('.menu-toggle')[0].click();document.querySelectorAll('.menu-toggle')[1].click()`);
      assert.equal(await evaluate(`document.querySelectorAll('.menu-toggle[aria-expanded="true"]').length`),1);
      await click('#menu-rename');assert.equal(await evaluate(`dialog.open`),true);await click('#manage-cancel');
      await evaluate(`document.querySelectorAll('.menu-toggle')[0].dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown'}))`);
      assert.equal(await evaluate(`document.activeElement.id`),'menu-rename');
      await evaluate(`menu.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown'}))`);assert.equal(await evaluate(`document.activeElement.id`),'menu-delete');await click('#menu-delete');assert.equal(await evaluate(`dialog.open`),true);await click('#manage-cancel');
    }else if(scenario==='bulk'){
      await click('#delete-all');assert.equal(await evaluate(`dialog.open&&document.activeElement.id==='manage-cancel'`),true);
      assert.match(await evaluate(`document.querySelector('#manage-description').textContent`),/all 2 browser Chat/);
      await click('#manage-cancel');assert.equal((await state()).count,2);
      await click('#delete-all');await click('#manage-confirm');await wait(`!dialog.open&&conversations.length===0&&active===null`);
      assert.equal(await evaluate(`fresh.disabled||!document.querySelector('#delete-all').disabled`),false);
      await command('Page.reload');await wait(`label.textContent.includes('Create a new conversation')`);assert.equal((await state()).count,0);
      console.log('RESTART_REQUEST');await new Promise(resolve=>process.stdin.once('data',()=>{process.stdin.pause();resolve();}));
      await go(fixture.base+'/#'+fixture.nextToken);await wait(`label.textContent.includes('Create a new conversation')`);assert.equal((await state()).count,0);
      await click('#new');await wait(`loaded&&conversations.length===1`);assert.equal(await evaluate(`document.querySelector('#delete-all').disabled`),false);
    }else if(scenario==='screenshots'){
      await select(fixture.a);
      for(const [name,width,height,mode] of [['dark-desktop',1280,900,'dark'],['light-desktop',1280,900,'light'],['dark-mobile',390,844,'dark']]){
        await command('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<621});await evaluate(`applyTheme(${JSON.stringify(mode)})`);
        const image=await command('Page.captureScreenshot',{format:'png'});fs.writeFileSync(fixture.output+'/'+name+'.png',Buffer.from(image.data,'base64'));
      }
    }else throw Error('Unknown scenario');
  }
  console.log('PASS browser '+scenario);
}
run().catch(error=>{console.error(error.stack);process.exitCode=1;}).finally(async()=>{try{await command('Browser.close');}catch{}socket?.close();for(const pending of replies.values())clearTimeout(pending.timer);});
