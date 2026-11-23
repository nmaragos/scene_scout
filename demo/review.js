const wipe = document.querySelector("#wipe");
const slider = document.querySelector("#wipe-position");

function updateWipe() {
  wipe.style.setProperty("--position", slider.value + "%");
  slider.setAttribute("aria-valuetext", slider.value + "% Stage A");
}

slider.addEventListener("input", updateWipe);
document.querySelectorAll("[data-wipe]").forEach((button) => {
  button.addEventListener("click", () => {
    slider.value = button.dataset.wipe;
    updateWipe();
  });
});
updateWipe();

const category = document.querySelector("#category");
const findingList = document.querySelector("#finding-list");
const count = document.querySelector("#finding-count");
const selectedLabel = document.querySelector("#selected-label");
const selectedPath = document.querySelector("#selected-path");
const evidenceGrid = document.querySelector("#evidence-grid");
let selectedFinding;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function stageEvidence(stage, index) {
  const slot = index === 0 ? "A" : "B";
  const card = element("article", undefined, "stage-evidence");
  const heading = element("h4");
  heading.append(element("span", slot, "badge badge-" + slot.toLowerCase()), index === 0 ? "pump_v012" : "pump_v013");
  const composed = element("p", undefined, "composed");
  composed.append(element("span", "Composed value", "muted"), element("code", stage.value));
  card.append(heading, composed, element("p", stage.summary, "muted summary"));
  const opinions = element("ol", undefined, "opinions");
  for (const opinion of stage.opinions) {
    const item = element("li");
    item.append(element("code", opinion.layer, "layer"), element("code", opinion.spec), element("code", opinion.value));
    opinions.append(item);
  }
  card.append(opinions);
  return card;
}

for (const name of new Set(pumpFindings.map((finding) => finding.category))) {
  category.add(new Option(name, name));
}

const rows = pumpFindings.map((finding) => {
  const row = element("li");
  const button = element("button", undefined, "finding");
  button.type = "button";
  button.append(element("strong", finding.label), element("code", finding.path));
  button.addEventListener("click", () => selectFinding(finding));
  row.append(button);
  findingList.append(row);
  return { finding, row, button };
});

function selectFinding(finding) {
  selectedFinding = finding;
  for (const entry of rows) {
    entry.button.setAttribute("aria-pressed", String(entry.finding === finding));
  }
  selectedLabel.textContent = finding?.label ?? "No findings";
  selectedPath.textContent = finding?.path ?? "";
  evidenceGrid.replaceChildren(...(finding?.stages ?? []).map(stageEvidence));
}

function applyFilter() {
  const visible = [];
  for (const entry of rows) {
    entry.row.hidden = Boolean(category.value && entry.finding.category !== category.value);
    if (!entry.row.hidden) visible.push(entry.finding);
  }
  count.textContent = visible.length === 1 ? "1 change" : visible.length + " changes";
  if (!visible.includes(selectedFinding)) selectFinding(visible[0]);
}

category.addEventListener("change", applyFilter);
applyFilter();
