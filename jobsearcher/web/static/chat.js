// The dashboard's chat with Claude Code: send a message, stream the reply (Server-Sent
// Events), show tool activity, stop a run. Plain JS, no dependencies.
(() => {
  const root = document.getElementById("chat");
  if (!root) return;
  const log = document.getElementById("chat-log");
  const form = document.getElementById("chat-form");
  const input = document.getElementById("chat-input");
  const sendButton = document.getElementById("chat-send");
  const stopButton = document.getElementById("chat-stop");
  const status = document.getElementById("chat-status");
  let chatId = root.dataset.chatId || "";
  let source = null;
  const HX = { "HX-Request": "true" };

  const escapeHtml = (text) =>
    text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // Small, safe formatting: code fences, `code`, **bold**, links, paragraphs.
  function format(text) {
    const parts = text.split(/```/);
    return parts.map((part, i) => {
      if (i % 2 === 1) {
        const body = part.replace(/^[a-zA-Z0-9_+-]*\n/, "");
        return `<pre>${escapeHtml(body.replace(/\n$/, ""))}</pre>`;
      }
      let html = escapeHtml(part)
        .replace(/`([^`\n]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(https?:\/\/[^\s<)]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
      return html.split(/\n{2,}/).map((p) => `<p>${p.replace(/\n/g, "<br>")}</p>`).join("");
    }).join("");
  }

  function add(role, text, id) {
    if (id && log.querySelector(`[data-msg-id="${id}"]`)) return null;  // already shown
    const el = document.createElement("div");
    el.className = `msg ${role}`;
    if (id) el.dataset.msgId = id;
    if (role === "tool") el.textContent = text;
    else el.innerHTML = format(text);
    log.appendChild(el);
    log.scrollTop = log.scrollHeight;
    return el;
  }

  function setRunning(running, note) {
    sendButton.disabled = running;
    stopButton.hidden = !running;
    status.textContent = running ? (note || "Claude is working…") : (note || "");
  }

  function listen(after) {
    if (source) source.close();
    setRunning(true);
    source = new EventSource(`/chat/${chatId}/stream?after=${after}`);
    source.onmessage = (message) => {
      const event = JSON.parse(message.data);
      if (event.type === "text") add("assistant", event.text, event.id);
      else if (event.type === "tool") add("tool", event.text, event.id);
      else if (event.type === "done") {
        if (event.error) add("error", event.error, event.id);
        source.close();
        setRunning(false);
        input.focus();
      }
    };
    source.onerror = () => {  // the connection dropped; the run carries on server-side
      source.close();
      setRunning(false, "Connection lost. Reload the page to see the reply.");
    };
  }

  async function send(text) {
    const body = new FormData();
    body.set("message", text);
    body.set("chat_id", chatId);
    setRunning(true, "Sending…");
    let response, data;
    try {
      response = await fetch("/chat/send", { method: "POST", body, headers: HX });
      data = await response.json();
    } catch (err) {
      setRunning(false);
      add("error", "Couldn't reach the server.");
      return;
    }
    if (!response.ok) {
      setRunning(false);
      add("error", data.error || "Something went wrong.");
      return;
    }
    const suggestions = document.getElementById("chat-suggestions");
    if (suggestions) suggestions.remove();
    add("user", text);
    input.value = "";
    if (!chatId) {
      chatId = data.chat_id;
      root.dataset.chatId = chatId;
      history.replaceState(null, "", `/?chat=${chatId}`);  // reload shows this chat
    }
    listen(0);
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (text) send(text);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  stopButton.addEventListener("click", () => {
    fetch(`/chat/${chatId}/cancel`, { method: "POST", headers: HX });
  });
  document.querySelectorAll("[data-suggest]").forEach((button) =>
    button.addEventListener("click", () => send(button.dataset.suggest)));
  document.getElementById("chat-switch")?.addEventListener("change", (event) => {
    location.href = event.target.value === "new" ? "/?chat=new" : `/?chat=${event.target.value}`;
  });
  document.getElementById("chat-delete")?.addEventListener("click", async (event) => {
    if (!confirm("Delete this conversation?")) return;
    const response = await fetch(`/chat/${event.target.dataset.chat}/delete`, { method: "POST", headers: HX });
    if (response.ok) location.href = "/?chat=new";
    else add("error", (await response.text()) || "Couldn't delete it.");
  });

  // Stored history first, then pick up a reply that is still running.
  for (const m of JSON.parse(document.getElementById("chat-history").textContent)) add(m.role, m.text, m.id);
  if (root.dataset.running) listen(Number(root.dataset.after || 0));
})();
