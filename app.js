
const $ = s => document.querySelector(s);
let rows = [];
let deferredPrompt = null;
let viewMode = "online";
const visitorId=localStorage.getItem("tcg-radar-visitor")||crypto.randomUUID(); localStorage.setItem("tcg-radar-visitor",visitorId);
const API_BASE = location.hostname.endsWith("github.io")
  ? "https://tcg-restock-radar-production.up.railway.app/"
  : "";

const statusLabels = {
  in_stock:"🟢 Confirmed retail stock", marketplace_in_stock:"🟠 Third-party seller",
  loaded:"🟡 Listing live — stock unconfirmed", invitation:"🟣 Invite / access required",
  sold_out:"🔴 Sold out", unknown:"⚪ Availability unknown",
  blocked:"⚫ Retailer blocked the check", error:"⚫ Check needs retry", not_found:"⚫ Listing not found"
};

function markupPct(item){
  if(item.price==null || !item.msrp || item.msrp <= 0) return null;
  return ((Number(item.price)-Number(item.msrp))/Number(item.msrp))*100;
}
function money(n){
  if(n==null) return "—";
  return new Intl.NumberFormat("en-US",{style:"currency",currency:"USD"}).format(Number(n)||0);
}
function retailerHref(item){
  const packages={"Amazon":"com.amazon.mShop.android.shopping","Walmart":"com.walmart.android","Target":"com.target.ui","Best Buy":"com.bestbuy.android"};
  if(!/Android/i.test(navigator.userAgent)||!packages[item.store]||!item.url) return item.url||"#";
  try{ const parsed=new URL(item.url); return `intent://${parsed.host}${parsed.pathname}${parsed.search}#Intent;scheme=https;package=${packages[item.store]};S.browser_fallback_url=${encodeURIComponent(item.url)};end`; }catch(_){ return item.url||"#"; }
}
function render(){
  const game=$("#gameFilter").value, area=$("#areaFilter").value;
  const retailer=$("#retailerFilter").value, status=$("#statusFilter").value;
  const maxMarkup=Number($("#markupFilter").value);
  const minQuantity=Number($("#quantityFilter").value);
  const q=$("#searchInput").value.trim().toLowerCase();

  const filtered=rows.filter(x=>{
    const m=markupPct(x);
    return (viewMode==="online"||x.area==="Local") &&
           (game==="all"||x.game===game) &&
           (area==="all"||x.area===area) &&
           (retailer==="all"||x.store===retailer) &&
           (status==="all"||x.status===status) &&
           (m==null||m<=maxMarkup) &&
           (minQuantity===0||Number(x.quantity||0)>=minQuantity) &&
           (!q||`${x.product} ${x.store} ${x.game} ${x.area}`.toLowerCase().includes(q));
  });

  $("#results").innerHTML="";
  const tpl=$("#itemTemplate");
  for(const item of filtered){
    const node=tpl.content.cloneNode(true);
    const image=node.querySelector(".product-image");
    if(item.image_url){ image.src=item.image_url; image.alt=item.product||"Product image"; image.onerror=()=>{image.hidden=true;}; } else image.hidden=true;
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
    node.querySelector(".checked").textContent=item.notification_at?`🔔 Last notification ${new Date(item.notification_at).toLocaleString()} • checked ${item.checked_at?new Date(item.checked_at).toLocaleTimeString():"—"}`:item.checked_at?`Last checked ${new Date(item.checked_at).toLocaleString()}`:"";
    const buy=node.querySelector(".buy"); buy.href=retailerHref(item); buy.textContent=/Android/i.test(navigator.userAgent)&&["Amazon","Walmart","Target","Best Buy"].includes(item.store)?`Open in ${item.store} app`:`Open ${item.store} listing`;
    node.querySelector(".report").onclick=async()=>{ const reason=prompt("What is wrong? Enter false alert, wrong price, broken link, or other.","false alert"); if(!reason)return; const normalized=reason.trim().toLowerCase().replace(/\s+/g,"_"); const allowed={"false_alert":"false_alert","wrong_price":"wrong_price","broken_link":"broken_link","other":"other"}; try{const response=await fetch(api("/api/reports"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({product_id:item.id,reason:allowed[normalized]||"other"})}); if(!response.ok)throw new Error("Report could not be saved"); alert("Thanks—your report was saved for review.");}catch(error){alert(error.message);} };
    $("#results").appendChild(node);
  }
  $("#resultCount").textContent=filtered.length;
  $("#msrpCount").textContent=filtered.filter(x=>markupPct(x)!=null&&markupPct(x)<=0).length;
  const confirmed=rows.filter(x=>x.status==="in_stock");
  $("#confirmedCount").textContent=confirmed.length;
  $("#activeCount").textContent=rows.length;
  const newestConfirmed=confirmed.sort((a,b)=>new Date(b.notification_at||b.checked_at||0)-new Date(a.notification_at||a.checked_at||0))[0];
  $("#latestVerified").textContent=newestConfirmed ? `${newestConfirmed.store}: ${newestConfirmed.product}` : "No confirmed retail stock";
  if(!filtered.length) $("#results").innerHTML=`<div class="status card">${viewMode==="local"?"No nearby products with confirmed local inventory are available yet.":"No drops match the current filters."}</div>`;
}

