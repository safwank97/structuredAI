// Vanilla JS client -- Option A: this file is served same-origin by
// FastAPI (see app/main.py's StaticFiles mount), so every fetch() below is
// a same-origin request against /api/v1/*. No CORS handling exists here,
// and none is configured on the API side either -- that's the whole point
// of serving the UI from the same app.

const STORAGE_KEYS = {
  access: "cp_access_token",
  refresh: "cp_refresh_token",
  user: "cp_user",
};

// sessionStorage (not localStorage): tokens survive a page refresh within
// the same tab, but disappear once the tab/window closes. That's a
// deliberate middle ground for this build -- localStorage would persist
// indefinitely across tabs and browser restarts, which is more exposure
// than a demo needs. A production build would move to a server-set
// httpOnly cookie instead, so client-side JS can never read the token at
// all (removes token theft via XSS as a risk entirely); that's a
// worthwhile hardening step for later, not implemented here.
function saveSession(tokens, user) {
  sessionStorage.setItem(STORAGE_KEYS.access, tokens.access_token);
  sessionStorage.setItem(STORAGE_KEYS.refresh, tokens.refresh_token);
  sessionStorage.setItem(STORAGE_KEYS.user, JSON.stringify(user));
}

function clearSession() {
  sessionStorage.removeItem(STORAGE_KEYS.access);
  sessionStorage.removeItem(STORAGE_KEYS.refresh);
  sessionStorage.removeItem(STORAGE_KEYS.user);
}

function getStoredUser() {
  const raw = sessionStorage.getItem(STORAGE_KEYS.user);
  return raw ? JSON.parse(raw) : null;
}

// FastAPI returns errors in two different shapes depending on where they
// come from: a plain {"detail": "message"} string for errors the app code
// raises itself (401 on bad credentials, 409 on duplicate email, 404 on a
// conversation that isn't yours, etc.), and {"detail": [{"msg": "...",
// ...}, ...]} for Pydantic's own 422 request-validation failures (e.g. a
// password that's too short, or an empty message body). Handle both so the
// UI never just prints "[object Object]".
function extractErrorMessage(body) {
  if (!body || !body.detail) return "Something went wrong. Please try again.";
  if (typeof body.detail === "string") return body.detail;
  if (Array.isArray(body.detail)) {
    return body.detail.map((e) => e.msg).join("; ");
  }
  return "Something went wrong. Please try again.";
}

async function parseJsonOrThrow(response) {
  if (response.status === 204) return null;
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(extractErrorMessage(body));
  }
  return body;
}

