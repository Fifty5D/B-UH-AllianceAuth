(() => {
    "use strict";

    const root = document.getElementById("buh-mining-app");
    if (!root) return;

    const $ = (selector) => root.querySelector(selector);
    const $$ = (selector) => Array.from(root.querySelectorAll(selector));
    const initialOptionsElement = document.getElementById("mining-initial-options");

    const state = {
        scope: root.dataset.initialScope,
        metric: "isk",
        options: JSON.parse(initialOptionsElement.textContent),
        draftCharacters: new Set(),
        appliedCharacters: new Set(),
        data: null,
        requestController: null,
    };

    const metricLabels = {
        isk: "Estimated ISK",
        volume: "Raw volume",
        units: "Ore units",
    };

    const metricShortLabels = {
        isk: "ISK",
        volume: "m³",
        units: "units",
    };

    function compactNumber(value, digits = 1) {
        const number = Number(value || 0);
        return new Intl.NumberFormat("en-US", {
            notation: Math.abs(number) >= 1000 ? "compact" : "standard",
            maximumFractionDigits: digits,
        }).format(number);
    }

    function fullNumber(value, digits = 0) {
        return new Intl.NumberFormat("en-US", {
            maximumFractionDigits: digits,
        }).format(Number(value || 0));
    }

    function formatMetric(value, metric = state.metric, compact = true) {
        const formatter = compact ? compactNumber : fullNumber;
        if (metric === "isk") return `${formatter(value, compact ? 1 : 0)} ISK`;
        if (metric === "volume") return `${formatter(value, compact ? 1 : 2)} m³`;
        return `${formatter(value, compact ? 1 : 0)} units`;
    }

    function formatDate(value, options = {}) {
        if (!value) return "—";
        const date = new Date(`${value.substring(0, 10)}T00:00:00Z`);
        return new Intl.DateTimeFormat("en-US", {
            month: "short",
            day: "numeric",
            year: options.year ? "numeric" : undefined,
            timeZone: "UTC",
        }).format(date);
    }

    function relativeTime(value) {
        if (!value) return "Never refreshed";
        const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
        if (seconds < 90) return "Updated just now";
        const minutes = Math.round(seconds / 60);
        if (minutes < 90) return `Updated ${minutes}m ago`;
        const hours = Math.round(minutes / 60);
        if (hours < 48) return `Updated ${hours}h ago`;
        return `Updated ${Math.round(hours / 24)}d ago`;
    }

    function htmlEscape(value) {
        const element = document.createElement("span");
        element.textContent = String(value ?? "");
        return element.innerHTML;
    }

    function showAlert(message, type = "info") {
        const alert = $("#mining-alert");
        alert.textContent = message;
        alert.className = `mining-alert is-${type}`;
        alert.classList.remove("d-none");
        window.clearTimeout(showAlert.timer);
        showAlert.timer = window.setTimeout(() => alert.classList.add("d-none"), 9000);
    }

    function setLoading(loading) {
        $("#mining-loading").classList.toggle("is-hidden", !loading);
    }

    function selectedOptions() {
        return state.options.characters.filter((item) => state.draftCharacters.has(item.id));
    }

    function selectedAccountIds() {
        return new Set(selectedOptions().map((item) => item.account_id));
    }

    function setAllCharacters(selected) {
        state.draftCharacters = selected
            ? new Set(state.options.characters.map((item) => item.id))
            : new Set();
        syncSelectionUI();
    }

    function setAccountCharacters(accountId, selected) {
        for (const character of state.options.characters) {
            if (character.account_id !== accountId) continue;
            if (selected) state.draftCharacters.add(character.id);
            else state.draftCharacters.delete(character.id);
        }
        syncSelectionUI();
    }

    function createCheckRow({ id, name, subtitle, checked, disabledBadge, kind, search }) {
        const label = document.createElement("label");
        label.className = "mining-check-row";
        label.dataset.search = search.toLocaleLowerCase();

        const input = document.createElement("input");
        input.type = "checkbox";
        input.className = "form-check-input mt-0";
        input.checked = checked;
        input.dataset.id = String(id);
        input.dataset.kind = kind;
        label.appendChild(input);

        const copy = document.createElement("span");
        copy.className = "mining-check-copy";
        const strong = document.createElement("strong");
        strong.textContent = name;
        const small = document.createElement("small");
        small.textContent = subtitle;
        copy.append(strong, small);
        label.appendChild(copy);

        if (disabledBadge) {
            const badge = document.createElement("span");
            badge.className = "mining-disabled-badge";
            badge.textContent = "Not updating";
            label.appendChild(badge);
        }
        return label;
    }

    function renderSelectors() {
        const accountList = $("#account-list");
        const characterList = $("#character-list");
        accountList.replaceChildren();
        characterList.replaceChildren();
        const activeAccounts = selectedAccountIds();

        for (const account of state.options.accounts) {
            const accountCharacters = state.options.characters.filter((item) => item.account_id === account.id);
            const selectedCount = accountCharacters.filter((item) => state.draftCharacters.has(item.id)).length;
            const row = createCheckRow({
                id: account.id,
                name: account.name,
                subtitle: `${account.character_count} registered character${account.character_count === 1 ? "" : "s"}`,
                checked: selectedCount > 0,
                kind: "account",
                search: account.name,
            });
            const checkbox = row.querySelector("input");
            checkbox.indeterminate = selectedCount > 0 && selectedCount < accountCharacters.length;
            checkbox.addEventListener("change", () => setAccountCharacters(account.id, checkbox.checked));
            accountList.appendChild(row);
        }

        for (const character of state.options.characters) {
            const row = createCheckRow({
                id: character.id,
                name: character.name,
                subtitle: `${character.account_name} · ${character.corporation_name || "Unknown corporation"}`,
                checked: state.draftCharacters.has(character.id),
                disabledBadge: character.disabled,
                kind: "character",
                search: `${character.name} ${character.account_name} ${character.corporation_name || ""}`,
            });
            row.querySelector("input").addEventListener("change", (event) => {
                if (event.target.checked) state.draftCharacters.add(character.id);
                else state.draftCharacters.delete(character.id);
                syncSelectionUI(false);
            });
            characterList.appendChild(row);
        }
        updateSelectionText(activeAccounts);
        applySearchFilter("#account-search", "#account-list");
        applySearchFilter("#character-search", "#character-list");
    }

    function syncSelectionUI(rerender = true) {
        if (rerender) {
            renderSelectors();
            return;
        }
        const activeAccounts = selectedAccountIds();
        for (const checkbox of $$('#character-list input[data-kind="character"]')) {
            checkbox.checked = state.draftCharacters.has(Number(checkbox.dataset.id));
        }
        for (const checkbox of $$('#account-list input[data-kind="account"]')) {
            const accountId = Number(checkbox.dataset.id);
            const members = state.options.characters.filter((item) => item.account_id === accountId);
            const selected = members.filter((item) => state.draftCharacters.has(item.id)).length;
            checkbox.checked = selected > 0;
            checkbox.indeterminate = selected > 0 && selected < members.length;
        }
        updateSelectionText(activeAccounts);
    }

    function updateSelectionText(activeAccounts = selectedAccountIds()) {
        const count = state.draftCharacters.size;
        const total = state.options.characters.length;
        let summary = "No characters";
        if (count === total && total) summary = `All ${total} characters`;
        else if (count) summary = `${count} of ${total} characters`;
        $("#mining-selection-summary").textContent = summary;
        $("#mining-selection-detail").textContent = count
            ? `${count} character${count === 1 ? "" : "s"} across ${activeAccounts.size} Auth account${activeAccounts.size === 1 ? "" : "s"} selected.`
            : "No characters selected. Apply to show an empty dashboard.";
    }

    function applySearchFilter(inputSelector, listSelector) {
        const needle = $(inputSelector).value.trim().toLocaleLowerCase();
        for (const row of $$(`${listSelector} .mining-check-row`)) {
            row.hidden = Boolean(needle) && !row.dataset.search.includes(needle);
        }
    }

    function selectionQuery(params = new URLSearchParams()) {
        params.set("scope", state.scope);
        const allSelected = state.appliedCharacters.size === state.options.characters.length;
        if (!allSelected) {
            params.set("characters", Array.from(state.appliedCharacters).sort((a, b) => a - b).join(","));
        }
        params.set("preset", $("#mining-preset").value);
        if ($("#mining-preset").value === "custom") {
            params.set("start", $("#mining-start").value);
            params.set("end", $("#mining-end").value);
        }
        params.set("comparison", $("#mining-comparison").value);
        return params;
    }

    async function fetchJson(url, options = {}) {
        const { headers: optionHeaders = {}, ...restOptions } = options;
        const response = await fetch(url, {
            credentials: "same-origin",
            headers: { "X-Requested-With": "XMLHttpRequest", ...optionHeaders },
            ...restOptions,
        });
        let payload;
        try {
            payload = await response.json();
        } catch (_error) {
            payload = { error: `Server returned ${response.status}.` };
        }
        if (!response.ok) throw new Error(payload.error || `Request failed (${response.status}).`);
        return payload;
    }

    async function loadOptions(scope) {
        setLoading(true);
        try {
            const url = new URL(root.dataset.optionsUrl, window.location.origin);
            url.searchParams.set("scope", scope);
            state.options = await fetchJson(url);
            state.scope = scope;
            state.draftCharacters = new Set(state.options.characters.map((item) => item.id));
            state.appliedCharacters = new Set(state.draftCharacters);
            $("#account-search").value = "";
            $("#character-search").value = "";
            renderSelectors();
            await loadData();
        } catch (error) {
            showAlert(error.message, "error");
        } finally {
            setLoading(false);
        }
    }

    async function loadData() {
        if ($("#mining-preset").value === "custom" && (!$("#mining-start").value || !$("#mining-end").value)) {
            return;
        }
        if (state.requestController) state.requestController.abort();
        state.requestController = new AbortController();
        setLoading(true);
        try {
            const url = new URL(root.dataset.dataUrl, window.location.origin);
            url.search = selectionQuery().toString();
            state.data = await fetchJson(url, { signal: state.requestController.signal });
            renderDashboard();
        } catch (error) {
            if (error.name !== "AbortError") showAlert(error.message, "error");
        } finally {
            setLoading(false);
        }
    }

    function changeMarkup(change, fallback) {
        if (change === null || change === undefined) return fallback;
        const direction = change >= 0 ? "up" : "down";
        const icon = change >= 0 ? "fa-arrow-trend-up" : "fa-arrow-trend-down";
        return `<span class="mining-change-${direction}"><i class="fa-solid ${icon}"></i> ${Math.abs(change)}%</span> vs previous range`;
    }

    function renderKpis() {
        const kpi = state.data.kpis;
        $("#kpi-value").textContent = formatMetric(kpi.total.isk, "isk");
        $("#kpi-volume").textContent = formatMetric(kpi.total.volume, "volume");
        $("#kpi-units").textContent = formatMetric(kpi.total.units, "units");
        $("#kpi-value-change").innerHTML = changeMarkup(kpi.change.isk, "Current EVE average prices");
        $("#kpi-volume-change").innerHTML = changeMarkup(kpi.change.volume, "Before compression");
        $("#kpi-unit-change").innerHTML = changeMarkup(kpi.change.units, "Across all ore types");
        $("#kpi-active-days").textContent = fullNumber(kpi.active_days);
        $("#kpi-active-days-sub").textContent = `${fullNumber(kpi.calendar_days)} calendar days selected`;
        $("#kpi-pace").textContent = formatMetric(kpi.pace_30_days[state.metric], state.metric);
        $("#kpi-streak").textContent = `${fullNumber(kpi.longest_streak)} day${kpi.longest_streak === 1 ? "" : "s"}`;
        $("#kpi-streak-sub").textContent = kpi.current_streak
            ? `Current streak: ${kpi.current_streak} day${kpi.current_streak === 1 ? "" : "s"}`
            : "Consecutive mining days";
    }

    function canvasContext(canvas) {
        const rect = canvas.getBoundingClientRect();
        const width = Math.max(1, rect.width);
        const height = Math.max(1, rect.height);
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
        const context = canvas.getContext("2d");
        context.setTransform(ratio, 0, 0, ratio, 0, 0);
        return { context, width, height };
    }

    function canvasColors() {
        const body = getComputedStyle(document.body);
        const muted = getComputedStyle(root).getPropertyValue("--mining-muted").trim() || "#7d8994";
        return {
            text: body.color || "#dce7e7",
            muted,
            grid: body.color.includes("255") ? "rgba(255,255,255,.1)" : "rgba(100,120,125,.18)",
        };
    }

    function drawTrendChart() {
        const chart = state.data.trend;
        const canvas = $("#trend-chart");
        const { context: ctx, width, height } = canvasContext(canvas);
        const colors = canvasColors();
        const values = chart.series.flatMap((series) => series.values.map((item) => Number(item[state.metric] || 0)));
        const maxValue = Math.max(...values, 0);
        $("#trend-empty").classList.toggle("d-none", maxValue > 0);
        ctx.clearRect(0, 0, width, height);

        const margin = { left: width < 500 ? 48 : 68, right: 14, top: 15, bottom: 34 };
        const plotWidth = Math.max(1, width - margin.left - margin.right);
        const plotHeight = Math.max(1, height - margin.top - margin.bottom);
        const ceiling = maxValue || 1;
        ctx.font = "11px system-ui, sans-serif";
        ctx.textBaseline = "middle";

        for (let index = 0; index <= 4; index += 1) {
            const y = margin.top + (plotHeight / 4) * index;
            const tick = ceiling * (1 - index / 4);
            ctx.strokeStyle = colors.grid;
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(margin.left, y);
            ctx.lineTo(width - margin.right, y);
            ctx.stroke();
            ctx.fillStyle = colors.muted;
            ctx.textAlign = "right";
            ctx.fillText(compactNumber(tick), margin.left - 8, y);
        }

        const labelCount = chart.labels.length;
        const step = labelCount > 1 ? plotWidth / (labelCount - 1) : plotWidth;
        const wantedLabels = Math.min(width < 500 ? 4 : 7, labelCount);
        const labelEvery = Math.max(1, Math.ceil(labelCount / wantedLabels));
        chart.labels.forEach((label, index) => {
            if (index % labelEvery && index !== labelCount - 1) return;
            const x = margin.left + (labelCount > 1 ? index * step : plotWidth / 2);
            ctx.fillStyle = colors.muted;
            ctx.textAlign = index === 0 ? "left" : index === labelCount - 1 ? "right" : "center";
            ctx.fillText(formatDate(label), x, height - 12);
        });

        chart.series.forEach((series, seriesIndex) => {
            const points = series.values.map((item, index) => ({
                x: margin.left + (labelCount > 1 ? index * step : plotWidth / 2),
                y: margin.top + plotHeight - (Number(item[state.metric] || 0) / ceiling) * plotHeight,
            }));
            if (!points.length) return;

            if (chart.series.length === 1 && maxValue) {
                const gradient = ctx.createLinearGradient(0, margin.top, 0, margin.top + plotHeight);
                gradient.addColorStop(0, `${series.color}45`);
                gradient.addColorStop(1, `${series.color}00`);
                ctx.beginPath();
                ctx.moveTo(points[0].x, margin.top + plotHeight);
                points.forEach((point) => ctx.lineTo(point.x, point.y));
                ctx.lineTo(points[points.length - 1].x, margin.top + plotHeight);
                ctx.closePath();
                ctx.fillStyle = gradient;
                ctx.fill();
            }

            ctx.beginPath();
            points.forEach((point, index) => index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y));
            ctx.strokeStyle = series.color;
            ctx.lineWidth = seriesIndex === 0 ? 2.4 : 1.8;
            ctx.lineJoin = "round";
            ctx.lineCap = "round";
            ctx.stroke();

            if (points.length <= 35) {
                for (const point of points) {
                    ctx.beginPath();
                    ctx.arc(point.x, point.y, 2.3, 0, Math.PI * 2);
                    ctx.fillStyle = series.color;
                    ctx.fill();
                }
            }
        });

        $("#trend-legend").innerHTML = chart.series.map((series) =>
            `<span><i class="mining-swatch" style="background:${series.color}"></i>${htmlEscape(series.label)}</span>`
        ).join("");
    }

    function drawDonut() {
        const canvas = $("#ore-chart");
        const { context: ctx, width, height } = canvasContext(canvas);
        const items = state.data.ore_mix;
        const values = items.map((item) => Number(item.values[state.metric] || 0));
        const total = values.reduce((sum, value) => sum + value, 0);
        const centerX = width / 2;
        const centerY = height / 2;
        const radius = Math.max(20, Math.min(width, height) / 2 - 24);
        ctx.clearRect(0, 0, width, height);
        ctx.lineWidth = Math.max(18, radius * .28);
        ctx.lineCap = "butt";

        if (!total) {
            ctx.beginPath();
            ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
            ctx.strokeStyle = "rgba(125, 145, 150, .18)";
            ctx.stroke();
        } else {
            let startAngle = -Math.PI / 2;
            items.forEach((item, index) => {
                const angle = (values[index] / total) * Math.PI * 2;
                ctx.beginPath();
                ctx.arc(centerX, centerY, radius, startAngle, startAngle + angle);
                ctx.strokeStyle = item.color;
                ctx.stroke();
                startAngle += angle;
            });
        }

        $("#ore-center-value").textContent = compactNumber(total);
        $("#ore-center-label").textContent = metricShortLabels[state.metric];
        $("#ore-list").innerHTML = items.slice(0, 7).map((item) => `
            <div class="mining-ranked-row">
                <span class="mining-ranked-label"><i class="mining-swatch" style="background:${item.color}"></i><span>${htmlEscape(item.label)}</span></span>
                <strong>${formatMetric(item.values[state.metric], state.metric)}</strong>
            </div>
        `).join("") || '<div class="text-secondary small">No ore in this range.</div>';
    }

    function renderSystems() {
        const items = state.data.systems.slice(0, 8);
        const max = Math.max(...items.map((item) => Number(item.values[state.metric] || 0)), 0);
        $("#system-bars").innerHTML = items.map((item) => {
            const width = max ? Math.max(1, (Number(item.values[state.metric] || 0) / max) * 100) : 0;
            return `<div class="mining-bar-row">
                <span class="mining-bar-label" title="${htmlEscape(item.label)}">${htmlEscape(item.label)}</span>
                <span class="mining-bar-track"><i class="mining-bar-fill" style="width:${width}%"></i></span>
                <span class="mining-bar-value">${formatMetric(item.values[state.metric], state.metric)}</span>
            </div>`;
        }).join("") || '<div class="text-secondary small">No systems in this range.</div>';
    }

    function renderLeaderboard() {
        $("#leaderboard-metric").textContent = metricLabels[state.metric];
        const rows = state.data.characters.filter((item) => item.id !== "other").slice(0, 30);
        const body = $("#character-leaderboard");
        body.innerHTML = rows.map((item, index) => `<tr data-character-id="${item.id}">
            <td class="mining-rank">${index + 1}</td>
            <td><strong>${htmlEscape(item.label)}</strong></td>
            <td class="text-end">${formatMetric(item.values[state.metric], state.metric)}</td>
        </tr>`).join("") || '<tr><td colspan="3" class="text-center text-secondary py-4">No character activity in this range.</td></tr>';
        for (const row of body.querySelectorAll("tr[data-character-id]")) {
            row.addEventListener("click", async () => {
                const characterId = Number(row.dataset.characterId);
                if (!state.options.characters.some((item) => item.id === characterId)) return;
                state.draftCharacters = new Set([characterId]);
                state.appliedCharacters = new Set([characterId]);
                renderSelectors();
                await loadData();
                window.scrollTo({ top: root.offsetTop, behavior: "smooth" });
            });
        }
    }

    function renderHeatmap() {
        const heatmap = state.data.heatmap;
        const values = heatmap.days.map((day) => Number(day.values[state.metric] || 0));
        const max = Math.max(...values, 0);
        const container = $("#mining-heatmap");
        container.replaceChildren();
        if (heatmap.days.length) {
            const first = new Date(`${heatmap.days[0].date}T00:00:00Z`);
            const mondayIndex = (first.getUTCDay() + 6) % 7;
            for (let index = 0; index < mondayIndex; index += 1) {
                const blank = document.createElement("span");
                blank.style.visibility = "hidden";
                container.appendChild(blank);
            }
        }
        heatmap.days.forEach((day) => {
            const value = Number(day.values[state.metric] || 0);
            const ratio = max ? value / max : 0;
            const level = !value ? 0 : ratio < .08 ? 1 : ratio < .28 ? 2 : ratio < .58 ? 3 : 4;
            const cell = document.createElement("span");
            cell.className = "mining-heatmap-cell";
            cell.dataset.level = String(level);
            cell.title = `${formatDate(day.date, { year: true })}: ${formatMetric(value, state.metric, false)}`;
            cell.setAttribute("aria-label", cell.title);
            container.appendChild(cell);
        });
        $("#heatmap-range").textContent = `${formatDate(heatmap.start)} – ${formatDate(heatmap.end)}`;
    }

    function renderPersonality() {
        const personality = state.data.personality;
        $("#personality-icon").className = personality.icon;
        $("#personality-name").textContent = personality.name;
        $("#personality-description").textContent = personality.description;
        const kpi = state.data.kpis;
        $("#record-day").textContent = kpi.best_day.date
            ? `${formatDate(kpi.best_day.date)} · ${formatMetric(kpi.best_day.values[state.metric], state.metric)}`
            : "No record yet";
        $("#record-average").textContent = formatMetric(kpi.average_active_day[state.metric], state.metric);
        $("#record-ores").textContent = fullNumber(kpi.ore_count);
        $("#record-systems").textContent = fullNumber(kpi.system_count);
    }

    function renderDataCoverage() {
        const meta = state.data.meta;
        const kpi = state.data.kpis;
        const health = meta.update_health;
        $("#source-note").textContent = meta.source_note;
        $("#stored-range").textContent = meta.stored_oldest
            ? `${formatDate(meta.stored_oldest, { year: true })} – ${formatDate(meta.stored_newest, { year: true })}`
            : "No rows stored";
        $("#price-coverage").textContent = `${kpi.price_coverage}% of ledger rows`;
        const issues = health.errors + health.token_errors + health.never_updated + health.disabled;
        $("#update-health").textContent = issues
            ? `${issues} character issue${issues === 1 ? "" : "s"}`
            : `${health.ok} character${health.ok === 1 ? "" : "s"} healthy`;
        $("#hero-freshness").textContent = relativeTime(health.last_attempt);
    }

    function renderDashboard() {
        const meta = state.data.meta;
        $("#hero-scope").textContent = meta.scope_label;
        $("#hero-character-count").textContent = fullNumber(meta.selected_character_count);
        $("#trend-granularity").textContent = {
            day: "Daily",
            week: "Weekly",
            month: "Monthly",
        }[state.data.trend.granularity] || state.data.trend.granularity;
        $("#trend-caption").textContent = `${metricLabels[state.metric]} · ${meta.selected_character_count} character${meta.selected_character_count === 1 ? "" : "s"}`;
        renderKpis();
        drawTrendChart();
        drawDonut();
        renderSystems();
        renderLeaderboard();
        renderHeatmap();
        renderPersonality();
        renderDataCoverage();
    }

    async function queueRefresh() {
        const body = new URLSearchParams();
        body.set("scope", state.scope);
        const allSelected = state.appliedCharacters.size === state.options.characters.length;
        if (!allSelected) body.set("characters", Array.from(state.appliedCharacters).join(","));
        const csrf = root.querySelector("input[name='csrfmiddlewaretoken']")?.value || "";
        setLoading(true);
        try {
            const payload = await fetchJson(root.dataset.refreshUrl, {
                method: "POST",
                body,
                headers: {
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "X-CSRFToken": csrf,
                },
            });
            showAlert(payload.message, "success");
            window.setTimeout(loadData, 5000);
        } catch (error) {
            showAlert(error.message, "error");
        } finally {
            setLoading(false);
        }
    }

    function setDefaultCustomDates() {
        const today = new Date();
        const end = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
        const start = new Date(end);
        start.setUTCDate(start.getUTCDate() - 29);
        $("#mining-start").value ||= start.toISOString().slice(0, 10);
        $("#mining-end").value ||= end.toISOString().slice(0, 10);
    }

    function debounce(callback, delay = 200) {
        let timer;
        return (...args) => {
            window.clearTimeout(timer);
            timer = window.setTimeout(() => callback(...args), delay);
        };
    }

    function bindEvents() {
        $("#mining-scope").addEventListener("change", (event) => loadOptions(event.target.value));
        $("#mining-preset").addEventListener("change", (event) => {
            const custom = event.target.value === "custom";
            $("#mining-custom-start-wrap").classList.toggle("d-none", !custom);
            $("#mining-custom-end-wrap").classList.toggle("d-none", !custom);
            if (custom) setDefaultCustomDates();
            loadData();
        });
        $("#mining-start").addEventListener("change", loadData);
        $("#mining-end").addEventListener("change", loadData);
        $("#mining-comparison").addEventListener("change", loadData);

        for (const button of $$(".mining-segmented button[data-metric]")) {
            button.addEventListener("click", () => {
                state.metric = button.dataset.metric;
                $$(".mining-segmented button").forEach((item) => item.classList.toggle("active", item === button));
                if (state.data) renderDashboard();
            });
        }

        $("#accounts-all").addEventListener("click", () => setAllCharacters(true));
        $("#accounts-none").addEventListener("click", () => setAllCharacters(false));
        $("#characters-all").addEventListener("click", () => setAllCharacters(true));
        $("#characters-none").addEventListener("click", () => setAllCharacters(false));
        $("#account-search").addEventListener("input", () => applySearchFilter("#account-search", "#account-list"));
        $("#character-search").addEventListener("input", () => applySearchFilter("#character-search", "#character-list"));

        $("#apply-selection").addEventListener("click", async () => {
            state.appliedCharacters = new Set(state.draftCharacters);
            updateSelectionText();
            const panel = document.getElementById("mining-selection-panel");
            if (window.bootstrap?.Collapse) window.bootstrap.Collapse.getOrCreateInstance(panel).hide();
            await loadData();
        });

        $("#mining-refresh")?.addEventListener("click", queueRefresh);
        $("#mining-export")?.addEventListener("click", () => {
            const url = new URL(root.dataset.exportUrl, window.location.origin);
            url.search = selectionQuery().toString();
            window.location.assign(url.toString());
        });

        const rerenderCharts = debounce(() => {
            if (state.data) {
                drawTrendChart();
                drawDonut();
            }
        }, 120);
        if (window.ResizeObserver) new ResizeObserver(rerenderCharts).observe(root);
        else window.addEventListener("resize", rerenderCharts);
    }

    async function initialize() {
        state.draftCharacters = new Set(state.options.characters.map((item) => item.id));
        state.appliedCharacters = new Set(state.draftCharacters);
        renderSelectors();
        bindEvents();
        setLoading(true);
        await loadData();
    }

    initialize();
})();