function setMode(mode){
  viewMode=mode;
  const online=mode==="online";
  $("#onlineModeBtn").classList.toggle("secondary",!online); $("#localModeBtn").classList.toggle("secondary",online);
  $("#onlineModeBtn").setAttribute("aria-selected",String(online)); $("#localModeBtn").setAttribute("aria-selected",String(!online));
  $("#localPanel").hidden=online;
  $("#statusBox").textContent=online?"":"Choose a radius and allow location access to begin a nearby search.";
  render();
}

$("#onlineModeBtn").addEventListener("click",()=>setMode("online"));
$("#localModeBtn").addEventListener("click",()=>setMode("local"));
$("#useLocationBtn").addEventListener("click",()=>{
  const status=$("#locationStatus");
  if(!navigator.geolocation){status.textContent="This browser does not support location services.";return;}
  status.textContent="Requesting your location…";
  navigator.geolocation.getCurrentPosition(position=>{
    localStorage.setItem("tcg-radar-location",JSON.stringify({latitude:position.coords.latitude,longitude:position.coords.longitude,radius:Number($("#radiusFilter").value),saved_at:new Date().toISOString()}));
    status.textContent=`Location ready. Searching within ${$("#radiusFilter").value} miles.`;
    render();
  },error=>{status.textContent=error.code===1?"Location permission was denied. You can enable it in Chrome site settings.":"We couldn’t determine your location. Try again.";},{enableHighAccuracy:false,maximumAge:300000,timeout:10000});
});

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
  loadAlerts();
  loadPush();
  fetch(api("/api/visitors/heartbeat"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({client_id:visitorId})}).catch(()=>{});
}

async function loadHealth(){
  try{
    const res=await fetch(`${API_BASE}api/retailer-health?t=${Date.now()}`,{cache:"no-store"});
    if(!res.ok) throw new Error(`HTTP ${res.status}`);
    const data=await res.json();
    $("#healthGrid").innerHTML=(data.retailers||[]).map(x=>`<article class="card health-card"><strong>${x.retailer}</strong><span class="health-state ${x.status}">${(x.status||"unknown").replaceAll("_"," ")}</span><small>${x.monitored_product_count??0} product(s) • ${x.latency_ms??"—"} ms</small><small>${x.consecutive_failures||0} consecutive failures • ${Number(x.backoff_multiplier||1).toFixed(1)}× pacing</small></article>`).join("")||'<div class="status card">Health data will appear after the first checks.</div>';
  }catch(err){$("#healthGrid").innerHTML=`<div class="status card">Retailer health unavailable: ${err.message}</div>`}
}

async function loadAlerts(){
  const list=$("#alertHistory");
  try{
    const res=await fetch(api("/api/alerts?t="+Date.now()),{cache:"no-store"});
    if(!res.ok) throw new Error(`HTTP ${res.status}`);
    const items=(await res.json()).items||[];
    list.innerHTML="";
    if(!items.length){list.innerHTML="<small>No notifications recorded yet.</small>";return;}
    for(const item of items.slice(0,20)){
      const row=document.createElement("a"); row.className="alert-row"; row.href=item.url||"#"; row.target="_blank"; row.rel="noopener";
      const title=document.createElement("strong"); title.textContent=item.product||"Product update";
      const detail=document.createElement("span"); detail.textContent=`${item.store||"Retailer"} • ${statusLabels[item.status]||item.status||"Updated"}`;
      const time=document.createElement("time"); time.dateTime=item.created_at||""; time.textContent=item.created_at?new Date(item.created_at).toLocaleString():"";
      row.append(title,detail,time); list.appendChild(row);
    }
  }catch(_){list.innerHTML="<small>Notification history is temporarily unavailable.</small>";}
}