async function postJson(path, payload) {
  let response;
  try {
    response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (networkError) {
    throw new Error("Could not reach the server. Is the API container running?");
  }
  return parseJsonOrThrow(response);
}

// Guards against a race where two near-simultaneous 401s (e.g. from
// clicking between two project tiles right as the access token expires,
// each triggering their own authorizedFetch call) would otherwise each
// independently call /auth/refresh with the same refresh token. The server
// rotates and revokes a refresh token on every use (see auth.py's refresh()),
// so whichever of those two calls loses the race presents an
// already-rotated token -- which is exactly the signature the
// reuse-detection logic there was built to treat as a stolen-token replay,
// and it responds by revoking the *entire* session chain. Funneling every
// refresh attempt through one shared in-flight promise means concurrent
// callers all await the same single server round trip instead of racing
// each other and self-triggering that lockout over nothing more than a UI
// timing coincidence.
let refreshInFlight = null;

function refreshTokens(refreshToken) {
  if (!refreshInFlight) {
    refreshInFlight = postJson("/api/v1/auth/refresh", { refresh_token: refreshToken })
      .catch(() => null)
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

// Attaches the stored access token to a request and, on a 401, tries
// /api/v1/auth/refresh once before retrying. Every conversation/message/
// upload call below goes through this -- these are the first protected
// endpoints in the API, so this is also the first code path that actually
// exercises the refresh-on-401 plumbing that was wired up ahead of time.
async function authorizedFetch(path, options = {}) {
  const withAuth = (token) => ({
    ...options,
    headers: { ...(options.headers || {}), Authorization: `Bearer ${token}` },
  });

  let response;
  try {
    response = await fetch(path, withAuth(sessionStorage.getItem(STORAGE_KEYS.access)));
  } catch (networkError) {
    throw new Error("Could not reach the server. Is the API container running?");
  }
  if (response.status !== 401) return response;

  const refreshToken = sessionStorage.getItem(STORAGE_KEYS.refresh);
  if (!refreshToken) {
    clearSession();
    showAuthView();
    return response;
  }

  const refreshed = await refreshTokens(refreshToken);

  if (!refreshed) {
    clearSession();
    showAuthView();
    return response;
  }

  sessionStorage.setItem(STORAGE_KEYS.access, refreshed.access_token);
  sessionStorage.setItem(STORAGE_KEYS.refresh, refreshed.refresh_token);
  return fetch(path, withAuth(refreshed.access_token));
}

async function authorizedJson(path, options = {}) {
  return parseJsonOrThrow(await authorizedFetch(path, options));
}

// ---- Live field validation (registration password/confirm, email format) ----
//
// This only ever blocks a *submit* client-side as a convenience -- the
// server re-validates everything it receives regardless (RegisterRequest's
// EmailStr + password_has_variety validator in app/schemas/auth.py are the
// actual source of truth), so a user with JS disabled, or a request crafted
// directly against the API, still gets the same guarantees.

// Deliberately just format checking (user@domain.tld), not a fixed list of
// approved providers -- a construction firm's own work email address (e.g.
// j.smith@aceconstruction.com) has to work here, not just consumer webmail.
const EMAIL_FORMAT_REGEX = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// Mirrors app/schemas/auth.py's RegisterRequest.password_has_variety
// exactly (>=10 chars, mixing at least two of the four character classes) --
// keep these two in sync if the policy ever changes.
function passwordPolicyProblem(password) {
  if (password.length < 10) return "At least 10 characters, mixing two of: lowercase, uppercase, digits, symbols.";
  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^a-zA-Z0-9]/].filter((re) => re.test(password)).length;
  if (classes < 2) return "Mix in at least one more character type: lowercase, uppercase, digits, or symbols.";
  return null;
}

// Sets a field's live-validation display: the trailing badge inside the
// input (an encircled ! for a problem, a check once it's resolved) and the
// message line underneath. `state` is "neutral" | "error" | "valid".
function setFieldState(input, badge, message, state, text) {
  input.classList.remove("input-invalid", "input-valid");
  badge.classList.remove("field-badge-error", "field-badge-valid");
  message.classList.remove("field-message-error", "field-message-valid");

  if (state === "error") {
    input.classList.add("input-invalid");
    badge.classList.add("field-badge-error");
    badge.textContent = "!";
    badge.hidden = false;
    message.classList.add("field-message-error");
  } else if (state === "valid") {
    input.classList.add("input-valid");
    badge.classList.add("field-badge-valid");
    badge.textContent = "✓"; // checkmark
    badge.hidden = false;
    message.classList.add("field-message-valid");
  } else {
    badge.hidden = true;
  }
  if (text !== undefined) message.textContent = text;
}

function wireEmailField(inputId) {
  const input = document.getElementById(inputId);
  const badge = document.getElementById(`${inputId}-badge`);
  const message = document.getElementById(`${inputId}-message`);
  if (!input || !badge || !message) return null;
  const defaultHint = message.textContent;
  const evaluate = () => {
    const value = input.value.trim();
    if (value === "") {
      setFieldState(input, badge, message, "neutral", defaultHint);
      return true;
    }
    if (!EMAIL_FORMAT_REGEX.test(value)) {
      setFieldState(
        input,
        badge,
        message,
        "error",
        "Invalid email format -- expected something like name@example.com"
      );
      return false;
    }
    setFieldState(input, badge, message, "valid", "Looks good.");
    return true;
  };
  input.addEventListener("input", evaluate);
  input.addEventListener("blur", evaluate);
  return evaluate;
}

function wireRegisterPasswordFields() {
  const password = document.getElementById("register-password");
  const passwordBadge = document.getElementById("register-password-badge");
  const passwordMessage = document.getElementById("register-password-message");
  const defaultPasswordHint = passwordMessage.textContent;

  const confirm = document.getElementById("register-password-confirm");
  const confirmBadge = document.getElementById("register-password-confirm-badge");
  const confirmMessage = document.getElementById("register-password-confirm-message");

  const evaluatePassword = () => {
    const value = password.value;
    if (value === "") {
      setFieldState(password, passwordBadge, passwordMessage, "neutral", defaultPasswordHint);
      return true;
    }
    const problem = passwordPolicyProblem(value);
    if (problem) {
      setFieldState(password, passwordBadge, passwordMessage, "error", problem);
      return false;
    }
    setFieldState(password, passwordBadge, passwordMessage, "valid", "Meets the password requirements.");
    return true;
  };

  const evaluateConfirm = () => {
    if (confirm.value === "") {
      setFieldState(confirm, confirmBadge, confirmMessage, "neutral", "Re-enter your password.");
      return true;
    }
    if (confirm.value !== password.value) {
      setFieldState(confirm, confirmBadge, confirmMessage, "error", "Passwords don't match.");
      return false;
    }
    setFieldState(confirm, confirmBadge, confirmMessage, "valid", "Passwords match.");
    return true;
  };

  password.addEventListener("input", () => {
    evaluatePassword();
    // Re-check the confirm field too -- typing into the *first* box after
    // the second one is already filled in is exactly when a stale "match"
    // state would otherwise mislead someone into thinking they're done.
    if (confirm.value !== "") evaluateConfirm();
  });
  confirm.addEventListener("input", evaluateConfirm);
  confirm.addEventListener("blur", evaluateConfirm);

  return { evaluatePassword, evaluateConfirm };
}

const evaluateLoginEmail = wireEmailField("login-email");
const evaluateRegisterEmail = wireEmailField("register-email");
const { evaluatePassword: evaluateRegisterPassword, evaluateConfirm: evaluateRegisterConfirm } =
  wireRegisterPasswordFields();

// ---- View plumbing ----

const authView = document.getElementById("auth-view");
const appView = document.getElementById("app-view");
const welcomeName = document.getElementById("welcome-name");
const welcomeEmail = document.getElementById("welcome-email");

function setChatMode(on) {
  document.body.classList.toggle("chat-mode", on);
}

function showAuthView() {
  authView.hidden = false;
  appView.hidden = true;
  setChatMode(false);
  resetChatState();
}

function showAppView(user) {
  welcomeName.textContent = user.display_name;
  welcomeEmail.textContent = user.email;
  authView.hidden = true;
  appView.hidden = false;
  setChatMode(true);
  loadConversations();
}

// Tabs (Sign in / Register)
function showAuthTab(tabName) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
  document.getElementById(`${tabName}-form`).classList.add("active");
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => showAuthTab(btn.dataset.tab));
});

