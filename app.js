
const $ = s => document.querySelector(s);
let rows = [];
let deferredPrompt = null;
const API_BASE = location.hostname.endsWith("github.io")
  ? "https://tcg-restock-radar-production.up.railway.app/"
  : "";

const statusLabels = {
  in_stock:"🟢 Retail In Stock", marketplace_in_stock:"🟠 Marketplace In Stock",
  loaded:"🟡 Loaded / Not Released", sold_out:"🔴 Sold Out", unknown:"⚪ Unknown",
  blocked:"⚫ Blocked", error:"⚫ Error", not_found:"⚫ Not Found"
};

function markupPct(item){
  if(item.price==null || !item.msrp || item.msrp <= 0) return null;
  return ((Number(item.price)-Number(item.msrp))/Number(item.msrp))*100;
}
function money(n){
  if(n==null) return "—";
  return new Intl.NumberFormat("en-US",{style:"currency",currency:"USD"}).format(Number(n)||0);
}
function render(){
  const game=$("#gameFilter").value, area=$("#areaFilter").value;
  const retailer=$("#retailerFilter").value, status=$("#statusFilter").value;
  const maxMarkup=Number($("#markupFilter").value);
  const q=$("#searchInput").value.trim().toLowerCase();

  const filtered=rows.filter(x=>{
    const m=markupPct(x);
    return (game==="all"||x.game===game) &&
           (area==="all"||x.area===area) &&
           (retailer==="all"||x.store===retailer) &&
           (status==="all"||x.status===status) &&
           (m==null||m<=maxMarkup) &&
           (!q||`${x.product} ${x.store} ${x.game} ${x.area}`.toLowerCase().includes(q));
  });

  $("#results").innerHTML="";
  const tpl=$("#itemTemplate");
  for(const item of filtered){
    const node=tpl.content.cloneNode(true);
    node.querySelector(".game").textContent=item.game;
    node.querySelector(".area").textContent=item.area;
    node.querySelector(".product").textContent=item.product;
    node.querySelector(".store").textContent=item.store;
    node.querySelector(".product-status").textContent=statusLabels[item.status]||item.status||"Unknown";
    node.querySelector(".product-status").classList.add(item.status||"unknown");
    node.querySelector(".seller").textContent=item.seller?`Seller: ${item.seller}`:"";
    node.querySelector(".price").textContent=money(item.price);
    node.querySelector(".msrp").textContent=money(item.msrp);
    const m=markupPct(item), el=node.querySelector(".markup");
    el.textContent=m==null?"—":`${m>=0?"+":""}${m.toFixed(0)}%`;
    if(m!=null) el.classList.add(m<=0?"good":m<=40?"warn":"bad");
    node.querySelector(".evidence").textContent=item.evidence||"";
    node.querySelector(".checked").textContent=item.checked_at?`Last checked ${new Date(item.checked_at).toLocaleString()}`:"";
    node.querySelector(".buy").href=item.url||"#";
    $("#results").appendChild(node);
  }
  $("#resultCount").textContent=filtered.length;
  $("#msrpCount").textContent=filtered.filter(x=>markupPct(x)!=null&&markupPct(x)<=0).length;
  if(!filtered.length) $("#results").innerHTML=`<div class="status card">No drops match the current filters.</div>`;
}

async function loadFeed(){
  $("#statusBox").textContent="Checking latest feed…";
  try{
    const res=await fetch(`${API_BASE}api/feed?t=${Date.now()}`,{cache:"no-store"});
    if(!res.ok) throw new Error(`HTTP ${res.status}`);
    const data=await res.json();
    rows=Array.isArray(data)?data:(data.items||[]);
    const retailers=[...new Set(rows.map(x=>x.store).filter(Boolean))].sort();
    const current=$("#retailerFilter").value;
    $("#retailerFilter").innerHTML='<option value="all">All retailers</option>'+retailers.map(x=>`<option>${x}</option>`).join("");
    if(retailers.includes(current)) $("#retailerFilter").value=current;
    const stamp=data.generated_at ? new Date(data.generated_at).toLocaleString() : new Date().toLocaleTimeString();
    $("#updatedAt").textContent=stamp;
    $("#statusBox").textContent=`Live Railway feed: ${rows.length} tracked product${rows.length===1?"":"s"}. High-priority checks are staggered around 30–60 seconds.`;
  }catch(err){
    rows=[];
    $("#updatedAt").textContent="—";
    $("#statusBox").textContent=`Feed unavailable: ${err.message}. Please try refreshing shortly.`;
  }
  render();
  loadHealth();
  loadPush();
}

async function loadHealth(){
  try{
    const res=await fetch(`${API_BASE}api/retailer-health?t=${Date.now()}`,{cache:"no-store"});
    if(!res.ok) throw new Error(`HTTP ${res.status}`);
    const data=await res.json();
    $("#healthGrid").innerHTML=(data.retailers||[]).map(x=>`<article class="card health-card"><strong>${x.retailer}</strong><span class="health-state ${x.status}">${(x.status||"unknown").replaceAll("_"," ")}</span><small>${x.monitored_product_count??0} product(s) • ${x.latency_ms??"—"} ms</small><small>${x.consecutive_failures||0} consecutive failures • ${Number(x.backoff_multiplier||1).toFixed(1)}× pacing</small></article>`).join("")||'<div class="status card">Health data will appear after the first checks.</div>';
  }catch(err){$("#healthGrid").innerHTML=`<div class="status card">Retailer health unavailable: ${err.message}</div>`}
}

