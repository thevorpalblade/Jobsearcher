// Settings forms: add/remove repeated blocks (roles, companies), filter company rows,
// and warn before leaving with unsaved changes. Plain JS, no dependencies.
(() => {
  let next = 0;
  let dirty = false;

  document.addEventListener("click", (event) => {
    const add = event.target.closest("[data-add]");
    if (add) {
      event.preventDefault();
      const template = document.getElementById(add.dataset.add);
      // Table rows sit in <table><tbody> inside the template so they parse as rows.
      const source = template.content.querySelector("tbody")?.innerHTML ?? template.innerHTML;
      const html = source.replaceAll("__i__", `new${Date.now()}${next++}`);
      const into = document.querySelector(add.dataset.into);
      into.insertAdjacentHTML(add.dataset.where || "beforeend", html);
      const added = add.dataset.where === "afterbegin" ? into.firstElementChild : into.lastElementChild;
      added.querySelector("input, textarea")?.focus();
      dirty = true;
      return;
    }
    const remove = event.target.closest("[data-remove]");
    if (remove) {
      event.preventDefault();
      remove.closest(remove.dataset.remove).remove();
      dirty = true;
    }
  });

  document.addEventListener("input", (event) => {
    const filter = event.target.closest("[data-filter]");
    if (filter) {
      const query = filter.value.trim().toLowerCase();
      for (const row of document.querySelectorAll(filter.dataset.filter)) {
        const text = [row.textContent, ...[...row.querySelectorAll("input, select")].map((i) => i.value)]
          .join(" ").toLowerCase();
        row.hidden = query !== "" && !text.includes(query);
      }
      return;
    }
    if (event.target.closest("form.settings-form")) dirty = true;
  });

  // A successful save clears the warning (the result box says "Saved").
  document.addEventListener("htmx:afterSwap", (event) => {
    if (event.target.querySelector?.(".result.ok[data-saved]")) dirty = false;
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirty) event.preventDefault();
  });
})();
