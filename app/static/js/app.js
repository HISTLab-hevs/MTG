/* MTG · Mind The Gap, browser-side behaviour.
   Progressive enhancement over server-rendered HTML and htmx: no build step,
   no framework, everything wired by delegation so it survives htmx swaps. */

/* ---------- Form errors ---------- */
/* Replaces the native bubbles, which cannot be styled and vanish on click. */

/**
 * Readable message for a failed constraint.
 * @param {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement} input
 * @returns {string} Message to show under the field.
 */
function fieldMessage(input) {
  const v = input.validity;
  if (v.valueMissing) {
    if (input.type === "file") return "Please choose a file.";
    if (input.tagName === "SELECT") return "Please select an option.";
    return "This field is required.";
  }
  if (v.typeMismatch && input.type === "email") return "Please enter a valid email address.";
  if (v.tooShort) return `Must be at least ${input.minLength} characters.`;
  if (v.tooLong) return `Must be ${input.maxLength} characters or fewer.`;
  return input.validationMessage || "Invalid value.";
}

/**
 * Show a validation message under a field and mark the field invalid.
 * @param {Element} input Field that failed validation.
 * @param {string} message Message to display.
 */
function showFieldError(input, message) {
  clearFieldError(input);
  input.classList.add("is-invalid");
  const note = document.createElement("p");
  note.className = "field-error";
  // Announced the moment it appears, and tied to the field so a screen reader
  // reads the reason together with the control rather than as loose text.
  note.setAttribute("role", "alert");
  note.textContent = message;
  input.setAttribute("aria-invalid", "true");
  // The real <input type=file> inside a dropzone is visually hidden, so the
  // message would land inside the drop target, invisible. Put it after the
  // whole zone instead, where the professor is actually looking.
  const anchor = input.closest(".dropzone") || input;
  anchor.insertAdjacentElement("afterend", note);
}

/**
 * Remove a field's validation message and invalid state.
 * @param {Element} input Field to clear.
 */
function clearFieldError(input) {
  input.classList.remove("is-invalid");
  input.removeAttribute("aria-invalid");
  // Mirror showFieldError's anchor, so a message parked after a dropzone is
  // cleared as reliably as one parked after a plain field.
  const anchor = input.closest(".dropzone") || input;
  const next = anchor.nextElementSibling;
  if (next && next.classList.contains("field-error")) next.remove();
}

document.addEventListener("invalid", (e) => {
  e.preventDefault();
  showFieldError(e.target, fieldMessage(e.target));
  if (!document.activeElement?.classList.contains("is-invalid")) e.target.focus();
}, true);

["input", "change"].forEach((type) =>
  document.addEventListener(type, (e) => {
    const el = e.target;
    if (el.classList?.contains("is-invalid") && el.validity.valid) clearFieldError(el);
  })
);

/* ---------- Confirm dialogs ---------- */

/**
 * Ask for confirmation in an in-page dialog instead of window.confirm.
 * @param {string} message Question to show.
 * @param {string} [confirmLabel] Label of the confirming button.
 * @param {Function} onConfirm Called when the professor confirms.
 */
function platformConfirm(message, confirmLabel, onConfirm) {
  const dlg = document.createElement("dialog");
  dlg.className = "modal";
  dlg.innerHTML = `
    <p class="modal-message"></p>
    <div class="modal-actions">
      <button type="button" class="btn" data-cancel>Cancel</button>
      <button type="button" class="btn btn-danger" data-ok></button>
    </div>`;
  dlg.querySelector(".modal-message").textContent = message;
  const ok = dlg.querySelector("[data-ok]");
  ok.textContent = confirmLabel || "Confirm";
  dlg.addEventListener("close", () => dlg.remove());
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  dlg.querySelector("[data-cancel]").addEventListener("click", () => dlg.close());
  ok.addEventListener("click", () => { dlg.close(); onConfirm(); });
  // Enter confirms, Esc cancels (Esc is the <dialog>'s own behaviour). These
  // dialogs guard destructive actions, so the confirm button, not the
  // dialog, takes focus: Enter is then a deliberate press of what is
  // highlighted, and a stray Enter from the page behind cannot reach it.
  dlg.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); dlg.close(); onConfirm(); }
  });
  document.body.append(dlg);
  dlg.showModal();
  ok.focus();
}

