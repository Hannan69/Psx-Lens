const {test}=require('node:test');
const assert=require('node:assert/strict');
const {markers}=require('../static/chart-model.js');
const time='2026-10-20T11:00:00+05:00';
const stock={symbol:'BOP',source:'pyPSX · 5m',score:80,bars:[{time}]};
const alert={symbol:'BOP',source:stock.source,time,previous:'WAIT',score:80};
test('stale or closed data never gets a current entry marker',()=>{
 assert.deepEqual(markers(stock,[],[],false),[]);
 assert.equal(markers(stock,[],[],true)[0].text,'Review entry');
});
test('annotations exclude other stocks, sources and timestamps outside loaded candles',()=>{
 const events=[{...alert,symbol:'HBL'},{...alert,source:'Simulated'},{...alert,time:'2026-10-21T11:00:00+05:00'}];
 assert.deepEqual(markers(stock,events,[],false),[]);
});
test('setup changes are not misrepresented as executed trades',()=>{
 const m=markers(stock,[alert],[],false);assert.equal(m[0].text,'Setup recorded');
 assert.deepEqual(markers(stock,[{...alert,previous:'EXIT WATCH',score:5}],[],false),[]);
});
test('only recorded paper fills produce buy and sell markers',()=>{
 const p={symbol:'BOP',source:stock.source,opened:time,closed:time,entry:28,exit:29};
 const m=markers(stock,[],[p],false);assert.equal(m.length,2);
 assert.equal(m[0].text,'Paper buy 28.00');assert.equal(m[1].text,'Paper sell 29.00');
});
