// Detect login forms and fill from Keychain via Native Messaging.
// Does NOT auto-submit.

(function () {
  const MAX_ATTEMPTS = 1;
  let attempted = false;

  function findLoginFields() {
    const passField = document.querySelector('input[type="password"]');
    if (!passField || passField.disabled) {
      return null;
    }
    const form = passField.closest("form") || document;
    const userField =
      form.querySelector(
        'input[type="email"], input[type="text"], input[name*="user" i], input[name*="email" i], input[autocomplete="username"]'
      ) || document.querySelector('input[type="email"], input[autocomplete="username"]');
    return { userField, passField };
  }

  function setNativeValue(el, value) {
    if (!el) return;
    const proto = window.HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) {
      desc.set.call(el, value);
    } else {
      el.value = value;
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function requestFill() {
    if (attempted) return;
    const fields = findLoginFields();
    if (!fields) return;
    attempted = true;

    chrome.runtime.sendMessage(
      {
        action: "request_credentials",
        domain: window.location.hostname,
      },
      (response) => {
        if (chrome.runtime.lastError) {
          console.warn("[MacAgent]", chrome.runtime.lastError.message);
          return;
        }
        if (!response || !response.credentials) {
          console.warn("[MacAgent] no credentials", response && response.error);
          return;
        }
        const { username, password } = response.credentials;
        if (username && fields.userField) {
          setNativeValue(fields.userField, username);
        }
        if (password && fields.passField) {
          setNativeValue(fields.passField, password);
        }
        // Intentionally no submit click.
      }
    );
  }

  function maybeFill() {
    if (attempted) return;
    if (findLoginFields()) {
      requestFill();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", maybeFill, { once: true });
  } else {
    maybeFill();
  }

  // Late-rendered SPAs
  let tries = 0;
  const timer = setInterval(() => {
    tries += 1;
    maybeFill();
    if (attempted || tries >= 10) {
      clearInterval(timer);
    }
  }, 800);
})();
