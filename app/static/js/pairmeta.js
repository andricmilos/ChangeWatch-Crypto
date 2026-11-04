"use strict";

// Tags and a short note per pair in the markets table (/api/tags, /api/pairs).
// - Both lists load once with GET and then follow tags_updated / pair_meta_updated from
//   /ws/market. Messages that arrive while loading are buffered and applied afterwards; each
//   one carries the full state it describes, so applying one twice is harmless. After a
//   reconnect the lists load again, because messages sent while offline are lost.
// - A pair's chips take name and color from the tag list: renaming a tag only sends
//   tags_updated, not a pair_meta_updated for every pair that has it.
// - Responses of successful writes are applied right away as well, so the table does not wait
//   for the WebSocket; the later message is then a no-op.
// - Tag names and note text are only ever set via textContent; colors are CSS classes.
// - app.js owns the rows and the other filters: it calls init() and asks matches(symbol).

const pairMetaPanel = (() => {
  const COLORS = ["green", "red", "yellow", "blue", "purple", "orange", "teal", "gray"];
  const COLOR_LABELS = {
    green: "Zelena",
    red: "Crvena",
    yellow: "Žuta",
    blue: "Plava",
    purple: "Ljubičasta",
    orange: "Narandžasta",
    teal: "Tirkizna",
    gray: "Siva",
  };
  const MAX_VISIBLE_TAGS = 3;
  const MAX_TAGS_PER_PAIR = 5;
  const NAME_MAX_LENGTH = 20;
  const NOTE_MAX_LENGTH = 500;
  const FILTER_STORAGE_KEY = "changewatch.tagFilter";
  const DATE_LOCALE = "sr-Latn-RS";
  // Keys are fragments of the server's English validation messages (app/schemas/tag.py).
  const VALIDATION_MESSAGES = [
    ["Name must not be empty", "Ime oznake ne sme biti prazno."],
    ["Name must be at most", `Ime oznake može imati najviše ${NAME_MAX_LENGTH} znakova.`],
    ["Text must be at most", `Beleška može imati najviše ${NOTE_MAX_LENGTH} znakova.`],
    ["Input should be", "Izabrana boja nije podržana."],
  ];

  const elements = {
    section: document.querySelector(".panel-markets"),
    body: document.getElementById("markets-body"),
    tableWrap: document.querySelector(".panel-markets .table-wrap"),
    filter: document.getElementById("tag-filter"),
    filterName: document.getElementById("tag-filter-name"),
    filterClear: document.getElementById("tag-filter-clear"),
    manageButton: document.getElementById("tags-manage"),
    manager: document.getElementById("tags-manager"),
    managerEmpty: document.getElementById("tags-manager-empty"),
    managerList: document.getElementById("tags-manager-list"),
    managerError: document.getElementById("tags-manager-error"),
    tooltip: document.getElementById("pair-tooltip"),
    panel: document.getElementById("pair-panel"),
    panelTitle: document.getElementById("pair-panel-title"),
    panelTagCount: document.getElementById("pair-panel-tag-count"),
    panelNoTags: document.getElementById("pair-panel-no-tags"),
    panelTags: document.getElementById("pair-panel-tags"),
    panelNewName: document.getElementById("pair-panel-new-name"),
    panelNewAdd: document.getElementById("pair-panel-new-add"),
    panelNewColors: document.getElementById("pair-panel-new-colors"),
    panelNote: document.getElementById("pair-panel-note"),
    panelNoteCount: document.getElementById("pair-panel-note-count"),
    panelError: document.getElementById("pair-panel-error"),
    panelCancel: document.getElementById("pair-panel-cancel"),
  };
  elements.table = elements.body.closest("table");

  const state = {
    rows: new Map(),
    displaySymbol: (symbol) => symbol,
    onFilterChange: () => {},
    writeEnabled: false,
    loadId: 0,
    loading: false,
    buffer: [],
    connectedBefore: false,
    // id -> TagWithCount, sorted by name.
    tags: new Map(),
    // False until a tag list arrived: before that an unknown id may still exist.
    tagsKnown: false,
    // symbol -> { tags: TagOut[], note: { text, updated_at } | null }
    metas: new Map(),
    metasKnown: false,
    filterTagId: null,
    // { trigger, pinned } while the tooltip is shown.
    tooltip: null,
    // { symbol, button, tagIds: Set, busy } while the "⋯" panel is open.
    panel: null,
    newColor: COLORS[0],
    // id -> { name, color } typed in the manager but not saved yet.
    managerDrafts: new Map(),
    confirmDeleteId: null,
    managerBusy: false,
  };

  // ---------- Helpers ----------

  function textLength(text) {
    return [...text.trim()].length;
  }

  function colorClass(color) {
    return `tag-${COLORS.includes(color) ? color : "gray"}`;
  }

  function compareNames(a, b) {
    return a.name.localeCompare(b.name, DATE_LOCALE, { sensitivity: "base" });
  }

  // "1 par", "3 para", "5 parova"; the locative after "na" is "1 paru" and otherwise the same.
  function pairsText(count, locative = false) {
    const mod10 = count % 10;
    const mod100 = count % 100;
    if (mod10 === 1 && mod100 !== 11) return `${count} ${locative ? "paru" : "par"}`;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return `${count} para`;
    return `${count} parova`;
  }

  function deleteQuestion(tag) {
    if (tag.pair_count === 0) return "Oznaka se ne koristi ni na jednom paru. Obrisati?";
    return `Oznaka se koristi na ${pairsText(tag.pair_count, true)}. Obrisati?`;
  }

  function jsonRequest(method, body) {
    return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  }

  async function errorMessage(response) {
    if (response.status === 429) {
      const seconds = response.headers.get("Retry-After");
      return seconds
        ? `Previše zahteva. Pokušaj ponovo za ${seconds} s.`
        : "Previše zahteva. Pokušaj ponovo malo kasnije.";
    }
    let detail = null;
    try {
      ({ detail } = await response.json());
    } catch (error) {
      console.warn(`Tag request failed with HTTP ${response.status} and no JSON body`, error);
    }
    if (response.status === 422 && Array.isArray(detail)) {
      const messages = detail.map((item) => {
        const match = VALIDATION_MESSAGES.find(([key]) => String(item.msg).includes(key));
        return match ? match[1] : "Podaci nisu ispravni.";
      });
      return [...new Set(messages)].join(" ");
    }
    // 400, 403, 404, 409 and the route's own 422 carry a detail written for the user.
    if (typeof detail === "string") return detail;
    return `Zahtev nije uspeo (HTTP ${response.status}).`;
  }

  // Resolves to { body } on success or { error } with a message for the user.
  async function send(url, options) {
    let response;
    try {
      response = await fetch(url, options);
    } catch (error) {
      console.warn(`Request to ${url} failed`, error);
      return { error: "Server nije dostupan. Pokušaj ponovo." };
    }
    if (!response.ok) return { error: await errorMessage(response) };
    if (response.status === 204) return { body: null };
    try {
      return { body: await response.json() };
    } catch (error) {
      console.warn(`Response of ${url} is not JSON`, error);
      return { body: null };
    }
  }

  function showError(element, message) {
    element.textContent = message ?? "";
    element.hidden = message === null;
  }

  function createChip(tag, onClick) {
    const chip = document.createElement(onClick ? "button" : "span");
    chip.className = `tag-chip ${colorClass(tag.color)}`;
    chip.textContent = tag.name;
    if (onClick) {
      chip.type = "button";
      chip.setAttribute("aria-label", `Prikaži samo parove sa oznakom ${tag.name}`);
      chip.setAttribute("aria-pressed", String(state.filterTagId === tag.id));
      chip.addEventListener("click", (event) => {
        // A chip filters the table; it must not also select the row for the chart.
        event.stopPropagation();
        onClick(tag);
      });
    }
    return chip;
  }

  function createColorPicker(groupName, selected, label, onChange) {
    const group = document.createElement("div");
    group.className = "tag-colors";
    group.setAttribute("role", "radiogroup");
    group.setAttribute("aria-label", label);
    for (const color of COLORS) {
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = groupName;
      radio.value = color;
      radio.checked = color === selected;
      radio.className = `tag-swatch ${colorClass(color)}`;
      radio.setAttribute("aria-label", COLOR_LABELS[color]);
      radio.title = COLOR_LABELS[color];
      radio.dataset.focusKey = `${groupName}:${color}`;
      radio.addEventListener("change", () => onChange(color));
      group.append(radio);
    }
    return group;
  }

  // Re-rendering a list replaces its elements; this puts the focus back on the same control.
  function keepFocus(render) {
    const active = document.activeElement;
    const key = active instanceof HTMLElement ? active.dataset.focusKey : undefined;
    const selection =
      active instanceof HTMLInputElement && active.type === "text"
        ? [active.selectionStart, active.selectionEnd]
        : null;
    render();
    if (key === undefined) return;
    const next = document.querySelector(`[data-focus-key="${CSS.escape(key)}"]`);
    if (!(next instanceof HTMLElement)) return;
    next.focus();
    if (selection !== null && next instanceof HTMLInputElement) next.setSelectionRange(...selection);
  }

  // ---------- Data ----------

  function pairTags(symbol) {
    const meta = state.metas.get(symbol);
    if (!meta) return [];
    return meta.tags
      .map((tag) => state.tags.get(tag.id) ?? (state.tagsKnown ? null : tag))
      .filter((tag) => tag !== null)
      .sort(compareNames);
  }

  function setTags(list) {
    state.tags = new Map([...list].sort(compareNames).map((tag) => [tag.id, tag]));
    state.tagsKnown = true;
    for (const id of state.managerDrafts.keys()) {
      if (!state.tags.has(id)) state.managerDrafts.delete(id);
    }
    if (state.confirmDeleteId !== null && !state.tags.has(state.confirmDeleteId)) {
      state.confirmDeleteId = null;
    }
    if (state.filterTagId !== null && !state.tags.has(state.filterTagId)) {
      // The tag was deleted (maybe in another tab): the filter would hide every row.
      setFilter(null);
    } else {
      renderFilter();
    }
    renderAllRows();
    renderManager();
    if (state.panel) {
      for (const id of state.panel.tagIds) {
        if (!state.tags.has(id)) state.panel.tagIds.delete(id);
      }
      renderPanelTags();
    }
  }

  // A single tag from a POST/PATCH response; it has no pair_count, so the old one is kept.
  function mergeTag(tag) {
    const pairCount = state.tags.get(tag.id)?.pair_count ?? 0;
    setTags([...state.tags.values()].filter((other) => other.id !== tag.id).concat({
      ...tag,
      pair_count: pairCount,
    }));
  }

  function setPairMeta(meta) {
    if (!state.rows.has(meta.symbol)) return;
    state.metas.set(meta.symbol, { tags: meta.tags, note: meta.note });
    renderRow(meta.symbol);
    state.onFilterChange();
  }

  function applyMessage(message) {
    if (message.type === "tags_updated") setTags(message.data);
    if (message.type === "pair_meta_updated") setPairMeta(message.data);
  }

  function handleMessage(message) {
    if (state.loading) {
      state.buffer.push(message);
      return;
    }
    applyMessage(message);
  }

  async function load() {
    const loadId = ++state.loadId;
    state.loading = true;
    state.buffer = [];
    try {
      const [tagsResponse, metaResponse] = await Promise.all([
        fetch("/api/tags"),
        fetch("/api/pairs/meta"),
      ]);
      if (!tagsResponse.ok) throw new Error(`/api/tags: HTTP ${tagsResponse.status}`);
      if (!metaResponse.ok) throw new Error(`/api/pairs/meta: HTTP ${metaResponse.status}`);
      const [tags, metas] = await Promise.all([tagsResponse.json(), metaResponse.json()]);
      if (loadId !== state.loadId) return;
      state.metas = new Map(metas.map((meta) => [meta.symbol, meta]));
      state.metasKnown = true;
      setTags(tags);
    } catch (error) {
      if (loadId !== state.loadId) return;
      console.warn("Could not load pair tags and notes", error);
    }
    state.loading = false;
    for (const message of state.buffer.splice(0)) applyMessage(message);
    state.onFilterChange();
  }

  async function loadConfig() {
    try {
      const response = await fetch("/api/notes/config");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const config = await response.json();
      state.writeEnabled = config.write_enabled === true;
    } catch (error) {
      console.warn("Could not load the notes config; tags stay read-only", error);
      state.writeEnabled = false;
    }
    elements.table.classList.toggle("meta-readonly", !state.writeEnabled);
    elements.manageButton.hidden = !state.writeEnabled;
    for (const row of state.rows.values()) {
      row.querySelector(".row-menu").hidden = !state.writeEnabled;
    }
    if (!state.writeEnabled) {
      closePanel(false);
      toggleManager(false);
    }
  }

  // ---------- Table ----------

  function renderRow(symbol) {
    const row = state.rows.get(symbol);
    const tags = pairTags(symbol);
    const visible = tags.slice(0, MAX_VISIBLE_TAGS);
    const chips = visible.map((tag) => createChip(tag, toggleFilterTag));
    if (tags.length > visible.length) chips.push(createMoreChip(symbol, tags.length - visible.length));
    row.querySelector(".pair-tags").replaceChildren(...chips);

    const note = state.metas.get(symbol)?.note ?? null;
    const icon = row.querySelector(".pair-note-icon");
    icon.hidden = note === null;
    icon.setAttribute("aria-label", `Beleška o paru ${state.displaySymbol(symbol)}`);

    // The trigger of an open tooltip may just have been replaced or hidden.
    const trigger = state.tooltip?.trigger;
    if (trigger && (!trigger.isConnected || trigger.hidden)) {
      hideTooltip();
    } else if (trigger && row.contains(trigger)) {
      fillTooltip(trigger);
    }
  }

  function renderAllRows() {
    for (const symbol of state.rows.keys()) renderRow(symbol);
  }

  function createMoreChip(symbol, count) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "tag-more";
    button.textContent = `+${count}`;
    button.dataset.symbol = symbol;
    const names = pairTags(symbol).map((tag) => tag.name).join(", ");
    button.setAttribute("aria-label", `Sve oznake: ${names}`);
    attachTooltip(button);
    return button;
  }

  // ---------- Tooltip (all tags, pair note) ----------

  function fillTooltip(trigger) {
    const symbol = trigger.closest("tr").dataset.symbol;
    if (trigger.classList.contains("tag-more")) {
      const chips = pairTags(symbol).map((tag) =>
        createChip(tag, (clicked) => {
          hideTooltip();
          toggleFilterTag(clicked);
        }),
      );
      const list = document.createElement("div");
      list.className = "pair-tooltip-tags";
      list.append(...chips);
      elements.tooltip.replaceChildren(list);
      return;
    }
    const note = state.metas.get(symbol)?.note;
    if (!note) {
      hideTooltip();
      return;
    }
    const text = document.createElement("p");
    text.className = "pair-tooltip-note";
    text.textContent = note.text;
    const updated = document.createElement("p");
    updated.className = "note-muted pair-tooltip-time";
    const updatedAt = new Date(note.updated_at);
    updated.textContent = Number.isNaN(updatedAt.getTime())
      ? ""
      : `Izmenjeno ${updatedAt.toLocaleString(DATE_LOCALE)}`;
    elements.tooltip.replaceChildren(text, updated);
  }

  function showTooltip(trigger, pinned) {
    if (state.tooltip && state.tooltip.trigger !== trigger) hideTooltip();
    state.tooltip = { trigger, pinned };
    fillTooltip(trigger);
    if (state.tooltip === null) return;
    elements.tooltip.classList.toggle("is-pinned", pinned);
    elements.tooltip.hidden = false;
    trigger.setAttribute("aria-describedby", elements.tooltip.id);
    trigger.setAttribute("aria-expanded", String(pinned));
    placeBelow(elements.tooltip, trigger, false);
  }

  function hideTooltip() {
    if (state.tooltip === null) return;
    state.tooltip.trigger.removeAttribute("aria-describedby");
    state.tooltip.trigger.setAttribute("aria-expanded", "false");
    state.tooltip = null;
    elements.tooltip.hidden = true;
    elements.tooltip.replaceChildren();
  }

  function attachTooltip(trigger) {
    trigger.setAttribute("aria-expanded", "false");
    trigger.addEventListener("pointerenter", (event) => {
      // Touch has no hover; a tap arrives as a click and pins the tooltip instead.
      if (event.pointerType === "mouse" && !state.tooltip?.pinned) showTooltip(trigger, false);
    });
    trigger.addEventListener("pointerleave", () => {
      if (state.tooltip?.trigger === trigger && !state.tooltip.pinned) hideTooltip();
    });
    trigger.addEventListener("focus", () => {
      if (!state.tooltip?.pinned) showTooltip(trigger, false);
    });
    trigger.addEventListener("blur", (event) => {
      if (state.tooltip?.trigger !== trigger || state.tooltip.pinned) return;
      if (!elements.tooltip.contains(event.relatedTarget)) hideTooltip();
    });
    trigger.addEventListener("click", (event) => {
      // Opening the tooltip must not also select the row for the chart.
      event.stopPropagation();
      if (state.tooltip?.trigger === trigger && state.tooltip.pinned) {
        hideTooltip();
        return;
      }
      showTooltip(trigger, true);
      // From the keyboard (no pointer, so detail is 0) the chips in the tooltip are reached
      // only by moving the focus there; Escape brings it back.
      if (event.detail === 0) elements.tooltip.querySelector("button")?.focus();
    });
  }

  // Positions a floating element under its anchor, inside the markets panel. Both live outside
  // the table: its scroll container would otherwise clip them.
  function placeBelow(element, anchor, alignRight) {
    element.style.removeProperty("top");
    element.style.removeProperty("left");
    if (element === elements.panel && isPhone()) return;
    const box = elements.section.getBoundingClientRect();
    const rect = anchor.getBoundingClientRect();
    const width = element.offsetWidth;
    const wanted = alignRight ? rect.right - box.left - width : rect.left - box.left;
    const left = Math.max(8, Math.min(wanted, box.width - width - 8));
    element.style.top = `${rect.bottom - box.top + 4}px`;
    element.style.left = `${left}px`;
  }

  function isPhone() {
    return window.matchMedia("(max-width: 700px)").matches;
  }

  // ---------- Filter ----------

  function loadFilter() {
    try {
      const saved = Number(localStorage.getItem(FILTER_STORAGE_KEY));
      if (Number.isInteger(saved) && saved > 0) state.filterTagId = saved;
    } catch (error) {
      console.warn("Could not read the saved tag filter", error);
    }
  }

  function saveFilter() {
    try {
      if (state.filterTagId === null) {
        localStorage.removeItem(FILTER_STORAGE_KEY);
      } else {
        localStorage.setItem(FILTER_STORAGE_KEY, String(state.filterTagId));
      }
    } catch (error) {
      console.warn("Could not save the tag filter", error);
    }
  }

  function setFilter(tagId) {
    state.filterTagId = tagId;
    saveFilter();
    renderFilter();
    renderAllRows();
    state.onFilterChange();
  }

  function toggleFilterTag(tag) {
    setFilter(state.filterTagId === tag.id ? null : tag.id);
  }

  function renderFilter() {
    const tag = state.filterTagId === null ? undefined : state.tags.get(state.filterTagId);
    elements.filter.hidden = tag === undefined;
    if (tag === undefined) return;
    elements.filterName.className = `tag-chip ${colorClass(tag.color)}`;
    elements.filterName.textContent = tag.name;
  }

  function hasFilter() {
    return state.filterTagId !== null;
  }

  function clearFilter() {
    if (hasFilter()) setFilter(null);
  }

  function matches(symbol) {
    // Until the lists arrive nobody knows which pairs have the tag, so nothing is hidden.
    if (state.filterTagId === null || !state.metasKnown) return true;
    return (state.metas.get(symbol)?.tags ?? []).some((tag) => tag.id === state.filterTagId);
  }

  // ---------- "⋯" panel ----------

  function openPanel(symbol, button) {
    if (!state.writeEnabled) return;
    if (state.panel?.symbol === symbol) {
      closePanel();
      return;
    }
    closePanel(false);
    hideTooltip();
    const meta = state.metas.get(symbol);
    state.panel = {
      symbol,
      button,
      tagIds: new Set(pairTags(symbol).map((tag) => tag.id)),
      busy: false,
    };
    elements.panelTitle.textContent = `Oznake i beleška — ${state.displaySymbol(symbol)}`;
    elements.panelNote.value = meta?.note?.text ?? "";
    elements.panelNewName.value = "";
    state.newColor = COLORS[0];
    // The template holds the radiogroup itself, so only the radios are taken over.
    const picker = createColorPicker("pair-panel-color", state.newColor, "", (color) => {
      state.newColor = color;
    });
    elements.panelNewColors.replaceChildren(...picker.children);
    showError(elements.panelError, null);
    renderPanelTags();
    renderNoteCount();
    setPanelBusy(false);
    elements.panel.hidden = false;
    button.setAttribute("aria-expanded", "true");
    placeBelow(elements.panel, button, true);
    const first = elements.panel.querySelector("input:not(:disabled), textarea");
    first?.focus();
  }

  function closePanel(restoreFocus = true) {
    if (state.panel === null) return;
    const { button } = state.panel;
    state.panel = null;
    elements.panel.hidden = true;
    button.setAttribute("aria-expanded", "false");
    if (restoreFocus) button.focus();
  }

  function renderPanelTags() {
    keepFocus(() => {
      const items = [...state.tags.values()].map((tag) => {
        const item = document.createElement("li");
        const label = document.createElement("label");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = String(tag.id);
        checkbox.checked = state.panel.tagIds.has(tag.id);
        checkbox.dataset.focusKey = `pair-panel-tag:${tag.id}`;
        checkbox.addEventListener("change", () => {
          if (checkbox.checked) {
            state.panel.tagIds.add(tag.id);
          } else {
            state.panel.tagIds.delete(tag.id);
          }
          updateTagLimit();
        });
        label.append(checkbox, createChip(tag, null));
        item.append(label);
        return item;
      });
      elements.panelTags.replaceChildren(...items);
    });
    elements.panelNoTags.hidden = state.tags.size > 0;
    updateTagLimit();
  }

  // At the limit, unchecked tags are disabled so a sixth cannot be picked.
  function updateTagLimit() {
    const { tagIds, busy } = state.panel;
    const full = tagIds.size >= MAX_TAGS_PER_PAIR;
    for (const checkbox of elements.panelTags.querySelectorAll("input")) {
      checkbox.disabled = busy || (full && !checkbox.checked);
    }
    elements.panelTagCount.textContent = `${tagIds.size}/${MAX_TAGS_PER_PAIR}`;
    elements.panelTagCount.classList.toggle("is-full", full);
  }

  function renderNoteCount() {
    const length = textLength(elements.panelNote.value);
    elements.panelNoteCount.textContent = `${length}/${NOTE_MAX_LENGTH}`;
    elements.panelNoteCount.classList.toggle("is-over", length > NOTE_MAX_LENGTH);
  }

  function setPanelBusy(busy) {
    state.panel.busy = busy;
    for (const control of elements.panel.querySelectorAll("input, textarea, button")) {
      if (control !== elements.panelCancel) control.disabled = busy;
    }
    updateTagLimit();
  }

  async function createTag() {
    const panel = state.panel;
    if (panel === null || panel.busy) return;
    const name = elements.panelNewName.value.trim();
    if (name === "") {
      showError(elements.panelError, "Upiši ime nove oznake.");
      elements.panelNewName.focus();
      return;
    }
    if (textLength(name) > NAME_MAX_LENGTH) {
      showError(elements.panelError, `Ime oznake može imati najviše ${NAME_MAX_LENGTH} znakova.`);
      return;
    }
    setPanelBusy(true);
    const result = await send("/api/tags", jsonRequest("POST", { name, color: state.newColor }));
    if (state.panel !== panel) return;
    setPanelBusy(false);
    if (result.error) {
      showError(elements.panelError, result.error);
      elements.panelNewName.focus();
      return;
    }
    showError(elements.panelError, null);
    // A new tag is meant for this pair, so it is checked when the pair still has room.
    if (panel.tagIds.size < MAX_TAGS_PER_PAIR) panel.tagIds.add(result.body.id);
    mergeTag(result.body);
    elements.panelNewName.value = "";
    elements.panelNewName.focus();
  }

  function sameIds(a, b) {
    return a.length === b.length && a.every((id) => b.includes(id));
  }

  async function savePanel(event) {
    event.preventDefault();
    const panel = state.panel;
    if (panel === null || panel.busy) return;
    const { symbol } = panel;
    const noteText = elements.panelNote.value.trim();
    if (textLength(noteText) > NOTE_MAX_LENGTH) {
      showError(elements.panelError, `Beleška može imati najviše ${NOTE_MAX_LENGTH} znakova.`);
      return;
    }
    const tagIds = [...panel.tagIds];
    const currentIds = (state.metas.get(symbol)?.tags ?? []).map((tag) => tag.id);
    const tagsChanged = !sameIds(tagIds, currentIds);
    const noteChanged = noteText !== (state.metas.get(symbol)?.note?.text ?? "");
    const base = `/api/pairs/${encodeURIComponent(symbol)}`;

    setPanelBusy(true);
    showError(elements.panelError, null);
    for (const [changed, path, body] of [
      [tagsChanged, "tags", { tag_ids: tagIds }],
      [noteChanged, "note", { text: noteText }],
    ]) {
      if (!changed) continue;
      const result = await send(`${base}/${path}`, jsonRequest("PUT", body));
      if (state.panel !== panel) return;
      if (result.error) {
        setPanelBusy(false);
        showError(elements.panelError, result.error);
        return;
      }
      if (result.body) setPairMeta(result.body);
    }
    closePanel();
  }

  // Tab stays inside the panel while it is open, so the keyboard does not wander off into the
  // table behind it.
  function handlePanelKeydown(event) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closePanel();
      return;
    }
    if (event.key === "Enter" && event.target === elements.panelNewName) {
      // Enter in the name field adds the tag instead of submitting the whole panel.
      event.preventDefault();
      createTag();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...elements.panel.querySelectorAll("input, textarea, button")].filter(
      (control) => !control.disabled && !(control.type === "radio" && !control.checked),
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  // ---------- Tag manager ----------

  function toggleManager(open) {
    elements.manager.hidden = !open;
    elements.manageButton.setAttribute("aria-expanded", String(open));
    if (!open) {
      state.confirmDeleteId = null;
      state.managerDrafts.clear();
      showError(elements.managerError, null);
    }
    renderManager();
  }

  function managerDraft(tag) {
    return state.managerDrafts.get(tag.id) ?? { name: tag.name, color: tag.color };
  }

  function updateDraft(tag, change) {
    state.managerDrafts.set(tag.id, { ...managerDraft(tag), ...change });
  }

  function managerButton(text, ariaLabel, focusKey, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "note-button";
    button.textContent = text;
    button.setAttribute("aria-label", ariaLabel);
    button.dataset.focusKey = focusKey;
    button.disabled = state.managerBusy;
    button.addEventListener("click", onClick);
    return button;
  }

  function createManagerItem(tag) {
    const draft = managerDraft(tag);
    const item = document.createElement("li");
    item.className = "tags-manager-item";

    const name = document.createElement("input");
    name.type = "text";
    name.maxLength = NAME_MAX_LENGTH;
    name.value = draft.name;
    name.autocomplete = "off";
    name.disabled = state.managerBusy;
    name.className = "tags-manager-name";
    name.setAttribute("aria-label", `Ime oznake ${tag.name}`);
    name.dataset.focusKey = `manager-name:${tag.id}`;
    name.addEventListener("input", () => updateDraft(tag, { name: name.value }));
    name.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      saveTag(tag);
    });

    const colors = createColorPicker(
      `manager-color-${tag.id}`,
      draft.color,
      `Boja oznake ${tag.name}`,
      (color) => updateDraft(tag, { color }),
    );
    for (const radio of colors.querySelectorAll("input")) radio.disabled = state.managerBusy;

    const count = document.createElement("span");
    count.className = "note-muted tags-manager-count";
    count.textContent = pairsText(tag.pair_count);

    const actions = document.createElement("div");
    actions.className = "tags-manager-actions";
    if (state.confirmDeleteId === tag.id) {
      const question = document.createElement("span");
      question.className = "tags-manager-question";
      question.textContent = deleteQuestion(tag);
      actions.append(
        question,
        managerButton("Obriši", `Potvrdi brisanje oznake ${tag.name}`, `manager-confirm:${tag.id}`, () =>
          deleteTag(tag),
        ),
        managerButton("Otkaži", "Otkaži brisanje", `manager-keep:${tag.id}`, () => {
          state.confirmDeleteId = null;
          renderManager();
          focusKey(`manager-delete:${tag.id}`);
        }),
      );
    } else {
      actions.append(
        managerButton("Sačuvaj", `Sačuvaj oznaku ${tag.name}`, `manager-save:${tag.id}`, () =>
          saveTag(tag),
        ),
        managerButton("Obriši", `Obriši oznaku ${tag.name}`, `manager-delete:${tag.id}`, () => {
          state.confirmDeleteId = tag.id;
          renderManager();
          focusKey(`manager-confirm:${tag.id}`);
        }),
      );
    }
    item.append(name, colors, count, actions);
    return item;
  }

  function focusKey(key) {
    document.querySelector(`[data-focus-key="${CSS.escape(key)}"]`)?.focus();
  }

  function renderManager() {
    if (elements.manager.hidden) return;
    keepFocus(() => {
      elements.managerList.replaceChildren(...[...state.tags.values()].map(createManagerItem));
    });
    elements.managerEmpty.hidden = state.tags.size > 0;
  }

  // Controls are disabled while the request runs, which drops the focus; the caller gets the
  // key of the focused control back to restore it.
  async function runManagerRequest(url, options) {
    const focused = document.activeElement;
    const key = focused instanceof HTMLElement ? focused.dataset.focusKey : undefined;
    state.managerBusy = true;
    renderManager();
    const result = await send(url, options);
    state.managerBusy = false;
    showError(elements.managerError, result.error ?? null);
    return { ...result, focusKey: key };
  }

  async function saveTag(tag) {
    if (state.managerBusy) return;
    const draft = managerDraft(tag);
    const change = {};
    const name = draft.name.trim();
    if (name !== tag.name) change.name = name;
    if (draft.color !== tag.color) change.color = draft.color;
    if (Object.keys(change).length === 0) {
      showError(elements.managerError, null);
      return;
    }
    if (name === "") {
      showError(elements.managerError, "Ime oznake ne sme biti prazno.");
      return;
    }
    const result = await runManagerRequest(
      `/api/tags/${tag.id}`,
      jsonRequest("PATCH", change),
    );
    if (!result.error) state.managerDrafts.delete(tag.id);
    if (!result.error && result.body) {
      mergeTag(result.body);
    } else {
      renderManager();
    }
    if (result.focusKey !== undefined) focusKey(result.focusKey);
  }

  async function deleteTag(tag) {
    if (state.managerBusy) return;
    const result = await runManagerRequest(`/api/tags/${tag.id}`, { method: "DELETE" });
    if (result.error) {
      renderManager();
      if (result.focusKey !== undefined) focusKey(result.focusKey);
      return;
    }
    state.confirmDeleteId = null;
    // The server also sends tags_updated and pair_meta_updated; this just does not wait.
    for (const meta of state.metas.values()) {
      meta.tags = meta.tags.filter((other) => other.id !== tag.id);
    }
    setTags([...state.tags.values()].filter((other) => other.id !== tag.id));
    state.onFilterChange();
    elements.manageButton.focus();
  }

  // ---------- Wiring ----------

  function init({ rows, displaySymbol, onFilterChange }) {
    state.rows = rows;
    state.displaySymbol = displaySymbol;
    state.onFilterChange = onFilterChange;
    for (const [symbol, row] of rows) {
      attachTooltip(row.querySelector(".pair-note-icon"));
      const menu = row.querySelector(".row-menu");
      menu.addEventListener("click", (event) => {
        // The menu must not also select the row for the chart.
        event.stopPropagation();
        openPanel(symbol, menu);
      });
    }
    loadFilter();
    load();
    loadConfig();
  }

  // Called by app.js whenever the socket opens; the first open follows the initial load.
  function connected() {
    if (state.connectedBefore) load();
    state.connectedBefore = true;
  }

  elements.filterClear.addEventListener("click", () => setFilter(null));
  elements.manageButton.addEventListener("click", () => toggleManager(elements.manager.hidden));
  elements.panel.addEventListener("submit", savePanel);
  elements.panel.addEventListener("keydown", handlePanelKeydown);
  elements.panelCancel.addEventListener("click", () => closePanel());
  elements.panelNewAdd.addEventListener("click", createTag);
  elements.panelNote.addEventListener("input", renderNoteCount);
  elements.tableWrap.addEventListener("scroll", hideTooltip, { passive: true });
  window.addEventListener("resize", () => {
    hideTooltip();
    if (state.panel) placeBelow(elements.panel, state.panel.button, true);
  });
  document.addEventListener("click", (event) => {
    if (!state.tooltip?.pinned) return;
    if (!elements.tooltip.contains(event.target)) hideTooltip();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || state.tooltip === null) return;
    const { trigger } = state.tooltip;
    const focusInside = elements.tooltip.contains(document.activeElement);
    hideTooltip();
    if (focusInside) trigger.focus();
  });

  return { init, connected, handleMessage, matches, hasFilter, clearFilter };
})();
