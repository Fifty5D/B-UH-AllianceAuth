(() => {
  "use strict";
  const root = document.getElementById("buh-archive");
  if (!root) return;
  const csrf = document.querySelector("#ar-csrf input[name=csrfmiddlewaretoken]")?.value || "";
  const toast = document.getElementById("ar-toast");
  let toastTimer;
  function show(message, error=false){if(!toast)return;toast.textContent=message;toast.className=`ar-toast show${error?" error":""}`;clearTimeout(toastTimer);toastTimer=setTimeout(()=>toast.classList.remove("show"),6000);}
  async function start(kind, button){const url=root.dataset[`${kind}Url`];if(!url)return;button.disabled=true;try{const response=await fetch(url,{method:"POST",headers:{"X-CSRFToken":csrf,Accept:"application/json"},credentials:"same-origin"});let data={};try{data=await response.json();}catch(_ignored){}if(!response.ok||!data.ok)throw new Error(data.error||`Request failed (HTTP ${response.status})`);show(`${data.job.kind_label} queued. The audit trail will update when it finishes.`);setTimeout(()=>window.location.reload(),5000);}catch(error){show(error.message,true);button.disabled=false;}}
  document.querySelectorAll(".ar-job").forEach(button=>button.addEventListener("click",()=>start(button.dataset.kind,button)));
})();
