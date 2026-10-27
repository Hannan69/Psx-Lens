/* Pure chart annotations: recorded events only, never hindsight-generated trades. */
(function(root){
 const epoch=t=>Math.floor(Date.parse(t)/1000);
 function markers(stock,alerts,positions,actionable){
  const times=new Set(stock.bars.map(b=>epoch(b.time))), result=[];
  for(const a of alerts||[]){
   const t=epoch(a.time);
   if(a.symbol!==stock.symbol||a.source!==stock.source||!times.has(t))continue;
   if(a.score>=65&&!/BUY|STRONG/.test(a.previous))result.push({time:t,position:'belowBar',color:'#60a5fa',shape:'arrowUp',text:'Setup recorded'});
   else if(a.score<45&&(/BUY|STRONG/.test(a.previous)||a.previous==='WAIT'))result.push({time:t,position:'aboveBar',color:'#fbbf24',shape:'arrowDown',text:'Setup weakened'});
  }
  for(const p of positions||[]){
   if(p.symbol!==stock.symbol||p.source!==stock.source)continue;
   if(times.has(epoch(p.opened)))result.push({time:epoch(p.opened),position:'belowBar',color:'#34d399',shape:'arrowUp',text:'Paper buy '+Number(p.entry).toFixed(2)});
   if(p.closed&&times.has(epoch(p.closed)))result.push({time:epoch(p.closed),position:'aboveBar',color:'#fb7185',shape:'arrowDown',text:'Paper sell '+Number(p.exit).toFixed(2)});
  }
  // A current marker is a review prompt, never a fabricated execution.
  if(actionable&&stock.score>=65&&stock.bars.length)result.push({time:epoch(stock.bars.at(-1).time),position:'belowBar',color:'#34d399',shape:'arrowUp',text:'Review entry'});
  result.sort((a,b)=>a.time-b.time);
  // Keep arrows, but label only the latest of each setup type to avoid overlapping text.
  const labeled=new Set();
  for(let i=result.length-1;i>=0;i--){const m=result[i];if(m.text.startsWith('Setup')){if(labeled.has(m.text))m.text='';else labeled.add(m.text);}}
  return result;
 }
 const api={epoch,markers};root.PSXChartModel=api;
 if(typeof module!=='undefined')module.exports=api;
})(typeof window==='undefined'?globalThis:window);
