// Character/tag counters, a busy state for slow AI buttons, and category search.
document.querySelectorAll(".counter").forEach((el) => {
  const input = document.getElementById(el.dataset.for);
  const max = Number(el.dataset.max);
  const update = () => {
    const n = el.dataset.mode === "list"
      ? input.value.split(",").map((s) => s.trim()).filter(Boolean).length
      : input.value.length;
    el.textContent = `${n}/${max}`;
    el.classList.toggle("over", n > max);
  };
  input.addEventListener("input", update);
  update();
});

document.querySelectorAll("button[data-busy]").forEach((btn) => {
  btn.form.addEventListener("submit", () => {
    btn.disabled = true;
    btn.textContent = btn.dataset.busy;
  });
});

const search = document.getElementById("taxonomy_search");
if (search) {
  const results = document.getElementById("taxonomy_results");
  const hidden = document.getElementById("taxonomy_id");
  let timer;
  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (search.value.length < 2) { results.innerHTML = ""; return; }
      const rows = await (await fetch(`/api/taxonomy?q=${encodeURIComponent(search.value)}`)).json();
      results.innerHTML = "";
      rows.forEach((r) => {
        const li = document.createElement("li");
        li.textContent = r.path;
        li.onclick = () => { hidden.value = r.id; search.value = r.path; results.innerHTML = ""; };
        results.appendChild(li);
      });
    }, 200);
  });
}