const HELP_THREAD_KEY="tcg-radar-help-thread";
const HELP_CLIENT_KEY="tcg-radar-help-client";
const helpThread=localStorage.getItem(HELP_THREAD_KEY)||crypto.randomUUID();
const helpClient=localStorage.getItem(HELP_CLIENT_KEY)||crypto.randomUUID();
localStorage.setItem(HELP_THREAD_KEY,helpThread); localStorage.setItem(HELP_CLIENT_KEY,helpClient);
async function loadHelpThread(){
  const list=$("#helpMessages");
  try{const data=await fetch(api(`/api/help/messages?thread_id=${encodeURIComponent(helpThread)}`),{cache:"no-store"}).then(r=>r.json()); list.innerHTML=""; if(!(data.items||[]).length){list.innerHTML="<small>No messages yet.</small>";return;} for(const item of data.items){const bubble=document.createElement("div"); bubble.className=`help-bubble ${item.sender}`; const body=document.createElement("p"); body.textContent=item.body; const time=document.createElement("small"); time.textContent=`${item.sender==="owner"?"TCG Radar":"You"} • ${new Date(item.created_at).toLocaleString()}`; bubble.append(body,time); list.appendChild(bubble);}}
  catch(_){list.innerHTML="<small>Chat is temporarily unavailable.</small>";}
}
$("#helpBtn").addEventListener("click",()=>{$("#helpDialog").showModal();loadHelpThread();});
$("#closeHelpBtn").addEventListener("click",()=>$("#helpDialog").close());
$("#helpForm").addEventListener("submit",async event=>{event.preventDefault(); const body=$("#helpBody").value.trim(); if(!body)return; $("#helpMessage").textContent="Sending…"; try{await fetch(api("/api/help/messages"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({thread_id:helpThread,client_id:helpClient,body})}).then(async response=>{if(!response.ok)throw new Error((await response.json().catch(()=>({}))).detail||"Message could not be sent");}); $("#helpBody").value=""; $("#helpMessage").textContent="Sent. I’ll reply when I can."; await loadHelpThread();}catch(error){$("#helpMessage").textContent=error.message;}});

