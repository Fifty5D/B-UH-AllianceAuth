(() => {
  "use strict";
  const initialize = (picker) => {
    const search = picker.querySelector("[data-buh-recipient-search]");
    const options = [...picker.querySelectorAll("[data-buh-recipient-option]")];
    const count = picker.querySelector("[data-buh-recipient-count]");
    const refresh = () => {
      const checked = options.filter((item) => item.querySelector("input")?.checked).length;
      const visible = options.filter((item) => !item.hidden).length;
      count.textContent = `${checked} selected · ${visible} shown · ${options.length} available`;
    };
    search?.addEventListener("input", () => {
      const needle = search.value.trim().toLocaleLowerCase();
      options.forEach((item) => { item.hidden = Boolean(needle) && !item.dataset.search.includes(needle); });
      refresh();
    });
    picker.querySelector("[data-buh-recipient-select-visible]")?.addEventListener("click", () => {
      options.filter((item) => !item.hidden).forEach((item) => { const input = item.querySelector("input"); if (input) input.checked = true; });
      refresh();
    });
    picker.querySelector("[data-buh-recipient-clear]")?.addEventListener("click", () => {
      options.forEach((item) => { const input = item.querySelector("input"); if (input) input.checked = false; });
      refresh();
    });
    picker.addEventListener("change", refresh);
    refresh();
  };
  document.querySelectorAll("[data-buh-recipient-picker]").forEach(initialize);
})();
