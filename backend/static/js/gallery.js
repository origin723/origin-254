/* In-app photo viewer: click a thumbnail to open it over the page and move through
   that listing's photos (arrows, keyboard, swipe, filmstrip). Without JS the
   thumbnail links still open the photo directly. */
(function () {
  "use strict";

  var overlay, img, caption, counter, strip, prevBtn, nextBtn;
  var photos = [], index = 0, lastFocus = null, touchX = null;

  function build() {
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
    e.preventDefault();
    open(links, links.indexOf(a));
  });

  document.addEventListener("keydown", function (e) {
    if (!overlay || overlay.hidden) return;
    if (e.key === "Escape") close();
    else if (e.key === "ArrowLeft") show(index - 1);
    else if (e.key === "ArrowRight") show(index + 1);
  });
})();
