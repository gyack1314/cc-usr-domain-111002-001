const $=(s,root=document)=>root.querySelector(s);
const $$=(s,root=document)=>[...root.querySelectorAll(s)];
const data=await fetch('/data.json').then(r=>r.json());
const runtime=()=>fetch('/api/runtime').then(r=>r.json());
const action=(name,fields={})=>fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,...fields})}).then(r=>r.json());
const tick=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
const html=(value)=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const show=(s)=>{$('#status').textContent=s;};

let revision=1, changed=false;
const grid=$('#grid');
function rows(){let values=data.rows.filter(r=>$('#category').value==='all'||r.category===$('#category').value);return values.sort((a,b)=>$('#sort').value==='id'?a.id.localeCompare(b.id):a.name.localeCompare(b.name)||a.id.localeCompare(b.id));}
function render(){const items=rows(), start=Math.floor(grid.scrollTop/40);$('#summary').textContent=`共 ${items.length} 行；修订 ${revision}`;grid.dataset.revision=revision;grid.dataset.query=`category=${$('#category').value};sort=${$('#sort').value}`;grid.setAttribute('aria-rowcount',items.length);$('#spacer').style.height=items.length*40+'px';$('#viewport').style.top=start*40+'px';$('#viewport').innerHTML=items.slice(start,start+5).map((r,j)=>`<div class="row" role="row" aria-rowindex="${start+j+1}" data-id="${r.id}"><span>${r.id}</span> <span>${r.name}</span> <span>${r.site}</span> <span>${r.state}</span></div>`).join('');show('可读取当前视口');}
grid.addEventListener('scroll',async()=>{const state=await runtime();if(state.phase==='reorder'&&!changed&&grid.scrollTop>0){changed=true;revision++;data.rows.reverse();data.rows[0].id='DV-NEW';}render();});
for(const select of $$('select'))select.onchange=()=>{grid.scrollTop=0;revision++;render();};
render();