let googleToken=sessionStorage.getItem("tcg-radar-google-token")||"", accountUser=null;
function accountHeaders(){return googleToken?{Authorization:`Bearer ${googleToken}`}:{};}
async function renderPurchases(){const list=$("#purchaseList"); list.innerHTML=""; if(!googleToken){$("#purchaseForm").hidden=true;$("#signOutBtn").hidden=true;$("#accountStatus").textContent="Sign in with Google to save purchases across devices.";return;} try{const response=await fetch(api("/api/purchases"),{headers:accountHeaders(),cache:"no-store"}); if(!response.ok)throw new Error("Your Google session expired. Please sign in again."); const items=(await response.json()).items||[]; $("#purchaseForm").hidden=false;$("#signOutBtn").hidden=false;$("#accountStatus").textContent=accountUser?`Signed in as ${accountUser.email}`:"Signed in with Google";$("#purchaseTotal").textContent=new Intl.NumberFormat("en-US",{style:"currency",currency:"USD"}).format(items.reduce((sum,item)=>sum+Number(item.paid||0),0)); if(!items.length){list.innerHTML="<small>No purchases logged yet.</small>";return;} for(const item of items){const row=document.createElement("div"); row.className="purchase-row"; const info=document.createElement("span"); const title=document.createElement("strong"); title.textContent=`${item.name} × ${item.quantity}`; const detail=document.createElement("small"); detail.textContent=`${item.retailer||"Retailer not listed"} • ${item.date||"Date not listed"} • ${money(item.paid)}`; info.append(title,detail); const remove=document.createElement("button"); remove.className="remove"; remove.type="button"; remove.textContent="Remove"; remove.onclick=async()=>{await fetch(api(`/api/purchases/${encodeURIComponent(item.id)}`),{method:"DELETE",headers:accountHeaders()});renderPurchases();}; row.append(info,remove); list.appendChild(row);}}catch(error){googleToken="";sessionStorage.removeItem("tcg-radar-google-token");$("#accountStatus").textContent=error.message;$("#purchaseForm").hidden=true;}}
async function configureGoogleSignIn(){try{const config=await fetch(api("/api/auth/config"),{cache:"no-store"}).then(r=>r.json()); if(!config.enabled){$("#accountStatus").textContent="Google sign-in is being configured.";return;} if(googleToken){accountUser=await fetch(api("/api/auth/me"),{headers:accountHeaders()}).then(r=>r.ok?r.json():null);if(accountUser){renderPurchases();return;}googleToken="";sessionStorage.removeItem("tcg-radar-google-token");} if(!window.google?.accounts?.id){setTimeout(configureGoogleSignIn,500);return;} window.google.accounts.id.initialize({client_id:config.client_id,callback:credential=>{googleToken=credential.credential;sessionStorage.setItem("tcg-radar-google-token",googleToken);fetch(api("/api/auth/me"),{headers:accountHeaders()}).then(r=>r.json()).then(user=>{accountUser=user;renderPurchases();});}});$("#googleSignIn").innerHTML="";window.google.accounts.id.renderButton($("#googleSignIn"),{theme:"filled_black",size:"large",shape:"pill",text:"signin_with"});}catch(_){$("#accountStatus").textContent="Google sign-in is temporarily unavailable.";}}
$("#purchasesBtn").addEventListener("click",()=>{$("#purchasesDialog").showModal();$("#purchaseDate").value=new Date().toISOString().slice(0,10);configureGoogleSignIn();renderPurchases();});
$("#closePurchasesBtn").addEventListener("click",()=>$("#purchasesDialog").close());
$("#signOutBtn").addEventListener("click",()=>{googleToken="";accountUser=null;sessionStorage.removeItem("tcg-radar-google-token");$("#googleSignIn").innerHTML="";configureGoogleSignIn();renderPurchases();});
$("#purchaseForm").addEventListener("submit",async event=>{event.preventDefault();const item={name:$("#purchaseName").value.trim(),quantity:Math.max(1,Number($("#purchaseQty").value||1)),paid:Number($("#purchasePaid").value||0),retailer:$("#purchaseRetailer").value.trim(),date:$("#purchaseDate").value,notes:$("#purchaseNotes").value.trim()};const response=await fetch(api("/api/purchases"),{method:"POST",headers:{"Content-Type":"application/json",...accountHeaders()},body:JSON.stringify(item)});if(!response.ok){$("#accountStatus").textContent=(await response.json().catch(()=>({}))).detail||"Purchase could not be saved";return;}event.target.reset();$("#purchaseQty").value="1";renderPurchases();});

