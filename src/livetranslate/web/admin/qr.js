// The QR code page. In a file of its own: the pages only run scripts from files
// (Content-Security-Policy).
const $ = (id) => document.getElementById(id);
let addresses = [];
function show(url) {
  try { localStorage.setItem("lt.qr.url", url); } catch { /* ignore */ }
  $("qr").src = `/api/qr.svg?url=${encodeURIComponent(url)}`;
  $("url").textContent = url.replace(/^https?:\/\//, "").replace(/\/$/, "");
  const wifi = (addresses.find((a) => a.url === url) || {}).wifi;
  $("wifi").textContent = wifi ? `📶 Wi-Fi: ${wifi}` : "";
  $("nets").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.url === url));
}
// One button per network of this PC; the address picked in the operator page comes along in ?url=.
fetch("/api/qr/addresses").then((r) => r.json()).then((info) => {
  addresses = info.addresses;
  let saved = new URLSearchParams(location.search).get("url") || "";
  try { saved = saved || localStorage.getItem("lt.qr.url") || ""; } catch { /* ignore */ }
  for (const a of addresses) {
    const b = document.createElement("button");
    b.type = "button";
    b.dataset.url = a.url;
    b.textContent = a.name;
    b.title = a.detail;
    b.addEventListener("click", () => show(a.url));
    $("nets").appendChild(b);
  }
  $("nets").hidden = addresses.length < 2;
  show(addresses.some((a) => a.url === saved) ? saved : addresses[0].url);
}).catch(() => { $("qr").src = "/api/qr.svg"; });
fetch("/api/public").then((r) => r.json()).then((info) => {
  // The languages below are each written in their own language, so everyone can see theirs.
  if (info.church) document.getElementById("heading").textContent = `🌐 ${info.church}`;
  if (!$("url").textContent) $("url").textContent = info.url.replace(/^https?:\/\//, "").replace(/\/$/, "");
  if (!addresses.length) $("wifi").textContent = info.wifi ? `📶 Wi-Fi: ${info.wifi}` : "";
  document.getElementById("langs").textContent = info.languages.map((l) => l.name).join(" · ");
});
