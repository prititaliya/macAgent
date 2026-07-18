const NATIVE_HOST_NAME = "com.macagent.native_host";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.action !== "request_credentials") {
    return false;
  }

  let port;
  try {
    port = chrome.runtime.connectNative(NATIVE_HOST_NAME);
  } catch (err) {
    sendResponse({ ok: false, error: String(err), credentials: null });
    return true;
  }

  const onDisconnect = () => {
    const err = chrome.runtime.lastError
      ? chrome.runtime.lastError.message
      : "native host disconnected";
    sendResponse({ ok: false, error: err, credentials: null });
  };

  port.onDisconnect.addListener(onDisconnect);
  port.onMessage.addListener((response) => {
    port.onDisconnect.removeListener(onDisconnect);
    try {
      port.disconnect();
    } catch (_e) {
      /* ignore */
    }
    sendResponse(response);
  });

  port.postMessage({
    action: "request_credentials",
    domain: message.domain || "",
    username: message.username || "",
  });

  return true; // async sendResponse
});
