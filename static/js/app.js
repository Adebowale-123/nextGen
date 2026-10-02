/* NextGen Game — progressive enhancement (pages work without JS except the number picker). */
(function () {
  "use strict";

  const fmtMoney = (minor) =>
    "₦" + (minor / 100).toLocaleString("en-NG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  /* ---------- Countdowns: <span data-countdown="ISO8601"> ---------- */
  function tickCountdowns() {
    const now = Date.now();
    document.querySelectorAll("[data-countdown]").forEach((el) => {
      const diff = Math.max(0, new Date(el.dataset.countdown).getTime() - now);
      if (diff === 0) {
        el.textContent = el.dataset.doneText || "Drawing…";
        return;
      }
      const s = Math.floor(diff / 1000);
      const d = Math.floor(s / 86400);
      const h = String(Math.floor((s % 86400) / 3600)).padStart(2, "0");
      const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
      const sec = String(s % 60).padStart(2, "0");
      el.textContent = (d > 0 ? d + "d " : "") + `${h}:${m}:${sec}`;
    });
  }
  if (document.querySelector("[data-countdown]")) {
    tickCountdowns();
    setInterval(tickCountdowns, 1000);
  }

  /* ---------- Quick amount chips: <button data-amount="500.00" data-target="#id_amount"> ---------- */
  document.querySelectorAll("[data-amount]").forEach((chip) => {
    chip.addEventListener("click", (e) => {
      e.preventDefault();
      const input = document.querySelector(chip.dataset.target || "#id_amount");
      if (!input) return;
      input.value = chip.dataset.amount;
      document.querySelectorAll("[data-amount]").forEach((c) => c.classList.toggle("active", c === chip));
      input.dispatchEvent(new Event("input"));
    });
  });

  /* ---------- Confirm prompts: <form data-confirm="Are you sure?"> ---------- */
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  /* ---------- Number picker ---------- */
  const picker = document.querySelector("[data-picker]");
  if (picker) {
    const pickCount = Number(picker.dataset.pick);
    const maxNumber = Number(picker.dataset.max);
    const price = Number(picker.dataset.price);
    const maxLines = Number(picker.dataset.maxLines);
    const grid = picker.querySelector(".picker-grid");
    const status = picker.querySelector("[data-pick-status]");
    const linesEl = document.querySelector("[data-lines]");
    const totalEl = document.querySelector("[data-total]");
    const countEl = document.querySelector("[data-line-count]");
    const form = document.querySelector("[data-purchase-form]");
    const hidden = form.querySelector("input[name=lines]");
    const buyBtn = form.querySelector("button[type=submit]");
    const addBtn = picker.querySelector("[data-add-line]");
    let current = new Set();
    let currentQuick = false;
    let lines = [];

    const randomPick = () => {
      const pool = Array.from({ length: maxNumber }, (_, i) => i + 1);
      const out = [];
      for (let i = 0; i < pickCount; i++) {
        const buf = new Uint32Array(1);
        crypto.getRandomValues(buf);
        out.push(pool.splice(buf[0] % pool.length, 1)[0]);
      }
      return out.sort((a, b) => a - b);
    };

    const render = () => {
      grid.querySelectorAll(".pick").forEach((btn) => {
        const n = Number(btn.dataset.n);
        btn.classList.toggle("selected", current.has(n));
        btn.disabled = !current.has(n) && current.size >= pickCount;
      });
      const left = pickCount - current.size;
      status.textContent = left > 0 ? `Pick ${left} more number${left === 1 ? "" : "s"}` : "Line complete ✓";
      addBtn.disabled = current.size !== pickCount || lines.length >= maxLines;

      linesEl.innerHTML = "";
      if (!lines.length) {
        linesEl.innerHTML = '<p class="muted mb-0">No numbers yet. Tap numbers or press “Pick for me”.</p>';
      }
      lines.forEach((line, idx) => {
        const row = document.createElement("div");
        row.className = "line-row";
        const balls = line.numbers.map((n) => `<span class="ball">${n}</span>`).join("");
        row.innerHTML = `<div class="balls">${balls}</div>
          <div class="row">${line.quick ? '<span class="badge badge-accent">QP</span>' : ""}
          <button type="button" class="btn btn-ghost btn-sm" aria-label="Remove line">✕</button></div>`;
        row.querySelector("button").addEventListener("click", () => {
          lines.splice(idx, 1);
          render();
        });
        linesEl.appendChild(row);
      });
      countEl.textContent = lines.length;
      totalEl.textContent = fmtMoney(lines.length * price);
      buyBtn.disabled = lines.length === 0;
      hidden.value = JSON.stringify(lines);
    };

    grid.addEventListener("click", (e) => {
      const btn = e.target.closest(".pick");
      if (!btn) return;
      const n = Number(btn.dataset.n);
      if (current.has(n)) current.delete(n);
      else if (current.size < pickCount) current.add(n);
      currentQuick = false;
      render();
    });

    const commitCurrent = () => {
      if (current.size !== pickCount || lines.length >= maxLines) return;
      lines.push({ numbers: [...current].sort((a, b) => a - b), quick: currentQuick });
      current = new Set();
      currentQuick = false;
    };

    addBtn.addEventListener("click", () => {
      commitCurrent();
      render();
    });
    picker.querySelector("[data-quick-pick]").addEventListener("click", () => {
      current = new Set(randomPick());
      currentQuick = true;
      render();
    });
    picker.querySelector("[data-clear]").addEventListener("click", () => {
      current = new Set();
      render();
    });
    const qpLines = document.querySelector("[data-quick-lines]");
    if (qpLines) {
      qpLines.addEventListener("click", () => {
        const want = Number(qpLines.dataset.quickLines);
        for (let i = 0; i < want && lines.length < maxLines; i++) lines.push({ numbers: randomPick(), quick: true });
        render();
      });
    }
    form.addEventListener("submit", (e) => {
      commitCurrent();
      render();
      if (!lines.length) {
        e.preventDefault();
        return;
      }
      buyBtn.disabled = true;
      buyBtn.textContent = "Processing…";
    });
    render();
  }

  /* ---------- Provably fair verifier ---------- */
  const verifier = document.querySelector("[data-verifier]");
  if (verifier && window.crypto && crypto.subtle) {
    const enc = new TextEncoder();
    const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
    const field = (name) => verifier.querySelector(`[name=${name}]`);

    async function deriveNumbers(serverSeed, clientSeed, nonce, count, maximum) {
      const key = await crypto.subtle.importKey("raw", enc.encode(serverSeed), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
      const limit = Math.floor(2 ** 32 / maximum) * maximum;
      const numbers = [];
      for (let round = 0; numbers.length < count && round < 1000; round++) {
        const digest = new DataView(await crypto.subtle.sign("HMAC", key, enc.encode(`${clientSeed}:${nonce}:${round}`)));
        for (let i = 0; i < 32 && numbers.length < count; i += 4) {
          const value = digest.getUint32(i, false);
          if (value >= limit) continue;
          const n = (value % maximum) + 1;
          if (!numbers.includes(n)) numbers.push(n);
        }
      }
      return numbers;
    }

    verifier.addEventListener("submit", async (e) => {
      e.preventDefault();
      const out = verifier.querySelector("[data-verify-output]");
      const serverSeed = field("server_seed").value.trim();
      const hash = hex(await crypto.subtle.digest("SHA-256", enc.encode(serverSeed)));
      const hashOk = hash === field("server_seed_hash").value.trim().toLowerCase();
      const numbers = await deriveNumbers(serverSeed, field("client_seed").value.trim(), field("nonce").value.trim(),
        Number(field("pick").value), Number(field("max").value));
      const expected = (field("expected").value || "").split(",").map(Number).filter(Boolean);
      const numbersOk = expected.length === numbers.length && expected.every((n, i) => n === numbers[i]);
      out.innerHTML = `
        <p>${hashOk ? "✅" : "❌"} SHA-256(server seed) ${hashOk ? "matches" : "does NOT match"} the pre-published hash.</p>
        <p class="mb-0">Recomputed numbers (draw order):</p>
        <div class="balls" style="margin:8px 0">${numbers.map((n) => `<span class="ball win">${n}</span>`).join("")}</div>
        <p class="mb-0">${numbersOk ? "✅ Identical to the published result." : "❌ Different from the published result."}</p>`;
      out.hidden = false;
    });
  }
})();

/* ---------- Play Game popup: confirm -> your 4 numbers -> Done ---------- */
(function () {
  "use strict";
  const forms = document.querySelectorAll("form[data-play-form]");
  if (!forms.length || !window.fetch) return;

  const modal = document.createElement("div");
  modal.className = "modal-backdrop";
  modal.hidden = true;
  modal.innerHTML = '<div class="modal" role="dialog" aria-modal="true"><div data-modal-body></div></div>';
  document.body.appendChild(modal);
  const body = modal.querySelector("[data-modal-body]");
  const open = (html) => { body.innerHTML = html; modal.hidden = false; };
  const close = () => { modal.hidden = true; };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  forms.forEach((form) => {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const { game, price, count } = form.dataset;
      open(`
        <div class="modal-icon">🎟️</div>
        <h2>Play ${esc(game)}?</h2>
        <p class="muted">It costs <b>${esc(price)}</b>. Your ${esc(count)} numbers will be picked for you.</p>
        <div class="modal-actions">
          <button type="button" class="btn btn-ghost btn-lg" data-cancel>Cancel</button>
          <button type="button" class="btn btn-primary btn-lg" data-go>Play ${esc(price)}</button>
        </div>`);
      body.querySelector("[data-cancel]").onclick = close;
      body.querySelector("[data-go]").onclick = async (ev) => {
        ev.target.disabled = true;
        ev.target.textContent = "Getting your numbers…";
        let data;
        try {
          const res = await fetch(form.action, {
            method: "POST",
            body: new FormData(form),
            headers: { "X-Requested-With": "fetch" },
            credentials: "same-origin",
          });
          data = await res.json();
        } catch (err) {
          data = { ok: false, error: "Connection problem. Please check My Tickets before trying again." };
        }
        if (!data.ok) {
          open(`
            <div class="modal-icon">⚠️</div>
            <h2>Couldn't play</h2>
            <p class="muted">${esc(data.error)}</p>
            <div class="modal-actions">
              ${data.deposit_url ? `<a class="btn btn-primary btn-lg" href="${esc(data.deposit_url)}">Add money</a>` : ""}
              <button type="button" class="btn btn-ghost btn-lg" data-done>Close</button>
            </div>`);
          body.querySelector("[data-done]").onclick = close;
          return;
        }
        const balls = data.numbers.map((n, i) => `<span class="ball xl win" style="animation-delay:${i * 0.25}s">${n}</span>`).join("");
        open(`
          <div class="modal-icon">🍀</div>
          <h2>Your numbers</h2>
          <div class="ticket-balls">${balls}</div>
          <p class="muted">${esc(data.game)} · Ticket <b>${esc(data.serial)}</b><br>
            Paid ${esc(data.paid)}${data.from_bonus ? " from your bonus" : ""}.
            Winners are announced after the game closes at <b>${esc(data.closes_at)}</b>.</p>
          <div class="modal-actions single">
            <button type="button" class="btn btn-primary btn-lg btn-block" data-done>Done</button>
          </div>`);
        body.querySelector("[data-done]").onclick = () => window.location.reload();
      };
    });
  });
})();

/* ---------- Simple tabs: <div data-tabs><a data-tab="x"> + <div data-panel="x"> ---------- */
document.querySelectorAll("[data-tabs]").forEach((tabs) => {
  const scope = tabs.parentElement;
  tabs.querySelectorAll("[data-tab]").forEach((tab) => {
    tab.addEventListener("click", (e) => {
      e.preventDefault();
      tabs.querySelectorAll("[data-tab]").forEach((t) => t.classList.toggle("active", t === tab));
      scope.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== tab.dataset.tab; });
    });
  });
});