["gameFilter","areaFilter","retailerFilter","statusFilter","markupFilter","quantityFilter","searchInput"].forEach(id=>$("#"+id).addEventListener("input",render));
$("#refreshBtn").addEventListener("click",loadFeed);
const ADMIN_KEY="tcg-radar-manager-secret";
const MANAGER_MODE_KEY="tcg-radar-manager-mode";
let managerRole="";
let helpPollTimer=null, lastHelpMessageId="";
function updateManagerButton(){ $("#ownerBtn").hidden=!(location.search.includes("manager=1")||sessionStorage.getItem(ADMIN_KEY)||localStorage.getItem(MANAGER_MODE_KEY)); }
updateManagerButton();
function api(path){ return `${API_BASE}${path.replace(/^\//,"")}`; }
async function ownerFetch(path, options={}){
  const secret=sessionStorage.getItem(ADMIN_KEY);
  if(!secret) throw new Error("A manager code is required");
  const response=await fetch(api(path),{...options,headers:{Authorization:`Bearer ${secret}`,"Content-Type":"application/json",...(options.headers||{})}});
  if(response.status===401){ sessionStorage.removeItem(ADMIN_KEY); throw new Error("That owner or moderator code was not accepted"); }
  if(!response.ok){ const data=await response.json().catch(()=>({})); throw new Error(data.detail||`HTTP ${response.status}`); }
  return response.status===204?null:response.json();
}
async function loadManagerControls(){
  $("#productForm").hidden=true; $("#ownerOnlyControls").hidden=true; $("#adminOverview").hidden=true;
  try{const data=await ownerFetch("/api/admin/products"); managerRole=data.role||""; localStorage.setItem(MANAGER_MODE_KEY,"1"); updateManagerButton(); $("#managerLogin").hidden=true; $("#productForm").hidden=false; $("#ownerOnlyControls").hidden=managerRole!=="owner"; renderOwnerProducts(data.items||[], managerRole==="owner"); $("#adminOverview").hidden=false; $("#adminRole").textContent=managerRole==="owner"?"Owner session":"Moderator session"; $("#adminProductCount").textContent=(data.items||[]).length; $("#adminAccess").textContent=managerRole==="owner"?"Full command access":"Product links only"; if(managerRole==="owner"){await loadModerators(); await loadHelpInbox(); clearInterval(helpPollTimer); helpPollTimer=setInterval(loadHelpInbox,20000);} $("#ownerMessage").textContent=managerRole==="owner"?"Owner access: messages and moderator controls are available.":"Moderator access: you can add and remove public product URLs.";}
  catch(error){sessionStorage.removeItem(ADMIN_KEY); $("#managerLogin").hidden=false; $("#adminOverview").hidden=true; $("#ownerMessage").textContent=error.message;}
}
function showOwner(){
  $("#ownerDialog").showModal(); $("#managerLogin").hidden=false; $("#productForm").hidden=true; $("#ownerOnlyControls").hidden=true; $("#managerCode").value=""; $("#ownerMessage").textContent="Enter a manager code to unlock these controls.";
  if(sessionStorage.getItem(ADMIN_KEY)) loadManagerControls();
}
$("#cancelManager").addEventListener("click",()=>{ $("#ownerDialog").close(); });
$("#unlockManager").addEventListener("click",async()=>{const code=$("#managerCode").value.trim(); if(!code){$("#ownerMessage").textContent="Enter a manager code first."; return;} sessionStorage.setItem(ADMIN_KEY,code); $("#ownerMessage").textContent="Checking code…"; await loadManagerControls();});
$("#lockManagerBtn").addEventListener("click",()=>{clearInterval(helpPollTimer); sessionStorage.removeItem(ADMIN_KEY); localStorage.removeItem(MANAGER_MODE_KEY); managerRole=""; updateManagerButton(); $("#managerLogin").hidden=false; $("#productForm").hidden=true; $("#ownerOnlyControls").hidden=true; $("#managerCode").value=""; $("#ownerMessage").textContent="Manager controls locked.";});
async function loadHelpInbox(){
  try{const items=(await ownerFetch("/api/admin/help")).items||[]; const list=$("#ownerHelp"); list.innerHTML=""; if(!items.length){list.innerHTML="<small>No help messages yet.</small>";return;} const threads=new Map(); for(const item of items){if(!threads.has(item.thread_id))threads.set(item.thread_id,item);} for(const item of threads.values()){const row=document.createElement("div"); row.className="owner-help-row"; const message=document.createElement("small"); message.textContent=`${item.body} • ${new Date(item.created_at).toLocaleString()}`; const reply=document.createElement("textarea"); reply.rows=2; reply.maxLength=1000; reply.placeholder="Reply to this user…"; const actions=document.createElement("div"); actions.className="help-owner-actions"; const button=document.createElement("button"); button.type="button"; button.textContent="Reply"; button.onclick=async()=>{if(!reply.value.trim())return; await ownerFetch(`/api/admin/help/${encodeURIComponent(item.thread_id)}/reply`,{method:"POST",body:JSON.stringify({body:reply.value})}); reply.value=""; await loadHelpInbox();}; const ban=document.createElement("button"); ban.type="button"; ban.className="secondary danger-button"; ban.textContent="Ban device"; ban.onclick=async()=>{if(!confirm("Block this device from sending more help messages?"))return; await ownerFetch(`/api/admin/help/${encodeURIComponent(item.client_id)}/ban`,{method:"POST"}); row.remove();}; actions.append(button,ban); row.append(message,reply,actions); list.appendChild(row); if(lastHelpMessageId&&item.id!==lastHelpMessageId&&item.sender==="user"&&Notification.permission==="granted")new Notification("TCG Radar help request",{body:item.body}); lastHelpMessageId=item.id;}}
  catch(error){$("#ownerMessage").textContent=error.message;}
}
$("#loadHelpBtn").addEventListener("click",loadHelpInbox);
$("#loadUsageBtn").addEventListener("click",async()=>{const d=await ownerFetch("/api/admin/usage");$("#ownerUsage").textContent=`Anonymous devices: ${d.anonymous_devices} • Google accounts: ${d.google_accounts} • Push devices: ${d.push_devices} • Help users: ${d.help_devices}`;});
function renderOwnerProducts(items,isOwner){
  $("#ownerProducts").innerHTML="";
  for(const item of items){
    const el=document.createElement("div"); el.className="owner-product";
    el.innerHTML=`<span><strong></strong><small></small></span><div class="product-actions">${isOwner?'<button type="button" class="toggle"></button>':''}<button type="button" class="remove">Remove</button></div>`;
    el.querySelector("strong").textContent=item.product;
    el.querySelector("small").textContent=`${item.store} • ${item.priority} priority • alert at ≤ ${item.max_markup ?? 80}% markup`;
    const toggle=el.querySelector(".toggle");
    if(toggle){ toggle.textContent=item.enabled===false?"Resume":"Pause"; toggle.classList.toggle("secondary",true); toggle.onclick=async()=>{try{await ownerFetch(`/api/admin/products/${item.id}`,{method:"PATCH",body:JSON.stringify({enabled:item.enabled===false})}); await showOwner(); loadFeed();}catch(error){$("#ownerMessage").textContent=error.message;}}; }
    el.querySelector(".remove").onclick=async()=>{if(!confirm(`Remove ${item.product}?`))return; try{await ownerFetch(`/api/admin/products/${item.id}`,{method:"DELETE"}); await showOwner(); loadFeed();}catch(error){$("#ownerMessage").textContent=error.message;}};
    $("#ownerProducts").appendChild(el);
  }
}
$("#ownerBtn").addEventListener("click",showOwner);
$("#closeOwnerBtn").addEventListener("click",()=>$("#ownerDialog").close());
$("#testPushBtn").addEventListener("click",async()=>{ $("#ownerMessage").textContent="Test scheduled—close TCG Radar completely now."; try{const result=await ownerFetch("/api/admin/push/test",{method:"POST"}); $("#ownerMessage").textContent=result.attempted?"Test scheduled for 10 seconds. Close TCG Radar completely now.":"No phones are subscribed yet—tap Enable Push Alerts on the main screen first.";}catch(error){$("#ownerMessage").textContent=error.message;} });
$("#announcementForm").addEventListener("submit",async event=>{event.preventDefault(); try{const result=await ownerFetch("/api/admin/announcements",{method:"POST",body:JSON.stringify({title:$("#announcementTitle").value,body:$("#announcementBody").value,url:location.href})}); $("#announcementBody").value=""; $("#ownerMessage").textContent=result.attempted?`Message sent to ${result.attempted} subscribed phone(s).`:"No phones are subscribed yet.";}catch(error){$("#ownerMessage").textContent=error.message;}});
async function loadReports(){try{const data=await ownerFetch("/api/admin/reports"); const list=$("#ownerReports"); list.innerHTML=""; if(!(data.items||[]).length){list.innerHTML="<small>No user reports yet.</small>";return;} for(const report of data.items){const row=document.createElement("div"); row.className="owner-product"; row.innerHTML=`<span><strong></strong><small></small></span>`; row.querySelector("strong").textContent=report.reason.replaceAll("_"," "); row.querySelector("small").textContent=`Product ${report.product_id} • ${new Date(report.created_at).toLocaleString()}`; list.appendChild(row);}}catch(error){$("#ownerMessage").textContent=error.message;}}
$("#loadReportsBtn").addEventListener("click",loadReports);
async function loadModerators(){try{const data=await ownerFetch("/api/admin/moderators"); const list=$("#moderatorList"); list.innerHTML=""; for(const moderator of data.items||[]){const row=document.createElement("div"); row.className="owner-product"; row.innerHTML=`<span><strong></strong><small>Can add and remove tracked URLs only</small></span><button type="button" class="remove">Remove</button>`; row.querySelector("strong").textContent=moderator.name; row.querySelector("button").onclick=async()=>{if(!confirm(`Remove ${moderator.name}'s moderator access?`))return; try{await ownerFetch(`/api/admin/moderators/${moderator.id}`,{method:"DELETE"}); await loadModerators();}catch(error){$("#ownerMessage").textContent=error.message;}}; list.appendChild(row);}}catch(error){$("#ownerMessage").textContent=error.message;}}
$("#moderatorForm").addEventListener("submit",async event=>{event.preventDefault(); try{const result=await ownerFetch("/api/admin/moderators",{method:"POST",body:JSON.stringify({name:$("#moderatorName").value})}); $("#moderatorName").value=""; await loadModerators(); prompt(`Copy this one-time moderator code for ${result.name}. Send it privately; it will not be shown again. They can use the manager link ending in ?manager=1.`,result.access_code);}catch(error){$("#ownerMessage").textContent=error.message;}});
$("#productForm").addEventListener("submit",async event=>{event.preventDefault(); $("#ownerMessage").textContent="Saving product…"; try{await ownerFetch("/api/admin/products",{method:"POST",body:JSON.stringify({product:$("#ownerProduct").value,url:$("#ownerUrl").value,game:$("#ownerGame").value,msrp:$("#ownerMsrp").value||null,priority:$("#ownerPriority").value,max_markup:$("#ownerMaxMarkup").value||80,area:"Online"})}); event.target.reset(); await showOwner(); loadFeed();}catch(error){$("#ownerMessage").textContent=error.message;}});

