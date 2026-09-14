
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
    $("#statusBox").textContent=`Feed unavailable: ${err.message}. The first GitHub Action may still be running.`;
  }
  render();
  loadHealth();
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
$("#settingsBtn").style.display="none";

$("#notifyBtn").addEventListener("click",async()=>{
  if(!("Notification" in window)){ alert("Notifications are not supported in this browser."); return; }
  const p=await Notification.requestPermission();
  if(p==="granted") new Notification("TCG Restock Radar",{body:"Device notifications are enabled."});
});

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
