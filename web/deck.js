"use strict";

// Shared by the private workbench and the standalone Toy. Navigation never
// changes a task's business state; the caller retains all action handlers.
window.TaskDeck = class TaskDeck {
  static feedbackDuration = 700;

  static showToast(node, text, error = false) {
    clearTimeout(node.gtdToastTimer);
    node.hidden = true;
    node.textContent = text;
    node.className = error ? "error" : "";
    // Restart the acknowledgement when another action finishes during its fade.
    void node.offsetWidth;
    node.hidden = false;
    if (!error) node.gtdToastTimer = setTimeout(() => node.hidden = true, TaskDeck.feedbackDuration);
  }

  constructor(onChange, root = document) {
    this.onChange = onChange;
    this.positions = new Map();
    this.feedback = new Map();
    this.actions = new Set();
    this.root = root;
    root.addEventListener("click", event => {
      const button = event.target.closest("[data-deck-step]");
      if (button && !button.disabled) {
        this.move(button.dataset.deckId, Number(button.dataset.deckStep), true);
      }
    });
    root.addEventListener("keydown", event => {
      const deck = event.target.closest("[data-task-deck]");
      if (!deck || this.isEditing(event.target) || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      this.move(deck.dataset.taskDeck, event.key === "ArrowRight" ? 1 : -1, true);
    });
    root.addEventListener("pointerdown", event => {
      const deck = event.target.closest("[data-task-deck]");
      if (!deck || event.button !== 0 || event.isPrimary === false ||
          event.target.closest("button,a,input,textarea,select,summary,label,[contenteditable]")) return;
      this.gesture = {id: event.pointerId, deck, x: event.clientX, y: event.clientY};
    });
    root.addEventListener("pointermove", event => {
      const gesture = this.gesture;
      if (!gesture || gesture.id !== event.pointerId) return;
      if (!gesture.deck.isConnected) {
        gesture.deck = this.find(gesture.deck.dataset.taskDeck);
        if (!gesture.deck) { this.gesture = null; return; }
      }
      const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
      if (Math.abs(dy) > Math.abs(dx) && Math.abs(dy) > 12) {
        this.resetGesture();
        return;
      }
      if (Math.abs(dx) > 14 && Math.abs(dx) > Math.abs(dy) * 1.5) {
        gesture.deck.classList.add("deck-dragging");
        gesture.deck.querySelector(".deck-page").style.transform =
          `translateX(${Math.max(-85, Math.min(85, dx))}px) rotate(${dx / 180}deg)`;
      }
    });
    root.addEventListener("pointerup", event => {
      const gesture = this.gesture;
      if (!gesture || gesture.id !== event.pointerId) return;
      if (!gesture.deck.isConnected) {
        gesture.deck = this.find(gesture.deck.dataset.taskDeck);
        if (!gesture.deck) { this.gesture = null; return; }
      }
      const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
      this.resetGesture();
      if (gesture.deck.isConnected && Math.abs(dx) >= 55 && Math.abs(dx) > Math.abs(dy) * 1.5) {
        this.move(gesture.deck.dataset.taskDeck, dx < 0 ? 1 : -1, true);
      }
    });
    root.addEventListener("pointercancel", () => this.resetGesture());
    window.addEventListener("blur", () => this.resetGesture());
    root.addEventListener("wheel", event => {
      const node = event.target.closest("[data-task-deck]");
      if (!node || event.ctrlKey || this.isEditing(event.target) ||
          Math.abs(event.deltaX) <= Math.abs(event.deltaY) * 1.5) return;
      event.preventDefault();
      const now = Date.now(), id = node.dataset.taskDeck;
      if (!this.wheel || this.wheel.id !== id || now - this.wheel.at > 250) {
        this.wheel = {id, total: 0, at: now, moved: false};
      }
      this.wheel.at = now;
      if (this.wheel.moved) return;
      this.wheel.total += event.deltaX;
      if (Math.abs(this.wheel.total) >= 55) {
        this.wheel.moved = true;
        this.move(id, this.wheel.total > 0 ? 1 : -1);
      }
    }, {passive: false});
  }

  isEditing(node) {
    return node.closest("input,textarea,select,[contenteditable]");
  }

  resetGesture() {
    if (this.gesture) {
      this.gesture.deck.classList.remove("deck-dragging");
      const page = this.gesture.deck.querySelector(".deck-page");
      if (page) page.style.transform = "";
    }
    this.gesture = null;
  }

  escape(value) {
    return String(value ?? "").replace(/[&<>"']/g, c =>
      ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  }

  find(id) {
    return [...this.root.querySelectorAll("[data-task-deck]")]
      .find(node => node.dataset.taskDeck === id);
  }

  feedbackMarkup(id) {
    const feedback = this.feedback.get(id);
    return feedback ? `<span class="deck-feedback-icon" aria-hidden="true">${feedback.type === "pending" ? "◌" : feedback.type === "error" ? "!" : "✓"}</span><span>${this.escape(feedback.text)}</span>` : "";
  }

  paintFeedback(id) {
    const node = this.find(id);
    if (!node) return;
    const busy = this.actions.has(id), feedback = this.feedback.get(id);
    node.classList.toggle("deck-busy", busy);
    node.setAttribute("aria-busy", String(busy));
    node.querySelector(".deck-page").inert = busy;
    for (const slot of node.querySelectorAll("[data-deck-feedback]")) {
      slot.innerHTML = this.feedbackMarkup(id);
      slot.dataset.phase = feedback?.type || "";
      slot.hidden = !feedback;
    }
  }

  holdFeedback(badge, bounds) {
    if (!badge || !bounds?.width) return;
    this.heldNotice?.remove();
    const notice = badge.cloneNode(true);
    notice.className = "deck-success-notice";
    notice.setAttribute("aria-hidden", "true");
    notice.style.left = bounds.left + "px";
    notice.style.top = bounds.top + "px";
    notice.style.width = bounds.width + "px";
    document.body.append(notice);
    this.heldNotice = notice;
    setTimeout(() => {
      notice.remove();
      if (this.heldNotice === notice) this.heldNotice = null;
    }, TaskDeck.feedbackDuration);
  }

  async act({id, pending, success, kind = "advance", origin}, operation, refresh = this.onChange) {
    this.heldNotice?.remove();
    this.heldNotice = null;
    const original = this.find(id);
    // List rows retain their independent request lifecycles.
    if (!original) {
      const result = await operation();
      if (await refresh(result) === false && result && typeof result === "object") result.view_refresh_pending = true;
      return result;
    }
    if (this.actions.has(id)) throw new Error("这张卡片正在保存，请稍候。");
    const keepFocus = original.contains(origin || document.activeElement);
    this.resetGesture();
    this.actions.add(id);
    this.feedback.set(id, {type:"pending", text:pending});
    this.paintFeedback(id);
    try {
      const result = await operation();
      this.feedback.set(id, {type:"success", text:typeof success === "function" ? success(result) : success});
      this.paintFeedback(id);
      const old = this.find(id);
      const badge = old?.querySelector('.deck-action-overlay>[data-phase="success"]');
      const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (old && !reduced) {
        // Let the acknowledgement pop in while the card starts moving.
        await new Promise(resolve => setTimeout(resolve, 70));
        old.classList.add(kind === "save" ? "deck-save-out" : "deck-action-out");
        await new Promise(resolve => setTimeout(resolve, 150));
      }
      const badgeBounds = badge?.getBoundingClientRect();
      const refreshed = await refresh(result);
      if (refreshed === false) {
        this.feedback.set(id, {type:"success", text:"操作已保存，页面待同步；可点刷新重试。"});
        if (result && typeof result === "object") result.view_refresh_pending = true;
      }
      const next = this.find(id);
      const active = document.activeElement;
      if (keepFocus && (active === document.body || original.contains(active))) {
        (next || this.root.querySelector("#main"))?.focus({preventScroll: true});
      }
      if (next && next !== old && !reduced) {
        next.classList.add("deck-action-in");
        setTimeout(() => next.classList.remove("deck-action-in"), 280);
      }
      this.holdFeedback(badge, badgeBounds);
      return result;
    } catch (error) {
      this.actions.delete(id);
      this.feedback.set(id, {type:"error", text:error.message || "操作未完成，请重试"});
      const node = this.find(id);
      node?.classList.remove("deck-save-out", "deck-action-out");
      this.paintFeedback(id);
      throw error;
    } finally {
      this.actions.delete(id);
      for (const node of new Set([original, this.find(id)])) {
        if (!node) continue;
        node.classList.remove("deck-busy", "deck-save-out", "deck-action-out");
        node.setAttribute("aria-busy", "false");
        const page = node.querySelector(".deck-page");
        if (page) page.inert = false;
      }
      this.paintFeedback(id);
    }
  }

  render({id, items, key, renderItem, label}) {
    if (!items.length) return "";
    const keys = items.map(key);
    const previous = this.positions.get(id);
    const retained = previous ? keys.indexOf(previous.key) : -1;
    const index = retained >= 0 ? retained : Math.min(previous?.index || 0, keys.length - 1);
    this.positions.set(id, {key: keys[index], index, keys});
    const safeId = this.escape(id);
    const busy = this.actions.has(id), feedback = this.feedback.get(id);
    return `<section class="task-deck ${busy ? "deck-busy" : ""}" data-task-deck="${safeId}" tabindex="0" aria-busy="${busy}"
      role="region" aria-roledescription="卡片轮播" aria-label="${this.escape(label)}">
      <div class="deck-caption"><span class="eyebrow">ONE THING AT A TIME</span>
        <span class="deck-counter" aria-live="polite" aria-atomic="true">第 ${index + 1} / ${items.length} 张</span></div>
      <div class="deck-receipt" data-deck-feedback data-phase="${feedback?.type || ""}" role="status" aria-live="polite" ${feedback ? "" : "hidden"}>${this.feedbackMarkup(id)}</div>
      <div class="deck-stage"><div class="deck-paper" aria-hidden="true"></div>
        <div class="deck-page" ${busy ? "inert" : ""}>${renderItem(items[index], index)}</div>
        <div class="deck-action-overlay" aria-hidden="true"><div data-deck-feedback data-phase="${feedback?.type || ""}" ${feedback ? "" : "hidden"}>${this.feedbackMarkup(id)}</div></div></div>
      <div class="deck-controls">
        <button class="deck-arrow" data-deck-id="${safeId}" data-deck-step="-1"
          aria-label="上一张" ${index === 0 ? "disabled" : ""}>←</button>
        <div class="deck-progress"><progress max="${items.length}" value="${index + 1}" aria-label="卡片位置"></progress>
          <span class="deck-hint">左右滑动切换 · 也可用 ← →</span></div>
        <button class="deck-arrow" data-deck-id="${safeId}" data-deck-step="1"
          aria-label="下一张" ${index === items.length - 1 ? "disabled" : ""}>→</button>
      </div></section>`;
  }

  move(id, direction, focus = false) {
    if (this.actions.has(id)) return;
    const position = this.positions.get(id);
    if (!position) return;
    const index = Math.max(0, Math.min(position.keys.length - 1, position.index + direction));
    if (index === position.index) return;
    this.resetGesture();
    position.index = index;
    position.key = position.keys[index];
    this.onChange();
    const node = [...this.root.querySelectorAll("[data-task-deck]")]
      .find(deck => deck.dataset.taskDeck === id);
    if (focus && node) node.focus({preventScroll: true});
    if (node) {
      node.classList.add(direction > 0 ? "deck-enter-next" : "deck-enter-previous");
      setTimeout(() => node.classList.remove("deck-enter-next", "deck-enter-previous"), 220);
    }
  }
};