const PERSONAL_MARKUP_KEY="tcg-radar-personal-markup";
function personalMarkup(){ return Number(localStorage.getItem(PERSONAL_MARKUP_KEY)||80); }
function base64UrlToBytes(value){ const padded=value.replace(/-/g,"+").replace(/_/g,"/")+"=".repeat((4-value.length%4)%4); const raw=atob(padded); return Uint8Array.from(raw,c=>c.charCodeAt(0)); }
async function savePushSubscription(subscription){
  const response=await fetch(api("/api/push/subscribe"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({subscription,preferences:{max_markup:personalMarkup()}})});
  if(!response.ok){ const data=await response.json().catch(()=>({})); throw new Error(data.detail||"Could not save this device for alerts"); }
}
async function enablePush(){
  const config=await fetch(api("/api/push/config"),{cache:"no-store"}).then(r=>r.json());
  if(!config.enabled) throw new Error("Push alerts are still being set up");
  if(!("serviceWorker" in navigator)||!("PushManager" in window)) throw new Error("This browser does not support push alerts");
  const permission=await Notification.requestPermission();
  if(permission!=="granted") throw new Error("Notification permission was not granted");
  const registration=await navigator.serviceWorker.ready;
  const existing=await registration.pushManager.getSubscription();
  const subscription=existing||await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:base64UrlToBytes(config.public_key)});
  await savePushSubscription(subscription);
  return subscription;
}
async function loadPush(){
  const button=$("#notifyBtn");
  try{ const config=await fetch(api("/api/push/config"),{cache:"no-store"}).then(r=>r.json());
    if(!config.enabled){ button.textContent="Push alerts: setup pending"; button.disabled=true; return; }
    button.textContent=Notification.permission==="granted"?"Push alerts enabled":"Enable Push Alerts"; button.disabled=false;
    button.onclick=async()=>{ try{ await enablePush(); button.textContent="Push alerts enabled"; }catch(error){ alert(error.message); } };
  }catch(error){ button.textContent="Push alerts unavailable"; button.disabled=true; }
}
$("#alertSettingsBtn").addEventListener("click",()=>{ $("#personalMarkup").value=String(personalMarkup()); $("#alertSettingsMessage").textContent=""; $("#alertSettingsDialog").showModal(); });
$("#alertSettingsForm").addEventListener("submit",async event=>{ event.preventDefault(); localStorage.setItem(PERSONAL_MARKUP_KEY,$("#personalMarkup").value); try{ const registration=await navigator.serviceWorker.ready; const subscription=await registration.pushManager.getSubscription(); if(subscription) await savePushSubscription(subscription); $("#alertSettingsMessage").textContent="Saved for this phone."; setTimeout(()=>$("#alertSettingsDialog").close(),500); }catch(error){ $("#alertSettingsMessage").textContent=`Saved here. ${error.message}`; } });

const WELCOME_GUIDE_KEY="tcg-radar-welcome-guide-dismissed";
if(localStorage.getItem(WELCOME_GUIDE_KEY)) $("#welcomeGuide").hidden=true;
$("#dismissGuide").addEventListener("click",()=>{localStorage.setItem(WELCOME_GUIDE_KEY,"1");$("#welcomeGuide").hidden=true;});

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