// Login
document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorEl = document.getElementById("login-error");
  errorEl.textContent = "";
  if (!evaluateLoginEmail()) return;
  try {
    const tokens = await postJson("/api/v1/auth/login", {
      email: document.getElementById("login-email").value,
      password: document.getElementById("login-password").value,
    });
    saveSession(tokens, tokens.user);
    showAppView(tokens.user);
  } catch (err) {
    // This message comes straight from the API -- it's either the generic
    // "Invalid email or password" (deliberately identical for "no such
    // account" and "wrong password", to avoid account enumeration -- see
    // app/api/v1/auth.py's login()) or, only reachable once the correct
    // password was already supplied, the distinct "please verify your
    // email" message. No client-side branching needed for that difference.
    errorEl.textContent = err.message;
  }
});

// Register
document.getElementById("register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorEl = document.getElementById("register-error");
  errorEl.textContent = "";

  // Client-side guard only -- app/schemas/auth.py re-validates all of this
  // server-side regardless, so this just avoids a round trip for the
  // common case of an obviously-incomplete form.
  const emailOk = evaluateRegisterEmail();
  const passwordOk = evaluateRegisterPassword();
  const confirmOk = evaluateRegisterConfirm();
  if (!emailOk || !passwordOk || !confirmOk) {
    errorEl.textContent = "Please fix the highlighted fields above.";
    return;
  }

  try {
    const result = await postJson("/api/v1/auth/register", {
      display_name: document.getElementById("register-display-name").value,
      email: document.getElementById("register-email").value,
      password: document.getElementById("register-password").value,
    });
    showRegisterSuccess(result);
  } catch (err) {
    errorEl.textContent = err.message;
  }
});