/* hx-confirm on an htmx element opens the dialog above. */
document.addEventListener("htmx:confirm", (e) => {
  if (!e.detail.question) return;
  e.preventDefault();
  platformConfirm(e.detail.question, e.detail.elt.dataset.confirmLabel, () => e.detail.issueRequest(true));
});

/* data-confirm does the same for a plain, non-htmx form. */
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (!form.dataset.confirm || form.dataset.confirmed) { delete form.dataset.confirmed; return; }
  e.preventDefault();
  platformConfirm(form.dataset.confirm, form.dataset.confirmLabel, () => {
    form.dataset.confirmed = "1";
    form.requestSubmit();
  });
});


/* ---------- Suggestion block serialiser ---------- */

/**
 * Serialise an edited suggestion block back to Markdown.
 *
 * Covers only the subset render_markdown() produces: paragraphs, lists, bold,
 * italic and code. Deliberately narrow, to avoid an HTML-to-Markdown dependency.
 *
 * @param {Element} root The contenteditable block.
 * @returns {string} The block as Markdown.
 */
function htmlToMarkdown(root) {
  /* Escape the professor's text as each text node is read, never afterwards:
     once the serialiser has emitted its own markers, a second pass could not
     tell them from typed ones and would break them. Backslash goes first, or it
     would double-escape what follows. Square brackets are deliberately left
     alone: they are syntax only before a link target, and the report writes
     rule codes in brackets on nearly every line. */
  const escapeInline = (text) =>
    text.replace(/\\/g, "\\\\").replace(/([*_`])/g, "\\$1");
  // Markers that are structural only at the start of a line: a heading, a list
  // item, a horizontal rule. Mid-sentence they are ordinary punctuation, so
  // they are left alone; this runs on an already inline-escaped line.
  const escapeLineStart = (text) =>
    text.replace(/^(\s*)([-+#>=]|\d+[.)]|~~~)/, "$1\\$2");

  const inline = (node) => {
    let out = "";
    node.childNodes.forEach((n) => {
      if (n.nodeType === Node.TEXT_NODE) {
        out += escapeInline(n.textContent);
      } else if (n.nodeType === Node.ELEMENT_NODE) {
        const tag = n.tagName.toLowerCase();
        if (tag === "strong" || tag === "b") out += `**${inline(n).trim()}**`;
        else if (tag === "em" || tag === "i") out += `*${inline(n).trim()}*`;
        else if (tag === "code") out += `\`${n.textContent.trim()}\``;
        else if (tag === "br") out += "\n";
        else out += inline(n);
      }
    });
    return out;
  };

  const blocks = [];
  root.childNodes.forEach((node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      const t = escapeInline(node.textContent).trim();
      if (t) blocks.push(escapeLineStart(t));
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const tag = node.tagName.toLowerCase();
    if (tag === "ul" || tag === "ol") {
      // Only items with text become lines: Enter in a list leaves an empty
      // <li> that would render as a bare bullet. A numbered list counts the
      // kept items, so dropping one leaves no gap.
      let n = 0;
      node.querySelectorAll(":scope > li").forEach((li) => {
        // Judged on the text, not the serialised line: an empty <strong>
        // serialises to bare emphasis markers, a non-empty string holding
        // nothing.
        if (!li.textContent.trim()) return;
        const text = inline(li).trim().replace(/\s+/g, " ");
        if (!text) return;
        n += 1;
        const marker = tag === "ol" ? `${n}.` : "-";
        blocks.push(`${marker} ${escapeLineStart(text)}`);
      });
    } else {
      const text = inline(node).trim().replace(/[ \t]+/g, " ");
      if (text) blocks.push(escapeLineStart(text));
    }
  });
  // List items are contiguous lines; prose blocks are separated by a blank line.
  let md = "";
  blocks.forEach((b, i) => {
    const isItem = /^(-|\d+\.)\s/.test(b);
    const prevItem = i > 0 && /^(-|\d+\.)\s/.test(blocks[i - 1]);
    if (i > 0) md += isItem && prevItem ? "\n" : "\n\n";
    md += b;
  });
  return md.trim();
}


