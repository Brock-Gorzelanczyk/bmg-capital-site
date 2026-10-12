(function(){
  var d = document.documentElement, btn = null;
  var MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
  var SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4.5"/><path d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M4.9 19.1l1.8-1.8M17.3 6.7l1.8-1.8"/></svg>';
  function dark(){ return d.getAttribute("data-theme") === "dark"; }
  function show(){
    if (!btn) return;
    btn.innerHTML = dark() ? SUN : MOON;
    var l = dark() ? "Switch to light mode" : "Switch to dark mode";
    btn.setAttribute("aria-label", l); btn.title = l;
  }
  function set(t){
    if (t === "dark") d.setAttribute("data-theme", "dark"); else d.removeAttribute("data-theme");
    try { localStorage.setItem("bmg-theme", t); } catch (e) {}
    show();
  }
  function mount(){
    btn = document.createElement("button");
    btn.type = "button"; btn.className = "theme-toggle";
    btn.addEventListener("click", function(){ set(dark() ? "light" : "dark"); });
    var nav = document.querySelector("nav.site-nav") || document.querySelector(".site-header-inner");
    if (nav) nav.appendChild(btn); else { btn.className += " floating"; document.body.appendChild(btn); }
    show();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
})();
