const connection = document.querySelector(".connection");
const connectionLabel = document.querySelector("#connection-label");
const retryButton = document.querySelector("#retry");

async function checkConnection() {
  connection.dataset.state = "loading";
  connectionLabel.textContent = "正在连接本地服务";
  retryButton.hidden = true;

  try {
    const response = await fetch("/api/health", {
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
    });
    if (!response.ok || (await response.json()).status !== "ok") {
      throw new Error("Local service unavailable");
    }
    connection.dataset.state = "ready";
    connectionLabel.textContent = "本地服务已连接";
  } catch {
    connection.dataset.state = "error";
    connectionLabel.textContent = "无法连接本地服务";
    retryButton.hidden = false;
  }
}

retryButton.addEventListener("click", checkConnection);
checkConnection();
