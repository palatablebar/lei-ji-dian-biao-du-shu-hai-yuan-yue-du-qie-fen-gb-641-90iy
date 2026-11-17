// 双向净结算页：选补抄包后发布新版；「重发」沿用上次并入的补抄包。
const state = { busy: false };

function notice(message, isError = false) {
  const box = document.getElementById("notice");
  if (!box) return;
  box.textContent = message;
  box.classList.toggle("is-error", Boolean(isError));
  box.hidden = false;
}

async function publish(button, body, label) {
  if (state.busy) return;
  state.busy = true;
  const original = button.textContent;
  button.textContent = "发布中";
  button.disabled = true;
  try {
    const response = await fetch("/api/settlement/publish", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error((payload.error || response.status) + " " + (payload.message || ""));
    }
    notice("已发布 · 批次 " + payload.batch_id);
    window.setTimeout(() => window.location.reload(), 500);
  } catch (error) {
    notice(String(error.message || error), true);
  } finally {
    state.busy = false;
    button.textContent = original;
    button.disabled = false;
    if (label) button.dataset.label = label;
  }
}

const publishButton = document.getElementById("publish-button");
if (publishButton) {
  publishButton.addEventListener("click", () => {
    const select = document.getElementById("supplement-select");
    const sample = select ? select.value : "";
    publish(publishButton, sample ? { supplements: [sample] } : {});
  });
}

const republish = document.getElementById("republish-button");
if (republish) republish.addEventListener("click", () => publish(republish, {}));
