// Pythonator website: theme switch, docs navigation, heading anchors, code copy and colours, links to the code.
(function () {
  "use strict";
  var root = document.documentElement;
  var site = window.SITE || {repo: "cfunkz/Pythonator", base: "", path: ""};

  // -- light / dark ----------------------------------------------------------------------------------
  var toggle = document.querySelector(".theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var dark = root.dataset.theme ? root.dataset.theme === "dark"
        : window.matchMedia("(prefers-color-scheme: dark)").matches;
      root.dataset.theme = dark ? "light" : "dark";
      try { localStorage.setItem("theme", root.dataset.theme); } catch (e) { /* private window */ }
    });
  }

  // -- the docs menu on a phone ------------------------------------------------------------------------
  var sideToggle = document.querySelector(".sidebar-toggle");
  if (sideToggle) {
    sideToggle.addEventListener("click", function () {
      var open = document.getElementById("sidebar").classList.toggle("open");
      sideToggle.setAttribute("aria-expanded", String(open));
    });
  }

  var prose = document.querySelector(".prose");
  if (!prose) return;

  // -- links into the repository's code open it on GitHub ---------------------------------------------
  // The pages link to files next to them (../pythonator/core/runtimes.py, ../plugins/...): on the website those are
  // on GitHub. Links to other pages, anchors and other sites stay as they are.
  var pageDir = site.path.indexOf("/") >= 0 ? site.path.slice(0, site.path.lastIndexOf("/") + 1) : "";
  prose.querySelectorAll("a[href]").forEach(function (a) {
    var href = a.getAttribute("href");
    if (/^([a-z]+:|#|\/)/i.test(href) || /\.html(#.*)?$/.test(href)) return;
    var hash = href.indexOf("#") >= 0 ? href.slice(href.indexOf("#")) : "";  // #L120: the line in the code
    var parts = (pageDir + href.split("#")[0]).split("/"), out = [];
    parts.forEach(function (p) { if (p === "..") out.pop(); else if (p && p !== ".") out.push(p); });
    var path = out.join("/");
    if (/\.md$/i.test(path)) return;  // a page the site didn't convert: leave it
    var kind = /\.[a-z0-9]+$/i.test(path) ? "blob" : "tree";
    a.href = "https://github.com/" + site.repo + "/" + kind + "/main/" + path + hash;
  });
  prose.querySelectorAll("a[href^='http']").forEach(function (a) {
    if (a.host !== location.host) { a.target = "_blank"; a.rel = "noopener"; }
  });

  // -- heading anchors and "on this page" ---------------------------------------------------------------
  var toc = document.getElementById("toc");
  var headings = prose.querySelectorAll("h2[id], h3[id]");
  headings.forEach(function (h) {
    var label = h.textContent;  // before the "#" is added; includes text inside <code>
    var mark = document.createElement("a");
    mark.className = "anchor"; mark.href = "#" + h.id; mark.textContent = "#"; mark.setAttribute("aria-label", "Link to this section");
    h.appendChild(mark);
    if (toc) {
      var link = document.createElement("a");
      link.href = "#" + h.id;
      link.textContent = label;
      if (h.tagName === "H3") link.className = "sub";
      toc.appendChild(link);
    }
  });
  if (toc && "IntersectionObserver" in window && headings.length) {
    var links = {};
    toc.querySelectorAll("a").forEach(function (a) { links[a.getAttribute("href").slice(1)] = a; });
    var current = null;
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting && links[e.target.id]) {
          if (current) current.classList.remove("active");
          current = links[e.target.id]; current.classList.add("active");
        }
      });
    }, {rootMargin: "-70px 0px -70% 0px"});
    headings.forEach(function (h) { observer.observe(h); });
  }

  // -- code blocks: copy button and light colouring ----------------------------------------------------
  var KEYWORDS = {
    py: "and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield self",
    js: "async await break case catch class const continue default delete do else export extends false finally for from function if import in instanceof let new null of return switch this throw true try typeof undefined var void while yield",
    sh: "cd echo export if then fi for do done in sudo git pip python python3 pythonator npm cat"
  };
  function language(code) {
    var holder = code.closest("[class*='language-']");
    var m = /language-(\w+)/.exec(holder ? holder.className : "");
    var lang = m ? m[1].toLowerCase() : "";
    if (/^(python|py|pycon)$/.test(lang)) return "py";
    if (/^(js|javascript|ts|typescript|json)$/.test(lang)) return "js";
    if (/^(bash|sh|shell|console|powershell|ps1|text|toml|ini)$/.test(lang)) return "sh";
    return lang ? "sh" : "";
  }
  function escapeHtml(s) { return s.replace(/[&<>]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;"}[c]; }); }
  function colour(text, lang) {
    var words = new Set(KEYWORDS[lang].split(" "));
    var comment = lang === "js" ? "\\/\\/[^\\n]*" : "#[^\\n]*";
    var re = new RegExp("(" + comment + ")|(\"(?:\\\\.|[^\"\\\\\\n])*\"|'(?:\\\\.|[^'\\\\\\n])*'|`[^`]*`)|\\b(\\d+(?:\\.\\d+)?)\\b|([A-Za-z_][\\w]*)(\\s*\\()?|(@[\\w.]+)", "g");
    var out = "", last = 0, m;
    while ((m = re.exec(text))) {
      out += escapeHtml(text.slice(last, m.index));
      if (m[1]) out += '<span class="tok-c">' + escapeHtml(m[1]) + "</span>";
      else if (m[2]) out += '<span class="tok-s">' + escapeHtml(m[2]) + "</span>";
      else if (m[3]) out += '<span class="tok-n">' + m[3] + "</span>";
      else if (m[4]) {
        if (words.has(m[4])) out += '<span class="tok-k">' + m[4] + "</span>" + (m[5] || "");
        else if (m[5]) out += '<span class="tok-f">' + m[4] + "</span>" + m[5];
        else out += m[4];
      } else if (m[6]) out += '<span class="tok-p">' + escapeHtml(m[6]) + "</span>";
      last = re.lastIndex;
    }
    return out + escapeHtml(text.slice(last));
  }
  prose.querySelectorAll("pre").forEach(function (pre) {
    var code = pre.querySelector("code") || pre;
    var lang = language(code);
    if (lang && code.textContent.length < 20000) code.innerHTML = colour(code.textContent, lang);
    var button = document.createElement("button");
    button.type = "button"; button.className = "copy"; button.textContent = "Copy";
    button.addEventListener("click", function () {
      navigator.clipboard.writeText(code.textContent.replace(/\n$/, "")).then(function () {
        button.textContent = "Copied"; setTimeout(function () { button.textContent = "Copy"; }, 1400);
      });
    });
    pre.appendChild(button);
  });
})();
