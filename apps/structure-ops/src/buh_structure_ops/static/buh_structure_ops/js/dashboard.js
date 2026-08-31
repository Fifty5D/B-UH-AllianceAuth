(function () {
    "use strict";

    function drawSparkline(canvas) {
        let points;
        try { points = JSON.parse(canvas.dataset.history || "[]"); } catch (_) { points = []; }
        const ctx = canvas.getContext("2d");
        const dpr = window.devicePixelRatio || 1;
        const width = canvas.clientWidth || 420;
        const height = canvas.clientHeight || 90;
        canvas.width = width * dpr;
        canvas.height = height * dpr;
        ctx.scale(dpr, dpr);
        ctx.clearRect(0, 0, width, height);
        if (points.length < 2) {
            ctx.fillStyle = getComputedStyle(document.body).getPropertyValue("--bs-secondary-color") || "#778";
            ctx.font = "12px sans-serif";
            ctx.fillText("Trend begins after two structure syncs", 12, height / 2);
            return;
        }
        const values = points.map((point) => Number(point.days || 0));
        const max = Math.max(30, ...values);
        const pad = 10;
        const x = (index) => pad + index * (width - pad * 2) / (values.length - 1);
        const y = (value) => height - pad - value / max * (height - pad * 2);
        ctx.strokeStyle = "rgba(95,226,210,.22)";
        ctx.beginPath(); ctx.moveTo(pad, y(7)); ctx.lineTo(width - pad, y(7)); ctx.stroke();
        ctx.strokeStyle = "#5fe2d2"; ctx.lineWidth = 2; ctx.beginPath();
        values.forEach((value, index) => index ? ctx.lineTo(x(index), y(value)) : ctx.moveTo(x(index), y(value)));
        ctx.stroke();
        ctx.lineTo(x(values.length - 1), height - pad); ctx.lineTo(pad, height - pad); ctx.closePath();
        const gradient = ctx.createLinearGradient(0, 0, 0, height);
        gradient.addColorStop(0, "rgba(95,226,210,.25)"); gradient.addColorStop(1, "rgba(95,226,210,0)");
        ctx.fillStyle = gradient; ctx.fill();
    }

    function countdown(element) {
        const deadline = new Date(element.dataset.countdown);
        if (Number.isNaN(deadline.getTime())) return;
        const delta = deadline.getTime() - Date.now();
        if (delta <= 0) { element.textContent = "Ready / due"; return; }
        const minutes = Math.floor(delta / 60000);
        const days = Math.floor(minutes / 1440);
        const hours = Math.floor((minutes % 1440) / 60);
        element.textContent = days ? `${days}d ${hours}h` : `${hours}h ${minutes % 60}m`;
    }

    function start() {
        document.querySelectorAll("canvas.ops-sparkline").forEach(drawSparkline);
        const timers = Array.from(document.querySelectorAll("[data-countdown]"));
        const update = () => timers.forEach(countdown);
        update();
        window.setInterval(update, 60000);
    }
    document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", start) : start();
}());

