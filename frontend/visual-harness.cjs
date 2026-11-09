// Lightweight AG adapter for existing action contracts. Real rendering/CSP checks use Playwright.
const fs=require('node:fs'),vm=require('node:vm');
module.exports=function install(c,node){
  const priorCreate=c.document.createElement;
  const enhance=n=>Object.assign(n,{isConnected:true,contains(){return false;},replaceWith(){},toggleAttribute(){},childNodes:[],append(child){this.childNodes.push(child);},setAttribute(){}});
  c.document.getElementById=id=>enhance(node('#'+id));
  c.document.createElement=tag=>{
    if(tag==='a')return priorCreate?priorCreate(tag):enhance({click(){}});
    const n=enhance({className:'',textContent:'',title:''});
    if(tag==='template')n.content={querySelector:selector=>enhance(node(selector))};
    return n;
  };
  for(const selector of ['#screen','[data-live-finance]','.agents-screen','.agents-screen.a-live','.wallets-screen','.f-stats','.f-history','.f-reconciliation','.f-chart-grid'])enhance(node(selector));
  c.matchMedia=()=>({matches:true});
  c.__testGrids=[];
  c.agGrid={createGrid(_el,options){c.__testGrids.push(options);return {
    options,destroy(){},setGridOption(key,value){options[key]=value;},
    exportDataAsCsv(){const opts=options.defaultCsvExportParams,quote=v=>'"'+String(v).replace(/"/g,'""')+'"';
      const csv=[opts.columnKeys,...options.rowData.map(r=>opts.columnKeys.map(k=>opts.processCellCallback({value:r[k]})))].map(row=>row.map(quote).join(',')).join('\r\n');
      c.URL.createObjectURL(new c.Blob([csv]));c.document.createElement('a').click();
    }
  };}};
  c.agCharts={AgCharts:{create(){return {update(){},destroy(){}};}}};
  vm.runInContext(fs.readFileSync('site/live-visuals.js','utf8'),c,{filename:'live-visuals.js'});
};