// Shown after a successful registration in place of the form -- a new
// account can't sign in yet (see app/schemas/auth.py's RegisterResponseOut:
// register() deliberately does NOT return usable tokens), so there is no
// app view to jump into here the way login/verify-email do.
function showRegisterSuccess(result) {
  document.getElementById("register-success-text").textContent = result.message;
  const devBox = document.getElementById("dev-verify-box");
  const devLink = document.getElementById("dev-verify-link");
  if (result.dev_verification_url) {
    // A real deployment leaves dev_verification_url null (see
    // app/core/mailer.py) -- this box only ever appears against the local
    // docker-compose stack, which has no real email provider wired up.
    // Setting href directly (no click handler) means clicking this behaves
    // exactly like clicking the link in a real email would: a normal page
    // navigation that the boot logic below picks up via ?verify_token=.
    devLink.href = result.dev_verification_url;
    devBox.hidden = false;
  } else {
    devBox.hidden = true;
  }
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
  document.getElementById("register-success").classList.add("active");
}

document.getElementById("register-success-back").addEventListener("click", () => {
  document.getElementById("login-email").value = document.getElementById("register-email").value;
  showAuthTab("login");
});

// Logout
document.getElementById("logout-btn").addEventListener("click", () => {
  const refreshToken = sessionStorage.getItem(STORAGE_KEYS.refresh);
  clearSession();
  showAuthView();
  if (refreshToken) {
    // Best-effort -- /auth/logout is designed to be idempotent server-side
    // (204 whether the token was valid, already-revoked, or unknown), so
    // the UI doesn't need to wait on or branch on this response.
    fetch("/api/v1/auth/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    }).catch(() => {});
  }
});

// ---- Chat: conversations, messages, uploads ----

let conversations = [];
let activeConversationId = null;

const conversationBar = document.getElementById("conversation-bar");
const newConversationBtn = document.getElementById("new-conversation-btn");
const messageList = document.getElementById("message-list");
const composerForm = document.getElementById("composer-form");
const composerInput = document.getElementById("composer-input");
const uploadBtn = document.getElementById("upload-btn");
const fileInput = document.getElementById("file-input");
const chatError = document.getElementById("chat-error");

function setChatError(message) {
  chatError.textContent = message || "";
}

// Same document-icon glyph used in the initial HTML empty state (see
// index.html) -- kept as one function so JS-driven re-renders (which
// replace #message-list's entire innerHTML) look identical to the markup
// the page ships with, instead of visibly downgrading to plain text the
// moment the first render runs.
function emptyStateHtml(message) {
  return (
    '<div class="empty-state">' +
    '<svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />' +
    '<path d="M14 2v6h6" />' +
    "</svg>" +
    `<p>${message}</p>` +
    "</div>"
  );
}

function resetChatState() {
  conversations = [];
  activeConversationId = null;
  renderConversationChips();
  messageList.innerHTML = emptyStateHtml("Upload a drawing or ask a question to get started.");
  setChatError("");
}

function setComposerEnabled(enabled) {
  composerInput.disabled = !enabled;
  uploadBtn.disabled = !enabled;
  composerForm.querySelector(".icon-btn-send").disabled = !enabled;
}

function renderConversationChips() {
  conversationBar.querySelectorAll(".conversation-chip").forEach((el) => el.remove());
  conversations.forEach((conversation) => {
    const label = conversation.title || "Untitled project";
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "conversation-chip" + (conversation.id === activeConversationId ? " active" : "");
    chip.title = label;

    const icon = document.createElement("span");
    icon.className = "conversation-chip-icon";
    icon.innerHTML =
      '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>';

    const labelSpan = document.createElement("span");
    labelSpan.className = "conversation-chip-label";
    // textContent, not innerHTML -- a renamed conversation's title is
    // user-entered text (see rename_conversation in
    // app/api/v1/conversations.py), so this can no longer be interpolated
    // into an HTML string the way it safely could back when a title was
    // always server-controlled (always null).
    labelSpan.textContent = label;

    const renameBtn = document.createElement("button");
    renameBtn.type = "button";
    renameBtn.className = "conversation-chip-rename";
    renameBtn.title = "Rename";
    renameBtn.setAttribute("aria-label", "Rename project");
    renameBtn.innerHTML =
      '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9" /><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" /></svg>';
    renameBtn.addEventListener("click", (event) => {
      event.stopPropagation(); // don't also trigger selectConversation below
      startRenamingChip(chip, conversation);
    });

    chip.append(icon, labelSpan, renameBtn);
    chip.addEventListener("click", () => selectConversation(conversation.id));
    conversationBar.insertBefore(chip, newConversationBtn);
  });
}

// Swaps a chip's label span for an inline <input>, pre-filled with its
// current title (empty if it's still "Untitled project", with that as the
// placeholder instead -- so renaming doesn't start by making someone
// delete four placeholder words first). Enter or blur saves via PATCH;
// Escape cancels. `conversation` is the plain data object (from the
// `conversations` array), not the DOM node -- always has the current
// server-known title even if this chip was re-rendered since it was built.
function startRenamingChip(chip, conversation) {
  if (chip.querySelector(".conversation-chip-rename-input")) return; // already editing

  const labelSpan = chip.querySelector(".conversation-chip-label");
  const input = document.createElement("input");
  input.type = "text";
  input.className = "conversation-chip-rename-input";
  input.maxLength = 255;
  input.value = conversation.title || "";
  input.placeholder = "Untitled project";
  input.addEventListener("click", (event) => event.stopPropagation());

  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    if (save) {
      try {
        const updated = await authorizedJson(`/api/v1/conversations/${conversation.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: input.value }),
        });
        const idx = conversations.findIndex((c) => c.id === conversation.id);
        if (idx !== -1) conversations[idx] = updated;
      } catch (err) {
        setChatError(err.message);
      }
    }
    renderConversationChips();
  };

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      finish(true);
    } else if (event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener("blur", () => finish(true));

  labelSpan.replaceWith(input);
  input.focus();
  input.select();
}

// An upload creates a role="system" message shaped exactly like
// "Uploaded: <filename> (<human size>)" (see app/api/v1/conversations.py's
// upload_file handler, which owns and controls that exact format) -- match
// it here to render a proper attachment card instead of a plain text
// bubble. Any other system message (there are none today, but the schema
// allows it) falls through to the plain pill style unchanged.
const UPLOAD_NOTE_PATTERN = /^Uploaded: (.+) \(([^)]+)\)$/;

function renderMessages(messages) {
  messageList.innerHTML = "";
  if (messages.length === 0) {
    messageList.innerHTML = emptyStateHtml("No messages in this project yet.");
    return;
  }
  messages.forEach((message) => {
    const uploadMatch = message.role === "system" && message.content.match(UPLOAD_NOTE_PATTERN);
    const bubble = document.createElement("div");
    if (uploadMatch) {
      const [, filename, size] = uploadMatch;
      bubble.className = "message attachment-card";
      bubble.innerHTML =
        '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" class="attachment-icon"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></svg>' +
        '<span class="attachment-meta">' +
        `<span class="attachment-name"></span>` +
        `<span class="attachment-size">${size}</span>` +
        "</span>";
      bubble.querySelector(".attachment-name").textContent = filename; // textContent, not innerHTML -- filename is untrusted
    } else {
      bubble.className = "message " + (message.role === "system" ? "message-system" : "message-user");
      bubble.textContent = message.content;
    }
    messageList.appendChild(bubble);
  });
  messageList.scrollTop = messageList.scrollHeight;
}

async function loadConversations() {
  setComposerEnabled(false);
  try {
    conversations = await authorizedJson("/api/v1/conversations");
  } catch (err) {
    setChatError(err.message);
    return;
  }
  if (conversations.length === 0) {
    await createConversation();
    return;
  }
  renderConversationChips();
  await selectConversation(conversations[0].id);
}

async function createConversation() {
  setChatError("");
  try {
    const conversation = await authorizedJson("/api/v1/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    conversations.unshift(conversation);
    renderConversationChips();
    await selectConversation(conversation.id);
  } catch (err) {
    setChatError(err.message);
  }
}

async function selectConversation(conversationId) {
  activeConversationId = conversationId;
  renderConversationChips();
  setChatError("");
  try {
    const messages = await authorizedJson(`/api/v1/conversations/${conversationId}/messages`);
    renderMessages(messages);
    setComposerEnabled(true);
  } catch (err) {
    setChatError(err.message);
  }
}

async function refreshActiveMessages() {
  if (!activeConversationId) return;
  try {
    const messages = await authorizedJson(`/api/v1/conversations/${activeConversationId}/messages`);
    renderMessages(messages);
  } catch (err) {
    setChatError(err.message);
  }
}

newConversationBtn.addEventListener("click", () => createConversation());

composerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = composerInput.value.trim();
  if (!text || !activeConversationId) return;
  setChatError("");
  composerInput.value = "";
  setComposerEnabled(false);
  try {
    await authorizedJson(`/api/v1/conversations/${activeConversationId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text }),
    });
    await refreshActiveMessages();
  } catch (err) {
    setChatError(err.message);
    composerInput.value = text; // give the message back so nothing is lost
  } finally {
    setComposerEnabled(true);
    composerInput.focus();
  }
});

