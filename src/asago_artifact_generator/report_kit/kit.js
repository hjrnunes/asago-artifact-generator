(function(){'use strict';
var root=document.documentElement,THEME_KEY='asago-report-theme';
root.classList.add('js');
function all(sel,scope){return [].slice.call((scope||document).querySelectorAll(sel));}
function parseHash(){var h=decodeURIComponent(location.hash.slice(1)),i=h.indexOf('?');
 return {id:i<0?h:h.slice(0,i),query:i<0?'':h.slice(i+1)};}
function openTo(id){var el=id&&document.getElementById(id);if(!el)return;
 for(var n=el;n;n=n.parentElement){if(n.tagName==='DETAILS')n.open=true;}
 el.scrollIntoView({block:'start'});}
function initTheme(){var tc=document.getElementById('theme');if(!tc)return;tc.hidden=false;
 function paint(){var t=root.getAttribute('data-theme')||'auto';
  all('button',tc).forEach(function(b){b.setAttribute('aria-pressed',String(b.getAttribute('data-theme-set')===t));});}
 all('button',tc).forEach(function(b){b.addEventListener('click',function(){var t=b.getAttribute('data-theme-set');
  if(t==='auto')root.removeAttribute('data-theme');else root.setAttribute('data-theme',t);
  try{localStorage.setItem(THEME_KEY,t);}catch(e){}paint();});});
 paint();}
function initExpand(){all('button[data-expand]').forEach(function(b){b.addEventListener('click',function(){
 var scope=document.getElementById(b.getAttribute('data-scope'))||document,open=b.getAttribute('data-expand')==='open',
  level=b.getAttribute('data-level'),sel=open&&level?'details[data-level="'+level+'"]':'details';
 all(sel,scope).forEach(function(d){d.open=open;});});});
 window.addEventListener('beforeprint',function(){all('details').forEach(function(d){d.open=true;});});}
function initCopy(){all('button.copy[data-copy]').forEach(function(b){b.addEventListener('click',function(){
 if(navigator.clipboard)navigator.clipboard.writeText(b.getAttribute('data-copy'));});});}
function cellValue(row,idx){var c=row.cells[idx];return c?(c.getAttribute('data-v')||c.textContent):'';}
function initSort(table){all('th[data-sort]',table).forEach(function(th){th.addEventListener('click',function(){
 var body=table.tBodies[0],idx=[].indexOf.call(th.parentNode.children,th),dir=th.getAttribute('data-dir')==='asc'?-1:1;
 all('th[data-sort]',table).forEach(function(o){o.removeAttribute('data-dir');});
 th.setAttribute('data-dir',dir===1?'asc':'desc');
 all('tr',body).sort(function(a,b){var x=cellValue(a,idx),y=cellValue(b,idx),nx=parseFloat(x),ny=parseFloat(y);
  if(!isNaN(nx)&&!isNaN(ny)&&String(nx)===x.trim()&&String(ny)===y.trim())return (nx-ny)*dir;
  return x.localeCompare(y)*dir;}).forEach(function(r){body.appendChild(r);});});});}
function initFilters(bar){var table=document.getElementById(bar.getAttribute('data-for'));if(!table)return;
 var state={},search=bar.querySelector('input.search'),shown=bar.querySelector('.shown');
 function apply(write){var q=search?search.value.toLowerCase():'',count=0;
  all('tr',table.tBodies[0]).forEach(function(r){var ok=Object.keys(state).every(function(k){return r.getAttribute('data-f-'+k)===state[k];});
   if(q&&r.textContent.toLowerCase().indexOf(q)<0)ok=false;r.hidden=!ok;if(ok)count++;});
  all('button[data-f]',bar).forEach(function(b){b.setAttribute('aria-pressed',String(state[b.getAttribute('data-f')]===b.getAttribute('data-v')));});
  if(shown)shown.textContent=count+' shown';
  if(write){var parts=Object.keys(state).sort().map(function(k){return k+'='+state[k];});if(q)parts.push('q='+q);
   history.replaceState(null,'','#'+table.id+(parts.length?'?'+parts.join('&'):''));}}
 all('button[data-f]',bar).forEach(function(b){b.addEventListener('click',function(){var k=b.getAttribute('data-f'),v=b.getAttribute('data-v');
  if(state[k]===v)delete state[k];else state[k]=v;apply(true);});});
 if(search)search.addEventListener('input',function(){apply(true);});
 function fromHash(){var h=parseHash();if(h.id!==table.id)return;state={};
  h.query.split('&').forEach(function(p){var i=p.indexOf('=');if(i<1)return;var k=p.slice(0,i),v=p.slice(i+1);
   if(k==='q'&&search)search.value=v;else if(k!=='q')state[k]=v;});apply(false);}
 window.addEventListener('hashchange',fromHash);fromHash();}
document.addEventListener('DOMContentLoaded',function(){
 initTheme();initExpand();initCopy();
 all('table[data-sortable]').forEach(initSort);
 all('.filters[data-for]').forEach(initFilters);
 openTo(parseHash().id);
 window.addEventListener('hashchange',function(){openTo(parseHash().id);});});
})();
