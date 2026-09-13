(() => {
  "use strict";
  const root = document.getElementById("buh-archive");
  if (!root) return;
  const collator = new Intl.Collator(undefined, {numeric: true, sensitivity: "base"});
  root.querySelectorAll("table[data-history-sort]").forEach(table => {
    table.querySelectorAll("thead th").forEach((heading, index) => {
      const label = heading.textContent.trim();
      if (!label) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-link text-reset p-0 text-start";
      button.textContent = label + " ↕";
      button.setAttribute("aria-label", "Sort " + label);
      heading.replaceChildren(button);
      heading.setAttribute("aria-sort", "none");
      button.addEventListener("click", () => {
        const direction = heading.getAttribute("aria-sort") === "ascending" ? -1 : 1;
        table.querySelectorAll("thead th[aria-sort]").forEach(th => th.setAttribute("aria-sort", "none"));
        heading.setAttribute("aria-sort", direction === 1 ? "ascending" : "descending");
        const body = table.tBodies[0];
        const rows = [...body.rows];
        if (rows.some(row => row.cells.length !== table.tHead.rows[0].cells.length)) return;
        rows.sort((a, b) => direction * collator.compare(
          a.cells[index].dataset.sortValue || a.cells[index].textContent.trim(),
          b.cells[index].dataset.sortValue || b.cells[index].textContent.trim()
        ));
        body.append(...rows);
      });
    });
  });
  const csrf = document.querySelector("#ar-csrf input[name=csrfmiddlewaretoken]")?.value || "";
  const toast = document.getElementById("ar-toast");
  let toastTimer;
  function show(message, error=false){if(!toast)return;toast.textContent=message;toast.className=`ar-toast show${error?" error":""}`;clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.classList.remove("show"),6000);}
  async function start(kind, button){const url=root.dataset[`${kind}Url`];if(!url)return;button.disabled=true;try{const response=await fetch(url,{method:"POST",headers:{"X-CSRFToken":csrf,Accept:"application/json"},credentials:"same-origin"});let data={};try{data=await response.json();}catch(_ignored){}if(!response.ok||!data.ok)throw new Error(data.error||`Request failed (HTTP ${response.status})`);show(`${data.job.kind_label} queued. The audit trail will update when it finishes.`);setTimeout(()=>window.location.reload(),5000);}catch(error){show(error.message,true);button.disabled=false;}}
  document.querySelectorAll(".ar-job").forEach(button=>button.addEventListener("click",()=>start(button.dataset.kind,button)));
})();
