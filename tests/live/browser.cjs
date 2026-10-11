const {chromium}=require('playwright');
const fs=require('fs');
const url=process.env.COMFY_URL;
if(!url)throw Error('Set COMFY_URL to an isolated acceptance gateway');
const evidence=process.env.EVIDENCE_DIR||'local/browser-evidence';
fs.mkdirSync(evidence,{recursive:true});
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 let prompt;
 page.on('response',async r=>{if(r.url().endsWith('/prompt')&&r.request().method()==='POST'){try {prompt=(await r.json()).prompt_id;console.log('prompt',prompt)}catch{}}});
 try{
 await page.goto(url,{waitUntil:'networkidle'});
 await page.keyboard.press('Escape');
 await page.evaluate(async()=>{
  const {app}=await import('/scripts/app.js');const {api}=await import('/scripts/api.js');
  window.probeEvents=[];for(const name of ['progress','b_preview','b_preview_with_metadata','executed','execution_success','execution_error'])api.addEventListener(name,e=>window.probeEvents.push({name,data:name.startsWith('b_preview')?{size:e.detail?.blob?.size||e.detail?.size,metadata:e.detail?.metadata}:e.detail}));
  app.graph.clear();
  const a=LiteGraph.createNode('EmptyImage'),b=LiteGraph.createNode('CueRelayProbe'),c=LiteGraph.createNode('SaveImage');
  for(const n of [a,b,c])app.graph.add(n);
  a.pos=[50,150];b.pos=[420,150];c.pos=[760,150];
  for(const w of a.widgets||[])if(w.name==='width'||w.name==='height')w.value=32;
  a.connect(0,b,0);b.connect(0,c,0);app.canvas.setDirty(true,true);
  await app.queuePrompt(0,1);
 });
 await page.waitForFunction(()=>window.probeEvents.some(x=>x.name==='progress')&&window.probeEvents.some(x=>x.name.startsWith('b_preview')),{},{timeout:90000});
 await page.waitForFunction(async()=>{const {app}=await import('/scripts/app.js');return app.graph._nodes.some(n=>n.type==='CueRelayProbe'&&n.imgs?.some(i=>i.complete&&i.naturalWidth===32))},{},{timeout:10000});
 await page.screenshot({path:evidence+'/browser-progress.png'});
 await page.waitForFunction(()=>window.probeEvents.some(x=>x.name==='execution_success'),{},{timeout:90000});
 await page.waitForTimeout(1000);
 const result=await page.evaluate(async()=>{const {app}=await import('/scripts/app.js');return {events:window.probeEvents,outputs:app.nodeOutputs,images:app.graph._nodes.map(n=>({id:n.id,type:n.type,images:n.imgs?.map(i=>({complete:i.complete,width:i.naturalWidth,height:i.naturalHeight}))}))}});
 result.prompt_id=prompt; result.preview_decoded=true;
 fs.writeFileSync(evidence+'/browser-progress.json',JSON.stringify(result,null,2)+'\n');
 if(!result.images.some(n=>n.type==='SaveImage'&&n.images?.some(i=>i.complete&&i.width===32)))throw Error('No decoded final image on SaveImage node');
 await page.screenshot({path:evidence+'/browser-output.png'});
 console.log(JSON.stringify({prompt,events:result.events.length,images:result.images}));
 }finally{
 if(prompt){
  let h=await page.request.get(url+'/history/'+prompt);
  if(!(await h.json())[prompt]){
   await page.request.post(url+'/interrupt',{data:{prompt_id:prompt}});
   const deadline=Date.now()+60000;let stopped=false;
   while(Date.now()<deadline){h=await page.request.get(url+'/history/'+prompt);if((await h.json())[prompt]){stopped=true;break}await new Promise(r=>setTimeout(r,1000))}
   if(!stopped)console.error('Cancellation unconfirmed; inspect comfy-'+prompt);
  }
 }
 await browser.close();
 }
})().catch(e=>{console.error(e);process.exit(1)});
