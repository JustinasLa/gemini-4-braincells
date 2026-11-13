// Victoria-style tooltips: any element with data-tip="<html>" (content is built by us and escaped where needed).
export function initTooltips() {
  const tip = document.createElement("div");
  tip.className = "tip";
  document.body.appendChild(tip);
  let cur: HTMLElement | null = null;
  const place = (x: number, y: number) => {
    const r = tip.getBoundingClientRect();
    let left = x + 16;
    let top = y + 18;
    if (left + r.width > innerWidth - 8) left = x - r.width - 12;
    if (top + r.height > innerHeight - 8) top = y - r.height - 12;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  };
  document.addEventListener("mouseover", (e) => {
    const el = (e.target as HTMLElement).closest<HTMLElement>("[data-tip]");
    if (el === cur) return;
    cur = el;
    if (!el) return tip.classList.remove("on");
    tip.innerHTML = el.dataset.tip!;
    tip.classList.add("on");
    place(e.clientX, e.clientY);
  });
  document.addEventListener("mousemove", (e) => cur && place(e.clientX, e.clientY));
  document.addEventListener("pointerdown", () => {
    cur = null;
    tip.classList.remove("on");
  });
}
