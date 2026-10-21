(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const MAX_CONTEXT = parseInt(document.body.dataset.maxContext, 10);
  const MAX_ANSWER = parseInt(document.body.dataset.maxAnswer, 10);

  const contextEl = $("context");
  const answerEl = $("answer");
  const analyzeBtn = $("analyze-btn");
  let lastRun = null; // context, answer and results from the last successful analysis

  const SAMPLE = {
    context:
      "The Eiffel Tower is a wrought-iron lattice tower in Paris, France. It was built for the 1889 World's Fair " +
      "and completed in March 1889. The tower is 330 metres tall, including its antennas. It was designed by the " +
      "engineering company of Gustave Eiffel. Construction took two years, two months and five days.",
    answer:
      "The Eiffel Tower was completed in 1889. It is 330 metres tall. It was built for the 1900 World's Fair. " +
      "Construction took about two years. More than 7 million people visit it every year.",
  };

  const LABELS = {
    SUPPORTED: { text: "Supported", cls: "supported" },
    CONTRADICTED: { text: "Contradicted", cls: "contradicted" },
    "NOT VERIFIABLE": { text: "Not Verifiable", cls: "unverifiable" },
  };

  // All dynamic text goes through textContent (never innerHTML).
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showState(name) {
    ["empty", "loading", "error", "dashboard"].forEach((id) => ($(id).hidden = id !== name));
  }

  function updateCounters() {
    [[contextEl, "context-count", MAX_CONTEXT], [answerEl, "answer-count", MAX_ANSWER]].forEach(([ta, id, max]) => {
      const span = $(id);
      span.textContent = ta.value.length;
      span.parentElement.classList.toggle("over", ta.value.length > max);
    });
  }

  function setFormError(msg) {
    const p = $("form-error");
    p.textContent = msg || "";
    p.hidden = !msg;
  }

  async function loadHealth() {
    const box = $("status");
    const text = $("status-text");
    try {
      const res = await fetch("/api/health");
      const data = await res.json();
      box.classList.remove("ok", "warn", "bad");
      if (data.configured) {
        box.classList.add("ok");
        text.textContent = "Detector configured";
      } else {
        box.classList.add("warn");
        text.textContent = "API key missing";
      }
    } catch (e) {
      box.classList.add("bad");
      text.textContent = "Server unreachable";
    }
  }

  function validate() {
    const c = contextEl.value.trim();
    const a = answerEl.value.trim();
    if (!c) return "Reference context cannot be empty.";
    if (!a) return "AI-generated answer cannot be empty.";
    if (c.length > MAX_CONTEXT) return "Reference context is over the " + MAX_CONTEXT + " character limit.";
    if (a.length > MAX_ANSWER) return "Answer is over the " + MAX_ANSWER + " character limit.";
    return "";
  }

  async function analyze() {
    const problem = validate();
    setFormError(problem);
    if (problem) return;

    showState("loading");
    analyzeBtn.disabled = true;
    const sentContext = contextEl.value;
    const sentAnswer = answerEl.value;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 90000);

    try {
      const res = await fetch("/api/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          context: contextEl.value,
          answer: answerEl.value,
          prompt_variant: $("variant").value,
        }),
        signal: controller.signal,
      });
      let data = null;
      try { data = await res.json(); } catch (_) { /* non-JSON response */ }
      if (!res.ok) throw new Error((data && data.error) || "The server returned an error (" + res.status + ").");
      renderResults(data, sentContext, sentAnswer);
    } catch (err) {
      const msg = err.name === "AbortError" ? "The request took too long. Please retry." : err.message || "Something went wrong.";
      $("error-message").textContent = msg;
      showState("error");
    } finally {
      clearTimeout(timer);
      analyzeBtn.disabled = false;
    }
  }

  function renderResults(data, ctx, ans) {
    lastRun = { context: ctx, answer: ans, results: data.results };
    setupAssistant(data.results);
    const s = data.summary;
    $("n-supported").textContent = s.supported;
    $("n-contradicted").textContent = s.contradicted;
    $("n-unverifiable").textContent = s.not_verifiable;

    const rate = data.assessment.contradiction_rate;
    $("rate-value").textContent = rate === null ? "n/a" : rate + "%";
    $("rate-bar").style.width = (rate === null ? 0 : rate) + "%";
    $("assessment-text").textContent = data.assessment.message;

    const list = $("claims");
    list.replaceChildren();
    data.results.forEach((r, i) => list.appendChild(buildClaim(r, i)));
    $("toggle-all").hidden = data.results.length === 0;
    $("toggle-all").textContent = "Collapse all";

    const meta = data.meta || {};
    $("model-meta").textContent = "Model: " + (meta.model || "unknown") + " · Prompt: " + (meta.prompt_variant || "unknown");
    showState("dashboard");
  }

  function buildClaim(r, index) {
    const info = LABELS[r.label] || LABELS["NOT VERIFIABLE"];
    const d = el("details", "claim " + info.cls);
    d.open = true;
    const sum = el("summary");
    sum.appendChild(el("span", "badge", info.text));
    sum.appendChild(el("span", "claim-text", r.claim));
    d.appendChild(sum);

    const body = el("div", "claim-body");
    body.appendChild(el("h3", "", "Evidence from context"));
    if (r.evidence) {
      body.appendChild(el("p", "evidence", r.evidence));
      if (r.evidence_found_in_context === false) {
        body.appendChild(el("p", "warn-note", "Warning: this quote was not found word-for-word in your context. Check it manually."));
      }
    } else {
      body.appendChild(el("p", "", "No supporting evidence was found in the context."));
    }
    body.appendChild(el("h3", "", "Explanation"));
    body.appendChild(el("p", "", r.explanation || "No explanation provided."));
    const ask = el("button", "btn ghost small ask-btn", "Ask the assistant about this claim");
    ask.type = "button";
    ask.addEventListener("click", () => selectClaim(index));
    body.appendChild(ask);
    d.appendChild(body);
    return d;
  }

  contextEl.addEventListener("input", updateCounters);
  answerEl.addEventListener("input", updateCounters);
  analyzeBtn.addEventListener("click", analyze);
  $("retry-btn").addEventListener("click", analyze);

  $("sample-btn").addEventListener("click", () => {
    contextEl.value = SAMPLE.context;
    answerEl.value = SAMPLE.answer;
    updateCounters();
    setFormError("");
  });

  $("clear-btn").addEventListener("click", () => {
    contextEl.value = "";
    answerEl.value = "";
    updateCounters();
    setFormError("");
    showState("empty");
  });

  $("toggle-all").addEventListener("click", () => {
    const items = document.querySelectorAll("details.claim");
    const anyOpen = Array.from(items).some((d) => d.open);
    items.forEach((d) => (d.open = !anyOpen));
    $("toggle-all").textContent = anyOpen ? "Expand all" : "Collapse all";
  });


  // ---------------------------------------------------------------- assistant
  const assistOut = $("assist-output");

  function setupAssistant(results) {
    const select = $("assist-claim");
    select.replaceChildren();
    results.forEach((r, i) => {
      const text = r.claim.length > 70 ? r.claim.slice(0, 67) + "..." : r.claim;
      select.appendChild(el("option", "", (i + 1) + ". " + text));
      select.lastChild.value = String(i);
    });
    $("assist-question").value = "";
    assistOut.hidden = true;
    assistOut.replaceChildren();
    $("assistant").hidden = results.length === 0;
  }

  function selectClaim(index) {
    $("assist-claim").value = String(index);
    $("assistant").scrollIntoView({ behavior: "smooth", block: "center" });
    $("assist-question").focus();
  }

  function section(title, node) {
    const wrap = el("div", "sec");
    wrap.appendChild(el("h3", "", title));
    wrap.appendChild(node);
    return wrap;
  }

  function listNode(items) {
    const ul = el("ul");
    items.forEach((t) => ul.appendChild(el("li", "", t)));
    return ul;
  }

  function showAssistLoading(label) {
    assistOut.hidden = false;
    assistOut.replaceChildren(el("p", "assist-note", label));
  }

  function showAssistError(message) {
    assistOut.hidden = false;
    const p = el("p", "assist-error", message + " The verification results above are not affected.");
    assistOut.replaceChildren(p);
  }

  function renderAssistant(data) {
    assistOut.replaceChildren();
    if (data.action === "explain") {
      assistOut.appendChild(section("Claim (" + (LABELS[data.label] || LABELS["NOT VERIFIABLE"]).text + ")", el("div", "text-box", data.claim)));
      assistOut.appendChild(section("Explanation", el("div", "text-box", data.explanation)));
      assistOut.appendChild(section("Evidence from your context",
        el("div", data.evidence ? "evidence-box" : "text-box", data.evidence || "No supporting evidence in the context.")));
      if (data.evidence && data.evidence_found_in_context === false) {
        assistOut.appendChild(el("p", "warn-note", "Warning: this quote was not found word-for-word in your context."));
      }
      if (data.assumptions) assistOut.appendChild(section("Assumptions (not from the context)", el("div", "assume-box", data.assumptions)));
    } else if (data.action === "safer") {
      assistOut.appendChild(section("Suggested safer answer", el("div", "text-box", data.safer_answer)));
      const row = el("div", "row-actions");
      const verify = el("button", "btn primary small", "Verify this revised answer");
      verify.type = "button";
      verify.addEventListener("click", () => {
        answerEl.value = data.safer_answer;
        updateCounters();
        window.scrollTo({ top: 0, behavior: "smooth" });
        analyze();
      });
      row.appendChild(verify);
      assistOut.appendChild(row);
      if (data.changes.length) assistOut.appendChild(section("What changed", listNode(data.changes)));
      assistOut.appendChild(section("Left out because the context cannot verify it",
        data.unverifiable_items.length ? listNode(data.unverifiable_items) : el("div", "text-box", "Nothing was left out.")));
    } else if (data.action === "prompt") {
      assistOut.appendChild(section("Improved prompt", el("div", "text-box", data.improved_prompt)));
      const row = el("div", "row-actions");
      const copy = el("button", "btn ghost small", "Copy prompt");
      copy.type = "button";
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(data.improved_prompt); copy.textContent = "Copied"; }
        catch (_) { copy.textContent = "Copy failed: select the text manually"; }
      });
      row.appendChild(copy);
      assistOut.appendChild(row);
      if (data.reasons.length) assistOut.appendChild(section("Why this helps", listNode(data.reasons)));
      if (data.assumptions) assistOut.appendChild(section("Assumptions", el("div", "assume-box", data.assumptions)));
    }
    assistOut.appendChild(el("p", "assist-note", data.disclaimer));
  }

  async function runAssistant(action) {
    if (!lastRun) return;
    const buttons = document.querySelectorAll(".assist-actions .btn");
    buttons.forEach((b) => (b.disabled = true));
    showAssistLoading("Working on it...");
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 90000);
    try {
      const res = await fetch("/api/assistant", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: action,
          context: lastRun.context,
          answer: lastRun.answer,
          results: lastRun.results,
          claim_index: parseInt($("assist-claim").value, 10),
          question: $("assist-question").value,
          original_prompt: $("assist-prompt").value,
        }),
        signal: controller.signal,
      });
      let data = null;
      try { data = await res.json(); } catch (_) { /* non-JSON */ }
      if (!res.ok) throw new Error((data && data.error) || "The assistant request failed (" + res.status + ").");
      renderAssistant(data);
    } catch (err) {
      showAssistError(err.name === "AbortError" ? "The assistant took too long. Please retry." : err.message);
    } finally {
      clearTimeout(timer);
      buttons.forEach((b) => (b.disabled = false));
    }
  }

  document.querySelectorAll(".assist-actions .btn").forEach((b) =>
    b.addEventListener("click", () => runAssistant(b.dataset.action)));

  updateCounters();
  loadHealth();
})();