uploadBtn.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", async () => {
  const file = fileInput.files[0];
  if (!file || !activeConversationId) return;
  setChatError("");
  setComposerEnabled(false);
  try {
    const formData = new FormData();
    formData.append("file", file);
    // No Content-Type header here on purpose -- the browser sets
    // multipart/form-data with the correct boundary itself; setting it
    // manually is a classic way to silently break the upload.
    await authorizedJson(`/api/v1/conversations/${activeConversationId}/files`, {
      method: "POST",
      body: formData,
    });
    await refreshActiveMessages();
  } catch (err) {
    setChatError(err.message);
  } finally {
    fileInput.value = ""; // allow re-selecting the same file later
    setComposerEnabled(true);
  }
});

// ---- Boot ----
//
// A confirmation link (see app/api/v1/auth.py's register(), which builds it
// from request.base_url) points right back at this same page with
// ?verify_token=<raw token> -- clicking it is a normal navigation, not an
// API call from JS, so this is the one place that link's effect actually
// runs. This check comes before the "restore an existing session" check
// below on purpose: a verification link should always win over whatever
// session (or lack of one) happened to already be in this browser tab.
async function bootFromVerificationLink() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("verify_token");
  if (!token) return false;

  // Strip the token from the address bar/history regardless of outcome --
  // it's single-use, so leaving it there just invites an accidental reload
  // to hit the API again and get a confusing "already used" error.
  const cleanUrl = window.location.pathname + window.location.hash;
  window.history.replaceState({}, document.title, cleanUrl);

  try {
    const tokens = await postJson("/api/v1/auth/verify-email", { token });
    saveSession(tokens, tokens.user);
    showAppView(tokens.user);
  } catch (err) {
    showAuthView();
    showAuthTab("login");
    document.getElementById("login-error").textContent = err.message;
  }
  return true;
}

(async () => {
  if (await bootFromVerificationLink()) return;

  const existingUser = getStoredUser();
  if (existingUser && sessionStorage.getItem(STORAGE_KEYS.access)) {
    showAppView(existingUser);
  } else {
    showAuthView();
  }
})();
