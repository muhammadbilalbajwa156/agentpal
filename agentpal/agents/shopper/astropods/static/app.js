"use strict";
const form = document.querySelector("#errand");
const chat = document.querySelector("#chat");
const statusLine = document.querySelector("#status");
const send = document.querySelector("#send");
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function say(speaker, message) {
  const paragraph = document.createElement("p");
  const name = document.createElement("strong");
  name.textContent = `${speaker}: `;
  paragraph.append(name, document.createTextNode(message));
  chat.append(paragraph);
}

async function poll(jobId) {
  let seen = 0;
  while (true) {
    const response = await fetch(`/errands/${encodeURIComponent(jobId)}`);
    if (!response.ok) throw new Error("poll_failed");
    const result = await response.json();
    for (const event of result.events.slice(Math.max(0, seen - result.offset))) {
      let message = event.message || "";
      for (const key of ["errand_id", "amount", "merchant", "justification", "payer_action_url", "error"]) {
        if (event[key]) message += ` ${key}: ${event[key]}`;
      }
      say("Shopper", message);
    }
    seen = result.offset + result.events.length;
    statusLine.textContent = result.request_id
      ? `Request ${result.request_id} · ${result.status || "waiting"} · Open your AgentPal dashboard.`
      : "Shopper is planning and searching…";
    if (result.done) return;
    await sleep(2000);
  }
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  send.disabled = true;
  const task = document.querySelector("#task").value;
  const budget = document.querySelector("#budget").value;
  const payee = document.querySelector("#payee").value;
  // The task is displayed only after the server's redaction, never echoed here.
  say("You", "Shopping errand submitted.");
  try {
    const response = await fetch("/errands", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({task, budget, errand_id: crypto.randomUUID(),
                            merchant_paypal_email: payee || null})
    });
    if (!response.ok) throw new Error("submit_failed");
    const result = await response.json();
    sessionStorage.setItem("shopper_job", result.job_id);
    await poll(result.job_id);
    sessionStorage.removeItem("shopper_job");
  } catch (_) {
    say("Shopper", "Unable to continue displaying this errand. Reload to reconnect; check AgentPal before starting another errand.");
  } finally {
    send.disabled = false;
  }
});

const previous = sessionStorage.getItem("shopper_job");
if (previous) {
  send.disabled = true;
  poll(previous).then(() => sessionStorage.removeItem("shopper_job"))
    .catch(() => say("Shopper", "This run is unavailable. Check its status in AgentPal."))
    .finally(() => { send.disabled = false; });
}
