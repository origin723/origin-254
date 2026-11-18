/* In-app photo viewer: click a thumbnail to open it over the page and move through
   that listing's photos (arrows, keyboard, swipe, filmstrip). Without JS the
   thumbnail links still open the photo directly. */
(function () {
  "use strict";

  // Styles live here (not in app.css) so the viewer can never load with a stale cached stylesheet.
  var CSS = `body.gallery-open { overflow: hidden; }
.gallery { position: fixed; inset: 0; z-index: 1000; display: grid; grid-template-columns: 1fr minmax(0, 900px) 1fr;
  grid-template-rows: 1fr auto; align-items: center; gap: 12px; padding: 24px 16px; background: rgba(12, 20, 12, .82);
  backdrop-filter: blur(3px); }
.gallery[hidden] { display: none; }
.gallery-stage { grid-column: 2; grid-row: 1; margin: 0; min-height: 0; height: 100%; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 10px; }
.gallery-stage img { max-width: 100%; max-height: min(70vh, calc(100vh - 200px)); object-fit: contain; border-radius: 12px;
  background: #0b120b; box-shadow: 0 10px 40px rgba(0,0,0,.5); }
.gallery-stage figcaption { display: flex; gap: 14px; color: #e8f2e4; font-size: .95rem; }
.gallery-counter { color: #a9c3a2; font-variant-numeric: tabular-nums; }
.gallery-nav, .gallery-close { border: 0; border-radius: 999px; background: rgba(255,255,255,.14); color: #fff;
  cursor: pointer; line-height: 1; }
.gallery-nav { width: 44px; height: 44px; font-size: 1.8rem; grid-row: 1; }
.gallery-nav.prev { grid-column: 1; justify-self: end; }
.gallery-nav.next { grid-column: 3; justify-self: start; }
.gallery-close { position: absolute; top: 12px; right: 12px; width: 40px; height: 40px; font-size: 1.6rem; }
.gallery-nav:hover, .gallery-close:hover { background: rgba(255,255,255,.28); }
.gallery-nav:focus-visible, .gallery-close:focus-visible, .gallery-strip button:focus-visible { outline: 3px solid var(--accent, #ff9a1f); outline-offset: 2px; }
.gallery-strip { grid-column: 1 / -1; grid-row: 2; display: flex; gap: 8px; overflow-x: auto; padding: 4px 4px 8px;
  justify-content: safe center; scroll-snap-type: x mandatory; scrollbar-width: thin; }
.gallery-strip button { flex: none; width: 56px; height: 56px; padding: 0; border: 2px solid transparent; border-radius: 8px;
  overflow: hidden; background: #0b120b; cursor: pointer; opacity: .6; scroll-snap-align: center; }
.gallery-strip button[aria-selected="true"] { border-color: var(--accent, #ff9a1f); opacity: 1; }
.gallery-strip img { width: 100%; height: 100%; object-fit: cover; display: block; }
@media (max-width: 600px) {
  .gallery { grid-template-columns: 1fr; padding: 12px 8px; }
  .gallery-stage { grid-column: 1; }
  .gallery-nav { position: absolute; top: 50%; transform: translateY(-50%); width: 40px; height: 40px; font-size: 1.6rem; }
  .gallery-nav.prev { left: 6px; }
  .gallery-nav.next { right: 6px; }
  .gallery-strip button { width: 52px; height: 52px; }
}`;

  var overlay, img, caption, counter, strip, prevBtn, nextBtn;
  var photos = [], index = 0, lastFocus = null, touchX = null;

  function build() {
    if (!document.getElementById("gallery-css")) {
      var style = document.createElement("style");
      style.id = "gallery-css";
      style.textContent = CSS;
      document.head.appendChild(style);
    }
    overlay = document.createElement("div");
    overlay.className = "gallery";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Photo viewer");
    overlay.hidden = true;
    overlay.innerHTML =
      '<button type="button" class="gallery-close" aria-label="Close">×</button>' +
      '<button type="button" class="gallery-nav prev" aria-label="Previous photo">‹</button>' +
      '<figure class="gallery-stage"><img alt=""><figcaption><span class="gallery-caption"></span>' +
      '<span class="gallery-counter"></span></figcaption></figure>' +
      '<button type="button" class="gallery-nav next" aria-label="Next photo">›</button>' +
      '<div class="gallery-strip" role="listbox" aria-label="All photos"></div>';
    document.body.appendChild(overlay);
    img = overlay.querySelector(".gallery-stage img");
    caption = overlay.querySelector(".gallery-caption");
    counter = overlay.querySelector(".gallery-counter");
    strip = overlay.querySelector(".gallery-strip");
    prevBtn = overlay.querySelector(".prev");
    nextBtn = overlay.querySelector(".next");

    overlay.querySelector(".gallery-close").addEventListener("click", close);
    prevBtn.addEventListener("click", function () { show(index - 1); });
    nextBtn.addEventListener("click", function () { show(index + 1); });
    overlay.addEventListener("click", function (e) { if (e.target === overlay) close(); });
    overlay.addEventListener("touchstart", function (e) { touchX = e.touches[0].clientX; }, { passive: true });
    overlay.addEventListener("touchend", function (e) {
      if (touchX === null) return;
      var dx = e.changedTouches[0].clientX - touchX;
      touchX = null;
      if (Math.abs(dx) > 40) show(index + (dx < 0 ? 1 : -1));
    });
    strip.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-i]");
      if (b) show(Number(b.dataset.i));
    });
  }

  function show(i) {
    if (!photos.length) return;
    index = (i + photos.length) % photos.length;  // wrap around
    var p = photos[index];
    img.src = p.src;
    img.alt = p.alt;
    caption.textContent = p.alt;
    counter.textContent = photos.length > 1 ? (index + 1) + " / " + photos.length : "";
    var many = photos.length > 1;
    prevBtn.hidden = nextBtn.hidden = strip.hidden = !many;
    Array.prototype.forEach.call(strip.children, function (b, n) {
      b.setAttribute("aria-selected", n === index ? "true" : "false");
      if (n === index) b.scrollIntoView({ block: "nearest", inline: "center" });
    });
  }

  function open(links, start) {
    if (!overlay) build();
    if (getComputedStyle(overlay).position !== "fixed") return false;  // styles missing: let the link open normally
    photos = links.map(function (a) {
      var t = a.querySelector("img");
      return { src: a.href, alt: (t && t.alt) || a.title || "Photo" };
    });
    strip.innerHTML = photos.map(function (p, n) {
      return '<button type="button" role="option" data-i="' + n + '" aria-label="Photo ' + (n + 1) + '">' +
             '<img src="' + p.src + '" alt="" loading="lazy"></button>';
    }).join("");
    lastFocus = document.activeElement;
    overlay.hidden = false;
    document.body.classList.add("gallery-open");
    show(start);
    overlay.querySelector(".gallery-close").focus();
    return true;
  }

  function close() {
    overlay.hidden = true;
    img.removeAttribute("src");
    document.body.classList.remove("gallery-open");
    if (lastFocus) lastFocus.focus();
  }

  document.addEventListener("click", function (e) {
    var a = e.target.closest(".thumbs a[href]");
    if (!a || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;  // allow open-in-new-tab
    var links = Array.prototype.slice.call(a.closest(".thumbs").querySelectorAll("a[href]"));
    if (open(links, links.indexOf(a))) e.preventDefault();
  });

  document.addEventListener("keydown", function (e) {
    if (!overlay || overlay.hidden) return;
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") show(index - 1);
    else if (e.key === "ArrowRight") show(index + 1);
  });
})();
