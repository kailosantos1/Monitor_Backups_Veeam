// Painel: abas de status, busca e clientes que abrem/fecham. Recarrega a cada 60 s.
(function () {
  const root = document.getElementById("objs");
  if (root) {
    const rows = [...root.querySelectorAll("tr[data-status]")];
    const groups = [...root.querySelectorAll("section.grp")];
    const tabs = [...document.querySelectorAll("#f-status button")];
    const q = document.getElementById("f-q"), cl = document.getElementById("f-client");
    let status = new URLSearchParams(location.search).get("status") || "todos";
    // Clientes abertos sobrevivem só à atualização automática (reload). Ao
    // chegar de outra página ou pelo botão Voltar, tudo começa fechado.
    let manual = {};
    const save = () => { try { sessionStorage.setItem("abertos", JSON.stringify(manual)); } catch (e) {} };
    let navType = "navigate";
    try { navType = performance.getEntriesByType("navigation")[0].type; } catch (e) {}
    if (navType === "reload") {
      try { manual = JSON.parse(sessionStorage.getItem("abertos") || "{}"); } catch (e) {}
    } else {
      save();
    }

    function apply() {
      const text = q.value.trim().toLowerCase(), vis = {};
      rows.forEach(r => {
        let ok = status === "todos" || (status === "problemas" ? r.dataset.problem === "1"
                 : status === "executando" ? r.dataset.run === "1" : r.dataset.status === status);
        if (ok && cl.value) ok = r.dataset.client === cl.value;
        if (ok && text) ok = r.dataset.search.includes(text);
        r.classList.toggle("hide", !ok);
        if (ok) vis[r.dataset.client] = 1;
      });
      groups.forEach(g => {
        const c = g.dataset.client;
        g.classList.toggle("hide", !vis[c]);
        g.classList.toggle("open", !!manual[c]);  // só abre quando o usuário clica
      });
      root.querySelectorAll("tbody.sec").forEach(tb =>
        tb.classList.toggle("hide", !tb.querySelector("tr[data-status]:not(.hide)")));
      document.getElementById("f-empty").classList.toggle("hide", Object.keys(vis).length > 0);
      tabs.forEach(t => t.classList.toggle("on", t.dataset.filter === status));
      const u = new URL(location); u.searchParams.set("status", status); history.replaceState(null, "", u);
    }
    // Trocar de aba fecha todos os clientes.
    tabs.forEach(t => t.addEventListener("click", () => {
      if (t.dataset.filter !== status) { manual = {}; save(); }
      status = t.dataset.filter; apply();
    }));
    [q, cl].forEach(e => e.addEventListener("input", apply));
    groups.forEach(g => g.querySelector(".gh").addEventListener("click", () => { manual[g.dataset.client] = !g.classList.contains("open"); save(); apply(); }));
    // Voltar restaurado do cache do navegador (bfcache) não recarrega o script:
    // fecha tudo aqui.
    window.addEventListener("pageshow", e => { if (e.persisted) { manual = {}; save(); apply(); } });
    apply();
  }
  if (document.body.dataset.autorefresh === "1") {
    setInterval(() => {
      const a = document.activeElement;
      if (a && ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName)) return;
      location.reload();
    }, 60000);
  }
})();
