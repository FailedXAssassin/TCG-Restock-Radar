
const DEMO = [
  {game:"Pokemon", area:"Morgantown", store:"Walmart", product:"Pokémon Elite Trainer Box", price:49.98, msrp:49.99, evidence:"Demo listing — replace with live feed.", url:"https://www.walmart.com/"},
  {game:"Pokemon", area:"Martinsburg", store:"Target", product:"Pokémon Booster Bundle", price:29.99, msrp:29.99, evidence:"Demo listing — replace with live feed.", url:"https://www.target.com/"},
  {game:"One Piece", area:"Online", store:"GameStop", product:"One Piece Card Game Double Pack", price:19.99, msrp:19.99, evidence:"Demo listing — replace with live feed.", url:"https://www.gamestop.com/"},
  {game:"Magic", area:"Online", store:"Best Buy", product:"Magic Collector Booster", price:39.99, msrp:37.99, evidence:"Demo listing — replace with live feed.", url:"https://www.bestbuy.com/"},
  {game:"Pokemon", area:"Online", store:"TCG Retailer", product:"Pokémon Center-style Premium Box", price:99.99, msrp:79.99, evidence:"Demo example at 25% over MSRP.", url:"https://www.pokemoncenter.com/"}
];

const $ = s => document.querySelector(s);
let rows = [];
let deferredPrompt = null;

function markupPct(item){
  if(!item.msrp || item.msrp <= 0) return 999;
  return ((item.price-item.msrp)/item.msrp)*100;
}
function money(n){ return new Intl.NumberFormat("en-US",{style:"currency",currency:"USD"}).format(n); }

function render(){
  const game = $("#gameFilter").value;
  const area = $("#areaFilter").value;
  const maxMarkup = Number($("#markupFilter").value);
  const q = $("#searchInput").value.trim().toLowerCase();

  const filtered = rows.filter(x => {
    const m = markupPct(x);
    return (game==="all" || x.game===game)
      && (area==="all" || x.area===area)
      && m <= maxMarkup
      && (!q || `${x.product} ${x.store} ${x.game} ${x.area}`.toLowerCase().includes(q));
  });

  $("#results").innerHTML = "";
  const tpl = $("#itemTemplate");

  for(const item of filtered){
    const node = tpl.content.cloneNode(true);
    node.querySelector(".game").textContent = item.game;
    node.querySelector(".area").textContent = item.area;
    node.querySelector(".product").textContent = item.product;
    node.querySelector(".store").textContent = item.store;
    node.querySelector(".price").textContent = money(item.price);
    node.querySelector(".msrp").textContent = money(item.msrp);
    const m = markupPct(item);
    const markupEl = node.querySelector(".markup");
    markupEl.textContent = `${m>=0?"+":""}${m.toFixed(0)}%`;
    markupEl.classList.add(m<=0?"good":m<=40?"warn":"bad");
    node.querySelector(".evidence").textContent = item.evidence || "";
    const a = node.querySelector(".buy");
    a.href = item.url || "#";
    $("#results").appendChild(node);
  }

  $("#resultCount").textContent = filtered.length;
  $("#msrpCount").textContent = filtered.filter(x => markupPct(x) <= 0).length;
  if(!filtered.length) $("#results").innerHTML = `<div class="status card">No drops match the current filters.</div>`;
}

async function loadFeed(){
  const feed = localStorage.getItem("feedUrl") || "";
  $("#feedUrl").value = feed;
  $("#statusBox").textContent = "Refreshing…";
  try{
    if(feed){
      const res = await fetch(feed, {cache:"no-store"});
      if(!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      rows = Array.isArray(data) ? data : (data.items || []);
      $("#statusBox").textContent = `Loaded ${rows.length} live items from your configured feed.`;
    } else {
      rows = DEMO;
      $("#statusBox").textContent = "Using demo data. Open Feed Settings to connect a live JSON endpoint.";
    }
  } catch(err){
    rows = DEMO;
    $("#statusBox").textContent = `Live feed failed (${err.message}). Showing demo data instead.`;
  }
  $("#updatedAt").textContent = new Date().toLocaleTimeString([], {hour:"numeric", minute:"2-digit"});
  render();
}

["gameFilter","areaFilter","markupFilter","searchInput"].forEach(id => {
  $("#"+id).addEventListener("input", render);
});
$("#refreshBtn").addEventListener("click", loadFeed);
$("#settingsBtn").addEventListener("click", () => $("#settingsDialog").showModal());
$("#saveFeedBtn").addEventListener("click", () => {
  localStorage.setItem("feedUrl", $("#feedUrl").value.trim());
  setTimeout(loadFeed, 0);
});

$("#notifyBtn").addEventListener("click", async () => {
  if(!("Notification" in window)){
    alert("Notifications are not supported in this browser.");
    return;
  }
  const p = await Notification.requestPermission();
  if(p === "granted"){
    new Notification("TCG Restock Radar", {body:"Notifications are enabled on this device."});
  }
});

window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredPrompt = e;
  $("#installBtn").hidden = false;
});
$("#installBtn").addEventListener("click", async () => {
  if(!deferredPrompt) return;
  deferredPrompt.prompt();
  await deferredPrompt.userChoice;
  deferredPrompt = null;
  $("#installBtn").hidden = true;
});

if("serviceWorker" in navigator){
  navigator.serviceWorker.register("service-worker.js");
}
loadFeed();
