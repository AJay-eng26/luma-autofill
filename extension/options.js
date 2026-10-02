const PROFILE_KEYS = ["name", "email", "phone", "company", "role", "linkedin", "x_handle", "telegram", "website", "location", "about"];
const $ = (id) => document.getElementById(id);

async function load() {
  const { profile = {}, apiKey = "", model = "" } = await chrome.storage.local.get(["profile", "apiKey", "model"]);
  for (const k of PROFILE_KEYS) $(k).value = profile[k] || "";
  $("apiKey").value = apiKey;
  $("model").value = model;
}

$("import").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    for (const k of PROFILE_KEYS) if (data[k]) $(k).value = data[k];
    $("status").textContent = "Imported — check the boxes, then Save.";
  } catch (err) {
    $("status").textContent = `Couldn't read that file: ${err.message}`;
  }
});

$("save").addEventListener("click", async () => {
  const profile = Object.fromEntries(PROFILE_KEYS.map((k) => [k, $(k).value.trim()]));
  if (!profile.name || !profile.email) {
    $("status").textContent = "Name and email are required.";
    return;
  }
  await chrome.storage.local.set({ profile, apiKey: $("apiKey").value.trim(), model: $("model").value.trim() });
  $("status").textContent = "Saved ✓";
});

load();
