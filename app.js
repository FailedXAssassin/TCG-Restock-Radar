
const $ = s => document.querySelector(s);
let rows = [];
const WATCHLIST_KEY="tcg-radar-watchlist";
const RETAILER_SELECTION_KEY="tcg-radar-retailer-selection";
const SORT_MODE_KEY="tcg-radar-sort-mode";
const ALERT_VISIBLE_MS=30*60*1000;
const FRESH_DROP_MS=5*60*1000;
const OLDER_ALERT_BATCH_SIZE=5;
let showEarlierAlerts=false;
let olderAlertsToShow=OLDER_ALERT_BATCH_SIZE;
let watchlistOnly=false;
const FILTER_IDS=["gameFilter","retailerFilter","statusFilter","markupFilter","quantityFilter","searchInput"];
let appliedFilters={};
function readFilters(){return Object.fromEntries(FILTER_IDS.map(id=>[id,$("#"+id).value]));}
function setFilters(values){FILTER_IDS.forEach(id=>{$("#"+id).value=values[id]??"";});}
function watchlist(){try{return new Set(JSON.parse(localStorage.getItem(WATCHLIST_KEY)||"[]"));}catch(_){return new Set();}}
function saveWatchlist(items){localStorage.setItem(WATCHLIST_KEY,JSON.stringify([...items]));}
function retailerSelections(){try{const value=JSON.parse(localStorage.getItem(RETAILER_SELECTION_KEY)||"{}");return value&&typeof value==="object"?value:{};}catch(_){return {};}}
let deferredPrompt = null;
let viewMode = "online";
let activePage = "radar";
let healthAllowed = false;
let canManageFeed = false;
let ownerProductItems = [];
let ownerCanEditProducts = false;
let reportProductId = "";
let initialFeedRendered = false;
const SEEN_ALERTS_KEY = "tcg-radar-seen-alerts";
function createVisitorId(){
  if(globalThis.crypto&&typeof globalThis.crypto.randomUUID==="function") return globalThis.crypto.randomUUID();
  return `visitor-${Date.now()}-${Math.random().toString(36).slice(2,12)}`;
}
const visitorId=localStorage.getItem("tcg-radar-visitor")||createVisitorId();
localStorage.setItem("tcg-radar-visitor",visitorId);
const API_BASE = location.hostname.endsWith("github.io")
  ? "https://tcg-restock-radar-production.up.railway.app/"
  : "";

