(function () {
    "use strict";

    function mount(select) {
        if (!select || select.dataset.buhAccountSearchReady === "true") {
            return;
        }
        select.dataset.buhAccountSearchReady = "true";

        const search = document.createElement("input");
        search.type = "search";
        search.className = "vTextField buh-account-search";
        search.placeholder = select.dataset.searchPlaceholder || "Search Auth accounts…";
        search.autocomplete = "off";
        search.setAttribute("aria-label", search.placeholder);

        const status = document.createElement("small");
        status.className = "help buh-account-search-status";
        status.setAttribute("aria-live", "polite");

        select.parentNode.insertBefore(search, select);
        select.insertAdjacentElement("afterend", status);

        const options = Array.from(select.options);
        const applyFilter = function () {
            const needle = search.value.trim().toLocaleLowerCase();
            let matches = 0;
            options.forEach(function (option) {
                if (!option.value) {
                    option.hidden = false;
                    option.disabled = false;
                    return;
                }
                const matched = !needle || option.text.toLocaleLowerCase().includes(needle);
                const keepSelected = option.value === select.value;
                option.hidden = !matched && !keepSelected;
                option.disabled = !matched && !keepSelected;
                if (matched) {
                    matches += 1;
                }
            });
            status.textContent = needle ? `${matches} matching Auth account${matches === 1 ? "" : "s"}` : "";
        };

        search.addEventListener("input", applyFilter);
        search.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                search.value = "";
                applyFilter();
                search.focus();
            }
        });
    }

    function mountAll(root) {
        (root || document).querySelectorAll("select[data-buh-account-select]").forEach(mount);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () { mountAll(document); });
    } else {
        mountAll(document);
    }
})();