/* ---------- Session hygiene ---------- */

/* Wipe every saved chat on logout, so a conversation never outlives the
   session: on a shared computer the next person must not see the previous
   professor's questions. The keys are scoped per user anyway; this clears them
   eagerly rather than at tab close. */
document.addEventListener("submit", (e) => {
  const form = e.target;
  if (form.matches?.('form[action="/logout"]')) {
    for (let i = sessionStorage.length - 1; i >= 0; i--) {
      const key = sessionStorage.key(i);
      if (key && (key.startsWith("materials-chat:") || key.startsWith("report-chat:"))) {
        sessionStorage.removeItem(key);
      }
    }
  }
});


/* ---------- Keyboard and input helpers ---------- */

/* In a dialog, Ctrl/Cmd+Enter submits from a textarea. Plain Enter must stay a
   newline there (rule text is multi-line) and single-line inputs already submit
   on Enter, so this fills the one gap for keyboard users. */
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey)) return;
  const el = e.target;
  if (!el.matches?.("dialog textarea")) return;
  const form = el.form;
  if (!form) return;
  e.preventDefault();
  form.requestSubmit();
});


/**
 * Grow a textarea to fit its content.
 * @param {HTMLTextAreaElement} el
 */
function resizeAutoGrow(el) {
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight}px`;
}

document.querySelectorAll("textarea.auto-grow").forEach((el) => resizeAutoGrow(el));
document.addEventListener("input", (e) => {
  if (e.target.matches?.("textarea.auto-grow")) resizeAutoGrow(e.target);
});


/**
 * Refresh the "used/maximum" counter paired with a field by data-count-for.
 * @param {HTMLInputElement|HTMLTextAreaElement} input
 */
function updateCharCount(input) {
  const counter = document.querySelector(`[data-count-for="${input.name}"]`);
  if (!counter || !input.maxLength || input.maxLength < 0) return;
  counter.textContent = `${input.value.length}/${input.maxLength}`;
}

document.querySelectorAll("input[maxlength], textarea[maxlength]").forEach((el) => updateCharCount(el));
document.addEventListener("input", (e) => {
  if (e.target.matches?.("input[maxlength], textarea[maxlength]")) updateCharCount(e.target);
});


/* ---------- Rules list ---------- */

/* Toggling, accepting, deleting or saving a rule replaces a tall subtree, so
   the scroll position is kept. Restored in afterSwap, the same task as the
   mutation, so no intermediate position is ever painted. */
let rulesAreaScroll = null;
document.addEventListener("htmx:beforeSwap", (e) => {
  if (e.detail?.target?.id === "rules-area") rulesAreaScroll = window.scrollY;
});
document.addEventListener("htmx:afterSwap", (e) => {
  if (e.detail?.target?.id === "rules-area" && rulesAreaScroll !== null) {
    window.scrollTo(0, rulesAreaScroll);
    rulesAreaScroll = null;
  }
});


/* ---------- Report comparison picker ---------- */

/* Wired once by delegation, not inside the partial: the analysis poll re-inserts
   this panel every two seconds, so a script in the fragment would add a fresh
   listener per poll, each with its own idea of what was ticked.

   The selection therefore lives in the DOM, the only state a swap preserves.
   Ticking a third box replaces the oldest rather than refusing; tick order is
   not recorded, so the box just ticked is stamped and the other stamped one
   goes. */

/** Enable Compare only on exactly two ticked reports, and update its count. */
function syncReportCompare() {
  const btn = document.getElementById("report-compare-btn");
  if (!btn) return;
  const n = document.querySelectorAll(".report-pick-box:checked").length;
  btn.disabled = n !== 2;
  btn.title = n === 2
    ? "Compare the two selected reports"
    : "Tick two reports below to compare them";
  const count = document.getElementById("report-compare-count");
  if (count) count.textContent = n === 0 ? "" : `${n} of 2 selected`;
}

document.addEventListener("change", (e) => {
  const box = e.target;
  if (!box.matches?.(".report-pick-box")) return;
  if (box.checked) {
    const picked = [...document.querySelectorAll(".report-pick-box:checked")];
    // Drop the oldest: the one still carrying a stamp from an earlier tick.
    while (picked.length > 2) {
      const oldest = picked.find((b) => b !== box && b.dataset.picked) || picked[0];
      oldest.checked = false;
      delete oldest.dataset.picked;
      picked.splice(picked.indexOf(oldest), 1);
    }
    box.dataset.picked = "1";
  } else {
    delete box.dataset.picked;
  }
  syncReportCompare();
});

// The poll swaps in new checkboxes with the server's ticks; re-read them.
document.addEventListener("htmx:afterSwap", syncReportCompare);
syncReportCompare();


/* ---------- Document-type pickers ---------- */

/**
 * Wire a "Select all" shortcut over a group of document-type checkboxes.
 *
 * A text action, not a checkbox: it carries no value of its own and is not one
 * of the choices, so it must not be styled as one. Flips to "Clear" once
 * everything is ticked, so one control covers both directions.
 *
 * @param {Element} field Container holding the checkboxes.
 * @param {Element} toggle The shortcut control.
 * @param {Function} [onSync] Each page's own extra, an inline error to clear or
 *   a note to reveal; runs after every change and on form reset.
 * @returns {Function} The sync function, to call after changing boxes in code.
 */
function wireSelectAll(field, toggle, onSync) {
  if (!field || !toggle) return () => {};
  const boxes = [...field.querySelectorAll('input[name="document_type"]')];
  const sync = () => {
    const all = boxes.length > 0 && boxes.every((b) => b.checked);
    toggle.textContent = all ? "Clear" : "Select all";
    toggle.setAttribute("aria-pressed", all ? "true" : "false");
    if (onSync) onSync(boxes);
  };
  toggle.addEventListener("click", () => {
    const turnOn = !boxes.every((b) => b.checked);
    boxes.forEach((b) => { b.checked = turnOn; });
    sync();
  });
  boxes.forEach((b) => b.addEventListener("change", sync));
  // A form is reset after a successful submit; re-sync on the next tick, once
  // the browser has actually cleared the boxes.
  field.closest("form")?.addEventListener("reset", () => setTimeout(sync));
  sync();
  return sync;
}


/* ---------- Chat panes ---------- */

/**
 * Set up a chat pane, shared by the report assistant and the materials chat.
 *
 * Both are the same widget: an auto-growing box, Enter to send, an optimistic
 * pending turn, a conversation in sessionStorage and never on the server, and
 * older context compacted before it outgrows the prompt budget.
 *
 * @param {Object} opts What differs between the two panes.
 * @param {string} opts.logId Id of the conversation log.
 * @param {string} opts.messageId Id of the message box.
 * @param {string} opts.historyId Id of the hidden history field.
 * @param {string} opts.newBtnId Id of the "New chat" button.
 * @param {string} opts.emptyId Id of the empty state, restored by "New chat".
 * @param {string} opts.storeKey sessionStorage key for this conversation.
 * @param {string} opts.compactUrl Endpoint that compacts the history.
 * @param {Function} [opts.assistantExtra] Pulls extra content out of a rendered
 *   turn, used by the report chat for its edit record.
 * @param {Function} [opts.enterHandled] Lets the page consume Enter first.
 * @param {Function} [opts.afterSwap] Runs after each turn is swapped in.
 * @returns {Object} The elements and functions the page still wires itself.
 */
function setupChat(opts) {
  const log = document.getElementById(opts.logId);
  const message = document.getElementById(opts.messageId);
  const history = document.getElementById(opts.historyId);
  const newBtn = document.getElementById(opts.newBtnId);
  const form = message.closest("form");
  // Kept so "New chat" can restore the empty state (intro + starter chips).
  const emptyHTML = document.getElementById(opts.emptyId).outerHTML;
  const TTL_MS = 2 * 60 * 60 * 1000; // 2 hours
  const COMPACT_THRESHOLD_CHARS = 24000;
  const maxChars = Number(message.dataset.maxChars || 0);
  let compactSummary = null;
  let lastSent = "";

  const limitNote = document.createElement("p");
  limitNote.className = "chat-context-note chat-limit-note";
  limitNote.setAttribute("aria-live", "polite");
  limitNote.hidden = true;
  form.appendChild(limitNote);

  const compactNote = document.createElement("p");
  compactNote.className = "chat-context-note chat-compact-note text-muted";
  compactNote.hidden = true;
  form.appendChild(compactNote);

  // Array.from counts Unicode code points the way Python's len() does, whereas
  // string.length counts an emoji as two UTF-16 units. Both guards must agree.
  const characterCount = () => Array.from(message.value).length;
  const isTooLong = () => maxChars > 0 && characterCount() > maxChars;
  const updateLimit = () => {
    if (!maxChars) return;
    const used = characterCount();
    const over = used - maxChars;
    if (over > 0) {
      limitNote.textContent = `Maximum ${maxChars.toLocaleString()} characters. ` +
        `Shorten the message by ${over.toLocaleString()} before sending.`;
      limitNote.classList.add("chat-context-warn");
      limitNote.hidden = false;
      message.setAttribute("aria-invalid", "true");
    } else if (used >= maxChars * 0.9) {
      limitNote.textContent = `${(maxChars - used).toLocaleString()} characters ` +
        `remaining (maximum ${maxChars.toLocaleString()}).`;
      limitNote.classList.remove("chat-context-warn");
      limitNote.hidden = false;
      message.removeAttribute("aria-invalid");
    } else {
      limitNote.hidden = true;
      message.removeAttribute("aria-invalid");
    }
  };
  const grow = () => {
    message.style.height = "auto";
    const full = message.scrollHeight;
    message.style.height = `${Math.min(full, 160)}px`;
    // Scroll internally only once the height cap is reached; below it keep
    // overflow hidden so no scrollbar arrows show on a short input.
    message.classList.toggle("is-scroll", full > 160);
    updateLimit();
    // The one place that keeps Send in step: every path that changes the box
    // comes through here, and setting the value in code fires no input event.
    syncChatSend(message);
  };
  const scrollDown = () => { log.scrollTop = log.scrollHeight; };
  const stampTurns = () => {
    const turns = [...log.querySelectorAll(".chat-turn:not(.chat-pending)")];
    const base = Date.now() - turns.length;
    turns.forEach((t, i) => { if (!t.dataset.at) t.dataset.at = String(base + i); });
  };
  const setCompacting = (busy, text) => {
    // Animated so the pause reads as work in progress, not a frozen chat;
    // the input is locked for the duration and always unlocked in finally.
    compactNote.innerHTML = "";
    if (busy && text) {
      const spin = document.createElement("span");
      spin.className = "spinner spinner-sm";
      compactNote.append(spin, ` ${text}`);
    } else if (text) {
      compactNote.textContent = text;
    }
    compactNote.hidden = !busy && !text;
    message.disabled = busy;
    if (busy) form.querySelector('button[type="submit"]').disabled = true;
    else syncChatSend(message);
  };
  const refreshNewBtn = () => {
    newBtn.hidden = !log.querySelector(".chat-turn:not(.chat-pending)");
  };
  const persist = () => {
    const turns = log.querySelectorAll(".chat-turn:not(.chat-pending)");
    if (!turns.length) { sessionStorage.removeItem(opts.storeKey); return; }
    const html = Array.from(turns).map((t) => t.outerHTML).join("");
    sessionStorage.setItem(opts.storeKey,
      JSON.stringify({ at: Date.now(), html, summary: compactSummary }));
  };
  const restore = () => {
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem(opts.storeKey) || "null"); }
    catch { saved = null; }
    if (!saved || Date.now() - saved.at > TTL_MS) {
      sessionStorage.removeItem(opts.storeKey);
      return;
    }
    compactSummary = saved.summary || null;
    document.getElementById(opts.emptyId)?.remove();
    log.insertAdjacentHTML("beforeend", saved.html);
    scrollDown();
  };
  // Rebuild the history from the rendered turns for the next request; the
  // optimistic pending turn and anything already compacted are excluded.
  const syncHistory = () => {
    stampTurns();
    const turns = [];
    if (compactSummary?.content) turns.push(compactSummary);
    log.querySelectorAll(
      ".chat-turn:not(.chat-pending):not([data-compacted='1'])"
    ).forEach((t) => {
      const at = Number(t.dataset.at || Date.now());
      const u = t.querySelector(".chat-msg-user");
      const a = t.querySelector(".chat-msg-assistant");
      if (u) turns.push({ role: "user", content: u.textContent.trim(), at });
      if (a) {
        // The report chat appends its "Updated: <blocks>" line here, so the
        // model has a deterministic record of which turns really edited what.
        const extra = opts.assistantExtra ? opts.assistantExtra(t) : "";
        turns.push({ role: "assistant", content: a.textContent.trim() + extra, at });
      }
    });
    history.value = JSON.stringify(turns);
  };
  const applyCompaction = (payload) => {
    if (!payload?.history) return;
    compactSummary = payload.history.find((t) => t.role === "summary") || compactSummary;
    if (payload.compacted_until_at) {
      log.querySelectorAll(".chat-turn:not(.chat-pending)").forEach((t) => {
        if (Number(t.dataset.at || 0) <= payload.compacted_until_at) t.dataset.compacted = "1";
      });
    }
    syncHistory();
    persist();
  };
  const ensureCompacted = async () => {
    syncHistory();
    if (history.value.length < COMPACT_THRESHOLD_CHARS) return;
    setCompacting(true, "Compacting older chat context. Please wait…");
    try {
      const res = await fetch(opts.compactUrl, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ history: history.value }),
      });
      if (res.ok) applyCompaction(await res.json());
    } catch {
      // Do not strand the professor if compaction fails; the backend prompt
      // budgeter still protects the model call.
    } finally {
      setCompacting(false, "");
    }
  };
  const addPending = (text) => {
    document.getElementById(opts.emptyId)?.remove();
    const turn = document.createElement("div");
    turn.className = "chat-turn chat-pending";
    const user = document.createElement("p");
    user.className = "chat-msg chat-msg-user";
    user.textContent = text;
    const dots = document.createElement("p");
    dots.className = "chat-msg chat-msg-assistant chat-typing";
    dots.innerHTML = "<span></span><span></span><span></span>";
    turn.append(user, dots);
    log.appendChild(turn);
    scrollDown();
  };
  const clearPending = () => {
    log.querySelectorAll(".chat-pending").forEach((t) => t.remove());
  };
  // Never leave the typing dots forever: drop the placeholder, put the text
  // back for a one-click resend, say what happened.
  const failTurn = () => {
    if (!log.querySelector(".chat-pending")) return; // already handled
    clearPending();
    if (lastSent && !message.value.trim()) { message.value = lastSent; grow(); }
    const note = document.createElement("p");
    note.className = "chat-msg chat-msg-assistant chat-error";
    note.textContent = "That message didn't go through (temporary error). " +
      "Your text is back in the box, send it again.";
    log.appendChild(note);
    scrollDown();
    message.focus();
  };
  const newChat = () => {
    sessionStorage.removeItem(opts.storeKey);
    compactSummary = null;
    log.innerHTML = emptyHTML;
    history.value = "[]";
    message.value = "";
    grow();
    refreshNewBtn();
    message.focus();
  };

  message.addEventListener("input", grow);
  message.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      // The report chat opens an @-mention menu that must consume Enter first.
      if (opts.enterHandled && opts.enterHandled(e)) return;
      e.preventDefault();
      if (message.value.trim() && !isTooLong()) form.requestSubmit();
    }
  });
  // Compact the history (if needed) before the turn is actually sent; the real
  // submit is re-triggered once compaction has settled, marked by compactReady
  // so this handler lets it through instead of looping.
  form.addEventListener("submit", async (e) => {
    if (isTooLong()) {
      delete form.dataset.compactReady;
      e.preventDefault();
      grow();
      return;
    }
    if (form.dataset.compactReady === "1") { delete form.dataset.compactReady; return; }
    e.preventDefault();
    if (!message.value.trim()) return;
    await ensureCompacted();
    // If anything emptied the box during the wait, resubmitting would leak the
    // compactReady flag and wedge the next send.
    if (!message.value.trim()) return;
    form.dataset.compactReady = "1";
    form.requestSubmit();
  });
  form.addEventListener("htmx:beforeRequest", () => {
    log.querySelectorAll(".chat-error").forEach((n) => n.remove());
    syncHistory();
    lastSent = message.value.trim();
    addPending(lastSent);
    message.value = "";
    grow();
  });
  log.addEventListener("htmx:afterSwap", () => {
    clearPending();
    persist();
    refreshNewBtn();
    scrollDown();
    message.focus();
    if (opts.afterSwap) opts.afterSwap();
  });
  form.addEventListener("htmx:afterRequest", (e) => {
    if (e.detail.successful === false) failTurn();
  });
  form.addEventListener("htmx:sendError", failTurn);
  form.addEventListener("htmx:timeout", failTurn);
  newBtn.addEventListener("click", newChat);

  restore();
  refreshNewBtn();
  grow();
  return { log, message, form, grow, scrollDown, syncHistory };
}


/* An empty chat message must never reach the server: the endpoint answers with
   a validation blob that lands straight in the message log.

   htmx listens for submit on the form itself, so preventDefault() in another
   submit listener stops the browser but not the request; htmx:configRequest is
   the only hook that cancels one. The Send button is disabled on an empty box
   too, making the rule visible before the click; this is the backstop for Enter
   and for anything submitting the form directly. */
document.addEventListener("htmx:configRequest", (e) => {
  const form = e.detail.elt;
  if (!form.matches?.("form.report-chat-form")) return;
  const box = form.querySelector('textarea[name="message"]');
  const maxChars = Number(box?.dataset.maxChars || 0);
  const used = box ? Array.from(box.value).length : 0;
  if (box && (!box.value.trim() || (maxChars && used > maxChars))) {
    e.preventDefault();
  }
});

/* Wired by delegation: covers both chats and survives htmx swaps. */
/**
 * Keep a chat's Send button disabled until there is something to send.
 * @param {HTMLTextAreaElement} box The message box.
 */
function syncChatSend(box) {
  const form = box.form;
  const send = form?.querySelector('button[type="submit"]');
  // Never fight hx-disabled-elt: while a turn is in flight htmx owns the
  // button's disabled state, and re-enabling it here would let a second send
  // race the first.
  if (send && !form.classList.contains("htmx-request")) {
    const maxChars = Number(box.dataset.maxChars || 0);
    const used = Array.from(box.value).length;
    send.disabled = !box.value.trim() || (maxChars && used > maxChars);
  }
}
document.addEventListener("input", (e) => {
  if (e.target.matches?.('form.report-chat-form textarea[name="message"]')) {
    syncChatSend(e.target);
  }
});
// htmx re-enables the controls after a request, which would leave Send enabled
// over a box just emptied.
document.addEventListener("htmx:afterRequest", (e) => {
  const form = e.detail?.elt;
  if (!form?.matches?.("form.report-chat-form")) return;
  const box = form.querySelector('textarea[name="message"]');
  if (box) setTimeout(() => syncChatSend(box));
});
document
  .querySelectorAll('form.report-chat-form textarea[name="message"]')
  .forEach(syncChatSend);


/* ---------- Disclosure state ---------- */

/* Keep every open disclosure open across an htmx swap.

   Panels that poll while work is in progress replace their subtree every few
   seconds, which would snap a <details> shut exactly while it is being read.
   The state is keyed by the element's own data-key and reapplied in afterSwap,
   the same task as the mutation, so the closed frame is never painted. Anything
   with a data-key participates, with no per-page wiring. Held in sessionStorage
   per page, so a reload keeps what was open without leaking between courses. */
(function () {
  const KEY = `mtg-open:${location.pathname}`;
  // Two sets, not one: a disclosure the page renders open by default must stay
  // open until closed by hand, so only an explicit entry in the closed set
  // means "close it" — being absent from the opened set does not.
  const read = () => {
    try {
      const raw = JSON.parse(sessionStorage.getItem(KEY) || "{}");
      return { open: new Set(raw.open || []), shut: new Set(raw.shut || []) };
    } catch {
      return { open: new Set(), shut: new Set() };
    }
  };
  const write = (state) => {
    try {
      sessionStorage.setItem(
        KEY,
        JSON.stringify({ open: [...state.open], shut: [...state.shut] })
      );
    } catch {
      /* Storage full or blocked: the panels still work, they just forget. */
    }
  };
  const restore = (root) => {
    const { open, shut } = read();
    (root || document).querySelectorAll("details[data-key]").forEach((d) => {
      if (open.has(d.dataset.key)) d.open = true;
      else if (shut.has(d.dataset.key)) d.open = false;
    });
  };
  // The toggle event does not bubble, so it is caught in the capture phase.
  document.addEventListener("toggle", (e) => {
    const d = e.target;
    if (!d.matches?.("details[data-key]")) return;
    const state = read();
    const key = d.dataset.key;
    if (d.open) {
      state.open.add(key);
      state.shut.delete(key);
    } else {
      state.shut.add(key);
      state.open.delete(key);
    }
    write(state);
  }, true);
  document.addEventListener("htmx:afterSwap", (e) => restore(e.detail?.target));
  restore(document);
})();


/* ---------- Custom select ---------- */

/**
 * Render a native select as a stylable button and listbox.
 *
 * The open list is drawn by the OS and always clashes with the app, so the
 * select is visually hidden, keeping its form semantics, and a button and
 * listbox stand in for it.
 *
 * @param {HTMLSelectElement} select The select to enhance.
 */
function enhanceSelect(select) {
  select.dataset.enhanced = "true";
  const wrap = document.createElement("div");
  wrap.className = "select-wrap";
  select.parentNode.insertBefore(wrap, select);
  wrap.appendChild(select);

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "select-trigger";
  trigger.setAttribute("aria-haspopup", "listbox");
  trigger.setAttribute("aria-expanded", "false");
  const label = document.createElement("span");
  label.className = "select-trigger-label";
  trigger.appendChild(label);
  wrap.appendChild(trigger);

  const menu = document.createElement("ul");
  menu.className = "select-menu";
  menu.setAttribute("role", "listbox");
  menu.hidden = true;
  wrap.appendChild(menu);

  let active = -1;
  const syncLabel = () => {
    label.textContent = select.selectedOptions[0]?.text || "";
  };
  const highlight = () => {
    [...menu.children].forEach((el, i) => el.classList.toggle("is-active", i === active));
    menu.children[active]?.scrollIntoView({ block: "nearest" });
  };
  const close = () => {
    menu.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
  };
  const choose = (i) => {
    select.selectedIndex = i;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    syncLabel();
    close();
    trigger.focus();
  };
  const open = () => {
    menu.innerHTML = "";
    [...select.options].forEach((opt, i) => {
      const li = document.createElement("li");
      li.className = "select-option";
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === select.selectedIndex));
      li.textContent = opt.text;
      li.addEventListener("click", () => choose(i));
      menu.appendChild(li);
    });
    menu.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    active = select.selectedIndex;
    highlight();
  };

  trigger.addEventListener("click", () => (menu.hidden ? open() : close()));
  trigger.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (menu.hidden) return open();
      const step = e.key === "ArrowDown" ? 1 : -1;
      active = Math.min(Math.max(active + step, 0), select.options.length - 1);
      highlight();
    } else if ((e.key === "Enter" || e.key === " ") && !menu.hidden) {
      e.preventDefault();
      choose(active);
    } else if (e.key === "Escape" && !menu.hidden) {
      e.stopPropagation(); // keep an open dialog from closing on the same Esc
      close();
    }
  });
  document.addEventListener("click", (e) => {
    if (!wrap.contains(e.target)) close();
  });
  syncLabel();
}

/**
 * Enhance every not-yet-enhanced fancy select under a root.
 * @param {Element} [root] Defaults to the whole document.
 */
function enhanceSelects(root) {
  (root || document)
    .querySelectorAll("select.fancy-select:not([data-enhanced])")
    .forEach(enhanceSelect);
}

enhanceSelects(document);