const productTypeLabels={
  elite_trainer_box:"ETB",booster_bundle:"Booster bundle",booster_box:"Booster box",
  booster_pack:"Booster pack",sleeved_booster:"Sleeved booster",two_pack:"2-pack",
  three_pack_blister:"3-pack blister",checklane_blister:"Checklane blister",tin:"Tin",
  collection:"Collection",poster_collection:"Poster collection",ex_box:"ex Box",
  knockout_collection:"Knock Out collection",super_premium_collection:"Super premium",
  tech_sticker_collection:"Tech sticker collection",figure_collection:"Figure collection",
  premium_collection:"Premium collection",ultra_premium_collection:"Ultra-premium",
  build_and_battle:"Build & Battle",deck:"Deck with packs",other_pack_product:"Pack product"
};

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
function timeAgo(value){
  const seconds=Math.max(0,Math.floor((Date.now()-new Date(value).getTime())/1000));
  if(seconds<45) return "just now";
  const minutes=Math.floor(seconds/60);
  if(minutes<60) return `${minutes} min${minutes===1?"":"s"}`;
  const hours=Math.floor(minutes/60);
  if(hours<24) return `${hours} hour${hours===1?"":"s"}`;
  const days=Math.floor(hours/24);
  return `${days} day${days===1?"":"s"}`;
}
function formatDropTime(value){
  const date=new Date(value);
  if(Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-US",{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}).format(date);
}
function isTodayLocal(value){
  const date=new Date(value);
  if(Number.isNaN(date.getTime())) return false;
  const now=new Date();
  return date.getFullYear()===now.getFullYear()&&date.getMonth()===now.getMonth()&&date.getDate()===now.getDate();
}
function isFreshDrop(value){
  const timestamp=new Date(value||0).getTime();
  return Boolean(timestamp)&&Date.now()-timestamp>=0&&Date.now()-timestamp<=FRESH_DROP_MS;
}
function isVisibleLiveAlert(value){
  const timestamp=new Date(value||0).getTime();
  return Boolean(timestamp)&&Date.now()-timestamp>=0&&Date.now()-timestamp<=ALERT_VISIBLE_MS;
}
function retailerHref(item){
  const packages={"Amazon":"com.amazon.mShop.android.shopping","Walmart":"com.walmart.android","Target":"com.target.ui","Best Buy":"com.bestbuy.android"};
  if(!/Android/i.test(navigator.userAgent)||!packages[item.store]||!item.url) return item.url||"#";
  try{ const parsed=new URL(item.url); return `intent://${parsed.host}${parsed.pathname}${parsed.search}#Intent;scheme=https;package=${packages[item.store]};S.browser_fallback_url=${encodeURIComponent(item.url)};end`; }catch(_){ return item.url||"#"; }
}
function recordRetailerLinkClick(item){
  if(!item?.store||!item?.url) return;
  fetch(api("/api/analytics/link-click"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({retailer:item.store}),keepalive:true}).catch(()=>{});
}
function render(){
  const game=appliedFilters.gameFilter??$("#gameFilter").value;
  const retailer=appliedFilters.retailerFilter??$("#retailerFilter").value, status=appliedFilters.statusFilter??$("#statusFilter").value;
  const maxMarkup=Number(appliedFilters.markupFilter??$("#markupFilter").value);
  const minQuantity=Number(appliedFilters.quantityFilter??$("#quantityFilter").value);
  const q=(appliedFilters.searchInput??$("#searchInput").value).trim().toLowerCase();

  const filtered=rows.filter(x=>{
    const m=markupPct(x);
    return (!watchlistOnly||watchlist().has(x.id)) &&
           (viewMode==="online"||x.area==="Local") &&
           (game==="all"||x.game===game) &&
(retailer==="all"||x.store===retailer) &&
           (status==="all"||x.status===status) &&
           (m==null||x.status==="marketplace_in_stock"||m<=maxMarkup) &&
           (minQuantity===0||Number(x.quantity||0)>=minQuantity) &&
           (!q||`${x.product} ${x.store} ${x.game} ${x.area}`.toLowerCase().includes(q));
  });

  const groups=new Map();
  for(const item of filtered){
    const key=item.catalog_key||`unique:${item.id}`;
    if(!groups.has(key))groups.set(key,[]);
    groups.get(key).push(item);
  }
  const selections=retailerSelections();
  const offerPriority=offer=>{
    if(offer.status==="in_stock"&&offer.official_seller_verified)return 0;
    if(offer.official_seller_verified)return 1;
    if(offer.status==="marketplace_in_stock")return 3;
    return 2;
  };
  const displayItems=[...groups.entries()].map(([key,offers])=>{
    const orderedOffers=[...offers].sort((left,right)=>offerPriority(left)-offerPriority(right));
    const directLive=orderedOffers.find(offer=>offer.status==="in_stock"&&offer.official_seller_verified);
    const selected=directLive||orderedOffers.find(offer=>offer.id===selections[key])||orderedOffers[0];
    return {...selected,retailer_offers:orderedOffers,catalog_key:key};
  });
  const sortMode=localStorage.getItem(SORT_MODE_KEY)||"newest";
  displayItems.sort((left,right)=>{
    if(sortMode==="price-asc"){
      const a=Number.isFinite(Number(left.price))?Number(left.price):Infinity;
      const b=Number.isFinite(Number(right.price))?Number(right.price):Infinity;
      return a-b;
    }
    if(sortMode==="price-desc"){
      const a=Number.isFinite(Number(left.price))?Number(left.price):-Infinity;
      const b=Number.isFinite(Number(right.price))?Number(right.price):-Infinity;
      return b-a;
    }
    const a=new Date(left.notification_at||left.created_at||left.first_seen_at||0).getTime()||0;
    const b=new Date(right.notification_at||right.created_at||right.first_seen_at||0).getTime()||0;
    return b-a;
  });
  const sortControl=$("#sortMode");
  if(sortControl&&sortControl.value!==sortMode) sortControl.value=sortMode;
  const todayCount=$("#todayDropCount");
  if(todayCount) todayCount.textContent=String(rows.filter(item=>isTodayLocal(item.notification_at)).length);
  const resultContainer=viewMode==="local"?$("#localResults"):$("#results");
  resultContainer.innerHTML="";
  const tpl=$("#itemTemplate");
  for(const item of displayItems){
    const node=tpl.content.cloneNode(true);
    const image=node.querySelector(".product-image");
    if(item.image_url){ image.src=item.image_url; image.alt=item.product||"Product image"; image.onerror=()=>{image.hidden=true;}; } else image.hidden=true;
    node.querySelector(".game").textContent=item.game;
    const setBadge=node.querySelector(".set");
    setBadge.textContent=item.set_name||"Set not labeled";
    setBadge.hidden=!item.set_name;
    const typeBadge=node.querySelector(".product-type");
    typeBadge.textContent=productTypeLabels[item.product_type]||"Pack product";
    node.querySelector(".product").textContent=item.product;
    node.querySelector(".store").textContent=item.store;
    node.querySelector(".product-status").textContent=statusLabels[item.status]||item.status||"Unknown";
    node.querySelector(".product-status").classList.add(item.status||"unknown");
    node.querySelector(".seller").textContent=item.status==="marketplace_in_stock"?`3rd party: ${item.seller||"Marketplace seller"}`:(item.seller?`Seller: ${item.seller}`:"");
    node.querySelector(".price").textContent=money(item.price);
    node.querySelector(".msrp").textContent=money(item.msrp);
    const m=markupPct(item), el=node.querySelector(".markup");
    el.textContent=m==null?"—":`${m>=0?"+":""}${m.toFixed(0)}%`;
    if(m!=null) el.classList.add(m<=0?"good":m<=40?"warn":"bad");
    node.querySelector(".evidence").textContent=item.evidence||"";
    const addedAt=item.notification_at||item.created_at||item.added_at;
    const isFresh=isFreshDrop(item.notification_at);
    if(isFresh) node.querySelector(".drop").classList.add("fresh-drop");
    node.querySelector(".checked").textContent=item.notification_at?`Dropped ${formatDropTime(item.notification_at)}`:(addedAt?`Added ${formatDropTime(addedAt)}`:"");
    const estimate=node.querySelector(".stock-estimate");
    if(item.stock_estimate){
      const reported=item.stock_estimate_reported_at?` • added ${formatDropTime(item.stock_estimate_reported_at)}`:"";
      estimate.textContent=`Unverified stock estimate: ${item.stock_estimate}${reported}`;
      estimate.hidden=false;
    }
    const buy=node.querySelector(".buy"); buy.href=retailerHref(item); buy.textContent=/Android/i.test(navigator.userAgent)&&["Amazon","Walmart","Target","Best Buy"].includes(item.store)?`Open in ${item.store} app`:`Open ${item.store} listing`;
    buy.addEventListener("click",()=>recordRetailerLinkClick(item));
    if((item.retailer_offers||[]).length>1){
      const selector=document.createElement("select"); selector.className="retailer-offer-select"; selector.setAttribute("aria-label","Choose retailer listing");
      for(const offer of item.retailer_offers){
        const option=document.createElement("option"); option.value=offer.id; option.selected=offer.id===item.id;
        const offerKind=offer.status==="marketplace_in_stock"?"3rd party":(offer.official_seller_verified?"Retailer-direct":(statusLabels[offer.status]||offer.status||"Unknown"));
        option.textContent=`${offer.store} • ${money(offer.price)} • ${offerKind}`;
        selector.appendChild(option);
      }
      selector.onchange=()=>{const saved=retailerSelections();saved[item.catalog_key]=selector.value;localStorage.setItem(RETAILER_SELECTION_KEY,JSON.stringify(saved));render();};
      buy.parentElement.insertBefore(selector,buy);
    }
    const watch=node.querySelector(".watch"), watched=watchlist().has(item.id);
    watch.textContent=watched?"★":"☆"; watch.classList.toggle("watched",watched); watch.setAttribute("aria-label",watched?"Remove from watchlist":"Add to watchlist");
    watch.onclick=()=>{const items=watchlist();items.has(item.id)?items.delete(item.id):items.add(item.id);saveWatchlist(items);updateWatchlistButton();render();};
    node.querySelector(".report").onclick=()=>{reportProductId=item.id;$("#reportMessage").textContent="";$("#reportDialog").showModal();};
    const alertKey=item.notification_at?`${item.id}|${item.notification_at}`:"";
    const seen=new Set(JSON.parse(localStorage.getItem(SEEN_ALERTS_KEY)||"[]"));
    if(initialFeedRendered&&alertKey&&!seen.has(alertKey)) node.querySelector(".drop").classList.add("is-new");
    if(alertKey) seen.add(alertKey);
    localStorage.setItem(SEEN_ALERTS_KEY,JSON.stringify([...seen].slice(-200)));
    resultContainer.appendChild(node);
  }
  initialFeedRendered=true;
  $("#resultCount").textContent=displayItems.length;
  $("#msrpCount").textContent=filtered.filter(x=>markupPct(x)!=null&&markupPct(x)<=0).length;
  const confirmed=rows.filter(x=>x.status==="in_stock");
  $("#confirmedCount").textContent=confirmed.length;
  $("#activeCount").textContent=rows.length;
  const newestConfirmed=confirmed.sort((a,b)=>new Date(b.notification_at||b.checked_at||0)-new Date(a.notification_at||a.checked_at||0))[0];
  $("#latestVerified").textContent=newestConfirmed ? `${newestConfirmed.store}: ${newestConfirmed.product}` : "No confirmed retail stock";
  if(!filtered.length) resultContainer.innerHTML=`<div class="status card">${viewMode==="local"?"No confirmed nearby stock yet. We keep it unknown instead of guessing.":"Nothing matches these filters yet. Try a higher markup limit, more retailers, or another game."}</div>`;
}

function setMode(mode){
  viewMode=mode;
  const online=mode==="online";
  $("#onlineModeBtn").classList.toggle("secondary",!online); $("#localModeBtn").classList.toggle("secondary",online);
  $("#onlineModeBtn").setAttribute("aria-selected",String(online)); $("#localModeBtn").setAttribute("aria-selected",String(!online));
  $("#localPanel").hidden=online || activePage!=="nearby";
  $("#statusBox").textContent=online?"":"Choose a radius and allow location access to begin a nearby search.";
  render();
}

$("#onlineModeBtn").addEventListener("click",()=>setMode("online"));
$("#localModeBtn").addEventListener("click",()=>setMode("local"));
let localSearch=null;
let localScanSession="";
let localCooldownUntil=0;
let localCooldownTimer=null;
function localDirections(store){
  return `https://www.google.com/maps/dir/?api=1&destination=${encodeURIComponent(`${store.latitude},${store.longitude}`)}`;
}
function renderLocalScan(data){
  const list=$("#localResults"); list.innerHTML="";
  const stores=data.stores||[];
  if(!stores.length){list.innerHTML='<div class="status card">No supported stores were found inside this radius. Try 20 or 50 miles.</div>';return;}
  for(const store of stores){
    const card=document.createElement("article"); card.className="drop card";
    const main=document.createElement("div"); main.className="drop-main";
    const tags=document.createElement("div"); tags.className="drop-tags";
    const retailer=document.createElement("span"); retailer.className="badge game"; retailer.textContent=store.retailer;
    const local=document.createElement("span"); local.className="badge set"; local.textContent="Local";
    tags.append(retailer,local);
    const title=document.createElement("h3"); title.textContent=store.name||store.retailer;
    const address=document.createElement("p"); address.className="store"; address.textContent=`${store.distance_miles} mi away${store.address?` • ${store.address}`:""}`;
    const checked=document.createElement("p"); checked.className="checked"; checked.textContent=store.inventory_status==="in_stock"?"🟢 Verified local inventory":`⚪ ${store.inventory_note||"Store inventory unavailable"}`;
    main.append(tags,title,address,checked);
    const side=document.createElement("div"); side.className="drop-side";
    const actions=document.createElement("div"); actions.className="card-actions";
    const directions=document.createElement("a"); directions.className="buy"; directions.target="_blank"; directions.rel="noopener"; directions.href=localDirections(store); directions.textContent="Directions";
    actions.append(directions); side.append(actions); card.append(main,side);
    for(const item of store.items||[]){
      const detail=document.createElement("p"); detail.className="evidence";
      detail.textContent=`${item.product||"Product"} • ${item.status||"Unknown"}${item.price!=null?` • ${money(item.price)}`:""}`;
      card.append(detail);
    }
    list.append(card);
  }
}
function localScanHeaders(){
  const token=sessionStorage.getItem(ADMIN_KEY);
  return {"Content-Type":"application/json",...(token?{Authorization:`Bearer ${token}`}:{})};
}
function localCooldownActive(){return Date.now()<localCooldownUntil;}
function startLocalCooldown(){
  localCooldownUntil=Date.now()+30000;
  const button=$("#searchZipBtn");
  clearInterval(localCooldownTimer);
  const update=()=>{
    const seconds=Math.max(0,Math.ceil((localCooldownUntil-Date.now())/1000));
    button.disabled=seconds>0;
    button.textContent=seconds>0?`Refresh in ${seconds}s`:"Scan this ZIP";
    if(!seconds)clearInterval(localCooldownTimer);
  };
  update();
  localCooldownTimer=setInterval(update,250);
}

async function runLocalScan(){
  const status=$("#locationStatus");
  const zip=localSearch?.zip_code;
  if(!zip)return;
  if(localCooldownActive()){ $("#locationStatus").textContent=`Refresh available in ${Math.ceil((localCooldownUntil-Date.now())/1000)} seconds.`; return; }
  startLocalCooldown();
  const radius=Number($("#radiusFilter").value);
  status.textContent=`Finding supported stores within ${radius} miles of ZIP ${zip} and checking local inventory…`;
  $("#localResults").innerHTML='<div class="status card">Scanning nearby supported stores…</div>';
  try{
    const response=await fetch(api("/api/local/scan"),{method:"POST",headers:localScanHeaders(),body:JSON.stringify({zip_code:zip,radius_miles:radius,client_id:visitorId,scan_session:localScanSession})});
    const data=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(data.detail||`HTTP ${response.status}`);
    localScanSession=data.scan_session||localScanSession;
    const suffix=data.manager_bypass?"Manager ZIP searches are unlimited.":data.zip_searches_remaining==null?"":` ${data.zip_searches_remaining} ZIP search${data.zip_searches_remaining===1?"":"es"} left in this three-hour window.`;
    status.textContent=`${data.stores?.length||0} supported store${data.stores?.length===1?"":"s"} found within ${radius} miles.${suffix}`;
    renderLocalScan(data);
  }catch(error){
    status.textContent=`Nearby search unavailable: ${error.message}`;
    $("#localResults").innerHTML='<div class="status card">We could not complete a nearby store scan. Nothing was marked in stock.</div>';
  }
}
$("#searchZipBtn").addEventListener("click",()=>{
  const zip=$("#zipFilter").value.trim();
  if(!/^\d{5}$/.test(zip)){$("#locationStatus").textContent="Enter a five-digit ZIP code.";return;}
  if(localSearch?.zip_code!==zip)localScanSession="";
  localSearch={zip_code:zip};
  runLocalScan();
});
$("#zipFilter").addEventListener("keydown",event=>{if(event.key==="Enter"){event.preventDefault();$("#searchZipBtn").click();}});
$("#radiusFilter").addEventListener("change",()=>{if(!localSearch)return;if(localCooldownActive()){$("#locationStatus").textContent=`Radius updated. Refresh available in \${Math.ceil((localCooldownUntil-Date.now())/1000)} seconds.`;return;}runLocalScan();});

async function loadFeed(){
  $("#statusBox").textContent="Checking latest feed…";
  try{
    const managerToken=sessionStorage.getItem("tcg-radar-manager-secret");
    const res=await fetch(`${API_BASE}api/feed?t=${Date.now()}`,{cache:"no-store",headers:managerToken?{Authorization:`Bearer ${managerToken}`}:{}});
    if(!res.ok) throw new Error(`HTTP ${res.status}`);
    const data=await res.json();
    rows=Array.isArray(data)?data:(data.items||[]);
    canManageFeed=Boolean(data.can_manage_feed);
    $("#activeTrackerSnapshot").hidden=!canManageFeed;
    const retailers=[...new Set(rows.map(x=>x.store).filter(Boolean))].sort();
    const current=$("#retailerFilter").value;
    $("#retailerFilter").innerHTML='<option value="all">All retailers</option>'+retailers.map(x=>`<option>${x}</option>`).join("");
    if(retailers.includes(current)) $("#retailerFilter").value=current;
    const stamp=data.generated_at ? new Date(data.generated_at).toLocaleString() : new Date().toLocaleTimeString();
    $("#updatedAt").textContent=stamp;
    $("#statusBox").textContent=canManageFeed
      ? `Live Railway feed: ${rows.length} tracked product${rows.length===1?"":"s"}. High-priority checks are staggered around 30–60 seconds.`
      : `${rows.length} live drop${rows.length===1?"":"s"} in the last 30 minutes.`;
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
  if(!healthAllowed) return;
  const managerToken=sessionStorage.getItem("tcg-radar-manager-secret");
  if(!managerToken) return;
  try{
    const res=await fetch(`${API_BASE}api/retailer-health?t=${Date.now()}`,{cache:"no-store",headers:{Authorization:`Bearer ${managerToken}`}});
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
    const items=((await res.json()).items||[]).sort((a,b)=>new Date(b.created_at||0)-new Date(a.created_at||0));
    const recent=items.filter(item=>isVisibleLiveAlert(item.created_at));
    const earlierToday=items.filter(item=>isTodayLocal(item.created_at)&&!isVisibleLiveAlert(item.created_at));
    const olderVisible=showEarlierAlerts?earlierToday.slice(0,olderAlertsToShow):[];
    const visible=[...recent,...olderVisible];
    list.innerHTML="";
    if(!visible.length){
      list.innerHTML=`<small>${earlierToday.length?"No drops in the last 30 minutes.":"No notifications recorded yet."}</small>`;
    }else{
      for(const item of visible){
        const row=document.createElement("a"); row.className=`alert-row ${isFreshDrop(item.created_at)?"fresh-alert":""}`; row.href=item.url||"#"; row.target="_blank"; row.rel="noopener";
        const title=document.createElement("strong"); title.textContent=item.product||"Product update";
        const detail=document.createElement("span"); const source=item.source==="owner_confirmed"?"🔵 Owner-confirmed direct drop":(item.source==="authorized_external"?"🟣 Authorized signal: "+(item.signal_label||"approved source"):(statusLabels[item.status]||item.status||"Updated")); detail.textContent=`${item.store||"Retailer"} • ${source}`;
        const time=document.createElement("time"); time.dateTime=item.created_at||""; time.textContent=item.created_at?`Dropped ${formatDropTime(item.created_at)}`:"";
        row.append(title,detail,time); list.appendChild(row);
      }
    }
    if(earlierToday.length&&!showEarlierAlerts){
      const reveal=document.createElement("button"); reveal.type="button"; reveal.className="alert-history-toggle secondary";
      reveal.textContent=`View older alerts today (${earlierToday.length})`;
      reveal.onclick=()=>{showEarlierAlerts=true;olderAlertsToShow=OLDER_ALERT_BATCH_SIZE;loadAlerts();};
      list.appendChild(reveal);
    }else if(showEarlierAlerts){
      const summary=document.createElement("small"); summary.className="older-alert-summary";
      summary.textContent=`Showing ${olderVisible.length} of ${earlierToday.length} earlier alerts from today.`;
      list.appendChild(summary);
      if(olderVisible.length<earlierToday.length){
        const loadMore=document.createElement("button"); loadMore.type="button"; loadMore.className="alert-history-toggle secondary";
        loadMore.textContent="Load 5 older alerts";
        loadMore.onclick=()=>{olderAlertsToShow+=OLDER_ALERT_BATCH_SIZE;loadAlerts();};
        list.appendChild(loadMore);
      }
    }
  }catch(_){list.innerHTML="<small>Notification history is temporarily unavailable.</small>";}
}
const HELP_THREAD_KEY="tcg-radar-help-thread";
const HELP_CLIENT_KEY="tcg-radar-help-client";
const helpThread=localStorage.getItem(HELP_THREAD_KEY)||createVisitorId();
const helpClient=localStorage.getItem(HELP_CLIENT_KEY)||createVisitorId();
localStorage.setItem(HELP_THREAD_KEY,helpThread); localStorage.setItem(HELP_CLIENT_KEY,helpClient);
async function loadHelpThread(){
  const list=$("#helpMessages");
  try{const data=await fetch(api(`/api/help/messages?thread_id=${encodeURIComponent(helpThread)}`),{cache:"no-store"}).then(r=>r.json()); list.innerHTML=""; if(!(data.items||[]).length){list.innerHTML="<small>No messages yet.</small>";return;} for(const item of data.items){const bubble=document.createElement("div"); bubble.className=`help-bubble ${item.sender}`; const body=document.createElement("p"); body.textContent=item.body; const time=document.createElement("small"); time.textContent=`${item.sender==="owner"?"TCG Radar":"You"} • ${new Date(item.created_at).toLocaleString()}`; bubble.append(body,time); list.appendChild(bubble);}}
  catch(_){list.innerHTML="<small>Chat is temporarily unavailable.</small>";}
}
$("#helpBtn").addEventListener("click",()=>{closeSettings();$("#helpDialog").showModal();loadHelpThread();});
$("#closeHelpBtn").addEventListener("click",()=>$("#helpDialog").close());
$("#helpForm").addEventListener("submit",async event=>{event.preventDefault(); const body=$("#helpBody").value.trim(); if(!body)return; $("#helpMessage").textContent="Sending…"; try{await fetch(api("/api/help/messages"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({thread_id:helpThread,client_id:helpClient,body})}).then(async response=>{if(!response.ok)throw new Error((await response.json().catch(()=>({}))).detail||"Message could not be sent");}); $("#helpBody").value=""; $("#helpMessage").textContent="Sent. I’ll reply when I can."; await loadHelpThread();}catch(error){$("#helpMessage").textContent=error.message;}});

let googleToken=sessionStorage.getItem("tcg-radar-google-token")||"", accountUser=null;
const THEME_KEY="tcg-radar-theme";
function applyTheme(theme){
  const light=theme==="light";
  document.body.classList.toggle("light-mode",light);
  $("#themeLabel").textContent=light?"Light mode":"Dark mode";
  $("#themeToggle").querySelector("b").textContent=light?"☀":"☾";
}
function updateAccountQuick(){
  const signed=Boolean(googleToken&&accountUser);
  $("#accountQuickTitle").textContent=signed?(accountUser.is_owner?"Owner":"Signed in"):"Sign in";
  $("#accountQuickDetail").textContent=signed?"Tap to sign out":"Use Google to save purchases";
}
function accountHeaders(){return googleToken?{Authorization:`Bearer ${googleToken}`}:{};}
async function renderPurchases(){const list=$("#purchaseList"); list.innerHTML=""; if(!googleToken){$("#purchaseForm").hidden=true;$("#purchaseLoginNote").hidden=false;$("#signOutBtn").hidden=true;$("#googleSignIn").hidden=false;$("#accountStatus").textContent="Sign in with Google to save purchases across devices.";updateAccountQuick();return;} try{const response=await fetch(api("/api/purchases"),{headers:accountHeaders(),cache:"no-store"}); if(!response.ok)throw new Error("Your Google session expired. Please sign in again."); const items=(await response.json()).items||[]; $("#purchaseForm").hidden=false;$("#purchaseLoginNote").hidden=true;$("#signOutBtn").hidden=false;$("#googleSignIn").hidden=true;$("#accountStatus").textContent=accountUser?`Signed in as ${accountUser.is_owner?"Owner":(accountUser.name||"Google member")}`:"Signed in with Google";updateAccountQuick();$("#purchaseTotal").textContent=new Intl.NumberFormat("en-US",{style:"currency",currency:"USD"}).format(items.reduce((sum,item)=>sum+Number(item.paid||0),0)); if(!items.length){list.innerHTML="<small>No purchases logged yet.</small>";return;} for(const item of items){const row=document.createElement("div"); row.className="purchase-row"; const info=document.createElement("span"); const title=document.createElement("strong"); title.textContent=`${item.name} × ${item.quantity}`; const detail=document.createElement("small"); detail.textContent=`${item.retailer||"Retailer not listed"} • ${item.date||"Date not listed"} • ${money(item.paid)}`; info.append(title,detail); const remove=document.createElement("button"); remove.className="remove"; remove.type="button"; remove.textContent="Remove"; remove.onclick=async()=>{await fetch(api(`/api/purchases/${encodeURIComponent(item.id)}`),{method:"DELETE",headers:accountHeaders()});renderPurchases();}; row.append(info,remove); list.appendChild(row);}}catch(error){googleToken="";sessionStorage.removeItem("tcg-radar-google-token");$("#accountStatus").textContent=error.message;$("#purchaseForm").hidden=true;}}
async function configureGoogleSignIn(){try{const config=await fetch(api("/api/auth/config"),{cache:"no-store"}).then(r=>r.json()); if(!config.enabled){$("#accountStatus").textContent="Google sign-in is being configured.";return;} if(googleToken){accountUser=await fetch(api("/api/auth/me"),{headers:accountHeaders()}).then(r=>r.ok?r.json():null);if(accountUser){renderPurchases();return;}googleToken="";sessionStorage.removeItem("tcg-radar-google-token");} if(!window.google?.accounts?.id){setTimeout(configureGoogleSignIn,500);return;} window.google.accounts.id.initialize({client_id:config.client_id,callback:credential=>{googleToken=credential.credential;sessionStorage.setItem("tcg-radar-google-token",googleToken);fetch(api("/api/auth/me"),{headers:accountHeaders()}).then(r=>r.json()).then(user=>{accountUser=user;renderPurchases();});}});$("#googleSignIn").innerHTML="";window.google.accounts.id.renderButton($("#googleSignIn"),{theme:"filled_black",size:"large",shape:"pill",text:"signin_with"});}catch(_){$("#accountStatus").textContent="Google sign-in is temporarily unavailable.";}}
function openPurchases(){ $("#purchasesDialog").showModal(); $("#purchaseDate").value=new Date().toISOString().slice(0,10); configureGoogleSignIn(); renderPurchases(); }
function openAccount(){ $("#accountDialog").showModal(); configureGoogleSignIn(); renderPurchases(); }
function signOut(){ googleToken=""; accountUser=null; sessionStorage.removeItem("tcg-radar-google-token"); $("#googleSignIn").innerHTML=""; $("#googleSignIn").hidden=false; configureGoogleSignIn(); renderPurchases(); }
$("#openPurchasePage").addEventListener("click",openPurchases);
$("#closePurchasesBtn").addEventListener("click",()=>$("#purchasesDialog").close());
$("#closeAccountBtn").addEventListener("click",()=>$("#accountDialog").close());
$("#accountQuickBtn").addEventListener("click",()=>googleToken?signOut():openAccount());
$("#signOutBtn").addEventListener("click",signOut);
$("#purchaseForm").addEventListener("submit",async event=>{event.preventDefault();const item={name:$("#purchaseName").value.trim(),quantity:Math.max(1,Number($("#purchaseQty").value||1)),paid:Number($("#purchasePaid").value||0),retailer:$("#purchaseRetailer").value.trim(),date:$("#purchaseDate").value,notes:$("#purchaseNotes").value.trim()};const response=await fetch(api("/api/purchases"),{method:"POST",headers:{"Content-Type":"application/json",...accountHeaders()},body:JSON.stringify(item)});if(!response.ok){$("#accountStatus").textContent=(await response.json().catch(()=>({}))).detail||"Purchase could not be saved";return;}event.target.reset();$("#purchaseQty").value="1";renderPurchases();});

appliedFilters=readFilters();
$("#applyFiltersBtn").addEventListener("click",()=>{appliedFilters=readFilters();$(".filter-drawer").open=false;render();});
$("#cancelFiltersBtn").addEventListener("click",()=>{setFilters(appliedFilters);$(".filter-drawer").open=false;});
$("#refreshBtn").addEventListener("click",loadFeed);
$("#sortMode").addEventListener("change",event=>{localStorage.setItem(SORT_MODE_KEY,event.target.value);render();});
const ADMIN_KEY="tcg-radar-manager-secret";
const MANAGER_MODE_KEY="tcg-radar-manager-mode";
let managerRole="";
let priorityAutomation={auto_high_priority:false,top_limit:20,trend_source:"not_configured",auto_selected_product_ids:[]};
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
async function loadIntakeSources(){
  const list=$("#intakeSourceList");
  if(!list) return;
  const data=await ownerFetch("/api/admin/intake-sources");
  list.innerHTML="";
  for(const item of data.items||[]){
    const row=document.createElement("div"); row.className="owner-product";
    const text=document.createElement("span"); const name=document.createElement("strong"); const detail=document.createElement("small");
    name.textContent=item.label; detail.textContent="Authorized webhook • created "+new Date(item.created_at).toLocaleString();
    text.append(name,detail);
    const remove=document.createElement("button"); remove.type="button"; remove.className="secondary"; remove.textContent="Revoke";
    remove.onclick=async()=>{if(!confirm("Revoke "+item.label+"? This source will no longer be accepted."))return; await ownerFetch("/api/admin/intake-sources/"+item.id,{method:"DELETE"}); await loadIntakeSources();};
    row.append(text,remove); list.append(row);
  }
}

async function loadManagerControls(){
  $("#productForm").hidden=true; $("#ownerOnlyControls").hidden=true; $("#adminOverview").hidden=true;
  try{
    const [data,settings]=await Promise.all([ownerFetch("/api/admin/products"),ownerFetch("/api/admin/priority-automation")]);
    priorityAutomation=settings||priorityAutomation;
    managerRole=data.role||""; healthAllowed=true; $("#healthSection").hidden=activePage!=="radar"; loadHealth(); loadFeed(); localStorage.setItem(MANAGER_MODE_KEY,"1"); updateManagerButton(); $("#managerLogin").hidden=true; $("#productForm").hidden=false; $("#ownerOnlyControls").hidden=managerRole!=="owner";
    const autoToggle=$("#autoPriorityToggle"), autoState=$("#autoPriorityState");
    autoToggle.checked=Boolean(priorityAutomation.auto_high_priority);
    autoToggle.disabled=managerRole!=="owner";
    autoState.textContent=priorityAutomation.auto_high_priority
      ? "On — the upcoming trends connector may promote up to 20 matched items. Manual priority controls are locked."
      : "Off — manual High / Normal / Low controls are active below.";
    renderOwnerProducts(data.items||[], managerRole==="owner");
    const staged=(data.items||[]).filter(item=>item.published===false).length;
    $("#publishTrackingWrap").hidden=managerRole!=="owner";
    $("#publishStagedBtn").disabled=!staged;
    $("#stagedCount").textContent=staged ? (staged+" staged item"+(staged===1?"":"s")+" waiting to go live") : "No staged items waiting";
    $("#adminOverview").hidden=false; $("#adminRole").textContent=managerRole==="owner"?"Owner session":"Moderator session"; $("#adminProductCount").textContent=(data.items||[]).length; $("#adminAccess").textContent=managerRole==="owner"?"Full command access":"Product links only";
    if(managerRole==="owner"){await loadModerators(); await loadHelpInbox(); await loadIntakeSources(); clearInterval(helpPollTimer); helpPollTimer=setInterval(loadHelpInbox,20000);}
    $("#ownerMessage").textContent=managerRole==="owner"?"Owner access: messages and moderator controls are available.":"Moderator access: you can add and remove public product URLs.";
  }catch(error){sessionStorage.removeItem(ADMIN_KEY); $("#managerLogin").hidden=false; $("#adminOverview").hidden=true; $("#ownerMessage").textContent=error.message;}
}
function showOwner(){
  $("#ownerDialog").showModal(); $("#managerLogin").hidden=false; $("#productForm").hidden=true; $("#ownerOnlyControls").hidden=true; $("#managerCode").value=""; $("#ownerMessage").textContent="Enter a manager code to unlock these controls.";
  if(sessionStorage.getItem(ADMIN_KEY)) loadManagerControls();
}
$("#cancelManager").addEventListener("click",()=>{ $("#ownerDialog").close(); });
$("#unlockManager").addEventListener("click",async()=>{const code=$("#managerCode").value.trim(); if(!code){$("#ownerMessage").textContent="Enter a manager code first."; return;} sessionStorage.setItem(ADMIN_KEY,code); $("#ownerMessage").textContent="Checking code…"; await loadManagerControls();});
$("#lockManagerBtn").addEventListener("click",()=>{clearInterval(helpPollTimer); sessionStorage.removeItem(ADMIN_KEY); localStorage.removeItem(MANAGER_MODE_KEY); managerRole=""; healthAllowed=false; loadFeed(); $("#healthSection").hidden=true; updateManagerButton(); $("#managerLogin").hidden=false; $("#productForm").hidden=true; $("#ownerOnlyControls").hidden=true; $("#managerCode").value=""; $("#ownerMessage").textContent="Manager controls locked.";});
async function loadHelpInbox(){
  try{const items=(await ownerFetch("/api/admin/help")).items||[]; const list=$("#ownerHelp"); list.innerHTML=""; if(!items.length){list.innerHTML="<small>No help messages yet.</small>";return;} const threads=new Map(); for(const item of items){if(!threads.has(item.thread_id))threads.set(item.thread_id,item);} for(const item of threads.values()){const row=document.createElement("div"); row.className="owner-help-row"; const message=document.createElement("small"); message.textContent=`${item.body} • ${new Date(item.created_at).toLocaleString()}`; const reply=document.createElement("textarea"); reply.rows=2; reply.maxLength=1000; reply.placeholder="Reply to this user…"; const actions=document.createElement("div"); actions.className="help-owner-actions"; const button=document.createElement("button"); button.type="button"; button.textContent="Reply"; button.onclick=async()=>{if(!reply.value.trim())return; await ownerFetch(`/api/admin/help/${encodeURIComponent(item.thread_id)}/reply`,{method:"POST",body:JSON.stringify({body:reply.value})}); reply.value=""; await loadHelpInbox();}; const ban=document.createElement("button"); ban.type="button"; ban.className="secondary danger-button"; ban.textContent="Ban device"; ban.onclick=async()=>{if(!confirm("Block this device from sending more help messages?"))return; await ownerFetch(`/api/admin/help/${encodeURIComponent(item.client_id)}/ban`,{method:"POST"}); row.remove();}; actions.append(button,ban); row.append(message,reply,actions); list.appendChild(row); if(lastHelpMessageId&&item.id!==lastHelpMessageId&&item.sender==="user"&&Notification.permission==="granted")new Notification("TCG Radar help request",{body:item.body}); lastHelpMessageId=item.id;}}
  catch(error){$("#ownerMessage").textContent=error.message;}
}
$("#loadHelpBtn").addEventListener("click",loadHelpInbox);
$("#loadUsageBtn").addEventListener("click",async()=>{
  const d=await ownerFetch("/api/admin/usage");
  const retailers=(d.link_clicks||[]).map(item=>`${item.retailer}: ${item.clicks}`).join(" • ")||"No product links clicked yet.";
  $("#ownerUsage").textContent=`Anonymous devices: ${d.anonymous_devices} • Google accounts: ${d.google_accounts} • Push devices: ${d.push_devices} • Help users: ${d.help_devices} • Links clicked: ${d.link_click_total||0} • ${retailers}`;
});
function renderOwnerProducts(items,isOwner){
  ownerProductItems=items;
  ownerCanEditProducts=isOwner;
  const allItems=items;
  const query=($("#ownerProductSearch")?.value||"").trim().toLowerCase();
  const sortMode=$("#ownerProductSort")?.value||"newest";
  items=items.map((item,index)=>({item,index})).filter(({item})=>{
    const text=`${item.product||""} ${item.store||""} ${item.game||""} ${item.set_name||""}`.toLowerCase();
    return !query||text.includes(query);
  }).sort((left,right)=>{
    if(sortMode==="alphabetical") return String(left.item.product||"").localeCompare(String(right.item.product||""));
    const leftTime=Date.parse(left.item.added_at||left.item.updated_at||"")||left.index;
    const rightTime=Date.parse(right.item.added_at||right.item.updated_at||"")||right.index;
    return sortMode==="oldest"?leftTime-rightTime:rightTime-leftTime;
  }).map(({item})=>item);
  $("#ownerProducts").innerHTML="";
  const estimateProduct=$("#stockEstimateProduct");
  if(estimateProduct){
    const selected=estimateProduct.value;
    estimateProduct.innerHTML=allItems.map(item=>`<option value="${item.id}">${item.product} — ${item.store}${item.stock_estimate?` (${item.stock_estimate})`:""}</option>`).join("");
    if([...estimateProduct.options].some(option=>option.value===selected)) estimateProduct.value=selected;
  }
  for(const item of items){
    const el=document.createElement("div"); el.className="owner-product";
    el.innerHTML=`<img class="owner-product-image" alt="" hidden><span><strong></strong><small></small></span><div class="product-actions">${isOwner?'<select class="priority-select" aria-label="Manual priority"><option value="high">High</option><option value="normal">Normal</option><option value="low">Low</option></select><button type="button" class="toggle"></button><button type="button" class="edit-image">Edit image</button>':''}<button type="button" class="remove">Remove</button></div>`;
    const ownerImage=el.querySelector(".owner-product-image");
    if(item.image_url){ ownerImage.src=item.image_url; ownerImage.alt=(item.product||"Product")+" image"; ownerImage.hidden=false; ownerImage.onerror=()=>{ownerImage.hidden=true;}; }
    el.querySelector("strong").textContent=item.product;
    el.querySelector("small").textContent=`${item.store} • ${item.published===false?"staged — not live":"live"} • ${item.priority} priority • alert at ≤ ${item.max_markup ?? 80}% markup`;
    const priority=el.querySelector(".priority-select");
    if(priority){
      priority.value=item.priority||"normal";
      priority.disabled=Boolean(priorityAutomation.auto_high_priority);
      priority.title=priority.disabled?"Turn off High Priority Auto to edit manual priorities.":"Manual monitoring priority";
      priority.onchange=async()=>{try{await ownerFetch(`/api/admin/products/${item.id}`,{method:"PATCH",body:JSON.stringify({priority:priority.value})}); $("#ownerMessage").textContent=`${item.product} is now ${priority.value} priority.`; await loadManagerControls(); loadFeed();}catch(error){$("#ownerMessage").textContent=error.message;}};
    }
    const toggle=el.querySelector(".toggle");
    if(toggle){ toggle.textContent=item.enabled===false?"Resume":"Pause"; toggle.classList.toggle("secondary",true); toggle.onclick=async()=>{try{await ownerFetch(`/api/admin/products/${item.id}`,{method:"PATCH",body:JSON.stringify({enabled:item.enabled===false})}); await showOwner(); loadFeed();}catch(error){$("#ownerMessage").textContent=error.message;}}; }
    const editImage=el.querySelector(".edit-image");
    if(editImage){ editImage.onclick=async()=>{
      const value=prompt("Paste a secure image URL to override the automatic picture. Leave blank to restore the official retailer image.",item.image_url||"");
      if(value===null) return;
      try{
        await ownerFetch("/api/admin/products/"+item.id,{method:"PATCH",body:JSON.stringify({image_url:value.trim()})});
        $("#ownerMessage").textContent=value.trim()?"Image override saved.":"Official retailer image restored.";
        await loadManagerControls(); loadFeed();
      }catch(error){$("#ownerMessage").textContent=error.message;}
    }; }
    el.querySelector(".remove").onclick=async()=>{if(!confirm(`Remove ${item.product}?`))return; try{await ownerFetch(`/api/admin/products/${item.id}`,{method:"DELETE"}); await showOwner(); loadFeed();}catch(error){$("#ownerMessage").textContent=error.message;}};
    $("#ownerProducts").appendChild(el);
  }
}
$("#ownerProductSearch").addEventListener("input",()=>renderOwnerProducts(ownerProductItems,ownerCanEditProducts));
$("#ownerProductSort").addEventListener("change",()=>renderOwnerProducts(ownerProductItems,ownerCanEditProducts));
$("#ownerBtn").addEventListener("click",showOwner);
$("#closeOwnerBtn").addEventListener("click",()=>$("#ownerDialog").close());
$("#testPushBtn").addEventListener("click",async()=>{ $("#ownerMessage").textContent="Test scheduled—close TCG Radar completely now."; try{const result=await ownerFetch("/api/admin/push/test",{method:"POST"}); $("#ownerMessage").textContent=result.attempted?"Test scheduled for 10 seconds. Close TCG Radar completely now.":"No phones are subscribed yet—tap Enable Push Alerts on the main screen first.";}catch(error){$("#ownerMessage").textContent=error.message;} });
$("#intakeSourceForm").addEventListener("submit",async event=>{
  event.preventDefault();
  const created=$("#intakeSourceCreated");
  try{
    const item=await ownerFetch("/api/admin/intake-sources",{method:"POST",body:JSON.stringify({label:$("#intakeSourceLabel").value})});
    $("#intakeSourceLabel").value="";
    created.hidden=false;
    created.textContent="Webhook: "+location.origin.replace("failedxassassin.github.io","tcg-restock-radar-production.up.railway.app") + item.webhook_url+"\nHeader: X-TCG-Radar-Intake-Key: "+item.secret+"\nSave this key now; it is shown only once.";
    await loadIntakeSources();
  }catch(error){$("#ownerMessage").textContent=error.message;}
});

$("#announcementForm").addEventListener("submit",async event=>{event.preventDefault(); try{const result=await ownerFetch("/api/admin/announcements",{method:"POST",body:JSON.stringify({title:$("#announcementTitle").value,body:$("#announcementBody").value,url:location.href})}); $("#announcementBody").value=""; $("#ownerMessage").textContent=result.attempted?`Message sent to ${result.attempted} subscribed phone(s).`:"No phones are subscribed yet.";}catch(error){$("#ownerMessage").textContent=error.message;}});
$("#verifiedDropForm").addEventListener("submit",async event=>{event.preventDefault(); try{const result=await ownerFetch("/api/admin/verified-drops",{method:"POST",body:JSON.stringify({product:$("#verifiedDropProduct").value,game:$("#verifiedDropGame").value,store:$("#verifiedDropStore").value,url:$("#verifiedDropUrl").value,price:$("#verifiedDropPrice").value||null,msrp:$("#verifiedDropMsrp").value||null,seller_confirmed:$("#verifiedDropSeller").checked})}); event.target.reset(); $("#ownerMessage").textContent=result.push_attempted?`Owner-confirmed drop posted for ${result.push_attempted} matching phone(s).`:"Owner-confirmed drop posted to Alert History; no subscribed phones matched it yet."; await loadAlerts();}catch(error){$("#ownerMessage").textContent=error.message;}});
$("#autoPriorityToggle").addEventListener("change",async event=>{
  try{
    priorityAutomation=await ownerFetch("/api/admin/priority-automation",{method:"PATCH",body:JSON.stringify({auto_high_priority:event.target.checked})});
    $("#ownerMessage").textContent=event.target.checked?"High Priority Auto is on. Manual priority controls are locked.":"High Priority Auto is off. Manual priority controls are active.";
    await loadManagerControls();
  }catch(error){event.target.checked=!event.target.checked;$("#ownerMessage").textContent=error.message;}
});
$("#stockEstimateForm").addEventListener("submit",async event=>{
  event.preventDefault();
  try{
    const estimate=$("#stockEstimateValue").value.trim();
    await ownerFetch(`/api/admin/products/${encodeURIComponent($("#stockEstimateProduct").value)}`,{method:"PATCH",body:JSON.stringify({stock_estimate:estimate,stock_estimate_ttl_hours:$("#stockEstimateTtl").value})});
    $("#stockEstimateValue").value="";
    $("#ownerMessage").textContent=estimate?"Unverified stock estimate saved. It does not change OOS status or send an alert.":"Stock estimate cleared.";
    await loadManagerControls(); await loadFeed();
  }catch(error){$("#ownerMessage").textContent=error.message;}
});
async function loadDiscoveryReview(){
  try{
    const data=await ownerFetch("/api/admin/discovery");
    const items=(await ownerFetch("/api/admin/catalog?status=pending_verification")).items||[];
    $("#discoverySummary").textContent="Best Buy discovery: "+(data.enabled?"enabled":"off")+" • "+(data.pending_verification||0)+" pending • "+(data.catalog_products||0)+" catalog products";
    const list=$("#discoveryProducts"); list.innerHTML="";
    if(!items.length){list.innerHTML="<small>No product candidates are waiting for review.</small>";return;}
    for(const item of items){
      const row=document.createElement("div"); row.className="owner-product";
      row.innerHTML='<span><strong></strong><small></small></span><div class="product-actions"><button type="button" class="approve">Approve monitor</button><button type="button" class="remove">Reject</button></div>';
      row.querySelector("strong").textContent=item.title||"Unnamed discovery";
      row.querySelector("small").textContent=(item.retailer||"Unknown retailer")+" • "+(item.tcg||"Other")+(item.set_name?" • "+item.set_name:"")+" • "+String(item.product_type||"other").replaceAll("_"," ");
      row.querySelector(".approve").onclick=async()=>{try{await ownerFetch("/api/admin/catalog/"+encodeURIComponent(item.canonical_key)+"/approve",{method:"POST"});await loadDiscoveryReview();await loadManagerControls();}catch(error){$("#ownerMessage").textContent=error.message;}};
      row.querySelector(".remove").onclick=async()=>{try{await ownerFetch("/api/admin/catalog/"+encodeURIComponent(item.canonical_key)+"/reject",{method:"POST"});await loadDiscoveryReview();}catch(error){$("#ownerMessage").textContent=error.message;}};
      list.appendChild(row);
    }
  }catch(error){$("#discoverySummary").textContent=error.message;}
}
$("#runDiscoveryBtn").addEventListener("click",async()=>{try{$("#discoverySummary").textContent="Running bounded public discovery and first-party verification…";const run=await ownerFetch("/api/admin/discovery/run",{method:"POST"});$("#discoverySummary").textContent=`Best Buy discovery: ${run.discovered||0} found • ${run.verified||0} first-party verified • ${run.monitoring_started||0} monitoring started`;await loadDiscoveryReview();}catch(error){$("#discoverySummary").textContent=error.message;}});
$("#loadDiscoveryBtn").addEventListener("click",loadDiscoveryReview);
async function loadReports(){try{const data=await ownerFetch("/api/admin/reports"); const list=$("#ownerReports"); list.innerHTML=""; if(!(data.items||[]).length){list.innerHTML="<small>No user reports yet.</small>";return;} for(const report of data.items){const row=document.createElement("div"); row.className="owner-product"; row.innerHTML=`<span><strong></strong><small></small></span>`; row.querySelector("strong").textContent=report.reason.replaceAll("_"," "); row.querySelector("small").textContent=`Product ${report.product_id} • ${new Date(report.created_at).toLocaleString()}`; list.appendChild(row);}}catch(error){$("#ownerMessage").textContent=error.message;}}
$("#loadReportsBtn").addEventListener("click",loadReports);
async function loadModerators(){try{const data=await ownerFetch("/api/admin/moderators"); const list=$("#moderatorList"); list.innerHTML=""; for(const moderator of data.items||[]){const row=document.createElement("div"); row.className="owner-product"; row.innerHTML=`<span><strong></strong><small>Can add and remove tracked URLs only</small></span><button type="button" class="remove">Remove</button>`; row.querySelector("strong").textContent=moderator.name; row.querySelector("button").onclick=async()=>{if(!confirm(`Remove ${moderator.name}'s moderator access?`))return; try{await ownerFetch(`/api/admin/moderators/${moderator.id}`,{method:"DELETE"}); await loadModerators();}catch(error){$("#ownerMessage").textContent=error.message;}}; list.appendChild(row);}}catch(error){$("#ownerMessage").textContent=error.message;}}
$("#moderatorForm").addEventListener("submit",async event=>{event.preventDefault(); try{const result=await ownerFetch("/api/admin/moderators",{method:"POST",body:JSON.stringify({name:$("#moderatorName").value})}); $("#moderatorName").value=""; await loadModerators(); prompt(`Copy this one-time moderator code for ${result.name}. Send it privately; it will not be shown again. They can use the manager link ending in ?manager=1.`,result.access_code);}catch(error){$("#ownerMessage").textContent=error.message;}});
$("#testProductLinkBtn").addEventListener("click",async()=>{
  const url=$("#ownerUrl").value.trim();
  const result=$("#linkTestResult");
  if(!url){result.hidden=false;result.textContent="Paste an official retailer product URL first.";return;}
  result.hidden=false; result.textContent="Testing the official retailer page…";
  try{
    const data=await ownerFetch("/api/admin/products/test-link",{method:"POST",body:JSON.stringify({url})});
    $("#ownerUrl").value=data.url;
    if(!$("#ownerProduct").value.trim()&&data.title) $("#ownerProduct").value=data.title;
    if(!$("#ownerImageUrl").value.trim()&&data.image_url) $("#ownerImageUrl").value=data.image_url;
    result.textContent=(data.retailer||"Retailer")+" confirmed • "+(data.title||"Product title not supplied")+" • Image "+(data.image_url?"found":"not supplied")+". This does not check stock.";
    result.classList.remove("error");
  }catch(error){result.textContent=error.message;result.classList.add("error");}
});

$("#productForm").addEventListener("submit",async event=>{event.preventDefault(); $("#ownerMessage").textContent="Adding to staged list…"; try{await ownerFetch("/api/admin/products",{method:"POST",body:JSON.stringify({product:$("#ownerProduct").value,url:$("#ownerUrl").value,game:$("#ownerGame").value,set_name:$("#ownerSet").value,product_type:$("#ownerProductType").value,packs:$("#ownerPacks").value||null,msrp:$("#ownerMsrp").value||null,priority:$("#ownerPriority").value,max_markup:$("#ownerMaxMarkup").value||80,area:"Online",image_url:$("#ownerImageUrl").value.trim()})}); event.target.reset(); await loadManagerControls(); $("#ownerMessage").textContent="Added to the staged list. Publish when your batch is ready.";}catch(error){$("#ownerMessage").textContent=error.message;}});

$("#publishStagedBtn").addEventListener("click",async()=>{
  if(!confirm("Publish all staged products to live monitoring?")) return;
  $("#ownerMessage").textContent="Publishing tracking changes…";
  try{
    const result=await ownerFetch("/api/admin/products/publish",{method:"POST"});
    $("#ownerMessage").textContent=result.message;
    await loadManagerControls();
    loadFeed();
  }catch(error){$("#ownerMessage").textContent=error.message;}
});

const PERSONAL_MARKUP_KEY="tcg-radar-personal-markup";
const ALERT_GAMES_KEY="tcg-radar-alert-games";
const ALERT_STORES_KEY="tcg-radar-alert-stores";
function personalMarkup(){ return Number(localStorage.getItem(PERSONAL_MARKUP_KEY)||80); }
function storedChoices(key, fallback){try{const value=JSON.parse(localStorage.getItem(key)||"");return Array.isArray(value)&&value.length?value:fallback;}catch(_){return fallback;}}
const THIRD_PARTY_ALERTS_KEY="tcg-radar-third-party-alerts";
function thirdPartyAlertsAllowed(){return localStorage.getItem(THIRD_PARTY_ALERTS_KEY)==="true";}
function alertPreferences(){return {max_markup:personalMarkup(),games:storedChoices(ALERT_GAMES_KEY,["Pokemon","One Piece","Magic"]),stores:storedChoices(ALERT_STORES_KEY,["Walmart","Target","Amazon","Best Buy"]),allow_third_party:thirdPartyAlertsAllowed()};}
function updateThirdPartyAlertsButton(){
  const button=$("#thirdPartyAlertsBtn"); if(!button)return;
  const enabled=thirdPartyAlertsAllowed();
  button.querySelector("strong").textContent=enabled?"Don't allow 3rd party":"Allow 3rd party";
  button.querySelector("small").textContent=enabled?"Alerts include eligible 3rd-party offers within your price limit":"3rd-party offers are visible, but only retailer-direct stock can alert";
}
function hydrateAlertChoices(){
  const prefs=alertPreferences();
  document.querySelectorAll("#settingsGames input").forEach(input=>input.checked=prefs.games.includes(input.value));
  document.querySelectorAll("#settingsStores input").forEach(input=>input.checked=prefs.stores.includes(input.value));
  updateThirdPartyAlertsButton();
}
function base64UrlToBytes(value){ const padded=value.replace(/-/g,"+").replace(/_/g,"/")+"=".repeat((4-value.length%4)%4); const raw=atob(padded); return Uint8Array.from(raw,c=>c.charCodeAt(0)); }
async function savePushSubscription(subscription){
  const response=await fetch(api("/api/push/subscribe"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({subscription,preferences:alertPreferences()})});
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
async function savePersonalMarkup(value){
  localStorage.setItem(PERSONAL_MARKUP_KEY,value);
  try{const registration=await navigator.serviceWorker.ready;const subscription=await registration.pushManager.getSubscription();if(subscription) await savePushSubscription(subscription);}catch(_){}
}
$("#settingsMarkup").addEventListener("change",()=>savePersonalMarkup($("#settingsMarkup").value));
$("#thirdPartyAlertsBtn").addEventListener("click",async()=>{
  localStorage.setItem(THIRD_PARTY_ALERTS_KEY,String(!thirdPartyAlertsAllowed()));
  updateThirdPartyAlertsButton();
  try{const registration=await navigator.serviceWorker.ready;const subscription=await registration.pushManager.getSubscription();if(subscription)await savePushSubscription(subscription);}catch(_){}
});
["#settingsGames","#settingsStores"].forEach(selector=>$(selector).addEventListener("change",async event=>{
  const container=$(selector), checked=[...container.querySelectorAll("input:checked")].map(input=>input.value);
  if(!checked.length){event.target.checked=true;return;}
  localStorage.setItem(selector==="#settingsGames"?ALERT_GAMES_KEY:ALERT_STORES_KEY,JSON.stringify(checked));
  try{const registration=await navigator.serviceWorker.ready;const subscription=await registration.pushManager.getSubscription();if(subscription) await savePushSubscription(subscription);}catch(_){}
}));
$("#alertSettingsForm").addEventListener("submit",async event=>{ event.preventDefault(); await savePersonalMarkup($("#personalMarkup").value); $("#alertSettingsMessage").textContent="Saved for this phone."; setTimeout(()=>$("#alertSettingsDialog").close(),500); });

function switchPage(page){
  activePage=page;
  document.querySelectorAll("[data-page]").forEach(section=>{ section.hidden=section.dataset.page!==page || (section.id==="welcomeGuide" && localStorage.getItem("tcg-radar-welcome-guide-dismissed")) || (section.id==="healthSection" && !healthAllowed); });
  const navMap={radar:"#navRadarBtn",alerts:"#navAlertsBtn",nearby:"#navNearbyBtn",collection:"#navCollectionBtn",more:"#navMoreBtn"};
  document.querySelectorAll(".bottom-nav button").forEach(button=>button.classList.toggle("active",button===$(navMap[page])));
  if(page==="nearby") setMode("local");
  if(page==="radar") setMode("online");
  if(page==="alerts") loadAlerts();
  window.scrollTo({top:0,behavior:"smooth"});
}
$("#navRadarBtn").addEventListener("click",()=>switchPage("radar"));
$("#navAlertsBtn").addEventListener("click",()=>switchPage("alerts"));
$("#navNearbyBtn").addEventListener("click",()=>switchPage("nearby"));
$("#navCollectionBtn").addEventListener("click",()=>switchPage("collection"));
$("#navMoreBtn").addEventListener("click",()=>switchPage("more"));

function openSettings(){
  $("#settingsMarkup").value=String(personalMarkup());
  hydrateAlertChoices();
  updateAccountQuick();
  $("#settingsScrim").hidden=false;
  $("#settingsDrawer").setAttribute("aria-hidden","false");
  document.body.classList.add("settings-open");
}
function closeSettings(){
  $("#settingsScrim").hidden=true;
  $("#settingsDrawer").setAttribute("aria-hidden","true");
  document.body.classList.remove("settings-open");
}
applyTheme(localStorage.getItem(THEME_KEY)||"dark");
function updateWatchlistButton(){const count=watchlist().size;$("#watchlistBtn").innerHTML=`${watchlistOnly?"★":"☆"} <span>${watchlistOnly?"Showing watchlist":`My watchlist${count?` (${count})`:""}`}</span>`;}
$("#watchlistBtn").addEventListener("click",()=>{watchlistOnly=!watchlistOnly;updateWatchlistButton();switchPage("radar");render();});
updateWatchlistButton();
$("#settingsBtn").addEventListener("click",openSettings);
$("#closeSettingsBtn").addEventListener("click",closeSettings);
$("#settingsScrim").addEventListener("click",closeSettings);
$("#themeToggle").addEventListener("click",()=>{const next=document.body.classList.contains("light-mode")?"dark":"light";localStorage.setItem(THEME_KEY,next);applyTheme(next);});


$("#closeReportBtn").addEventListener("click",()=>$("#reportDialog").close());
document.querySelectorAll("[data-report]").forEach(button=>button.addEventListener("click",async()=>{if(!reportProductId)return;$("#reportMessage").textContent="Sending…";try{const response=await fetch(api("/api/reports"),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({product_id:reportProductId,reason:button.dataset.report})});if(!response.ok)throw new Error("Report could not be saved");$("#reportMessage").textContent="Thanks — it was saved for review.";setTimeout(()=>$("#reportDialog").close(),650);}catch(error){$("#reportMessage").textContent=error.message;}}));
$("#statusHelpBtn").addEventListener("click",()=>$("#statusHelpDialog").showModal());
$("#closeStatusHelpBtn").addEventListener("click",()=>$("#statusHelpDialog").close());
$("#startSetupBtn").addEventListener("click",()=>{openSettings();$("#welcomeGuide").open=false;});

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
switchPage("radar");
loadFeed();
setInterval(loadFeed, 30000);

