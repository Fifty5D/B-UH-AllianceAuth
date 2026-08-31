(() => {
    "use strict";

    const root = document.getElementById("buh-ops-schedule");
    if (!root) return;

    const calendar = document.getElementById("schedule-calendar");
    const title = document.getElementById("calendar-title");
    const syncState = document.getElementById("schedule-sync-state");
    const nextEvents = document.getElementById("schedule-next-events");
    const canAdd = root.dataset.canAdd === "true";
    const state = {
        cursor: new Date(),
        view: "month",
        events: [],
        enabledKinds: new Set(),
        requestController: null,
    };

    const monthNames = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
    const dayNames = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
    const shortDayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

    function utcDate(year, month, day, hour = 0, minute = 0) {
        return new Date(Date.UTC(year, month, day, hour, minute));
    }

    function dayKey(date) {
        return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`;
    }

    function toUtcInput(date) {
        return `${dayKey(date)}T${String(date.getUTCHours()).padStart(2, "0")}:${String(date.getUTCMinutes()).padStart(2, "0")}`;
    }

    function parseEventDate(value) {
        return value ? new Date(value) : null;
    }

    function startOfUtcDay(date) {
        return utcDate(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate());
    }

    function addUtcDays(date, count) {
        const result = new Date(date.getTime());
        result.setUTCDate(result.getUTCDate() + count);
        return result;
    }

    function startOfWeek(date) {
        const start = startOfUtcDay(date);
        const mondayOffset = (start.getUTCDay() + 6) % 7;
        return addUtcDays(start, -mondayOffset);
    }

    function sameUtcDay(left, right) {
        return dayKey(left) === dayKey(right);
    }

    function formatTime(date, allDay = false) {
        if (allDay) return "All day";
        return `${String(date.getUTCHours()).padStart(2, "0")}:${String(date.getUTCMinutes()).padStart(2, "0")}`;
    }

    function formatLongDate(date) {
        return `${dayNames[date.getUTCDay()]}, ${monthNames[date.getUTCMonth()]} ${date.getUTCDate()}, ${date.getUTCFullYear()}`;
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function visibleEvents() {
        return state.events.filter((event) => state.enabledKinds.has(event.kind));
    }

    function eventsForDay(date) {
        const start = startOfUtcDay(date);
        const end = addUtcDays(start, 1);
        return visibleEvents().filter((event) => {
            const eventStart = parseEventDate(event.start);
            const eventEnd = parseEventDate(event.end) || eventStart;
            return eventStart < end && eventEnd >= start;
        });
    }

    function rangeForView() {
        if (state.view === "week") {
            const start = startOfWeek(state.cursor);
            return { start, end: addUtcDays(start, 7) };
        }
        if (state.view === "agenda") {
            const start = startOfUtcDay(state.cursor);
            return { start, end: addUtcDays(start, 60) };
        }
        const first = utcDate(state.cursor.getUTCFullYear(), state.cursor.getUTCMonth(), 1);
        const start = startOfWeek(first);
        return { start, end: addUtcDays(start, 42) };
    }

    function setTitle() {
        if (state.view === "month") {
            title.textContent = `${monthNames[state.cursor.getUTCMonth()]} ${state.cursor.getUTCFullYear()}`;
            return;
        }
        const range = rangeForView();
        if (state.view === "week") {
            const last = addUtcDays(range.end, -1);
            title.textContent = `${monthNames[range.start.getUTCMonth()]} ${range.start.getUTCDate()} – ${monthNames[last.getUTCMonth()]} ${last.getUTCDate()}, ${last.getUTCFullYear()}`;
            return;
        }
        title.textContent = `Agenda from ${monthNames[range.start.getUTCMonth()]} ${range.start.getUTCDate()}, ${range.start.getUTCFullYear()}`;
    }

    function eventButton(event, style = "pill") {
        const button = element("button", style === "pill" ? "schedule-event-pill" : "schedule-week-card");
        button.type = "button";
        button.style.setProperty("--event-color", event.color);
        const eventStart = parseEventDate(event.start);
        const time = element("time", "", formatTime(eventStart, event.allDay));
        const copy = element(style === "pill" ? "span" : "strong", "", event.title);
        button.append(time, copy);
        if (style !== "pill" && event.location) button.append(element("small", "", event.location));
        button.addEventListener("click", () => openDetails(event));
        return button;
    }

    function renderMonth() {
        const range = rangeForView();
        const weekdays = element("div", "schedule-month-weekdays");
        ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].forEach((day) => weekdays.append(element("span", "", day)));
        const grid = element("div", "schedule-month-grid");
        const today = new Date();
        for (let index = 0; index < 42; index += 1) {
            const date = addUtcDays(range.start, index);
            const day = element("article", "schedule-day");
            if (date.getUTCMonth() !== state.cursor.getUTCMonth()) day.classList.add("outside");
            if (sameUtcDay(date, today)) day.classList.add("today");
            day.dataset.date = dayKey(date);
            const heading = element("div", "schedule-day-heading");
            heading.append(element("span", "schedule-date-number", String(date.getUTCDate())));
            if (canAdd) {
                const add = element("button", "schedule-day-add", "+");
                add.type = "button";
                add.title = `Add event on ${formatLongDate(date)}`;
                add.addEventListener("click", () => openEditor(null, date));
                heading.append(add);
            }
            day.append(heading);
            const items = eventsForDay(date);
            items.slice(0, 3).forEach((event) => day.append(eventButton(event)));
            if (items.length > 3) day.append(element("div", "schedule-more-events", `+${items.length - 3} more`));
            grid.append(day);
        }
        calendar.replaceChildren(weekdays, grid);
    }

    function renderWeek() {
        const range = rangeForView();
        const week = element("div", "schedule-week");
        const today = new Date();
        for (let index = 0; index < 7; index += 1) {
            const date = addUtcDays(range.start, index);
            const column = element("section", "schedule-week-day");
            const heading = element("header", "schedule-week-heading");
            if (sameUtcDay(date, today)) heading.classList.add("today");
            heading.append(element("span", "", dayNames[date.getUTCDay()]));
            heading.append(element("strong", "", `${monthNames[date.getUTCMonth()].slice(0, 3)} ${date.getUTCDate()}`));
            if (canAdd) {
                const add = element("button", "btn btn-sm btn-link", "+ Add");
                add.type = "button";
                add.addEventListener("click", () => openEditor(null, date));
                heading.append(add);
            }
            const items = element("div", "schedule-week-events");
            const dayEvents = eventsForDay(date);
            dayEvents.forEach((event) => items.append(eventButton(event, "card")));
            if (!dayEvents.length) items.append(element("div", "schedule-empty py-4", "No events"));
            column.append(heading, items);
            week.append(column);
        }
        calendar.replaceChildren(week);
    }

    function renderAgenda() {
        const range = rangeForView();
        const agenda = element("div", "schedule-agenda");
        let daysShown = 0;
        for (let date = new Date(range.start); date < range.end; date = addUtcDays(date, 1)) {
            const dayEvents = eventsForDay(date);
            if (!dayEvents.length) continue;
            daysShown += 1;
            const row = element("section", "schedule-agenda-day");
            const dateBlock = element("div", "schedule-agenda-date");
            dateBlock.append(element("span", "", shortDayNames[date.getUTCDay()]));
            dateBlock.append(element("strong", "", `${monthNames[date.getUTCMonth()].slice(0, 3)} ${date.getUTCDate()}`));
            const eventList = element("div", "schedule-agenda-events");
            dayEvents.forEach((event) => {
                const button = element("button", "schedule-agenda-card");
                button.type = "button";
                button.style.setProperty("--event-color", event.color);
                button.append(element("time", "", formatTime(parseEventDate(event.start), event.allDay)));
                button.append(element("span", "schedule-agenda-marker"));
                const copy = element("div", "schedule-agenda-copy");
                copy.append(element("strong", "", event.title));
                copy.append(element("small", "", event.location || event.kind));
                button.append(copy);
                const icon = element("i", `fa-solid ${event.icon}`);
                icon.style.color = event.color;
                button.append(icon);
                button.addEventListener("click", () => openDetails(event));
                eventList.append(button);
            });
            row.append(dateBlock, eventList);
            agenda.append(row);
        }
        if (!daysShown) {
            const empty = element("div", "schedule-empty");
            empty.append(element("i", "fa-regular fa-calendar-check"));
            empty.append(element("strong", "", "No visible events in the next 60 days"));
            agenda.append(empty);
        }
        calendar.replaceChildren(agenda);
    }

    function renderNextUp() {
        const current = new Date();
        const upcoming = visibleEvents()
            .filter((event) => parseEventDate(event.end || event.start) >= current)
            .sort((left, right) => parseEventDate(left.start) - parseEventDate(right.start))
            .slice(0, 10);
        nextEvents.replaceChildren();
        if (!upcoming.length) {
            const empty = element("div", "schedule-empty compact");
            empty.append(element("i", "fa-regular fa-calendar-check"));
            empty.append(element("strong", "", "Nothing upcoming"));
            nextEvents.append(empty);
            return;
        }
        upcoming.forEach((event) => {
            const date = parseEventDate(event.start);
            const item = element("article", "schedule-next-item");
            item.style.setProperty("--event-color", event.color);
            item.tabIndex = 0;
            const dateBlock = element("div", "schedule-next-date");
            dateBlock.append(element("span", "", monthNames[date.getUTCMonth()].slice(0, 3)));
            dateBlock.append(element("strong", "", String(date.getUTCDate())));
            const copy = element("div", "schedule-next-copy");
            copy.append(element("strong", "", event.title));
            copy.append(element("small", "", `${formatTime(date, event.allDay)} UTC${event.location ? ` · ${event.location}` : ""}`));
            item.append(dateBlock, copy);
            item.addEventListener("click", () => openDetails(event));
            item.addEventListener("keydown", (keyboardEvent) => {
                if (keyboardEvent.key === "Enter" || keyboardEvent.key === " ") openDetails(event);
            });
            nextEvents.append(item);
        });
    }

    function render() {
        setTitle();
        if (state.view === "week") renderWeek();
        else if (state.view === "agenda") renderAgenda();
        else renderMonth();
        renderNextUp();
    }

    async function loadEvents() {
        if (state.requestController) state.requestController.abort();
        state.requestController = new AbortController();
        const range = rangeForView();
        const query = new URLSearchParams({ start: range.start.toISOString(), end: range.end.toISOString() });
        syncState.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i> Syncing';
        try {
            const response = await fetch(`${root.dataset.feedUrl}?${query}`, {
                headers: { "X-Requested-With": "XMLHttpRequest" },
                signal: state.requestController.signal,
            });
            const payload = await response.json();
            if (!response.ok) throw new Error(payload.error || "The schedule could not be loaded.");
            state.events = payload.events;
            render();
            const generated = new Date(payload.generatedAt);
            syncState.textContent = `Synced ${formatTime(generated)} UTC`;
        } catch (error) {
            if (error.name === "AbortError") return;
            const box = element("div", "schedule-error", error.message);
            calendar.replaceChildren(box);
            syncState.textContent = "Sync failed";
        }
    }

    const detailsModalElement = document.getElementById("schedule-event-details");
    const detailsModal = detailsModalElement ? bootstrap.Modal.getOrCreateInstance(detailsModalElement) : null;
    const editorModalElement = document.getElementById("schedule-event-editor");
    const editorModal = editorModalElement ? bootstrap.Modal.getOrCreateInstance(editorModalElement) : null;
    let selectedEvent = null;

    function openDetails(event) {
        selectedEvent = event;
        document.getElementById("event-detail-kind").textContent = event.kind.toUpperCase();
        document.getElementById("event-detail-kind").style.color = event.color;
        document.getElementById("event-detail-title").textContent = event.title;
        const start = parseEventDate(event.start);
        const end = parseEventDate(event.end);
        document.getElementById("event-detail-time").textContent = `${formatLongDate(start)} · ${formatTime(start, event.allDay)}${end ? ` → ${formatLongDate(end)} · ${formatTime(end, event.allDay)}` : ""}`;
        const locationWrap = document.getElementById("event-detail-location-wrap");
        document.getElementById("event-detail-location").textContent = event.location || "";
        locationWrap.classList.toggle("d-none", !event.location);
        document.getElementById("event-detail-description").textContent = event.description || "No additional details.";
        document.getElementById("event-detail-author").textContent = event.createdBy ? `Created by ${event.createdBy}` : "Live EVE data";
        const link = document.getElementById("event-detail-link");
        link.classList.toggle("d-none", !event.url);
        if (event.url) link.href = event.url;
        const edit = document.getElementById("event-detail-edit");
        if (edit) edit.classList.toggle("d-none", !event.updateUrl);
        const remove = document.getElementById("event-detail-delete");
        if (remove) remove.classList.toggle("d-none", !event.deleteUrl);
        detailsModal.show();
    }

    function openEditor(event = null, selectedDate = null) {
        if (!editorModal) return;
        const form = document.getElementById("schedule-event-form");
        form.reset();
        form.action = event && event.updateUrl ? event.updateUrl : form.dataset.addAction;
        document.getElementById("schedule-editor-title").textContent = event ? "Edit schedule event" : "Add to the schedule";
        if (event) {
            form.elements.title.value = event.title || "";
            form.elements.description.value = event.description || "";
            form.elements.location.value = event.location || "";
            form.elements.starts_at.value = toUtcInput(parseEventDate(event.start));
            form.elements.ends_at.value = event.end ? toUtcInput(parseEventDate(event.end)) : "";
            form.elements.all_day.checked = Boolean(event.allDay);
            form.elements.color.value = event.colorName || "violet";
            form.elements.link.value = event.url || "";
        } else {
            const base = selectedDate ? new Date(selectedDate) : new Date();
            base.setUTCHours(18, 0, 0, 0);
            form.elements.starts_at.value = toUtcInput(base);
        }
        editorModal.show();
    }

    document.getElementById("schedule-add-event")?.addEventListener("click", () => openEditor());
    document.getElementById("event-detail-edit")?.addEventListener("click", () => {
        if (!selectedEvent) return;
        detailsModal.hide();
        openEditor(selectedEvent);
    });
    document.getElementById("event-detail-delete")?.addEventListener("click", () => {
        if (!selectedEvent?.deleteUrl) return;
        if (!window.confirm(`Delete "${selectedEvent.title}" from the shared schedule?`)) return;
        const form = document.getElementById("schedule-delete-form");
        form.action = selectedEvent.deleteUrl;
        form.submit();
    });

    document.querySelectorAll(".schedule-filter-chip input").forEach((input) => {
        state.enabledKinds.add(input.value);
        input.addEventListener("change", () => {
            input.closest("label").classList.toggle("active", input.checked);
            if (input.checked) state.enabledKinds.add(input.value);
            else state.enabledKinds.delete(input.value);
            render();
        });
    });

    document.querySelectorAll("[data-calendar-view]").forEach((button) => {
        button.addEventListener("click", () => {
            document.querySelectorAll("[data-calendar-view]").forEach((item) => item.classList.remove("active"));
            button.classList.add("active");
            state.view = button.dataset.calendarView;
            loadEvents();
        });
    });

    document.getElementById("calendar-previous").addEventListener("click", () => {
        if (state.view === "month") state.cursor = utcDate(state.cursor.getUTCFullYear(), state.cursor.getUTCMonth() - 1, 1);
        else state.cursor = addUtcDays(state.cursor, state.view === "week" ? -7 : -60);
        loadEvents();
    });
    document.getElementById("calendar-next").addEventListener("click", () => {
        if (state.view === "month") state.cursor = utcDate(state.cursor.getUTCFullYear(), state.cursor.getUTCMonth() + 1, 1);
        else state.cursor = addUtcDays(state.cursor, state.view === "week" ? 7 : 60);
        loadEvents();
    });
    document.getElementById("calendar-today").addEventListener("click", () => {
        state.cursor = new Date();
        loadEvents();
    });
    document.getElementById("schedule-refresh").addEventListener("click", loadEvents);

    loadEvents();
})();
