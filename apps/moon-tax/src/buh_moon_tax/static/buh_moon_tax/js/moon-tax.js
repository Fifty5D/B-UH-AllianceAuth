(() => {
  "use strict";

  const setupAuditPolling = () => {
    const state = document.querySelector("[data-audit-url]");
    if (!state) return;
    const terminal = new Set(["COMPLETE", "WARNING", "FAILED"]);
    const poll = async () => {
      try {
        const response = await fetch(state.dataset.auditUrl, {
          headers: {"X-Requested-With": "XMLHttpRequest"},
        });
        if (response.ok) {
          const data = await response.json();
          const label = state.querySelector("[data-audit-label]");
          const summary = state.querySelector("[data-audit-summary]");
          if (label) label.textContent = data.status_label;
          state.className = `tax-audit-state ${data.status.toLowerCase()} mb-3`;
          if (summary && data.summary && Object.keys(data.summary).length) {
            summary.innerHTML = `<span>${data.summary.periods || 0} pulls</span><span>${data.summary.mining_lines_processed || 0} ledger rows</span><span>${data.summary.new_payment_candidates || 0} new payments</span>`;
          }
          if (terminal.has(data.status)) {
            window.setTimeout(() => window.location.reload(), 900);
            return;
          }
        }
      } catch (_) {
        // A temporary proxy or restart failure should not break the dashboard.
      }
      window.setTimeout(poll, 5000);
    };
    window.setTimeout(poll, 3000);
  };

  const setupPaymentFilters = () => {
    const panel = document.querySelector("[data-payment-filters]");
    if (!panel) return;
    const toggle = panel.querySelector("[data-toggle-advanced]");
    const advanced = panel.querySelector("[data-advanced-filters]");
    if (toggle && advanced) {
      toggle.addEventListener("click", () => {
        const open = advanced.classList.toggle("is-open");
        toggle.setAttribute("aria-expanded", String(open));
      });
    }
    document.querySelectorAll("[data-filter-member]").forEach((button) => {
      button.addEventListener("click", () => {
        const search = panel.querySelector("[data-payment-search]");
        if (!search) return;
        search.value = button.dataset.filterMember || "";
        search.form.submit();
      });
    });
  };

  const setupDensityToggle = () => {
    const list = document.querySelector("[data-payment-list]");
    const toggle = document.querySelector("[data-density-toggle]");
    if (!list || !toggle) return;
    const apply = (compact) => {
      list.classList.toggle("is-compact", compact);
      toggle.innerHTML = compact
        ? '<i class="fa-solid fa-expand"></i> Comfortable'
        : '<i class="fa-solid fa-compress"></i> Compact';
      toggle.setAttribute("aria-pressed", String(compact));
    };
    let compact = false;
    try {
      compact = window.localStorage.getItem("buh-moon-tax-payment-density") === "compact";
    } catch (_) {
      // Storage can be unavailable in hardened/private browser modes.
    }
    apply(compact);
    toggle.addEventListener("click", () => {
      compact = !list.classList.contains("is-compact");
      apply(compact);
      try {
        window.localStorage.setItem(
          "buh-moon-tax-payment-density",
          compact ? "compact" : "comfortable",
        );
      } catch (_) {
        // The preference is optional; the toggle still works for this page load.
      }
    });
  };

  const setupPaymentSelection = () => {
    const bulkForm = document.querySelector("#tax-bulk-form");
    const toolbar = document.querySelector("[data-bulk-toolbar]");
    const checkboxes = Array.from(document.querySelectorAll(".tax-payment-select"));
    if (!bulkForm || !toolbar || !checkboxes.length) return;

    const selectVisible = document.querySelector("[data-select-visible]");
    const countNode = toolbar.querySelector("[data-selected-count]");
    const totalNode = toolbar.querySelector("[data-selected-total]");
    const amountFormatter = new Intl.NumberFormat("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    let lastChecked = null;

    const selected = () => checkboxes.filter((checkbox) => checkbox.checked);
    const updateToolbar = () => {
      const chosen = selected();
      const total = chosen.reduce(
        (sum, checkbox) => sum + Number.parseFloat(checkbox.dataset.amount || "0"),
        0,
      );
      toolbar.hidden = chosen.length === 0;
      if (countNode) countNode.textContent = String(chosen.length);
      if (totalNode) totalNode.textContent = amountFormatter.format(total);
      if (selectVisible) {
        selectVisible.checked = chosen.length === checkboxes.length;
        selectVisible.indeterminate = chosen.length > 0 && chosen.length < checkboxes.length;
      }
    };

    checkboxes.forEach((checkbox) => {
      checkbox.addEventListener("click", (event) => {
        if (event.shiftKey && lastChecked) {
          const start = checkboxes.indexOf(lastChecked);
          const end = checkboxes.indexOf(checkbox);
          const [low, high] = start < end ? [start, end] : [end, start];
          for (let index = low; index <= high; index += 1) {
            checkboxes[index].checked = checkbox.checked;
          }
        }
        lastChecked = checkbox;
        updateToolbar();
      });
    });

    if (selectVisible) {
      selectVisible.addEventListener("change", () => {
        checkboxes.forEach((checkbox) => {
          checkbox.checked = selectVisible.checked;
        });
        updateToolbar();
      });
    }

    document.querySelectorAll("[data-select-member]").forEach((button) => {
      button.addEventListener("click", () => {
        const key = button.dataset.selectMember;
        checkboxes.forEach((checkbox) => {
          if (checkbox.dataset.memberKey === key) checkbox.checked = true;
        });
        updateToolbar();
        toolbar.scrollIntoView({behavior: "smooth", block: "nearest"});
      });
    });

    toolbar.querySelector("[data-clear-selection]")?.addEventListener("click", () => {
      checkboxes.forEach((checkbox) => {
        checkbox.checked = false;
      });
      lastChecked = null;
      updateToolbar();
    });

    bulkForm.addEventListener("submit", (event) => {
      const chosen = selected();
      if (!chosen.length) {
        event.preventDefault();
        return;
      }
      if (bulkForm.dataset.submitted === "true") {
        event.preventDefault();
        return;
      }
      const submitter = event.submitter;
      if (submitter?.hasAttribute("data-confirm-bulk")) {
        const decision = submitter.dataset.bulkDecision || "change";
        const total = totalNode?.textContent || "0.00";
        const confirmed = window.confirm(
          `${decision.charAt(0).toUpperCase()}${decision.slice(1)} ${chosen.length} selected payments totaling ${total} ISK?\n\nThis can be corrected later from the matching status tab.`,
        );
        if (!confirmed) {
          event.preventDefault();
          return;
        }
      }
      bulkForm.dataset.submitted = "true";
      toolbar.classList.add("is-submitting");
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && selected().length) {
        checkboxes.forEach((checkbox) => {
          checkbox.checked = false;
        });
        updateToolbar();
      }
    });

    updateToolbar();
  };

  const announce = (message, kind = "success") => {
    const region = document.querySelector("[data-tax-live-region]");
    if (!region) return;
    region.textContent = message;
    region.className = `tax-live-region is-visible ${kind}`;
    window.setTimeout(() => region.classList.remove("is-visible"), 5000);
  };

  const decisionNeedsConfirmation = (decision) =>
    ({DONATION: "classify as a donation", IGNORE: "ignore", REJECT: "reject"})[
      decision
    ];

  const submitPaymentDecision = async (card, decision, submitter = null) => {
    const form = card?.querySelector("[data-payment-decision-form]");
    if (!form || form.dataset.submitted === "true") return;
    const confirmation = decisionNeedsConfirmation(decision);
    if (
      confirmation &&
      !window.confirm(
        `Are you sure you want to ${confirmation} this payment? You can correct it later from its status tab.`,
      )
    ) {
      return;
    }
    const data = new FormData(form);
    data.set("decision", decision);
    form.dataset.submitted = "true";
    form.classList.add("is-submitting");
    card.classList.add("is-processing");
    if (submitter) submitter.disabled = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: data,
        credentials: "same-origin",
        headers: {"X-Requested-With": "XMLHttpRequest"},
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || "The payment decision was not saved.");
      }
      card.classList.remove("is-processing");
      card.classList.add("is-complete");
      announce(payload.message || "Payment decision saved.");
      window.setTimeout(() => window.location.reload(), 650);
    } catch (error) {
      form.dataset.submitted = "false";
      form.classList.remove("is-submitting");
      card.classList.remove("is-processing");
      if (submitter) submitter.disabled = false;
      announce(error.message || "The payment decision was not saved.", "error");
    }
  };

  const setupDecisionSafety = () => {
    document.querySelectorAll("[data-payment-decision-form]").forEach((form) => {
      form.querySelectorAll('button[name="decision"]').forEach((button) => {
        button.addEventListener("click", () => {
          // event.submitter is supported by current browsers, but keeping the
          // clicked value also protects the native form flow in older webviews.
          form.dataset.pendingDecision = button.value;
        });
      });
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const submitter = event.submitter;
        const decision = submitter?.value || form.dataset.pendingDecision;
        delete form.dataset.pendingDecision;
        if (!decision) {
          announce("Choose a payment decision and try again.", "error");
          return;
        }
        submitPaymentDecision(form.closest("[data-payment-card]"), decision, submitter);
      });
    });
  };

  const setupPolicyWorkspace = () => {
    const policySearch = document.querySelector("[data-policy-search]");
    if (policySearch) {
      policySearch.addEventListener("input", () => {
        const query = policySearch.value.trim().toLowerCase();
        document.querySelectorAll("[data-policy-card]").forEach((card) => {
          card.hidden = query && !card.dataset.policyName.includes(query);
        });
      });
    }
    document.querySelectorAll("[data-recipient-picker]").forEach((picker) => {
      const search = picker.querySelector("[data-recipient-search]");
      if (search) {
        search.addEventListener("input", () => {
          const query = search.value.trim().toLowerCase();
          picker.querySelectorAll("[data-recipient-option], .tax-recipient-checks li").forEach(
            (option) => {
              option.hidden = query && !option.textContent.toLowerCase().includes(query);
            },
          );
        });
      }
      const inherit = picker.querySelector("[data-inherit-recipients]");
      const area = picker.querySelector("[data-recipient-area]");
      if (inherit && area) {
        const apply = () => {
          area.classList.toggle("is-disabled", inherit.checked);
          area.querySelectorAll('input[type="checkbox"]').forEach((input) => {
            input.disabled = inherit.checked;
          });
        };
        inherit.addEventListener("change", apply);
        apply();
      }
    });
  };

  const setupBillAdjustments = () => {
    document.querySelectorAll("[data-bill-toggle]").forEach((button) => {
      const row = document.getElementById(button.dataset.billToggle);
      if (!row) return;
      button.setAttribute("aria-controls", row.id);
      button.setAttribute("aria-expanded", String(!row.hidden));
      button.addEventListener("click", () => {
        row.hidden = !row.hidden;
        button.setAttribute("aria-expanded", String(!row.hidden));
        if (!row.hidden) row.scrollIntoView({behavior: "smooth", block: "nearest"});
      });
    });
    document.querySelectorAll("[data-confirm-void]").forEach((button) => {
      button.addEventListener("click", (event) => {
        if (
          !window.confirm(
            "Void this entire bill? The mining record stays in history and the decision can be reversed.",
          )
        ) {
          event.preventDefault();
        }
      });
    });
  };

  const setupRowNavigation = () => {
    const interactive =
      "a, button, input, select, textarea, label, form, details, summary, [data-row-nav-ignore]";
    document.querySelectorAll("[data-row-url]").forEach((row) => {
      const navigate = () => window.location.assign(row.dataset.rowUrl);
      row.addEventListener("click", (event) => {
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey ||
          event.target.closest(interactive) ||
          window.getSelection()?.toString()
        ) {
          return;
        }
        navigate();
      });
      row.addEventListener("keydown", (event) => {
        if (
          event.defaultPrevented ||
          event.target !== row ||
          event.repeat ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey ||
          (event.key !== "Enter" && event.key !== " ")
        ) {
          return;
        }
        event.preventDefault();
        navigate();
      });
    });
  };

  const setupSortableTables = () => {
    const readValue = (row, column, type) => {
      const cell = row.cells[column];
      if (!cell) return {empty: true, value: ""};
      const raw = (cell.dataset.sortValue ?? cell.textContent).trim();
      if (!raw) return {empty: true, value: ""};
      if (type === "number") {
        const numeric = Number(raw.replaceAll(",", "").replace(/[\s%ISK]+$/gi, ""));
        return Number.isFinite(numeric)
          ? {empty: false, value: numeric}
          : {empty: true, value: ""};
      }
      if (type === "date") {
        const timestamp = Date.parse(raw.replace(" UTC", "Z"));
        return Number.isFinite(timestamp)
          ? {empty: false, value: timestamp}
          : {empty: true, value: ""};
      }
      return {empty: false, value: raw.toLocaleLowerCase()};
    };

    document.querySelectorAll("[data-sortable-table]").forEach((table) => {
      const headers = Array.from(table.tHead?.rows[0]?.cells || []);
      const body = table.tBodies[0];
      if (!body) return;
      headers.forEach((header, column) => {
        if (header.dataset.sortDisabled !== undefined || header.colSpan > 1) return;
        const labelText = header.textContent.trim();
        if (!labelText) return;
        const requestedType = header.dataset.sortType || "text";
        const type = ["text", "number", "date"].includes(requestedType)
          ? requestedType
          : "text";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "tax-sort-button";
        button.setAttribute("aria-label", `${labelText}: sort ascending`);
        const label = document.createElement("span");
        label.textContent = labelText;
        const icon = document.createElement("i");
        icon.className = "fa-solid fa-sort";
        icon.setAttribute("aria-hidden", "true");
        button.append(label, icon);
        header.replaceChildren(button);

        button.addEventListener("click", () => {
          const nextDirection =
            header.getAttribute("aria-sort") === "ascending" ? "descending" : "ascending";
          headers.forEach((item) => {
            item.removeAttribute("aria-sort");
            const itemIcon = item.querySelector(".tax-sort-button i");
            if (itemIcon) itemIcon.className = "fa-solid fa-sort";
          });
          header.setAttribute("aria-sort", nextDirection);
          button.setAttribute(
            "aria-label",
            `${labelText}: sort ${
              nextDirection === "ascending" ? "descending" : "ascending"
            }`,
          );
          icon.className = `fa-solid ${
            nextDirection === "ascending" ? "fa-sort-up" : "fa-sort-down"
          }`;

          const groups = [];
          Array.from(body.rows).forEach((row) => {
            if (row.dataset.sortChild !== undefined && groups.length) {
              groups.at(-1).rows.push(row);
              return;
            }
            groups.push({rows: [row], index: groups.length});
          });
          groups.sort((left, right) => {
            const a = readValue(left.rows[0], column, type);
            const b = readValue(right.rows[0], column, type);
            if (a.empty !== b.empty) return a.empty ? 1 : -1;
            if (a.empty && b.empty) return left.index - right.index;
            let comparison = 0;
            if (type === "text") {
              comparison = a.value.localeCompare(b.value, undefined, {
                numeric: true,
                sensitivity: "base",
              });
            } else {
              comparison = a.value - b.value;
            }
            // Equal values keep their current visual order in both directions.
            // Negating the tie-breaker would make tied people jump around each
            // time a director toggles the same heading.
            if (!comparison) return left.index - right.index;
            return nextDirection === "ascending" ? comparison : -comparison;
          });
          const fragment = document.createDocumentFragment();
          groups.forEach((group) => group.rows.forEach((row) => fragment.append(row)));
          body.append(fragment);
        });
      });
    });
  };

  setupAuditPolling();
  setupPaymentFilters();
  setupDensityToggle();
  setupPaymentSelection();
  setupDecisionSafety();
  setupPolicyWorkspace();
  setupBillAdjustments();
  setupRowNavigation();
  setupSortableTables();
})();
