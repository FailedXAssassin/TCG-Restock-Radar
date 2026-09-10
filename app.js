
const $ = s => document.querySelector(s);
let rows = [];
let deferredPrompt = null;

function markupPct(item){
  if(!item.msrp || item.msrp <= 0) return 999;
  return ((Number(item.price)-Number(item.msrp))/Number(item.msrp))*100;
}
function money(n){
  return new Intl.NumberFormat("en-US",{style:"currency",currency:"USD"}).format(Number(n)||0);
}
function render(){
  const game=$("#gameFilter").value, area=$("#areaFilter").value;
  const maxMarkup=Number($("#markupFilter").value);
  const q=$("#searchInput").value.trim().toLowerCase();

  const filtered=rows.filter(x=>{
    const m=markupPct(x);
    return (game==="all"||x.game===game) &&
           (area==="all"||x.area===area) &&
           m<=maxMarkup &&
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
    node.querySelector(".price").textContent=money(item.price);
    node.querySelector(".msrp").textContent=money(item.msrp);
    const m=markupPct(item), el=node.querySelector(".markup");
    el.textContent=`${m>=0?"+":""}${m.toFixed(0)}%`;
    el.classList.add(m<=0?"good":m<=40?"warn":"bad");
    node.querySelector(".evidence").textContent=item.evidence||"";
    node.querySelector(".buy").href=item.url||"#";
    $("#results").appendChild(node);
  }
  $("#resultCount").textContent=filtered.length;
  $("#msrpCount").textContent=filtered.filter(x=>markupPct(x)<=0).length;
  if(!filtered.length) $("#results").innerHTML=`<div class="status card">No drops match the current filters.</div>`;
}

async function loadFeed(){
  $("#statusBox").textContent="Checking latest feed…";
  try{
    const res=await fetch(`restocks.json?t=${Date.now()}`,{cache:"no-store"});
    if(!res.ok) throw new Error(`HTTP ${res.status}`);
    const data=await res.json();
    rows=Array.isArray(data)?data:(data.items||[]);
    const stamp=data.generated_at ? new Date(data.generated_at).toLocaleString() : new Date().toLocaleTimeString();
    $("#updatedAt").textContent=stamp;
    $("#statusBox").textContent=`Loaded ${rows.length} tracked in-stock result${rows.length===1?"":"s"}. GitHub checker is scheduled every 5 minutes.`;
  }catch(err){
    rows=[];
    $("#updatedAt").textContent="—";
    $("#statusBox").textContent=`Feed unavailable: ${err.message}. The first GitHub Action may still be running.`;
  }
  render();
}

["gameFilter","areaFilter","markupFilter","searchInput"].forEach(id=>$("#"+id).addEventListener("input",render));
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
setInterval(loadFeed, 60000); // Refresh visible app every minute; backend feed updates every ~5 min.
