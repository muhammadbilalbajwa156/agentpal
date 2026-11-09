"use strict";
const form = document.querySelector("#day");
const select = document.querySelector("#company");
const start = document.querySelector("#start");
const castBox = document.querySelector("#cast");
const log = document.querySelector("#log");
const results = document.querySelector("#results");
const statusLine = document.querySelector("#status");
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let companies = [];

function el(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

function showCast() {
  castBox.replaceChildren();
  const company = companies.find(c => c.company_id === select.value);
  if (!company) return;
  castBox.append(el("p", company.tagline));
  for (const agent of company.agents) {
    const box = el("div");
    box.className = "agent";
    box.append(el("h3", agent.name), el("p", agent.mission),
      el("p", `Wallet: ${agent.monthly_budget} USD / month · ${agent.per_transaction_limit} USD per transaction`));
    const list = el("ul");
    for (const errand of agent.errands) list.append(el("li", `${errand.goal} (budget ${errand.budget} USD, ${errand.kind})`));
    box.append(list);
    castBox.append(box);
  }
}

function say(event) {
  const line = el("p");
  line.append(el("strong", `${event.agent || event.company || "AgentPal"}: `));
  let message = event.message || "";
  for (const key of ["errand", "amount", "merchant", "payee", "available", "request_id", "status", "error"]) {
    if (event[key] !== undefined && event[key] !== null) message += ` · ${key}: ${event[key]}`;
  }
  line.append(document.createTextNode(message));
  log.append(line);
}

function showResults(rows) {
  results.replaceChildren();
  if (!rows.length) return;
  const table = el("table");
  const head = el("tr");
  for (const h of ["Agent", "Errand", "Kind", "Amount", "Status", "Request"]) head.append(el("th", h));
  table.append(head);
  for (const r of rows) {
    const tr = el("tr");
    for (const v of [r.agent, r.errand, r.kind, r.amount || "", r.status, r.request_id || r.error || ""]) tr.append(el("td", String(v)));
    table.append(tr);
  }
  results.append(table);
}

async function poll(jobId) {
  let seen = 0;
  while (true) {
    const response = await fetch(`/days/${encodeURIComponent(jobId)}`);
    if (!response.ok) throw new Error("poll_failed");
    const result = await response.json();
    for (const event of result.events.slice(Math.max(0, seen - result.offset))) say(event);
    seen = result.offset + result.events.length;
    showResults(result.results);
    statusLine.textContent = result.done
      ? "Business day finished. Approve or reject the requests in your AgentPal owner dashboard."
      : "Agents are working…";
    if (result.done) return;
    await sleep(1500);
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  start.disabled = true;
  log.replaceChildren();
  results.replaceChildren();
  try {
    const response = await fetch("/days", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({company: select.value, run_id: crypto.randomUUID()})
    });
    if (!response.ok) throw new Error(response.status === 429 ? "busy" : "submit_failed");
    const result = await response.json();
    sessionStorage.setItem("company_hq_job", result.job_id);
    await poll(result.job_id);
    sessionStorage.removeItem("company_hq_job");
  } catch (error) {
    statusLine.textContent = error.message === "busy"
      ? "Another business day is running. Try again shortly."
      : "Unable to continue displaying this day. Reload to reconnect.";
  } finally {
    start.disabled = false;
  }
});

fetch("/companies").then(r => r.json()).then(data => {
  companies = data.companies;
  for (const company of companies) {
    const option = el("option", company.name);
    option.value = company.company_id;
    select.append(option);
  }
  showCast();
});
select.addEventListener("change", showCast);

const previous = sessionStorage.getItem("company_hq_job");
if (previous) {
  start.disabled = true;
  poll(previous).then(() => sessionStorage.removeItem("company_hq_job"))
    .catch(() => { statusLine.textContent = "This run is unavailable. Check AgentPal."; })
    .finally(() => { start.disabled = false; });
}