["gameFilter","areaFilter","retailerFilter","statusFilter","markupFilter","searchInput"].forEach(id=>$("#"+id).addEventListener("input",render));
$("#refreshBtn").addEventListener("click",loadFeed);
const ADMIN_KEY="tcg-radar-owner-secret";
function api(path){ return `${API_BASE}${path.replace(/^\//,"")}`; }
async function ownerFetch(path, options={}){
  let secret=sessionStorage.getItem(ADMIN_KEY);
  if(!secret){ secret=prompt("Enter your TCG Radar owner secret"); if(!secret) throw new Error("Owner secret is required"); sessionStorage.setItem(ADMIN_KEY,secret); }
  const response=await fetch(api(path),{...options,headers:{Authorization:`Bearer ${secret}`,"Content-Type":"application/json",...(options.headers||{})}});
  if(response.status===401){ sessionStorage.removeItem(ADMIN_KEY); throw new Error("That owner secret was not accepted"); }
  if(!response.ok){ const data=await response.json().catch(()=>({})); throw new Error(data.detail||`HTTP ${response.status}`); }
  return response.status===204?null:response.json();
}
async function showOwner(){
  $("#ownerDialog").showModal(); $("#ownerMessage").textContent="Loading monitored products…";
  try{const data=await ownerFetch("/api/admin/products"); renderOwnerProducts(data.items||[]); $("#ownerMessage").textContent="Add a public retailer URL. New products start as unknown until their first safe check.";}
  catch(error){$("#ownerMessage").textContent=error.message;}
}
function renderOwnerProducts(items){
  $("#ownerProducts").innerHTML="";
  for(const item of items){ const el=document.createElement("div"); el.className="owner-product"; el.innerHTML=`<span><strong></strong><small></small></span><button type="button">Remove</button>`; el.querySelector("strong").textContent=item.product; el.querySelector("small").textContent=`${item.store} • ${item.priority} priority`; el.querySelector("button").onclick=async()=>{if(!confirm(`Remove ${item.product}?`))return; try{await ownerFetch(`/api/admin/products/${item.id}`,{method:"DELETE"}); showOwner(); loadFeed();}catch(error){$("#ownerMessage").textContent=error.message;}}; $("#ownerProducts").appendChild(el); }
}
$("#ownerBtn").addEventListener("click",showOwner);
$("#closeOwnerBtn").addEventListener("click",()=>$("#ownerDialog").close());
$("#productForm").addEventListener("submit",async event=>{event.preventDefault(); $("#ownerMessage").textContent="Saving product…"; try{await ownerFetch("/api/admin/products",{method:"POST",body:JSON.stringify({product:$("#ownerProduct").value,url:$("#ownerUrl").value,game:$("#ownerGame").value,msrp:$("#ownerMsrp").value||null,priority:$("#ownerPriority").value,area:"Online"})}); event.target.reset(); await showOwner(); loadFeed();}catch(error){$("#ownerMessage").textContent=error.message;}});

function base64UrlToBytes(value){ const padded=value.replace(/-/g,"+").replace(/_/g,"/")+"=".repeat((4-value.length%4)%4); const raw=atob(padded); return Uint8Array.from(raw,c=>c.charCodeAt(0)); }
async function loadPush(){
  const button=$("#notifyBtn");
  try{ const config=await fetch(api("/api/push/config"),{cache:"no-store"}).then(r=>r.json());
    if(!config.enabled){ button.textContent="Push alerts: setup pending"; button.disabled=true; return; }
    button.textContent=Notification.permission==="granted"?"Push alerts enabled":"Enable Push Alerts"; button.disabled=false;
    button.onclick=async()=>{ try{ if(!("serviceWorker" in navigator)||!("PushManager" in window)) throw new Error("This browser does not support push alerts"); const permission=await Notification.requestPermission(); if(permission!=="granted") throw new Error("Notification permission was not granted"); const registration=await navigator.serviceWorker.ready; const existing=await registration.pushManager.getSubscription(); const subscription=existing||await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:base64UrlToBytes(config.public_key)}); const response=await fetch(api("/api/push/subscribe"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(subscription)}); if(!response.ok) throw new Error("Could not save this device for alerts"); button.textContent="Push alerts enabled"; }catch(error){ alert(error.message); } };
  }catch(error){ button.textContent="Push alerts unavailable"; button.disabled=true; }
}


window.addEventListener("beforeinstallprompt",e=>{
  e.preventDefault(); deferredPrompt=e; $("#installBtn").hidden=false;
});
$("#installBtn").addEventListener("click",async()=>{
  if(!deferredPrompt)return;
  deferredPrompt.prompt(); await deferredPrompt.userChoice;
  deferredPrompt=null; $("#installBtn").hidden=true;
});
if("serviceWorker" in navigator) navigator.serviceWorker.register("service-worker.js");
loadFeed();
setInterval(loadFeed, 30000);
